"""Randomly sample EIS jobs from S3 and download their surya-2 per-page OCR JSON.

Layout: s3://impulse-data-prod/jobs/<PROJECT_ID>/<BARCODE>/{json,txt,uploaded_images}/
Uses the aws CLI (not boto3) so `aws login` sessions work without extra plumbing.
"""

import json
import os
import random
import subprocess
from pathlib import Path

BUCKET = "impulse-data-prod"
PREFIX = "jobs/"
ENV = {**os.environ, "AWS_CLI_AUTO_PROMPT": "off", "AWS_PAGER": ""}


def _aws(*args: str) -> str:
    out = subprocess.run(["aws", *args], env=ENV, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def list_prefixes(prefix: str) -> list[str]:
    """Immediate sub-prefixes ("directories") under prefix."""
    out = _aws("s3api", "list-objects-v2", "--bucket", BUCKET, "--prefix", prefix,
               "--delimiter", "/", "--query", "CommonPrefixes[].Prefix", "--output", "json")
    return json.loads(out) or []


def list_jobs(projects: list[str] | None) -> list[tuple[str, str]]:
    project_prefixes = [f"{PREFIX}{p}/" for p in projects] if projects else list_prefixes(PREFIX)
    jobs = []
    for pp in project_prefixes:
        project = pp[len(PREFIX):].strip("/")
        for bp in list_prefixes(pp):
            jobs.append((project, bp[len(pp):].strip("/")))
    return jobs


def has_json(project: str, barcode: str) -> bool:
    out = _aws("s3api", "list-objects-v2", "--bucket", BUCKET, "--prefix", f"{PREFIX}{project}/{barcode}/json/",
               "--max-items", "1", "--query", "Contents[].Key", "--output", "json")
    return bool(json.loads(out or "null"))


def sample(raw_dir: Path, n: int, seed: int, projects: list[str] | None) -> list[dict]:
    jobs = list_jobs(projects)
    print(f"found {len(jobs)} jobs across {len({p for p, _ in jobs})} project(s)")
    rng = random.Random(seed)
    rng.shuffle(jobs)
    chosen = []
    for project, barcode in jobs:
        if len(chosen) == n:
            break
        if not has_json(project, barcode):
            continue
        dest = raw_dir / barcode
        src = f"s3://{BUCKET}/{PREFIX}{project}/{barcode}/"
        _aws("s3", "sync", f"{src}json/", str(dest / "json"), "--only-show-errors")
        chosen.append({"project": project, "barcode": barcode, "s3": src})
        print(f"[{len(chosen)}/{n}] {project}/{barcode}")
    (raw_dir / "manifest.json").write_text(json.dumps({"seed": seed, "docs": chosen}, indent=2))
    return chosen
