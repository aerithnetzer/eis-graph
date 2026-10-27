"""eis-graph pipeline CLI.

  sample           pull a random sample of OCR'd EISs from S3
  prepare-extract  build phase-1 requests (per-chunk extraction) + cost estimate
  extract-all      phase 1 over every EIS in S3 into data/full/, stopping before --budget dollars (Bedrock)
  run PHASE        run a prepared phase (extract | link). Bedrock (default): runs to completion, writing
                   results to data/work/PHASE/; rerun to retry failures. Anthropic: submits a Message Batch.
  wait PHASE       (anthropic backend) poll until the batch ends
  collect PHASE    (anthropic backend) download batch results into data/work/PHASE/
  prepare-link     build phase-2 requests (cross-doc entity resolution + doc-to-doc links)

  build            write data/graph/graph.trig + evidence.json
  serve            run the visualization server

--data full points prepare-link / run / build / serve at data/full/ (the extract-all output) instead of the
50-doc sample in data/. `run --budget USD` caps one Bedrock run.

Backend/model env vars: EISGRAPH_BACKEND=bedrock|anthropic, EISGRAPH_EXTRACT_MODEL, EISGRAPH_LINK_MODEL,
EISGRAPH_CONCURRENCY. Bedrock auth: AWS_BEARER_TOKEN_BEDROCK + AWS_REGION.
"""

import argparse
import json
from pathlib import Path

from . import batching, extract, extract_all, graph, link, sample

ROOT = Path(__file__).resolve().parent.parent / "data"
SAMPLE_WORK = ROOT / "work"
RAW, WORK, OUT = ROOT / "raw", SAMPLE_WORK, ROOT / "graph"
STATE = WORK / "batches.json"


def _use_data(name: str) -> None:
    global RAW, WORK, OUT, STATE
    base = ROOT if name == "sample" else ROOT / name
    RAW, WORK, OUT = base / "raw", base / "work", base / "graph"
    STATE = WORK / "batches.json"


def _complete_docs() -> set[str] | None:
    """Barcodes extract-all finished; None for the sample, whose manifest has no per-doc status."""
    docs = json.loads((RAW / "manifest.json").read_text())["docs"]
    if not any("status" in d for d in docs):
        return None
    return {d["barcode"] for d in docs if d.get("status") == "complete"}
EST_OUTPUT = {"extract": 6000, "link": 8000}  # per-request output token guesses (incl. thinking)


def _state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def _show_estimate(phase: str, reqs: list[dict]) -> None:
    est = batching.estimate(reqs, EST_OUTPUT[phase])
    print(json.dumps(est, indent=2))
    print(f"Review {WORK / f'{phase}_requests.jsonl'}, then: uv run eisgraph run {phase}")


def _show_summary(summary: dict) -> None:
    print(json.dumps({k: v for k, v in summary.items() if k != "failures"}, indent=2))
    print(f"{len(summary['failures'])} failed: {list(summary['failures'].items())[:10]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", choices=["sample", "full"], default="sample", help="which data set to work on")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--n", type=int, default=50)
    s.add_argument("--seed", type=int, default=1274)
    s.add_argument("--project", action="append", default=None, help="project id(s); default p1274")
    sub.add_parser("prepare-extract")
    ea = sub.add_parser("extract-all")
    ea.add_argument("--budget", type=float, required=True, help="hard cap in USD at Bedrock rates, across reruns")
    ea.add_argument("--seed", type=int, default=1274)
    ea.add_argument("--project", action="append", default=None, help="project id(s); default p1274")
    sub.add_parser("prepare-link")
    for name in ("run", "wait", "collect"):
        sp = sub.add_parser(name)
        sp.add_argument("phase", choices=["extract", "link"])
        if name == "run":
            sp.add_argument("--budget", type=float, default=None, help="USD cap for this Bedrock run")
    sub.add_parser("build")
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    _use_data(a.data)

    WORK.mkdir(parents=True, exist_ok=True)
    if a.cmd == "sample":
        sample.sample(RAW, a.n, a.seed, a.project or ["p1274"])
    elif a.cmd == "prepare-extract":
        _show_estimate("extract", extract.build(RAW, WORK))
    elif a.cmd == "extract-all":
        print(json.dumps(extract_all.run(ROOT / "full", SAMPLE_WORK, a.budget, a.project or ["p1274"], a.seed), indent=2))
    elif a.cmd == "prepare-link":
        _show_estimate("link", link.build(WORK, _complete_docs()))
    elif a.cmd == "run":
        reqs = batching.read_requests(WORK / f"{a.phase}_requests.jsonl")
        if batching.BACKEND == "bedrock":
            _show_summary(batching.run_bedrock(reqs, WORK / a.phase, a.budget))
            return
        st = _state()
        if a.phase in st:
            raise SystemExit(f"{a.phase} already submitted as {st[a.phase]}; delete it from {STATE} to resubmit")
        st[a.phase] = batching.submit_batch(reqs)
        STATE.write_text(json.dumps(st, indent=2))
        print(f"submitted {a.phase}: {st[a.phase]}; next: uv run eisgraph wait {a.phase}")
    elif a.cmd in ("wait", "collect") and batching.BACKEND != "anthropic":
        raise SystemExit("wait/collect are for EISGRAPH_BACKEND=anthropic; on Bedrock `run` already wrote results")
    elif a.cmd == "wait":
        batching.wait_batch(_state()[a.phase])
    elif a.cmd == "collect":
        model = batching.read_requests(WORK / f"{a.phase}_requests.jsonl")[0]["params"]["model"]
        _show_summary(batching.collect_batch(_state()[a.phase], WORK / a.phase, model))
    elif a.cmd == "build":
        graph.build(RAW, WORK, OUT)
    elif a.cmd == "serve":
        from . import server
        server.serve(OUT, a.port)


if __name__ == "__main__":
    main()
