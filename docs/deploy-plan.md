# Plan: serverless MCP server on AWS Lambda container images

Status: planning only, nothing deployed. Constraints: idle cost ≈ $0, no Neptune, read-only graph rebuilt in batches.

## Shape

```
client ──HTTPS──> Lambda function URL (auth) ──> Lambda (container image, arm64)
                                                   ├─ /opt/graph/store/     Oxigraph on-disk store (read-only)
                                                   └─ /opt/graph/aux.sqlite evidence quotes + label FTS index
```

Everything the server reads is baked into the image at build time. No database, VPC, EFS, NAT or S3 reads on the
request path, so nothing bills while idle except image storage in ECR (~$0.10/GB-month).

## 1. Offline build artifacts (`eisgraph build-serving`)

New CLI step after `build`, writing `data/serving/`:

- **`store/`**: `pyoxigraph.Store(path).bulk_load(graph.trig)` then `optimize()`. Measured on the sample:
  ~210 B/quad, so ~0.5 GB per million pages, ~2.7 GB at 5M pages (re-measure at the pilot).
- **`aux.sqlite`**, one file for the two things Oxigraph is bad at:
  - `evidence(s, p, o, doc, chunk, quote)` indexed on `(s, p, o)`, replacing `evidence.json` (~1.2 GB as JSON at 5M
    pages; it can't stay an in-memory dict).
  - `labels` FTS5 table over `rdfs:label` + `skos:altLabel` with entity type and mention count, replacing the
    `CONTAINS` scan in `search_entities`.
  - Precomputed `doc_titles` and `graph_stats` (both currently full-graph queries per call).

This is simpler than the DynamoDB evidence table suggested earlier: one artifact, versioned with the image, and no
second service. Revisit only if the image passes ~8 GB (Lambda limit 10 GB).

## 2. Server changes (`eisgraph/mcp_server.py`, `eisgraph/server.py`)

- `Graph` opens the store with `Store.read_only(path)` and the SQLite file in `?mode=ro&immutable=1`; no parse on
  cold start. Keep the current trig loading for local dev (`--store` flag picks).
- Transport: `streamable_http_app()` with `stateless_http=True, json_response=True`, so any instance can serve any
  request and no SSE streaming is needed.
- Lambda adapter: run the ASGI app under **AWS Lambda Web Adapter** (an extension layer copied into the image;
  the app stays a normal uvicorn server, which also keeps `docker run` locally identical). Alternative: Mangum.
- Limits for a large graph: paginate `list_eis` (cursor + `limit`), cap `get_entity` relations (exists), cap
  `shared_entities` / `compare_eis`, and a wall-clock limit on `sparql_query` (run in a worker thread, abandon
  after ~10 s, plus the 500-row cap that exists today).

## 3. Image

- Base `public.ecr.aws/lambda/python:3.12-arm64` (or slim + Web Adapter); install only `mcp`, `pyoxigraph`,
  `uvicorn`. The current venv is 95 MB mostly because of `anthropic`; move extraction deps to an optional
  `pipeline` extra in `pyproject.toml`.
- Data layer last in the Dockerfile so code-only changes don't re-push gigabytes.
- Tag images with the graph build id; the function points at one tag, so rollback = repoint.

## 4. Infrastructure (SAM template, `deploy/template.yaml`)

| Resource | Setting | Why |
|---|---|---|
| ECR repo | lifecycle: keep last 3 images | caps storage cost |
| Lambda | arm64, 1–2 GB memory (tune at pilot), 30 s timeout, `ReservedConcurrentExecutions: 10` | cost ceiling, no runaway |
| Function URL | `AuthType: AWS_IAM`, or `NONE` + bearer-token check in the app | never open and unauthenticated |
| Log group | 14-day retention | log storage otherwise grows forever |
| Budget alarm | AWS Budgets, e.g. $5/month | catches anything unexpected |

Not used: Neptune (never scales to zero), API Gateway (adds $1/M requests; only needed for custom domains or
usage plans), VPC/EFS/NAT.

## 5. Cost (list prices, verify before deploying)

- Idle: ECR storage only, ~$0.30/month for a 3 GB image.
- Per request at 1 GB × ~100 ms: ~$0.0000015 compute + $0.0000002 request; 1M requests ≈ $1.50, mostly inside
  Lambda's always-free tier (1M requests, 400k GB-s per month).
- Cold start: expect 2–5 s while the image's data layer pages in (Lambda lazy-loads image chunks); warm requests
  are millisecond queries.

## 6. Order of work

1. `build-serving` + `Store.read_only` path, tested locally against the 50-doc graph.
2. Stateless HTTP + Web Adapter; `docker build` and run locally, test with MCP Inspector / Claude Code over HTTP.
3. Tool limits and pagination.
4. Re-measure store size, cold start and slow queries on the full-corpus extraction (`data/full/`).
5. SAM template, review, then deploy (needs explicit go-ahead).

## Open questions

- Who are the clients (Claude Code only, or claude.ai connectors)? claude.ai custom connectors expect OAuth or
  no auth, which changes the Function URL auth choice.
- Region: same as Bedrock (us-east-2)?
- The cross-document linking phase (`link.py`) doesn't scale past the sample; the serving plan assumes it is
  redesigned before the full graph is built.
