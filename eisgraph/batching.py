"""Model calls for both phases, on one of two backends.

  bedrock    (default) Amazon Bedrock `bedrock-runtime`, authenticated with a Bedrock API key
             (AWS_BEARER_TOKEN_BEDROCK; region from AWS_REGION, default us-east-2). Models are called through
             cross-region inference profiles (`us.anthropic.<model>`): this account's Mantle endpoint knows no
             models and on-demand model ids are rejected. Bedrock has no Message Batches API, so requests run as
             concurrent real-time calls; results are written as they finish and a rerun skips finished items.
  anthropic  Anthropic Message Batches API (ANTHROPIC_API_KEY), 50% batch discount, asynchronous.

Requests are stored with first-party model ids; the Bedrock runner adds the inference-profile prefix.
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

import anthropic

BACKEND = os.environ.get("EISGRAPH_BACKEND", "bedrock")
MODELS = {
    "extract": os.environ.get("EISGRAPH_EXTRACT_MODEL", "claude-haiku-5-5"),
    "link": os.environ.get("EISGRAPH_LINK_MODEL", "claude-opus-5-5"),
}
CONCURRENCY = int(os.environ.get("EISGRAPH_CONCURRENCY", "8"))
BEDROCK_PREFIX = os.environ.get("EISGRAPH_BEDROCK_PREFIX", "us.anthropic.")
BEDROCK_REGION = os.environ.get("AWS_REGION", "us-east-2")

# Anthropic list prices, $/MTok (input, output, cache read). Bedrock bills its own (similar) rates.
# Haiku 5.5's rate applies to prompts <= 100K tokens, which every request here is (~25K).
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
}
BATCH_DISCOUNT = 0.5 if BACKEND == "anthropic" else 1.0
# Bedrock's regional cross-region profiles (`us.anthropic.*`) bill 10% over list; `global.*` bills list.
# Checked for Haiku 5.5 in us-east-2 via the AWS Pricing API (2026-10-26): $0.11 in / $0.55 out / $0.011 cache read.
REGION_PREMIUM = 1.1 if BACKEND == "bedrock" and not BEDROCK_PREFIX.startswith("global.") else 1.0


def prices(model: str) -> tuple[float, float, float]:
    """$/MTok (input, output, cache read) as billed on the active backend, before any batch discount."""
    return tuple(x * REGION_PREMIUM for x in PRICES[model])

# Refusal fallback for Bedrock, done by hand: server-side `fallbacks` isn't offered there, and the SDK's
# BetaRefusalFallbackMiddleware crashes on Bedrock streams (it decodes the binary event stream as UTF-8 SSE).
# A refused request is re-sent once to the fallback model. Haiku 5.5 has none; its refusals are recorded failures.
BEDROCK_FALLBACKS = {
    "claude-opus-5-5": "claude-opus-4-8",
    "claude-sonnet-5-5": "claude-opus-4-8",
}


def safe_id(s: str) -> str:
    """custom_id must match ^[a-zA-Z0-9_-]{1,64}$."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", s)[:64]


def structured_request(custom_id: str, model: str, system: str, content: list[dict], schema: dict,
                       max_tokens: int = 32000, effort: str = "medium") -> dict:
    return {
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
            "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        },
    }


def _cost(usage: dict, model: str) -> float:
    p_in, p_out, p_cache = prices(model)
    return (usage["input"] * p_in + usage["output"] * p_out + usage["cache_read"] * p_cache
            + usage["cache_write"] * p_in * 1.25) / 1e6 * BATCH_DISCOUNT


def worst_cost(r: dict) -> float:
    """Upper bound for one request: all input billed as a cache write (at a pessimistic 2.5 chars/token) and
    every max_tokens used."""
    p_in, p_out, _ = prices(r["params"]["model"])
    in_tok = (len(json.dumps(r["params"]["system"])) + len(json.dumps(r["params"]["messages"]))) / 2.5
    return (in_tok * p_in * 1.25 + r["params"]["max_tokens"] * p_out) / 1e6


def estimate(requests: list[dict], est_output_per_request: int) -> dict:
    """Rough pre-flight cost estimate (chars/3.5 ≈ tokens for English OCR text), ignoring cache hits."""
    in_tok = sum(len(json.dumps(r["params"]["system"])) + len(json.dumps(r["params"]["messages"]))
                 for r in requests) / 3.5
    out_tok = est_output_per_request * len(requests)
    model = requests[0]["params"]["model"]
    usage = {"input": in_tok, "output": out_tok, "cache_read": 0, "cache_write": 0}
    return {"backend": BACKEND, "model": model, "requests": len(requests), "input_tokens_est": int(in_tok),
            "output_tokens_est": out_tok, "cost_usd_est": round(_cost(usage, model), 2)}


def write_requests(path: Path, requests: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in requests:
            f.write(json.dumps(r) + "\n")


def read_requests(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _add_usage(usage: dict, msg) -> None:
    u = msg.usage
    usage["input"] += u.input_tokens
    usage["output"] += u.output_tokens
    usage["cache_read"] += u.cache_read_input_tokens or 0
    usage["cache_write"] += u.cache_creation_input_tokens or 0


def _record(custom_id: str, msg, out_dir: Path, usage: dict, failures: dict) -> None:
    """Accumulate usage and write the parsed structured output, or record why there is none."""
    _add_usage(usage, msg)
    if msg.stop_reason != "end_turn":  # refusal (after any fallback) or max_tokens truncation
        failures[custom_id] = f"stop_reason={msg.stop_reason}"
        return
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        (out_dir / f"{custom_id}.json").write_text(json.dumps(json.loads(text), indent=1))
    except json.JSONDecodeError:
        failures[custom_id] = "invalid JSON"


def _summarize(out_dir: Path, model: str, usage: dict, failures: dict, **extra) -> dict:
    summary = {**extra, "backend": BACKEND, "model": model, "usage": usage,
               "cost_usd": round(_cost(usage, model), 2), "failures": failures}
    (out_dir / "_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


# ---------------------------------------------------------------- Bedrock (real-time, concurrent)

def run_bedrock(requests: list[dict], out_dir: Path, budget: float | None = None) -> dict:
    return asyncio.run(_run_bedrock(requests, out_dir, budget))


def bedrock_client(model: str) -> anthropic.AsyncAnthropicBedrock:
    # Picks up AWS_BEARER_TOKEN_BEDROCK from the environment.
    # A healthy stream sends events (at least pings) every few seconds; a read gap of 3 minutes means the
    # connection is dead (e.g. after a network drop) and the request should be retried, not waited on for hours.
    return anthropic.AsyncAnthropicBedrock(aws_region=BEDROCK_REGION, max_retries=8,
                                           timeout=anthropic.Timeout(600.0, connect=15.0, read=180.0))


async def call_bedrock(client: anthropic.AsyncAnthropicBedrock, r: dict, out_dir: Path, usage: dict,
                       failures: dict) -> None:
    """Run one stored request, recording its usage and output (or failure). Usage of a fallback attempt is
    counted at the primary model's price."""
    model = r["params"]["model"]
    try:
        for m in (model, BEDROCK_FALLBACKS.get(model)):
            # Streamed because max_tokens is large (the SDK refuses long non-streaming calls).
            async with client.beta.messages.stream(**{**r["params"], "model": f"{BEDROCK_PREFIX}{m}"}) as stream:
                msg = await stream.get_final_message()
            if msg.stop_reason != "refusal" or BEDROCK_FALLBACKS.get(model) in (None, m):
                break
            _add_usage(usage, msg)
        _record(r["custom_id"], msg, out_dir, usage, failures)
    except anthropic.APIStatusError as e:  # non-retryable, or retries exhausted
        failures[r["custom_id"]] = f"HTTP {e.status_code}: {e.message[:200]}"
    except anthropic.APIConnectionError as e:
        failures[r["custom_id"]] = f"connection: {e}"
    except Exception as e:  # anything else (e.g. a malformed stream) fails this item, not the whole run
        failures[r["custom_id"]] = f"{type(e).__name__}: {str(e)[:200]}"


async def _run_bedrock(requests: list[dict], out_dir: Path, budget: float | None = None) -> dict:
    """budget: USD cap for this run; a request is sent only if spend so far plus the worst case of everything
    in flight still fits, so the cap holds however the in-flight requests come back."""
    out_dir.mkdir(parents=True, exist_ok=True)
    model = requests[0]["params"]["model"]
    client = bedrock_client(model)
    todo = [r for r in requests if not (out_dir / f"{r['custom_id']}.json").exists()]
    print(f"{len(requests) - len(todo)} already done, {len(todo)} to run on Bedrock ({CONCURRENCY} at a time)")
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    failures: dict[str, str] = {}
    sem = asyncio.Semaphore(CONCURRENCY)
    done = 0
    reserved = 0.0
    skipped = 0

    async def one(r: dict) -> None:
        nonlocal done, reserved, skipped
        async with sem:
            worst = worst_cost(r)
            if budget is not None and _cost(usage, model) + reserved + worst > budget:
                skipped += 1
                return
            reserved += worst
            try:
                await call_bedrock(client, r, out_dir, usage, failures)
            finally:
                reserved -= worst
        done += 1
        if done % 10 == 0 or done == len(todo):
            print(f"  {done}/{len(todo)}  failures={len(failures)}  "
                  f"~${_cost(usage, r['params']['model']):.2f}", flush=True)

    await asyncio.gather(*(one(r) for r in todo))
    if skipped:
        print(f"budget ${budget:.2f} reached: {skipped} requests not sent; rerun with a higher --budget to continue")
    return _summarize(out_dir, model, usage, failures, **({"budget_usd": budget, "not_sent": skipped} if budget else {}))


# ---------------------------------------------------------------- Anthropic Message Batches

def submit_batch(requests: list[dict]) -> str:
    # 256 MB / 100k request cap per batch; our sample is far below either.
    return anthropic.Anthropic().messages.batches.create(requests=requests).id


def wait_batch(batch_id: str, poll_s: int = 60) -> None:
    client = anthropic.Anthropic()
    while True:
        b = client.messages.batches.retrieve(batch_id)
        c = b.request_counts
        print(f"{batch_id}: {b.processing_status} processing={c.processing} succeeded={c.succeeded} "
              f"errored={c.errored} expired={c.expired}", flush=True)
        if b.processing_status == "ended":
            return
        time.sleep(poll_s)


def collect_batch(batch_id: str, out_dir: Path, model: str) -> dict:
    """Write each succeeded result's parsed JSON to out_dir/<custom_id>.json. Returns usage + failures."""
    out_dir.mkdir(parents=True, exist_ok=True)
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    failures: dict[str, str] = {}
    for r in anthropic.Anthropic().messages.batches.results(batch_id):
        res = r.result
        if res.type == "succeeded":
            _record(r.custom_id, res.message, out_dir, usage, failures)
        else:
            failures[r.custom_id] = res.type if res.type != "errored" else f"errored: {res.error.error.type}"
    return _summarize(out_dir, model, usage, failures, batch_id=batch_id)
