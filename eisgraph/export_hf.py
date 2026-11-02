"""Export the built graph as a Hugging Face dataset: Parquet tables + the raw TriG.

  python -m eisgraph.export_hf data/full/graph data/full/hf

Writes:
  triples.parquet    one row per quad: graph, barcode, subject/predicate/object (+ labels), evidence quotes
  documents.parquet  one row per EIS: barcode, label, source URI
  entities.parquet   one row per entity: uri, type, label, alt labels
  graph.trig         copy of the RDF dataset
"""

import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from pyoxigraph import Literal, NamedNode, RdfFormat, Store

from .ontology import DATA, NS

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
ALT = "http://www.w3.org/2004/02/skos/core#altLabel"


def short(uri: str) -> str:
    for ns, p in ((DATA, "id:"), (NS, "eis:")):  # DATA is under NS, so test it first
        if uri.startswith(ns):
            return p + uri[len(ns):]
    return uri


def export(graph_dir: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    store = Store()
    store.bulk_load(path=str(graph_dir / "graph.trig"), format=RdfFormat.TRIG)
    evidence = json.loads((graph_dir / "evidence.json").read_text())

    labels, types, alts = {}, {}, defaultdict(list)
    for q in store.quads_for_pattern(None, None, None, NamedNode(DATA + "graph/entities")):
        s, p, o = q.subject.value, q.predicate.value, q.object
        if p == LABEL:
            labels[s] = o.value
        elif p == RDF_TYPE:
            types[s] = short(o.value)
        elif p == ALT:
            alts[s].append(o.value)

    cols = defaultdict(list)
    for q in store:
        g = q.graph_name.value
        s, p, o = q.subject.value, q.predicate.value, q.object
        lit = isinstance(o, Literal)
        barcode = g.rsplit("/", 1)[1] if "/graph/doc/" in g else None
        ev = [] if lit else [e for e in evidence.get(f"{s} {p} {o.value}", [])
                             if barcode is None or e.get("doc") == barcode]
        cols["graph"].append(short(g))
        cols["barcode"].append(barcode)
        cols["subject"].append(short(s))
        cols["subject_label"].append(labels.get(s))
        cols["predicate"].append(short(p))
        cols["object"].append(o.value if lit else short(o.value))
        cols["object_is_literal"].append(lit)
        cols["object_label"].append(None if lit else labels.get(o.value))
        cols["evidence"].append(list(dict.fromkeys(e["quote"] for e in ev)))
    pq.write_table(pa.table(cols), out / "triples.parquet", compression="zstd")

    docs = sorted(s for s, t in types.items() if t == "eis:EISDocument")
    src = {q.subject.value: q.object.value for q in store.quads_for_pattern(None, NamedNode(NS + "sourceUri"), None)}
    pq.write_table(pa.table({
        "barcode": [d.rsplit("/", 1)[1] for d in docs],
        "label": [labels.get(d) for d in docs],
        "source_uri": [src.get(d) for d in docs],
    }), out / "documents.parquet", compression="zstd")

    ents = sorted(s for s, t in types.items() if t != "eis:EISDocument")
    pq.write_table(pa.table({
        "uri": [short(e) for e in ents],
        "type": [types[e] for e in ents],
        "label": [labels.get(e) for e in ents],
        "alt_labels": [alts.get(e, []) for e in ents],
    }), out / "entities.parquet", compression="zstd")

    shutil.copy(graph_dir / "graph.trig", out / "graph.trig")
    print(f"{len(cols['graph']):,} triples, {len(docs):,} documents, {len(ents):,} entities -> {out}")


if __name__ == "__main__":
    export(Path(sys.argv[1]), Path(sys.argv[2]))
