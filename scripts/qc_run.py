#!/usr/bin/env python3
"""Run quantconnect/main.py as a QuantConnect cloud backtest, end to end.

    QC_USER_ID=... QC_API_TOKEN=... python scripts/qc_run.py --variant 4 --start 1999

Creates (or reuses) a QuantConnect project, uploads the algorithm with the
chosen parameters written into it, compiles it, runs a cloud backtest,
waits for it, and writes the statistics to reports/qc-v<variant>-<start>.txt.

The credentials come from the environment and are never printed or
written anywhere. QuantConnect's API authenticates each request with
the SHA-256 of "<token>:<unix time>", so the token itself is never sent.
Nothing here can trade: it only runs backtests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

API = "https://www.quantconnect.com/api/v2"
ALGORITHM = Path(__file__).resolve().parent.parent / "quantconnect" / "main.py"
PROJECT_NAME = "GCFP factor strategy"


class QCError(RuntimeError):
    pass


def _credentials() -> tuple[str, str]:
    user, token = os.environ.get("QC_USER_ID"), os.environ.get("QC_API_TOKEN")
    if not user or not token:
        raise SystemExit(
            "QC_USER_ID and QC_API_TOKEN must be set (QuantConnect → Account → "
            "API Access). They are read from the environment only."
        )
    return user, token


def call(endpoint: str, payload: dict | None = None) -> dict:
    import requests

    user, token = _credentials()
    stamp = str(int(time.time()))
    digest = hashlib.sha256(f"{token}:{stamp}".encode()).hexdigest()
    resp = requests.post(f"{API}/{endpoint}", json=payload or {}, auth=(user, digest),
                         headers={"Timestamp": stamp}, timeout=120)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success", False):
        raise QCError(f"{endpoint}: {body.get('errors') or body}")
    return body


def project_id(name: str) -> int:
    for project in call("projects/read").get("projects", []):
        if project.get("name") == name:
            return project["projectId"]
    return call("projects/create", {"name": name, "language": "Py"})["projects"][0]["projectId"]


def source_with(variant: int, start: int, end: int) -> str:
    """The algorithm with its defaults set to this run's parameters, so the
    run does not depend on project parameters being set in the web UI."""
    text = ALGORITHM.read_text()
    for name, value in (("variant", variant), ("start", start), ("end", end)):
        old = f'self.get_parameter("{name}") or '
        i = text.find(old)
        if i < 0:
            raise QCError(f"parameter {name} not found in {ALGORITHM}")
        j = text.find(")", i + len(old))
        text = text[:i + len(old)] + str(value) + text[j:]
    return text


def upload(pid: int, content: str) -> None:
    try:
        call("files/update", {"projectId": pid, "name": "main.py", "content": content})
    except QCError:
        call("files/create", {"projectId": pid, "name": "main.py", "content": content})


def compile_project(pid: int) -> str:
    cid = call("compile/create", {"projectId": pid})["compileId"]
    for _ in range(120):
        body = call("compile/read", {"projectId": pid, "compileId": cid})
        state = body.get("state")
        if state == "BuildSuccess":
            return cid
        if state == "BuildError":
            raise QCError("compile failed:\n" + "\n".join(body.get("logs", [])))
        time.sleep(5)
    raise QCError("compile did not finish in 10 minutes")


def run_backtest(pid: int, cid: str, name: str, poll_s: int = 30,
                 max_hours: float = 6.0) -> dict:
    bt = call("backtests/create", {"projectId": pid, "compileId": cid,
                                   "backtestName": name})["backtest"]
    bid = bt["backtestId"]
    print(f"backtest {bid} started", file=sys.stderr, flush=True)
    deadline = time.time() + max_hours * 3600
    while time.time() < deadline:
        body = call("backtests/read", {"projectId": pid, "backtestId": bid})["backtest"]
        if body.get("error") or body.get("stacktrace"):
            raise QCError(f"backtest error: {body.get('error')}\n{body.get('stacktrace')}")
        if body.get("completed") or body.get("status") == "Completed.":
            return body
        progress = body.get("progress")
        if progress is not None:
            print(f"  progress {float(progress):.0%}", file=sys.stderr, flush=True)
        time.sleep(poll_s)
    raise QCError(f"backtest {bid} still running after {max_hours} h")


def report(body: dict, variant: int, start: int, end: int) -> str:
    stats = body.get("statistics") or {}
    runtime = body.get("runtimeStatistics") or {}
    lines = [
        "=" * 78,
        f"QUANTCONNECT REPLICATION — factor variant {variant}, {start}-{end}",
        "=" * 78,
        "Data: QuantConnect US equities (dead companies included) with",
        "Morningstar fundamentals. Benchmark: SPY. Differences from our own",
        "engine are listed at the top of quantconnect/main.py.",
        f"backtest: {body.get('name')} ({body.get('backtestId')})",
        "",
        "STATISTICS",
        *[f"  {k}: {v}" for k, v in stats.items()],
        "",
        "RUNTIME",
        *[f"  {k}: {v}" for k, v in runtime.items()],
        "=" * 78,
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--variant", type=int, choices=(2, 4), default=4)
    parser.add_argument("--start", type=int, default=2015)
    parser.add_argument("--end", type=int, default=2025)
    parser.add_argument("--project", default=PROJECT_NAME)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--check", action="store_true",
                        help="only check the credentials work, then stop")
    args = parser.parse_args(argv)

    if args.check:
        call("authenticate")
        print("QuantConnect credentials OK")
        return 0
    pid = project_id(args.project)
    upload(pid, source_with(args.variant, args.start, args.end))
    cid = compile_project(pid)
    name = f"v{args.variant} {args.start}-{args.end} {time.strftime('%Y-%m-%d %H:%M')}"
    body = run_backtest(pid, cid, name)
    text = report(body, args.variant, args.start, args.end)
    out = args.out or Path(f"reports/qc-v{args.variant}-{args.start}.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    (out.with_suffix(".json")).write_text(json.dumps(
        {k: body.get(k) for k in ("statistics", "runtimeStatistics", "name", "backtestId")},
        indent=2))
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
