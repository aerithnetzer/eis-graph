"""Phase 2: cross-document linking.

1. Merge each document's chunk-level entities into per-document entities.
2. Pool GLOBAL-type entities across documents into candidates (deterministic normalization + alias matching).
3. Build a second batch: entity-resolution requests per type (Claude clusters name variants across docs), plus
   document-to-document link verification in two stages so it scales past what fits in one prompt:
   a. candidate pairs, scored without a model: documents sharing specific entities (waterbodies, places, species,
      referenced documents...), identifiers, or project-title words, each feature weighted by how rare it is;
   b. one request per document, holding its profile and its top PAIR_K partners' profiles with what they share,
      in which Claude asserts only the links that hold. Each unordered pair is judged once.
"""

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import batching
from .ontology import (CLASSES, DOC_LINK_SCHEMA, DOC_LINK_SYSTEM, RESERVED_DOC_KEY, RESOLUTION_SCHEMA,
                       RESOLUTION_SYSTEM)

RESOLVE_CHUNK = 1200  # candidates per resolution request
PAIR_K = 8  # partners per document sent for link verification
MIN_PAIR_SCORE = 2.0  # below this, sharing is too thin to be worth a model's look
MAX_DF_SHARE, MIN_DF_CAP = 0.05, 10  # skip features in more than max(5% of docs, 10 docs): too generic to link
# How much one shared feature of each kind says about two EISs being related (times its IDF).
PAIR_WEIGHTS = {"identifier": 4.0, "title": 1.5, "Document": 2.0, "Waterbody": 1.5, "Location": 1.0,
                "Species": 0.5, "Habitat": 0.5, "Agency": 0.3, "Organization": 0.5, "Person": 0.5}
TITLE_STOP = {"project", "projects", "environmental", "impact", "statement", "draft", "final", "supplement",
              "supplemental", "and", "of", "the", "for", "in", "on", "at", "to", "river", "lake", "county", "unit",
              "plan", "program", "local", "protection", "improvement", "maintenance", "operation", "control"}


def norm(label: str) -> str:
    s = label.lower().replace("u.s.", "us").replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^(the|a|an) ", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


class UnionFind:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def load_extractions(work_dir: Path) -> dict[str, list[tuple[int, dict]]]:
    """barcode -> [(chunk_no, extraction_json)]"""
    index = json.loads((work_dir / "extract_index.json").read_text())
    by_doc = defaultdict(list)
    for cid, meta in index.items():
        f = work_dir / "extract" / f"{cid}.json"
        if f.exists():
            by_doc[meta["barcode"]].append((meta["chunk"], json.loads(f.read_text())))
    return by_doc


def doc_entities(chunks: list[tuple[int, dict]]) -> tuple[dict, dict]:
    """Merge one doc's chunk entities. Returns (entities by doc-entity id, (chunk, key) -> doc-entity id)."""
    uf = UnionFind()
    labels = defaultdict(Counter)
    aliases = defaultdict(set)
    key_map = {}
    for c, ext in chunks:
        for e in ext["entities"]:
            if e["key"] == RESERVED_DOC_KEY or not e["label"].strip():
                continue
            node = (e["type"], norm(e["label"]))
            uf.find(node)
            labels[node][e["label"].strip()] += 1
            for a in e["aliases"]:
                if a.strip():
                    aliases[node].add(a.strip())
            key_map[(c, e["key"])] = node
    # Alias of one entity == label of another of the same type -> same entity (within a document this is safe).
    by_norm = {n for n in labels}
    for node, al in aliases.items():
        for a in al:
            other = (node[0], norm(a))
            if other in by_norm and other != node:
                uf.union(node, other)
    ents = {}
    for node in labels:
        root = uf.find(node)
        e = ents.setdefault(root, {"type": node[0], "labels": Counter(), "aliases": set()})
        e["labels"].update(labels[node])
        e["aliases"] |= aliases[node]
    out = {}
    for root, e in ents.items():
        label = e["labels"].most_common(1)[0][0]
        eid = f"{e['type']}:{norm(label)}"
        out[eid] = {"type": e["type"], "label": label,
                    "aliases": sorted((e["aliases"] | set(e["labels"])) - {label}),
                    "mentions": sum(e["labels"].values())}
        ents[root]["eid"] = eid
    key_to_eid = {k: ents[uf.find(node)]["eid"] for k, node in key_map.items()}
    return out, key_to_eid


def build_candidates(per_doc: dict[str, dict]) -> list[dict]:
    """Pool global-type entities across documents, pre-merging exact normalized-name/alias matches."""
    uf = UnionFind()
    info = {}
    for barcode, ents in per_doc.items():
        for eid, e in ents.items():
            if CLASSES[e["type"]][0] != "global":
                continue
            uf.find(eid)
            i = info.setdefault(eid, {"type": e["type"], "labels": Counter(), "aliases": set(), "docs": set()})
            i["labels"][e["label"]] += e["mentions"]
            i["aliases"] |= set(e["aliases"])
            i["docs"].add(barcode)
    for eid, i in info.items():
        for a in i["aliases"]:
            other = f"{i['type']}:{norm(a)}"
            if len(norm(a)) >= 3 and other in info and other != eid:
                uf.union(eid, other)
    groups = defaultdict(list)
    for eid in info:
        groups[uf.find(eid)].append(eid)
    cands = []
    for members in groups.values():
        labels, aliases, docs = Counter(), set(), set()
        for m in members:
            labels.update(info[m]["labels"])
            aliases |= info[m]["aliases"]
            docs |= info[m]["docs"]
        label = labels.most_common(1)[0][0]
        cands.append({"id": len(cands), "type": info[members[0]]["type"], "label": label,
                      "aliases": sorted(aliases - {label})[:8], "docs": sorted(docs), "members": sorted(members)})
    return cands


def doc_profile(barcode: str, chunks, ents: dict, key_to_eid: dict, cands_by_eid: dict) -> dict:
    attrs = defaultdict(list)
    rels = defaultdict(Counter)
    for c, ext in chunks:
        for a in ext["attributes"]:
            if a["subject"] == RESERVED_DOC_KEY:
                attrs[a["property"]].append(a["value"])
        for r in ext["relations"]:
            o = key_to_eid.get((c, r["object"]))
            if o:
                rels[r["predicate"]][ents[o]["label"]] += 1
    by_type = defaultdict(Counter)
    for e in ents.values():
        by_type[e["type"]][e["label"]] += e["mentions"]
    top = lambda t, n: [l for l, _ in by_type[t].most_common(n)]
    refs = [{"ref_id": cands_by_eid[eid]["id"], "title": ents[eid]["label"]}
            for eid in ents if ents[eid]["type"] == "Document" and eid in cands_by_eid][:40]
    return {
        "barcode": barcode,
        "stage": Counter(attrs["documentStage"]).most_common(1)[0][0] if attrs["documentStage"] else None,
        "date": attrs["date"][:3], "identifiers": attrs["identifier"][:5],
        "projects": top("Project", 3),
        "lead_agencies": [l for l, _ in rels["leadAgency"].most_common(3)] or top("Agency", 3),
        "cooperating": [l for l, _ in rels["cooperatingAgency"].most_common(8)],
        "places": top("Location", 15), "waterbodies": top("Waterbody", 10),
        "facilities": top("Facility", 8), "resources": top("ResourceTopic", 12),
        "referenced_documents": refs,
    }


def candidate_pairs(profiles: list[dict], per_doc: dict[str, dict], cands_by_eid: dict) -> dict[str, list[dict]]:
    """barcode -> [{"barcode": partner, "score": s, "shared": [labels]}], each unordered pair under one document."""
    feats: dict[str, dict[tuple, str]] = defaultdict(dict)  # doc -> feature key -> display label
    for b, ents in per_doc.items():
        for eid in ents:
            c = cands_by_eid.get(eid)
            if c and c["type"] in PAIR_WEIGHTS:
                feats[b][(c["type"], c["id"])] = f'{c["type"]}: {c["label"]}'
    for p in profiles:
        for i in p["identifiers"]:
            if len(k := re.sub(r"[^a-z0-9]", "", i.lower())) >= 4:
                feats[p["barcode"]][("identifier", k)] = f"identifier: {i}"
        for t in set(norm(" ".join(p["projects"][:1])).split()) - TITLE_STOP:
            if len(t) >= 3 and not t.isdigit():
                feats[p["barcode"]][("title", t)] = f"title word: {t}"
    docs_by_feat = defaultdict(list)
    for b, fs in feats.items():
        for f in fs:
            docs_by_feat[f].append(b)
    n = len(feats)
    max_df = max(MAX_DF_SHARE * n, MIN_DF_CAP)
    score: dict[tuple, float] = defaultdict(float)
    shared: dict[tuple, list] = defaultdict(list)
    for f, ds in docs_by_feat.items():
        if not 2 <= len(ds) <= max_df:
            continue
        w = PAIR_WEIGHTS[f[0]] * math.log(n / len(ds) + 1)
        ds = sorted(ds)
        for i, a in enumerate(ds):
            for b in ds[i + 1:]:
                score[(a, b)] += w
                shared[(a, b)].append((w, feats[a][f]))
    ranked = defaultdict(list)
    for (a, b), s in score.items():
        if s >= MIN_PAIR_SCORE:
            ranked[a].append((s, b))
            ranked[b].append((s, a))
    out, judged = defaultdict(list), set()
    for a in sorted(ranked, key=lambda d: -max(s for s, _ in ranked[d])):
        for s, b in sorted(ranked[a], reverse=True)[:PAIR_K]:
            key = tuple(sorted((a, b)))
            if key in judged:
                continue
            judged.add(key)
            out[a].append({"barcode": b, "score": round(s, 2),
                           "shared": [lab for _, lab in sorted(shared[key], reverse=True)[:12]]})
    return dict(out)


def build(work_dir: Path, complete: set[str] | None = None) -> list[dict]:
    """complete: barcodes whose extraction finished (None = all); partial documents are left out of the graph."""
    by_doc = {b: c for b, c in load_extractions(work_dir).items() if complete is None or b in complete}
    per_doc, key_maps = {}, {}
    for barcode, chunks in by_doc.items():
        per_doc[barcode], key_maps[barcode] = doc_entities(chunks)
    cands = build_candidates(per_doc)
    cands_by_eid = {m: c for c in cands for m in c["members"]}
    profiles = [doc_profile(b, by_doc[b], per_doc[b], key_maps[b], cands_by_eid) for b in sorted(by_doc)]

    requests = []
    by_type = defaultdict(list)
    for c in cands:
        by_type[c["type"]].append(c)
    for t, cs in sorted(by_type.items()):
        cs.sort(key=lambda c: norm(c["label"]))  # alphabetical so variants that span a chunk edge stay rare
        for k in range(0, len(cs), RESOLVE_CHUNK):
            part = cs[k:k + RESOLVE_CHUNK]
            if len(part) < 2:
                continue
            lines = "\n".join(f'{c["id"]}. {c["label"]}' + (f' (aka: {"; ".join(c["aliases"])})' if c["aliases"] else "")
                              for c in part)
            content = [{"type": "text", "text": f"Entity type: {t} — {CLASSES[t][1]}\n\n{lines}"}]
            requests.append(batching.structured_request(f"resolve__{t}__{k // RESOLVE_CHUNK}", batching.MODELS["link"],
                                                        RESOLUTION_SYSTEM, content, RESOLUTION_SCHEMA))
    by_barcode = {p["barcode"]: p for p in profiles}
    pairs = candidate_pairs(profiles, per_doc, cands_by_eid)
    for b, partners in sorted(pairs.items()):
        cands_txt = [{"shared": p["shared"], "profile": by_barcode[p["barcode"]]} for p in partners]
        content = [{"type": "text", "text": "Source EIS:\n" + json.dumps(by_barcode[b], indent=1)
                    + "\n\nCandidate EISs (with what they share with the source):\n" + json.dumps(cands_txt, indent=1)}]
        requests.append(batching.structured_request(f"doclinks__{batching.safe_id(b)[:50]}", batching.MODELS["link"],
                                                    DOC_LINK_SYSTEM, content, DOC_LINK_SCHEMA, max_tokens=16000))

    serial = lambda o: sorted(o) if isinstance(o, set) else o
    (work_dir / "doc_entities.json").write_text(json.dumps(
        {"entities": per_doc,
         "key_maps": {b: {f"{c}|{k}": v for (c, k), v in km.items()} for b, km in key_maps.items()}},
        default=serial))
    (work_dir / "candidates.json").write_text(json.dumps(cands, indent=0))
    (work_dir / "profiles.json").write_text(json.dumps(profiles, indent=1))
    (work_dir / "pairs.json").write_text(json.dumps(pairs, indent=1))
    batching.write_requests(work_dir / "link_requests.jsonl", requests)
    print(f"{sum(len(e) for e in per_doc.values())} doc-entities -> {len(cands)} global candidates; "
          f"{sum(map(len, pairs.values()))} candidate document pairs; {len(requests)} link requests")
    return requests
