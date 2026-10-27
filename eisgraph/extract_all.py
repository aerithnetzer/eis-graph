"""Phase 1 over every EIS in S3, document by document, under a hard dollar budget (Bedrock only).

Writes to data/full/{raw,work}/ so the 50-doc sample in data/raw + data/work stays as it is; chunks the sample
already extracted are copied over instead of re-run. Documents go in a fixed shuffled order (seed 1274), so a run
that stops on budget is still a random sample, and a rerun resumes where it stopped.

Budget: spend is tracked from each response's usage at the backend's billed rate (batching.prices) in work/extract/_ledger.json,
cumulatively across runs. Two guards keep the total under the cap:
  - a document starts only if spent + expected cost of in-flight docs + its own expected cost fits;
  - a request is sent only if spent + worst-case cost of every in-flight request + its own worst case fits
    (worst case = all input billed as a cache write, all max_tokens used), so the cap holds even if every
    in-flight request comes back maximal.
"""

import asyncio
import json
import random
import shutil
from pathlib import Path

from . import batching, sample
from .extract import doc_requests
from .render import render_doc

AVG_OUTPUT = 6200  # observed mean output tokens per extraction request in the 50-doc sample
PREFETCH = 4  # documents downloaded ahead of the extraction front


def _in_tokens(r: dict, chars_per_token: float) -> float:
    return (len(json.dumps(r["params"]["system"])) + len(json.dumps(r["params"]["messages"]))) / chars_per_token


def expected_cost(r: dict) -> float:
    p_in, p_out, _ = batching.prices(r["params"]["model"])
    return (_in_tokens(r, 3.5) * p_in + AVG_OUTPUT * p_out) / 1e6


class Ledger:
    def __init__(self, path: Path, budget: float):
        self.path, self.budget = path, budget
        saved = json.loads(path.read_text()) if path.exists() else {}
        self.usage = saved.get("usage", {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
        self.failures: dict[str, str] = saved.get("failures", {})
        self.pending = 0.0  # expected cost of requests in started docs that haven't finished
        self.reserved = 0.0  # worst-case cost of requests in flight
        self.stopped = False

    def spent(self, model: str) -> float:
        return batching._cost(self.usage, model)

    def save(self, model: str) -> None:
        self.path.write_text(json.dumps({"budget_usd": self.budget, "usage": self.usage,
                                         "cost_usd": round(self.spent(model), 4),
                                         "failures": self.failures}, indent=2))


def _job_order(work: Path, projects: list[str], seed: int) -> list[tuple[str, str]]:
    path = work / "order.json"
    if path.exists():
        return [tuple(j) for j in json.loads(path.read_text())]
    jobs = sample.list_jobs(projects)
    random.Random(seed).shuffle(jobs)
    path.write_text(json.dumps(jobs))
    print(f"found {len(jobs)} jobs across {len({p for p, _ in jobs})} project(s)")
    return jobs


def _seed_from_sample(sample_work: Path, out: Path, index: dict) -> None:
    """Reuse the 50-doc sample's finished chunk results (same custom_ids, same requests)."""
    sidx = sample_work / "extract_index.json"
    if not sidx.exists():
        return
    for cid, meta in json.loads(sidx.read_text()).items():
        src = sample_work / "extract" / f"{cid}.json"
        if src.exists() and not (out / f"{cid}.json").exists():
            shutil.copy(src, out / f"{cid}.json")
        index.setdefault(cid, meta)


def _download(raw: Path, project: str, barcode: str) -> Path | None:
    dest = raw / barcode
    if not (dest / "json").exists():
        src = f"s3://{sample.BUCKET}/{sample.PREFIX}{project}/{barcode}/json/"
        sample._aws("s3", "sync", src, str(dest / "json"), "--only-show-errors")
    return dest if any((dest / "json").glob("*.json")) else None


def run(root: Path, sample_work: Path, budget: float, projects: list[str], seed: int) -> dict:
    return asyncio.run(_run(root, sample_work, budget, projects, seed))


async def _run(root: Path, sample_work: Path, budget: float, projects: list[str], seed: int) -> dict:
    if batching.BACKEND != "bedrock":
        raise SystemExit("extract-all runs on Bedrock only (the budget guard needs real-time usage)")
    raw, work = root / "raw", root / "work"
    out = work / "extract"
    out.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)
    model = batching.MODELS["extract"]
    ledger = Ledger(out / "_ledger.json", budget)
    index_path, manifest_path = work / "extract_index.json", raw / "manifest.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"seed": seed, "docs": []}
    status = {d["barcode"]: d for d in manifest["docs"]}
    _seed_from_sample(sample_work, out, index)
    jobs = _job_order(work, projects, seed)
    print(f"budget ${budget:.2f}, already spent ${ledger.spent(model):.2f}; {len(jobs)} jobs in order")

    client = batching.bedrock_client(model)
    sem = asyncio.Semaphore(batching.CONCURRENCY)
    docs_in_flight = asyncio.Semaphore(batching.CONCURRENCY)
    tasks: set[asyncio.Task] = set()

    def checkpoint() -> None:
        ledger.save(model)
        index_path.write_text(json.dumps(index, indent=1))
        manifest["docs"] = list(status.values())
        manifest_path.write_text(json.dumps(manifest, indent=2))

    async def one(r: dict, exp: float) -> bool:
        async with sem:
            worst = batching.worst_cost(r)
            if ledger.stopped or ledger.spent(model) + ledger.reserved + worst > budget:
                ledger.stopped = True
                ledger.pending -= exp
                return False
            ledger.reserved += worst
            try:
                await batching.call_bedrock(client, r, out, ledger.usage, ledger.failures)
            finally:
                ledger.reserved -= worst
                ledger.pending -= exp
        if (out / f"{r['custom_id']}.json").exists():
            ledger.failures.pop(r["custom_id"], None)
        return True

    async def doc(project: str, barcode: str, reqs: list[dict], exps: list[float]) -> None:
        try:
            await asyncio.gather(*(one(r, e) for r, e in zip(reqs, exps)))
            done = all((out / f"{cid}.json").exists() for cid, m in index.items() if m["barcode"] == barcode)
            status[barcode] = {"project": project, "barcode": barcode,
                               "s3": f"s3://{sample.BUCKET}/{sample.PREFIX}{project}/{barcode}/",
                               "status": "complete" if done else "partial"}
            checkpoint()
            print(f"  {barcode}: {status[barcode]['status']}  spent ${ledger.spent(model):.2f}/${budget:.2f}  "
                  f"docs={sum(d['status'] == 'complete' for d in status.values())}  "
                  f"failures={len(ledger.failures)}", flush=True)
        finally:
            docs_in_flight.release()

    queue: asyncio.Queue = asyncio.Queue(PREFETCH)

    async def producer() -> None:
        try:
            for project, barcode in jobs:
                if ledger.stopped:
                    break
                if status.get(barcode, {}).get("status") in ("complete", "no_json"):
                    continue
                path = await asyncio.to_thread(_download, raw, project, barcode)
                await queue.put((project, barcode, path))
        except Exception as e:  # e.g. an expired `aws login` session: stop cleanly instead of hanging the loop
            print(f"download failed, stopping: {e}", flush=True)
            ledger.stopped = True
        finally:
            await queue.put(None)

    prod = asyncio.create_task(producer())
    while (item := await queue.get()) is not None:
        project, barcode, path = item
        if path is None:
            status[barcode] = {"project": project, "barcode": barcode, "status": "no_json"}
            continue
        try:
            reqs, idx = doc_requests(barcode, render_doc(path))
        except Exception as e:  # one unreadable document shouldn't end the run
            print(f"  {barcode}: unreadable ({type(e).__name__}: {e}); skipping", flush=True)
            status[barcode] = {"project": project, "barcode": barcode, "status": "unreadable"}
            continue
        index |= idx
        todo = [r for r in reqs if not (out / f"{r['custom_id']}.json").exists()]
        exps = [expected_cost(r) for r in todo]
        await docs_in_flight.acquire()
        if ledger.stopped or ledger.spent(model) + ledger.pending + sum(exps) > budget:
            docs_in_flight.release()
            ledger.stopped = True
            print(f"budget: not starting {barcode} (needs ~${sum(exps):.2f}); stopping")
            break
        ledger.pending += sum(exps)
        t = asyncio.create_task(doc(project, barcode, todo, exps))
        tasks.add(t)
        t.add_done_callback(tasks.discard)
    ledger.stopped = True
    prod.cancel()
    await asyncio.gather(*tasks)
    checkpoint()
    counts: dict[str, int] = {}
    for d in status.values():
        counts[d["status"]] = counts.get(d["status"], 0) + 1
    return {"budget_usd": budget, "cost_usd": round(ledger.spent(model), 2),
            "usage": ledger.usage, "docs": counts, "failures": len(ledger.failures)}
