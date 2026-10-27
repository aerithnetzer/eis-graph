"""Local visualization server: in-memory Oxigraph store + JSON API + static cytoscape.js front end.

Endpoints
  GET  /api/stats                 counts per class / graph
  GET  /api/overview?min_docs=2   EIS documents, doc-to-doc links, and the entities shared across EISs
  GET  /api/node?iri=...          one node: types, labels, attributes, neighbours (with source EIS)
  GET  /api/search?q=...          label search
  GET  /api/evidence?s=&p=&o=     supporting quotes for one triple
  POST /api/sparql                SPARQL SELECT/ASK (default graph = union of all named graphs)
"""

import json
import shutil
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pyoxigraph import Literal, NamedNode, QueryBoolean, QuerySolutions, RdfFormat, Store

from .ontology import DATA, NS

STATIC = Path(__file__).resolve().parent.parent / "static"
PREFIXES = f"""PREFIX eis: <{NS}>
PREFIX id: <{DATA}>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
"""


def _term(t):
    if t is None:
        return None
    if isinstance(t, NamedNode):
        return {"type": "uri", "value": t.value}
    if isinstance(t, Literal):
        return {"type": "literal", "value": t.value}
    return {"type": "bnode", "value": str(t)}


class SqliteEvidence:
    """evidence.json's {"s p o": [quotes]} mapping, read from the evidence.sqlite that build-serving writes."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, check_same_thread=False)

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT v FROM evidence WHERE k = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def __len__(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]


class Graph:
    def __init__(self, out_dir: Path):
        if (out_dir / "store").is_dir():  # build-serving output: open on disk, nothing to parse
            self.store = Store.read_only(str(out_dir / "store"))
            self.evidence = SqliteEvidence(out_dir / "evidence.sqlite")
        else:
            self.store = Store()
            self.store.load(path=str(out_dir / "graph.trig"), format=RdfFormat.TRIG)
            self.evidence = json.loads((out_dir / "evidence.json").read_text())
        print(f"loaded {len(self.store):,} quads", file=sys.stderr)


    def select(self, q: str, limit: int | None = None) -> dict:
        res = self.store.query(PREFIXES + q, use_default_graph_as_union=True)
        if isinstance(res, QueryBoolean):
            return {"boolean": bool(res)}
        if not isinstance(res, QuerySolutions):
            raise ValueError("only SELECT and ASK queries are supported here")
        vars_ = [v.value for v in res.variables]
        rows = []
        for sol in res:
            rows.append({v: _term(sol[v]) for v in vars_})
            if limit and len(rows) >= limit:
                break
        return {"vars": vars_, "rows": rows, "truncated": bool(limit and len(rows) >= limit)}

    def rows(self, q: str) -> list[dict]:
        return [{k: (v["value"] if v else None) for k, v in r.items()} for r in self.select(q)["rows"]]

    def stats(self) -> dict:
        classes = self.rows("SELECT ?c (COUNT(DISTINCT ?s) AS ?n) WHERE { ?s a ?c FILTER(STRSTARTS(STR(?c), STR(eis:))) } "
                            "GROUP BY ?c ORDER BY DESC(?n)")
        return {"quads": len(self.store),
                "classes": {r["c"][len(NS):]: int(r["n"]) for r in classes},
                "evidence_triples": len(self.evidence)}

    def overview(self, min_docs: int, max_docs: int, max_entities: int) -> dict:
        """EISs plus entities shared by min_docs..max_docs of them. Entities above max_docs (near-universal hubs
        like USFWS or NEPA) are returned separately as `hubs` instead of drawn, since they connect everything."""
        docs = self.rows("SELECT ?d ?l WHERE { ?d a eis:EISDocument ; rdfs:label ?l }")
        links = self.rows(f"SELECT ?s ?p ?o WHERE {{ GRAPH <{DATA}graph/doclinks> {{ ?s ?p ?o "
                          "FILTER(?p != owl:sameAs) } }")
        shared = self.rows(f"""
            SELECT ?e ?l ?t (COUNT(DISTINCT ?d) AS ?n) (GROUP_CONCAT(DISTINCT STR(?d); separator=" ") AS ?ds)
            WHERE {{ ?d a eis:EISDocument . GRAPH ?g {{ ?d eis:mentions ?e }} ?e a ?t ; rdfs:label ?l }}
            GROUP BY ?e ?l ?t HAVING (COUNT(DISTINCT ?d) >= {int(min_docs)})
            ORDER BY DESC(?n)""")
        hubs = [{"id": e["e"], "label": e["l"], "type": e["t"][len(NS):], "docs": int(e["n"])}
                for e in shared if int(e["n"]) > max_docs]
        nodes = [{"id": d["d"], "label": d["l"], "type": "EISDocument"} for d in docs]
        edges = [{"source": l["s"], "target": l["o"], "predicate": l["p"][len(NS):]} for l in links]
        for e in [e for e in shared if int(e["n"]) <= max_docs][:max_entities]:
            nodes.append({"id": e["e"], "label": e["l"], "type": e["t"][len(NS):], "docs": int(e["n"])})
            edges += [{"source": d, "target": e["e"], "predicate": "mentions"} for d in e["ds"].split()]
        return {"nodes": nodes, "edges": edges, "hubs": hubs}

    def node(self, iri: str, limit: int = 200) -> dict:
        n = f"<{iri}>"
        info = self.rows(f"SELECT ?p ?o ?g WHERE {{ GRAPH ?g {{ {n} ?p ?o }} FILTER(isLiteral(?o)) }}")
        types = [r["t"] for r in self.rows(f"SELECT ?t WHERE {{ {n} a ?t }}")]
        out = self.rows(f"""SELECT ?p ?o ?ol ?ot ?g WHERE {{ GRAPH ?g {{ {n} ?p ?o }} FILTER(isIRI(?o) && ?p != rdf:type)
                             OPTIONAL {{ ?o rdfs:label ?ol }} OPTIONAL {{ ?o a ?ot }} }} LIMIT {limit}""")
        inc = self.rows(f"""SELECT ?s ?p ?sl ?st ?g WHERE {{ GRAPH ?g {{ ?s ?p {n} }}
                             OPTIONAL {{ ?s rdfs:label ?sl }} OPTIONAL {{ ?s a ?st }} }} LIMIT {limit}""")
        doc_of = lambda g: g.rsplit("/", 1)[-1] if g and "/graph/doc/" in g else (g or "").rsplit("/", 1)[-1]
        edges, seen = [], set()
        for r in out:
            k = (iri, r["p"], r["o"])
            if k not in seen:
                seen.add(k)
                edges.append({"source": iri, "target": r["o"], "predicate": r["p"].rsplit("/", 1)[-1].rsplit("#", 1)[-1],
                              "target_label": r["ol"] or r["o"], "target_type": (r["ot"] or "").rsplit("/", 1)[-1],
                              "doc": doc_of(r["g"]), "p_iri": r["p"]})
        for r in inc:
            k = (r["s"], r["p"], iri)
            if k not in seen:
                seen.add(k)
                edges.append({"source": r["s"], "target": iri, "predicate": r["p"].rsplit("/", 1)[-1].rsplit("#", 1)[-1],
                              "source_label": r["sl"] or r["s"], "source_type": (r["st"] or "").rsplit("/", 1)[-1],
                              "doc": doc_of(r["g"]), "p_iri": r["p"]})
        return {"iri": iri, "types": [t.rsplit("/", 1)[-1] for t in types],
                "literals": [{"p": r["p"].rsplit("/", 1)[-1].rsplit("#", 1)[-1], "v": r["o"], "doc": doc_of(r["g"])}
                             for r in info],
                "edges": edges, "truncated": len(out) >= limit or len(inc) >= limit}

    def search(self, q: str) -> list[dict]:
        needle = json.dumps(q.lower())
        return self.rows(f"""SELECT DISTINCT ?e ?l ?t WHERE {{ ?e rdfs:label ?l ; a ?t
                             FILTER(STRSTARTS(STR(?t), STR(eis:)) && CONTAINS(LCASE(?l), {needle})) }}
                             ORDER BY STRLEN(?l) LIMIT 40""")


def build_serving(graph_dir: Path, out_dir: Path) -> None:
    """Turn graph.trig + evidence.json into an on-disk Oxigraph store + evidence.sqlite, for read-only serving."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    store = Store(str(out_dir / "store"))
    store.bulk_load(path=str(graph_dir / "graph.trig"), format=RdfFormat.TRIG)
    store.optimize()
    store.flush()
    n = len(store)
    del store  # release the RocksDB lock before anyone opens it read-only
    db = sqlite3.connect(out_dir / "evidence.sqlite")
    db.execute("CREATE TABLE evidence (k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID")
    ev = json.loads((graph_dir / "evidence.json").read_text())
    db.executemany("INSERT INTO evidence VALUES (?, ?)", ((k, json.dumps(v)) for k, v in ev.items()))
    db.commit()
    db.execute("VACUUM")
    db.close()
    print(f"{n:,} quads, {len(ev):,} evidence keys -> {out_dir}")


def serve(out_dir: Path, port: int) -> None:
    g = Graph(out_dir)

    class H(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/stats":
                    return self._json(g.stats())
                if u.path == "/api/overview":
                    return self._json(g.overview(int(q.get("min_docs", 3)), int(q.get("max_docs", 15)),
                                                  int(q.get("max", 150))))
                if u.path == "/api/node":
                    return self._json(g.node(q["iri"]))
                if u.path == "/api/search":
                    return self._json(g.search(q.get("q", "")))
                if u.path == "/api/evidence":
                    return self._json(g.evidence.get(f'{q["s"]} {q["p"]} {q["o"]}', []))
                self._json({"error": "not found"}, 404)
            except Exception as e:  # surface query errors to the UI
                self._json({"error": str(e)}, 400)

        def do_POST(self):
            if urlparse(self.path).path != "/api/sparql":
                return self._json({"error": "not found"}, 404)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            try:
                self._json(g.select(json.loads(body)["query"], limit=2000))
            except Exception as e:
                self._json({"error": str(e)}, 400)

        def log_message(self, fmt, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"serving on http://127.0.0.1:{port}")
    srv.serve_forever()
