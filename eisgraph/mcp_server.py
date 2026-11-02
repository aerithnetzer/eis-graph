"""Read-only MCP server over the EIS knowledge graph (data/graph/graph.trig).

Run:  uv run eisgraph-mcp                      # stdio (Claude Code, Claude Desktop, MCP Inspector)
      uv run eisgraph-mcp --http --port 8766   # streamable HTTP at http://127.0.0.1:8766/mcp
      add --data full to serve data/full/graph (the extract-all corpus) instead of the 50-doc sample

Deployed (AWS Lambda behind a function URL, see deploy/): `eisgraph-mcp --deployed` serves stateless JSON-response
streamable HTTP on $PORT, reading the build-serving directory EISGRAPH_GRAPH_DIR (or copying it at start-up from
EISGRAPH_GRAPH_S3, s3://bucket/prefix/, when it's too big for the Lambda zip) and requiring one of the API keys
listed in Parameter Store (EISGRAPH_API_KEYS_PARAM; see ApiKeys and deploy/keys.sh); clients send
`Authorization: Bearer <key>` or `x-api-key: <key>`.
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .ontology import ATTRIBUTES, CLASSES, DATA, DOC_LINK_PREDICATES, NS, RELATIONS
from .server import PREFIXES, Graph

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
GRAPH_DIR = Path(os.environ.get("EISGRAPH_GRAPH_DIR", DATA_DIR / "graph"))
READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
MAX_ROWS = 500

mcp = MCPServer(
    name="eis-graph",
    title="EIS Knowledge Graph",
    instructions=(
        "A knowledge graph extracted by Claude from U.S. Environmental Impact Statements (mostly 1970s-era "
        "scanned documents, OCR'd); list_eis shows which ones. Each EIS has a barcode; facts are stored in one named graph per EIS, "
        "so every relation can be traced to its source document and a verbatim evidence quote. Start with list_eis "
        "or search_entities, then get_entity / get_eis. Use sparql_query for anything else (prefixes eis:, id:, "
        "rdfs:, skos:, owl: are predeclared; the default graph is the union of all named graphs). Extraction was "
        "automated, so treat facts as leads to verify against the evidence."
    ),
)
_graph: Graph | None = None


def g() -> Graph:
    global _graph
    if _graph is None:  # loaded lazily so the server starts (and lists tools) instantly
        _graph = Graph(GRAPH_DIR)
    return _graph


def _local(iri: str) -> str:
    return iri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _doc_titles() -> dict[str, str]:
    return {r["b"]: r["l"] for r in g().rows("SELECT ?b ?l WHERE { ?d a eis:EISDocument ; eis:barcode ?b ; rdfs:label ?l }")}


def _resolve(entity: str) -> str | None:
    """Accept a full IRI, an id:-prefixed IRI, an EIS barcode, or an exact (case-insensitive) label."""
    if entity.startswith("http"):
        return entity
    if entity.startswith("id:"):
        return DATA + entity[3:]
    if entity.isdigit():
        return DATA + f"doc/{entity}"
    hits = g().rows(f"SELECT ?e WHERE {{ ?e rdfs:label ?l FILTER(LCASE(STR(?l)) = {json.dumps(entity.lower())}) }} LIMIT 1")
    return hits[0]["e"] if hits else None


@mcp.tool(annotations=READ_ONLY)
def graph_stats() -> dict:
    """Counts of quads, entities per class, and triples that carry evidence quotes."""
    return g().stats()


@mcp.tool(annotations=READ_ONLY)
def list_eis() -> list[dict]:
    """All sampled EISs: barcode, title (stage: project), date and identifiers when extracted."""
    rows = g().rows("""
        SELECT ?b ?l (GROUP_CONCAT(DISTINCT ?date; separator="; ") AS ?dates)
               (GROUP_CONCAT(DISTINCT ?ident; separator="; ") AS ?ids)
        WHERE { ?d a eis:EISDocument ; eis:barcode ?b ; rdfs:label ?l .
                OPTIONAL { GRAPH ?g { ?d eis:date ?date } } OPTIONAL { GRAPH ?g2 { ?d eis:identifier ?ident } } }
        GROUP BY ?b ?l ORDER BY ?l""")
    return [{"barcode": r["b"], "title": r["l"], "dates": r["dates"] or None, "identifiers": r["ids"] or None}
            for r in rows]


@mcp.tool(annotations=READ_ONLY)
def search_entities(query: str, entity_type: str | None = None, limit: int = 25) -> list[dict]:
    """Find entities whose label contains `query` (case-insensitive), with how many EISs mention each.

    entity_type: optional class name, e.g. Agency, Species, Statute, Waterbody, Location, Project, Impact.
    """
    type_filter = f"FILTER(?t = eis:{entity_type})" if entity_type in CLASSES or entity_type == "EISDocument" else ""
    rows = g().rows(f"""
        SELECT ?e ?l ?t (COUNT(DISTINCT ?d) AS ?n) WHERE {{
          ?e rdfs:label ?l ; a ?t FILTER(STRSTARTS(STR(?t), STR(eis:)) && CONTAINS(LCASE(?l), {json.dumps(query.lower())}))
          {type_filter}
          OPTIONAL {{ GRAPH ?g {{ ?d eis:mentions ?e }} }}
        }} GROUP BY ?e ?l ?t ORDER BY DESC(?n) STRLEN(?l) LIMIT {min(int(limit), MAX_ROWS)}""")
    return [{"iri": r["e"], "label": r["l"], "type": _local(r["t"]), "mentioned_in_eis": int(r["n"])} for r in rows]


@mcp.tool(annotations=READ_ONLY)
def get_entity(entity: str, max_relations: int = 100) -> dict:
    """Everything known about one entity: types, labels, attributes and relations, each tagged with the EIS
    (barcode + title) it came from. `entity` may be an IRI, an id:-prefixed IRI, an EIS barcode, or an exact label."""
    iri = _resolve(entity)
    if iri is None:
        return {"error": f"no entity labelled {entity!r}; try search_entities"}
    node = g().node(iri, limit=max(1, min(int(max_relations), MAX_ROWS)))
    titles = _doc_titles()
    src = lambda bc: {"barcode": bc, "title": titles.get(bc)} if bc in titles else bc
    labels = [l["v"] for l in node["literals"] if l["p"] == "label"]
    rels = []
    for e in node["edges"]:
        if e["predicate"] == "mentions":
            continue
        out = e["source"] == iri
        rels.append({"direction": "out" if out else "in", "predicate": e["predicate"],
                     "other": e["target"] if out else e["source"],
                     "other_label": e.get("target_label") if out else e.get("source_label"),
                     "other_type": e.get("target_type") if out else e.get("source_type"), "source": src(e["doc"])})
    mentioned = sorted({e["doc"] for e in node["edges"] if e["predicate"] == "mentions"})
    return {
        "iri": iri, "label": labels[0] if labels else None, "types": node["types"],
        "alt_labels": [l["v"] for l in node["literals"] if l["p"] == "altLabel"],
        "attributes": [{"property": l["p"], "value": l["v"], "source": src(l["doc"])}
                       for l in node["literals"] if l["p"] not in ("label", "altLabel", "barcode", "sourceUri")],
        "relations": rels, "mentioned_in": [src(b) for b in mentioned], "truncated": node["truncated"],
    }


@mcp.tool(annotations=READ_ONLY)
def get_eis(barcode: str, per_type: int = 15) -> dict:
    """Profile of one EIS: title, attributes, and its most-mentioned entities grouped by type."""
    d = f"<{DATA}doc/{barcode}>"
    head = g().rows(f"SELECT ?l ?s WHERE {{ {d} rdfs:label ?l OPTIONAL {{ {d} eis:sourceUri ?s }} }}")
    if not head:
        return {"error": f"unknown barcode {barcode!r}; see list_eis"}
    attrs = g().rows(f"SELECT ?p ?v WHERE {{ GRAPH ?g {{ {d} ?p ?v FILTER(isLiteral(?v)) }} }}")
    ents = g().rows(f"""SELECT ?e ?l ?t WHERE {{ GRAPH <{DATA}graph/doc/{barcode}> {{ {d} eis:mentions ?e }}
                         ?e rdfs:label ?l ; a ?t }}""")
    by_type: dict[str, list] = {}
    for e in ents:
        by_type.setdefault(_local(e["t"]), []).append({"iri": e["e"], "label": e["l"]})
    return {"barcode": barcode, "title": head[0]["l"], "source": head[0]["s"],
            "attributes": [{"property": _local(a["p"]), "value": a["v"]} for a in attrs
                           if _local(a["p"]) not in ("label", "barcode", "sourceUri")],
            "entity_counts": {t: len(v) for t, v in sorted(by_type.items())},
            "entities": {t: v[:per_type] for t, v in sorted(by_type.items())}}


@mcp.tool(annotations=READ_ONLY)
def shared_entities(min_eis: int = 3, max_eis: int = 50, entity_type: str | None = None, limit: int = 50) -> list[dict]:
    """Entities mentioned by between min_eis and max_eis different EISs: the links between documents."""
    type_filter = f"FILTER(?t = eis:{entity_type})" if entity_type in CLASSES else ""
    rows = g().rows(f"""
        SELECT ?e ?l ?t (COUNT(DISTINCT ?d) AS ?n) (GROUP_CONCAT(DISTINCT ?b; separator=" ") AS ?bs) WHERE {{
          GRAPH ?g {{ ?d eis:mentions ?e }} ?d eis:barcode ?b . ?e rdfs:label ?l ; a ?t {type_filter}
        }} GROUP BY ?e ?l ?t HAVING (COUNT(DISTINCT ?d) >= {int(min_eis)} && COUNT(DISTINCT ?d) <= {int(max_eis)})
        ORDER BY DESC(?n) LIMIT {min(int(limit), MAX_ROWS)}""")
    return [{"iri": r["e"], "label": r["l"], "type": _local(r["t"]), "eis_count": int(r["n"]),
             "barcodes": r["bs"].split()} for r in rows]


@mcp.tool(annotations=READ_ONLY)
def compare_eis(barcode_a: str, barcode_b: str) -> dict:
    """Entities two EISs have in common (shared agencies, places, species, statutes...)."""
    rows = g().rows(f"""SELECT ?e ?l ?t WHERE {{
        GRAPH <{DATA}graph/doc/{barcode_a}> {{ ?a eis:mentions ?e }}
        GRAPH <{DATA}graph/doc/{barcode_b}> {{ ?b eis:mentions ?e }}
        ?e rdfs:label ?l ; a ?t }} ORDER BY ?t ?l""")
    titles = _doc_titles()
    return {"a": {"barcode": barcode_a, "title": titles.get(barcode_a)},
            "b": {"barcode": barcode_b, "title": titles.get(barcode_b)},
            "shared": [{"iri": r["e"], "label": r["l"], "type": _local(r["t"])} for r in rows]}


@mcp.tool(annotations=READ_ONLY)
def get_evidence(subject: str, predicate: str, object: str) -> list[dict]:
    """Verbatim quotes supporting one relation. Pass IRIs as returned by get_entity; `predicate` may be the
    local name (e.g. 'leadAgency') or a full IRI."""
    p = predicate if predicate.startswith("http") else NS + predicate
    s, o = _resolve(subject) or subject, _resolve(object) or object
    titles = _doc_titles()
    return [{**e, "title": titles.get(e.get("doc"))} for e in g().evidence.get(f"{s} {p} {o}", [])]


@mcp.tool(annotations=READ_ONLY)
def sparql_query(query: str) -> dict:
    """Run a read-only SPARQL SELECT or ASK query (max 500 rows). Prefixes eis:, id:, rdf:, rdfs:, skos:, owl:
    are predeclared; the default graph is the union of every EIS's named graph. Use GRAPH ?g to get provenance
    (?g = <.../id/graph/doc/BARCODE>). Call describe_ontology for classes and predicates."""
    try:
        res = g().select(query, limit=MAX_ROWS)
    except Exception as e:  # syntax errors etc. go back to the model as data, not a protocol failure
        return {"error": str(e), "note": f"line numbers include {PREFIXES.count(chr(10))} predeclared PREFIX lines"}
    if "boolean" in res:
        return res
    return {"vars": res["vars"], "truncated": res["truncated"],
            "rows": [{k: (v["value"] if v else None) for k, v in r.items()} for r in res["rows"]]}


@mcp.tool(annotations=READ_ONLY)
def describe_ontology() -> dict:
    """The graph's vocabulary: entity classes, relation predicates, attribute properties, and EIS-to-EIS links."""
    return {"namespace": NS, "instances": DATA,
            "classes": {k: v[1] for k, v in CLASSES.items()} | {"EISDocument": "A sampled EIS."},
            "relations": RELATIONS, "attributes": ATTRIBUTES, "eis_links": DOC_LINK_PREDICATES,
            "provenance": "One named graph per EIS: <" + DATA + "graph/doc/BARCODE>. EIS --eis:mentions--> entity."}


class ApiKeys:
    """The accepted API-key digests. From EISGRAPH_API_KEYS_PARAM (an SSM Parameter Store SecureString holding
    {"label": {"sha256": hex, ...}}, maintained by deploy/keys.sh), re-read at most every KEYS_TTL seconds so keys
    added or revoked there take effect without a redeploy; or, for local runs, from EISGRAPH_API_KEY_SHA256
    (comma-separated hex digests). If a refresh fails, the last good list stays in force."""

    KEYS_TTL = 60.0

    def __init__(self):
        self.param = os.environ.get("EISGRAPH_API_KEYS_PARAM")
        self.static = os.environ.get("EISGRAPH_API_KEY_SHA256")
        if not (self.param or self.static):
            raise SystemExit("set EISGRAPH_API_KEYS_PARAM or EISGRAPH_API_KEY_SHA256; refusing to serve without API keys")
        self.digests: list[bytes] = []
        self.loaded_at = float("-inf")
        self.lock = asyncio.Lock()
        if self.static:
            self.digests = [bytes.fromhex(h.strip()) for h in self.static.split(",") if h.strip()]
        else:
            self._load()  # at cold start, so a misconfigured parameter fails loudly in the logs

    def _load(self) -> None:
        import boto3  # bundled with the Lambda Python runtime; only needed when reading Parameter Store

        value = boto3.client("ssm").get_parameter(Name=self.param, WithDecryption=True)["Parameter"]["Value"]
        self.digests = [bytes.fromhex(e["sha256"]) for e in json.loads(value).values()]
        self.loaded_at = time.monotonic()

    async def current(self) -> list[bytes]:
        if self.param and time.monotonic() - self.loaded_at > self.KEYS_TTL:
            async with self.lock:
                if time.monotonic() - self.loaded_at > self.KEYS_TTL:
                    try:
                        await asyncio.to_thread(self._load)
                    except Exception as e:  # keep serving with the last good list; retry after the TTL
                        self.loaded_at = time.monotonic()
                        print(f"api key refresh failed: {e!r}", file=sys.stderr)
        return self.digests


def require_api_key(app, keys: ApiKeys):
    """ASGI middleware: 401 unless the request carries one of the API keys (compared by SHA-256, in constant time)."""

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            h = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            auth = h.get("authorization", "")
            key = auth[7:].strip() if auth.lower().startswith("bearer ") else h.get("x-api-key", "")
            digest = hashlib.sha256(key.encode()).digest()
            # Check every key (no early exit), so timing doesn't reveal which one nearly matched.
            if not (key and sum(hmac.compare_digest(digest, w) for w in await keys.current())):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"missing or invalid API key"}'})
                return
        await app(scope, receive, send)

    return guarded


def fetch_graph(uri: str) -> Path:
    """Copy a build-serving directory from s3://bucket/prefix/ to local disk (once per Lambda instance) and return
    its path. The prefix is content-addressed by deploy/deploy.sh, so a present directory is never stale."""
    import boto3
    from concurrent.futures import ThreadPoolExecutor

    bucket, _, prefix = uri.removeprefix("s3://").partition("/")
    prefix = prefix.rstrip("/") + "/"
    dest = Path(os.environ.get("EISGRAPH_GRAPH_CACHE", "/tmp/graph")) / prefix.strip("/").replace("/", "_")
    if (dest / ".complete").exists():
        return dest
    s3 = boto3.client("s3")
    keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
            for o in page.get("Contents", [])]
    if not keys:
        raise SystemExit(f"no graph files under {uri}")

    def get(key: str) -> None:
        f = dest / key[len(prefix):]
        f.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, key, str(f))

    t = time.monotonic()
    with ThreadPoolExecutor(16) as ex:
        list(ex.map(get, keys))
    (dest / ".complete").touch()
    print(f"fetched {len(keys)} graph files from {uri} in {time.monotonic() - t:.1f}s", file=sys.stderr)
    return dest


def serve_deployed(port: int) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    global GRAPH_DIR
    if uri := os.environ.get("EISGRAPH_GRAPH_S3"):  # graph too big to ship in the code zip: load it from S3
        GRAPH_DIR = fetch_graph(uri)
    keys = ApiKeys()
    # Stateless + JSON responses: any Lambda instance can answer any request, and nothing streams.
    # Host checks are for localhost DNS-rebinding; the public function URL is guarded by the API key instead.
    app = mcp.streamable_http_app(stateless_http=True, json_response=True,
                                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    g()  # load at startup (cold start) rather than on the first tool call
    uvicorn.run(require_api_key(app, keys), host="0.0.0.0", port=port, log_level="warning")


def main() -> None:
    ap = argparse.ArgumentParser(description="EIS knowledge-graph MCP server")
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--data", choices=["sample", "full"], default="sample",
                    help="sample: data/graph (50 docs); full: data/full/graph (extract-all corpus)")
    ap.add_argument("--deployed", action="store_true",
                    help="serve as deployed on Lambda: stateless HTTP on $PORT with the API key check")
    a = ap.parse_args()
    if a.deployed:
        return serve_deployed(int(os.environ.get("PORT", a.port)))
    global GRAPH_DIR
    GRAPH_DIR = DATA_DIR / "graph" if a.data == "sample" else DATA_DIR / a.data / "graph"
    if a.http:
        mcp.run("streamable-http", host="127.0.0.1", port=a.port)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
