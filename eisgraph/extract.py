"""Phase 1: chunk each EIS and build one structured-extraction request per chunk."""

import json
import re
from pathlib import Path

from . import batching
from .render import render_doc
from .ontology import EXTRACTION_SCHEMA, EXTRACTION_SYSTEM

CHUNK_CHARS = 80_000  # ~20k tokens: small enough for dense extraction, large enough to keep context
HEADER_CHARS = 12_000  # cover sheet / abstract, prepended to every chunk of a doc (cached per doc)


def chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Greedy split on page marks / form feeds / blank lines, never mid-paragraph unless one exceeds size."""
    units = re.split(r"(?=\[\[page \d+\]\])|\f|\n\s*\n", text)
    chunks, cur = [], ""
    for u in units:
        u = u.strip()
        if not u:
            continue
        while len(u) > size:  # pathological paragraph (e.g. a table with no blank lines)
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(u[:size])
            u = u[size:]
        if len(cur) + len(u) + 2 > size and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{u}" if cur else u
    if cur:
        chunks.append(cur)
    return chunks


def doc_requests(barcode: str, text: str) -> tuple[list[dict], dict]:
    """One extraction request per chunk of one document, plus its extract_index entries."""
    header = text[:HEADER_CHARS]
    parts = chunk(text)
    doc_id = batching.safe_id(barcode)
    requests, index = [], {}
    for i, part in enumerate(parts):
        cid = f"{doc_id[:56]}__{i:04d}"
        pages = re.findall(r"\[\[page (\d+)\]\]", part)
        span = f' pages="{pages[0]}-{pages[-1]}"' if pages else ""
        content = [
            # Same bytes for every chunk of this doc, so later chunks can read it from cache.
            {"type": "text", "cache_control": {"type": "ephemeral"},
             "text": f'<document_opening barcode="{barcode}">\n{header}\n</document_opening>'},
            {"type": "text",
             "text": f'<excerpt index="{i + 1}" of="{len(parts)}"{span}>\n{part}\n</excerpt>\n\n'
                     "Extract the knowledge graph for this excerpt."},
        ]
        requests.append(batching.structured_request(cid, batching.MODELS["extract"], EXTRACTION_SYSTEM, content, EXTRACTION_SCHEMA))
        index[cid] = {"barcode": barcode, "chunk": i, "of": len(parts), "pages": span.strip()}
    return requests, index


def build(raw_dir: Path, work_dir: Path) -> list[dict]:
    manifest = json.loads((raw_dir / "manifest.json").read_text())
    requests, index = [], {}
    for doc in manifest["docs"]:
        text = render_doc(raw_dir / doc["barcode"])
        reqs, idx = doc_requests(doc["barcode"], text)
        requests += reqs
        index |= idx
        print(f"{doc['barcode']}: {len(text):,} chars -> {len(reqs)} chunks")
    batching.write_requests(work_dir / "extract_requests.jsonl", requests)
    (work_dir / "extract_index.json").write_text(json.dumps(index, indent=1))
    return requests
