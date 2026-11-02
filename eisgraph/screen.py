"""Screen a large S3 project (e.g. jobs/ien/) for Environmental Impact Statements before paying for extraction.

  heads    download the first few OCR pages of every job that has OCR JSON, render them to text
  batches  write screening task files of ~50 documents each, for subagents to classify
  results  collect the subagents' verdicts into one table

Inputs/outputs live under data/<project>/: json_pages.tsv (barcode, page count, bytes; from an S3 listing),
raw/<barcode>/json/ (downloaded pages), heads/<barcode>.txt, screen/tasks/*.txt, screen/out/*.json.
Credentials come from the environment (e.g. `eval $(aws configure export-credentials --format env)`).
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .render import render_page
from .sample import BUCKET, PREFIX

HEAD_PAGES = 3  # pages always read
MAX_HEAD_PAGES = 10  # keep reading past blank/image-only covers until MIN_HEAD_CHARS of text
MIN_HEAD_CHARS = 1500
HEAD_CHARS = 3000
BATCH = 50

SCREEN_INSTRUCTIONS = """You screen scanned U.S. government documents (OCR text of their first pages) to find Environmental
Impact Statements prepared under NEPA. For EACH document below, decide:
- is_eis: true if the document itself is an Environmental Impact Statement or Environmental Statement (draft, final,
  supplemental, programmatic) or a Record of Decision on one. False for design memoranda, feasibility or survey
  reports, environmental assessments that are not EISs, comment letters, studies, plans, hearings transcripts, etc.
  Judge from cover sheets, titles and front matter ("Draft Environmental Statement", "Final Environmental Impact
  Statement", "prepared pursuant to Section 102(2)(C)", CEQ/EIS numbers).
- doc_type: short label, e.g. "Final EIS", "Draft EIS", "Supplemental EIS", "Design memorandum", "Feasibility report".
- title: the document's title as best you can tell (short).
- confidence: high | medium | low (low when the OCR text is empty or unreadable).
Return one entry per document, in order, using exactly the barcodes given."""

SCREEN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["documents"],
    "properties": {
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["barcode", "is_eis", "doc_type", "title", "confidence"],
                "properties": {
                    "barcode": {"type": "string"},
                    "is_eis": {"type": "boolean"},
                    "doc_type": {"type": "string"},
                    "title": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        }
    },
}


def jobs(root: Path) -> list[tuple[str, int]]:
    rows = [line.split("\t") for line in (root / "json_pages.tsv").read_text().splitlines() if line.strip()]
    return sorted((b, int(n)) for b, n, _ in rows)


def heads(root: Path, project: str, workers: int = 32) -> None:
    import boto3

    s3 = boto3.client("s3", region_name="us-east-2")
    out = root / "heads"
    out.mkdir(parents=True, exist_ok=True)

    def one(job: tuple[str, int]) -> str:
        barcode, _ = job
        dest = out / f"{barcode}.txt"
        if dest.exists() and len(dest.read_text().strip()) >= 200:
            return "cached"
        prefix = f"{PREFIX}{project}/{barcode}/json/"
        keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix, MaxKeys=MAX_HEAD_PAGES + 5)
                      .get("Contents", []) if o["Key"].endswith(".json"))[:MAX_HEAD_PAGES]
        parts = []
        for n, k in enumerate(keys):
            if n >= HEAD_PAGES and sum(len(x.strip()) for x in parts) >= MIN_HEAD_CHARS:
                break
            raw = s3.get_object(Bucket=BUCKET, Key=k)["Body"].read()
            pdir = root / "raw" / barcode / "json"
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / Path(k).name).write_bytes(raw)
            try:
                parts.append(render_page(json.loads(raw)))
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError):
                parts.append(raw.decode("utf-8", "replace"))  # a few jobs store plain text under .json
        dest.write_text("\n\n".join(parts)[:HEAD_CHARS])
        return "fetched"

    todo = jobs(root)
    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for i, _ in enumerate(ex.map(one, todo), 1):
            done = i
            if i % 1000 == 0:
                print(f"  {i}/{len(todo)}", flush=True)
    print(f"heads: {done} jobs -> {out}")


def batches(root: Path) -> list[str]:
    tasks = root / "screen" / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (root / "screen" / "out").mkdir(parents=True, exist_ok=True)
    (root / "screen" / "schema.json").write_text(json.dumps(SCREEN_SCHEMA, indent=1))
    todo = jobs(root)
    ids = []
    for k in range(0, len(todo), BATCH):
        part = todo[k:k + BATCH]
        bid = f"batch_{k // BATCH:04d}"
        docs = "\n\n".join(f"### DOCUMENT barcode={b} total_pages={n}\n{(root / 'heads' / f'{b}.txt').read_text()}"
                           for b, n in part)
        (tasks / f"{bid}.txt").write_text(f"=== INSTRUCTIONS ===\n{SCREEN_INSTRUCTIONS}\n\n=== DOCUMENTS ({len(part)}) "
                                          f"===\n{docs}\n")
        ids.append(bid)
    print(f"{len(ids)} screening batches of up to {BATCH} -> {tasks}")
    return ids


def validate(root: Path, bid: str) -> list[str]:
    f = root / "screen" / "out" / f"{bid}.json"
    if not f.exists():
        return [f"{f} does not exist"]
    try:
        d = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        return [f"invalid JSON: {e}"]
    task = (root / "screen" / "tasks" / f"{bid}.txt").read_text()
    want = [line.split("barcode=")[1].split()[0] for line in task.splitlines() if line.startswith("### DOCUMENT ")]
    errs = []
    docs = d.get("documents") if isinstance(d, dict) else None
    if not isinstance(docs, list):
        return ["missing 'documents' array"]
    got = [x.get("barcode") for x in docs if isinstance(x, dict)]
    if sorted(got) != sorted(want):
        errs.append(f"barcodes don't match the batch: missing {sorted(set(want) - set(got))[:5]}, "
                    f"extra {sorted(set(got) - set(want))[:5]}, {len(got)} given for {len(want)}")
    for i, x in enumerate(docs):
        for k, t in (("is_eis", bool), ("doc_type", str), ("title", str)):
            if not isinstance(x.get(k), t):
                errs.append(f"documents[{i}].{k} missing or wrong type")
        if x.get("confidence") not in ("high", "medium", "low"):
            errs.append(f"documents[{i}].confidence must be high|medium|low")
        extra = set(x) - {"barcode", "is_eis", "doc_type", "title", "confidence"}
        if extra:
            errs.append(f"documents[{i}] has unexpected fields {sorted(extra)}")
    return errs[:20]


def results(root: Path) -> None:
    pages = dict(jobs(root))
    rows = []
    for f in sorted((root / "screen" / "out").glob("batch_*.json")):
        for x in json.loads(f.read_text())["documents"]:
            rows.append({**x, "pages": pages.get(x["barcode"])})
    (root / "screen" / "results.json").write_text(json.dumps(rows, indent=0))
    eis = [r for r in rows if r["is_eis"]]
    print(f"{len(rows)} screened, {len(eis)} EISs ({sum(r['pages'] or 0 for r in eis):,} pages)")


if __name__ == "__main__":
    cmd, root = sys.argv[1], Path(sys.argv[2])
    if cmd == "heads":
        heads(root, sys.argv[3])
    elif cmd == "batches":
        batches(root)
    elif cmd == "validate":
        bad = 0
        for bid in sys.argv[3:]:
            errs = validate(root, bid)
            print(f"{bid}: OK" if not errs else f"{bid}: {len(errs)} problem(s)\n  " + "\n  ".join(errs))
            bad += bool(errs)
        sys.exit(1 if bad else 0)
    elif cmd == "results":
        results(root)
