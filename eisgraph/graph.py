"""Assemble extraction + linking results into an RDF dataset (TriG), one named graph per EIS.

Graphs:
  <id/graph/ontology>   class & property declarations
  <id/graph/entities>   rdf:type / rdfs:label / skos:altLabel for every entity
  <id/graph/doc/BARCODE> facts asserted by that EIS (provenance = graph name)
  <id/graph/doclinks>   EIS-to-EIS relations and owl:sameAs for references that resolve to sampled docs
Evidence quotes are kept beside the graph in evidence.json, keyed by "s p o".
"""

import json
import re
from collections import defaultdict
from pathlib import Path

from pyoxigraph import DefaultGraph, Literal, NamedNode, Quad, RdfFormat, Store

from .link import RESERVED_DOC_KEY, load_extractions, norm
from .ontology import ATTRIBUTES, CLASSES, DATA, DOC_LINK_PREDICATES, NS, ONTOLOGY_IRI, RELATIONS

RDF_TYPE = NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
LABEL, COMMENT = NamedNode(RDFS + "label"), NamedNode(RDFS + "comment")
ALT = NamedNode("http://www.w3.org/2004/02/skos/core#altLabel")
SAME_AS = NamedNode("http://www.w3.org/2002/07/owl#sameAs")
OWL = "http://www.w3.org/2002/07/owl#"
G_ONT, G_ENT, G_LINKS = (NamedNode(DATA + f"graph/{g}") for g in ("ontology", "entities", "doclinks"))


# Entities the model sometimes mints that carry no information: placeholders echoing the reserved key, and
# generic document-type names ("the draft EIS") that would otherwise become a hub joining every EIS.
GENERIC_LABELS = {norm(x) for x in (
    "Draft Environmental Impact Statement", "Final Environmental Impact Statement", "Environmental Impact Statement",
    "Draft Environmental Statement", "Final Environmental Statement", "Environmental Statement", "EIS", "DEIS",
    "FEIS", "this statement", "this document")}


def is_junk(label: str) -> bool:
    return "this_document" in label.lower() or norm(label) in GENERIC_LABELS


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(s))[:80].strip("-") or "x"


def eis(term: str) -> NamedNode:
    return NamedNode(NS + term)


def doc_uri(barcode: str) -> NamedNode:
    return NamedNode(DATA + f"doc/{barcode}")


def ontology_quads(graph=None) -> list[Quad]:
    """Class and property declarations; with graph=None they go in the default graph (for ontology/eis.ttl)."""
    out = [Quad(NamedNode(ONTOLOGY_IRI), RDF_TYPE, NamedNode(OWL + "Ontology"), graph),
           Quad(NamedNode(ONTOLOGY_IRI), LABEL, Literal("EIS knowledge-graph ontology"), graph),
           Quad(NamedNode(ONTOLOGY_IRI), COMMENT, Literal(
               "Vocabulary for facts extracted from U.S. Environmental Impact Statements. "
               "Source: https://github.com/aerithnetzer/eis-graph"), graph)]
    for kinds, owl_type in (({"EISDocument": (None, "An Environmental Impact Statement.")} | CLASSES, "Class"),
                            ({**RELATIONS, **DOC_LINK_PREDICATES}, "ObjectProperty"), (ATTRIBUTES, "DatatypeProperty")):
        for term, desc in kinds.items():
            desc = desc[1] if isinstance(desc, tuple) else desc
            out += [Quad(eis(term), RDF_TYPE, NamedNode(OWL + owl_type), graph),
                    Quad(eis(term), LABEL, Literal(term), graph),
                    Quad(eis(term), COMMENT, Literal(desc), graph),
                    Quad(eis(term), NamedNode(RDFS + "isDefinedBy"), NamedNode(ONTOLOGY_IRI), graph)]
    return out


def write_ontology(path: Path) -> None:
    store = Store()
    store.extend(ontology_quads())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(store.dump(format=RdfFormat.TURTLE, from_graph=DefaultGraph(),
                                prefixes={"eis": NS, "owl": OWL, "rdfs": RDFS}))
    print(f"{len(store)} triples -> {path}")


def build(raw_dir: Path, work_dir: Path, out_dir: Path) -> Store:
    manifest = {d["barcode"]: d for d in json.loads((raw_dir / "manifest.json").read_text())["docs"]}
    de = json.loads((work_dir / "doc_entities.json").read_text())
    per_doc = de["entities"]
    key_maps = {b: {tuple(k.split("|", 1)): v for k, v in km.items()} for b, km in de["key_maps"].items()}
    cands = json.loads((work_dir / "candidates.json").read_text())
    profiles = {p["barcode"]: p for p in json.loads((work_dir / "profiles.json").read_text())}

    # Candidate id -> canonical (label, cluster id) after Claude's resolution clusters.
    canon = {c["id"]: c["id"] for c in cands}
    canon_label = {c["id"]: c["label"] for c in cands}
    # Linking results: subagent output (link_agent/) when present, else the Bedrock runner's (link/).
    link_dir = work_dir / "link_agent" if any((work_dir / "link_agent").glob("*.json")) else work_dir / "link"
    eid_to_cand = {m: c["id"] for c in cands for m in c["members"]}
    # Link results refer to candidate ids, which prepare-link renumbers whenever documents are added. If the
    # candidate list they were made against is frozen beside them (_candidates.json), translate old ids to the
    # current ones through the candidates' member entity ids, which are stable.
    frozen = link_dir / "_candidates.json"
    old_members = {c["id"]: c["members"] for c in json.loads(frozen.read_text())} if frozen.exists() else None

    def xlate(i: int) -> int | None:
        if old_members is None:
            return i
        return next((eid_to_cand[m] for m in old_members.get(i, []) if m in eid_to_cand), None)

    for f in sorted(link_dir.glob("resolve__*.json")):
        for cl in json.loads(f.read_text())["clusters"]:
            ids = [j for j in (xlate(i) for i in cl["member_ids"]) if j in canon]
            if len(ids) < 2:
                continue
            heads = {canon[i] for i in ids}
            head = min(heads)
            for i in canon:  # re-point anything already merged into one of these clusters
                if canon[i] in heads:
                    canon[i] = head
            canon_label[head] = cl["canonical_label"]

    def entity_uri(barcode: str, eid: str) -> NamedNode:
        e = per_doc[barcode][eid]
        if eid in eid_to_cand:
            head = canon[eid_to_cand[eid]]
            return NamedNode(DATA + f"{e['type']}/{slug(canon_label[head])}")
        return NamedNode(DATA + f"doc/{barcode}/{e['type']}/{slug(e['label'])}")

    store = Store()
    quads = []
    add = lambda s, p, o, g: quads.append(Quad(s, p, o, g))
    evidence = defaultdict(list)

    quads.extend(ontology_quads(G_ONT))

    labelled = set()
    for barcode, chunks in load_extractions(work_dir).items():
        if barcode not in profiles:  # extraction unfinished when prepare-link ran
            continue
        g = NamedNode(DATA + f"graph/doc/{barcode}")
        d = doc_uri(barcode)
        prof = profiles.get(barcode, {})
        title = (prof.get("projects") or [barcode])[0]
        stage = prof.get("stage")
        add(d, RDF_TYPE, eis("EISDocument"), G_ENT)
        add(d, LABEL, Literal(f"{stage + ': ' if stage else ''}{title} [{barcode}]"), G_ENT)
        add(d, eis("barcode"), Literal(barcode), G_ENT)
        add(d, eis("sourceUri"), Literal(manifest.get(barcode, {}).get("s3", "")), G_ENT)

        uris = {eid: entity_uri(barcode, eid) for eid, e in per_doc[barcode].items() if not is_junk(e["label"])}
        for eid, e in per_doc[barcode].items():
            if eid not in uris:
                continue
            u = uris[eid]
            add(d, eis("mentions"), u, g)
            if u in labelled:
                continue
            labelled.add(u)
            add(u, RDF_TYPE, eis(e["type"]), G_ENT)
            cand = eid_to_cand.get(eid)
            add(u, LABEL, Literal(canon_label[canon[cand]] if cand is not None else e["label"]), G_ENT)
            for a in e["aliases"][:10]:
                add(u, ALT, Literal(a), G_ENT)

        km = key_maps[barcode]
        resolve = lambda c, key: d if key == RESERVED_DOC_KEY else uris.get(km.get((str(c), key)))
        for c, ext in chunks:
            for r in ext["relations"]:
                s, o = resolve(c, r["subject"]), resolve(c, r["object"])
                if s is None or o is None or s == o:
                    continue
                p = eis(r["predicate"])
                add(s, p, o, g)
                evidence[f"{s.value} {p.value} {o.value}"].append(
                    {"doc": barcode, "chunk": c, "quote": r["evidence"][:300], "note": r["note"]})
            for a in ext["attributes"]:
                s = resolve(c, a["subject"])
                if s is not None and a["value"].strip():
                    add(s, eis(a["property"]), Literal(a["value"].strip()), g)

    by_id = {c["id"]: c for c in cands}
    # One file per source document (doclinks__<barcode>.json); a single doclinks.json from older runs also matches.
    for links_file in sorted(link_dir.glob("doclinks*.json")):
        try:
            links = json.loads(links_file.read_text())
        except json.JSONDecodeError:  # a linking run may be writing it right now
            print(f"skipping unreadable {links_file.name}")
            continue
        for ln in links["links"]:
            if ln["source"] in profiles and ln["target"] in profiles and ln["source"] != ln["target"]:
                s, p, o = doc_uri(ln["source"]), eis(ln["predicate"]), doc_uri(ln["target"])
                add(s, p, o, G_LINKS)
                evidence[f"{s.value} {p.value} {o.value}"].append({"doc": "doclinks", "quote": ln["rationale"]})
        for m in links["reference_matches"]:
            c = by_id.get(xlate(m["ref_id"]))
            if c and m["barcode"] in profiles:
                head = canon[c["id"]]
                add(NamedNode(DATA + f"Document/{slug(canon_label[head])}"), SAME_AS, doc_uri(m["barcode"]), G_LINKS)

    store.extend(quads)
    out_dir.mkdir(parents=True, exist_ok=True)
    store.dump(str(out_dir / "graph.trig"), RdfFormat.TRIG)
    (out_dir / "evidence.json").write_text(json.dumps(evidence))
    print(f"{len(store):,} quads -> {out_dir / 'graph.trig'}")
    return store


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["ontology"]:
        write_ontology(Path(__file__).resolve().parent.parent / "ontology" / "eis.ttl")
