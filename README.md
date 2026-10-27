# eis-graph

A small local test semantic graph of 50 randomly sampled Environmental Impact Statements
(`s3://impulse-data-prod/jobs/p1274/`, surya-2 OCR), extracted with Claude on Amazon Bedrock.

## Pipeline

```fish
uv run eisgraph sample             # 50 random barcodes (seed 1274) -> data/raw/<barcode>/json/*.json
uv run eisgraph prepare-extract    # reading-ordered text -> ~80k-char chunks -> data/work/extract_requests.jsonl
set -x AWS_BEARER_TOKEN_BEDROCK <your Bedrock API key>   # region defaults to us-east-2
uv run eisgraph run extract        # Haiku 5.5 -> data/work/extract/<custom_id>.json, _summary.json; rerun retries failures
uv run eisgraph prepare-link       # cross-doc entity resolution + doc-to-doc link requests
uv run eisgraph run link           # Opus 5.5
uv run eisgraph build              # -> data/graph/graph.trig, evidence.json
uv run eisgraph serve              # http://127.0.0.1:8765
```

Full corpus (all of `p1274`, into `data/full/`): `uv run eisgraph extract-all --budget 50`, then the same
`prepare-link` / `run link --budget USD` / `build` / `serve` steps with `--data full` before the command.

## Design

- **Input**: the per-page surya JSON, with blocks sorted by `reading_order`. `SectionHeader`s become `##` headings,
  tables become `|` rows, and `PageHeader`/`PageFooter` blocks are dropped. Every chunk carries `[[page N]]` markers,
  and the first 12k chars of its document (cover sheet / summary), as a cached prefix for context.
- **Backend**: Bedrock `bedrock-runtime` (`AsyncAnthropicBedrock`, Bedrock API key), calling cross-region inference
  profiles `us.anthropic.<model>` (`EISGRAPH_BEDROCK_PREFIX`). On this account the Mantle endpoint reports every model
  as nonexistent and on-demand `anthropic.*` ids are rejected. Bedrock has no Message Batches API, so each phase runs
  as concurrent streaming calls (`EISGRAPH_CONCURRENCY`, default 8) and is resumable.
  `EISGRAPH_BACKEND=anthropic` instead uses the Anthropic Message Batches API (`run` / `wait` / `collect`,
  `ANTHROPIC_API_KEY`). Choose models with `EISGRAPH_EXTRACT_MODEL` / `EISGRAPH_LINK_MODEL`.
- **Phase 1 (within documents)**: `claude-haiku-5-5` at effort `medium`, with structured outputs against a fixed EIS
  ontology (`eisgraph/ontology.py`): 17 entity classes, 26 relations, 12 attributes, and a verbatim evidence quote on
  every relation. Haiku has no refusal fallback; a refusal is listed as a failure in `_summary.json`.
- **Phase 2 (between documents)**, on `claude-opus-5-5`, with the SDK's client-side refusal fallback to Opus 4.8:
  1. Chunk-level entities are merged per document.
  2. Entities of *global* types (agencies, places, waterbodies, species, statutes, referenced documents…) are pooled
     across documents and pre-merged on normalized name and alias matches.
  3. Claude clusters the remaining name variants per type.
  4. EIS↔EIS links (`supplements`, `sameProject`, `tiersFrom`, `references`, `sharesGeography`,
     `similarActionType`, `cumulativeContext`) in two stages, so this scales past one prompt: candidate pairs are
     scored without a model from shared specific entities, identifiers and project-title words (each weighted by
     rarity; `data/work/pairs.json`), then one request per document shows Claude its profile plus its top 8 partners
     and what they share, and Claude asserts only the links that hold. Each unordered pair is judged once.
- **RDF**: one named graph per EIS (provenance), plus `entities`, `doclinks` and `ontology` graphs. Vocabulary is
  `https://example.org/eis/`, instances are under `…/eis/id/`. *Local* types (Project, Alternative, Impact,
  Mitigation, Facility, Permit) are scoped per document; global types share one URI across documents, and those shared
  nodes are what connect the EISs.
- **Server**: stdlib HTTP + in-memory Oxigraph. The front end is cytoscape.js and has four views:
  - an overview of the EISs plus the entities they share
  - click/expand exploration
  - evidence quotes on edges
  - a SPARQL console (the default graph is the union of all named graphs)

## MCP server

`eisgraph/mcp_server.py` exposes the built graph (`data/graph/`) as a read-only MCP server with 10 tools:
`list_eis`, `get_eis`, `search_entities`, `get_entity`, `shared_entities`, `compare_eis`, `get_evidence`,
`sparql_query`, `describe_ontology` and `graph_stats`. Every fact it returns is tagged with its source EIS.

```fish
uv run eisgraph-mcp                      # stdio
uv run eisgraph-mcp --http --port 8766   # streamable HTTP at http://127.0.0.1:8766/mcp
```

Claude Code picks it up from this directory's `.mcp.json` (approve it once when prompted). It reads
`data/graph/` on first tool call, so rerun `uv run eisgraph build` and restart the client after phase 2.

## Deployed MCP server (AWS)

`deploy/deploy.sh [graph-dir]` packages the server as an arm64 Lambda zip with a read-only graph
(`server.build_serving` output, default `data/full/serving`) and deploys `deploy/template.yaml` (stack
`eis-graph-mcp`, us-east-2): one Lambda behind a function URL, run through the Lambda Web Adapter layer.
Nothing bills while idle; reserved concurrency (10) caps simultaneous instances.

Every request needs an API key, sent as `Authorization: Bearer <key>` or `x-api-key: <key>`. Keys are managed
with `deploy/keys.sh add NAME | revoke NAME | list`, which edits one SSM Parameter Store SecureString
(`/eis-graph/mcp/api-keys`, free standard tier) holding only each key's label and SHA-256; the server re-reads it
every 60 s, so changes apply within about a minute with no redeploy. A key is printed once, when minted. To redeploy a newer graph:
`uv run eisgraph --data full build`, rebuild `data/full/serving`, then rerun `deploy/deploy.sh`.
Tear down with `aws cloudformation delete-stack --stack-name eis-graph-mcp --region us-east-2`.
