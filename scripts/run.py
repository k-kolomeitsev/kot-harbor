#!/usr/bin/env python3
"""Run the exact KOT configuration submitted to FrontierHarness Eval PR #9."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KOT_BINARY = ROOT / "kot_harbor" / "kot"
EXPECTED_KOT_SHA256 = "ac992437515974b8dae5ac96ef78fc5d584d3bbf28d72dac1163e2d1ce2d558e"
MODEL_PROVIDER = "moonshotai"
MODEL_ID = "k3"
API_HOST = "api.kimi.com"
FAMILIES = (
    ("terminal-bench", ROOT / "tasks" / "terminal-bench"),
    ("deep-swe", ROOT / "tasks" / "deep-swe"),
)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def resolve_harbor(explicit: str | None) -> str:
    candidate = explicit or shutil.which("harbor")
    if not candidate:
        raise SystemExit("harbor 0.22.0 is not on PATH; activate the repository virtual environment")
    version = subprocess.run(
        [candidate, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if version != "0.22.0":
        raise SystemExit(f"expected Harbor 0.22.0, found {version!r}")
    return candidate


def build_local_image() -> None:
    subprocess.run(
        [
            "docker", "build",
            "-t", "kot-frontier/build-cython-ext:20251031",
            "-f", str(ROOT / "build" / "Dockerfile.bundle"),
            str(ROOT / "build"),
        ],
        check=True,
    )


def run_family(harbor: str, run_root: Path, family: str, task_dir: Path, api_key: str) -> str:
    homes = run_root / "kot-homes"
    homes.mkdir(parents=True, exist_ok=True)
    mounts = json.dumps([{"type": "bind", "source": str(homes), "target": "/mnt/kot-homes"}])
    command = [
        harbor,
        "run",
        "-p", str(task_dir),
        "--agent", "kot_harbor.kot_agent:KotAgent",
        "-m", f"{MODEL_PROVIDER}/{MODEL_ID}",
        "-k", "1",
        "--n-concurrent", "1",
        "--environment-build-timeout-multiplier", "2",
        "--mounts-json", mounts,
        "--allow-agent-host", API_HOST,
        "--ae", f"KOT_BENCH_PROVIDER={MODEL_PROVIDER}",
        "--ae", f"KOT_BENCH_MODEL={MODEL_ID}",
        "--ae", f"MOONSHOT_API_KEY={api_key}",
    ]
    print(f"\n===== {family}: Harbor 0.22.0, {MODEL_PROVIDER}/{MODEL_ID}, k=1, n_concurrent=1 =====\n", flush=True)
    env = dict(os.environ)
    current_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + current_pythonpath if current_pythonpath else "")
    process = subprocess.Popen(
        command,
        cwd=run_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    return_code = process.wait()
    output = "".join(lines)
    matches = re.findall(r"Results written to (jobs[\\/][^\s]+)[\\/]result\.json", output)
    job = matches[-1].replace("\\", "/").split("/")[-1] if matches else ""
    if return_code != 0 or not job:
        raise SystemExit(f"{family} run failed: exit={return_code}, job={job!r}")
    return job


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harbor-bin", help="Path to the Harbor 0.22.0 executable")
    parser.add_argument(
        "--family",
        choices=("all", "terminal-bench", "deep-swe"),
        default="all",
        help="Run both task families, or one family only",
    )
    parser.add_argument(
        "--task",
        help="Run one task by id, for example terminal-bench/code-from-image",
    )
    args = parser.parse_args()
    if args.task and args.family != "all":
        parser.error("--task and --family cannot be combined")

    api_key = os.environ.get("MOONSHOT_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("MOONSHOT_API_KEY must be set in the environment")
    if file_sha256(KOT_BINARY) != EXPECTED_KOT_SHA256:
        raise SystemExit("kot_harbor/kot does not match the submitted executable")
    harbor = resolve_harbor(args.harbor_bin)
    subprocess.run(["docker", "version"], stdout=subprocess.DEVNULL, check=True)
    if not args.task or args.task == "terminal-bench/build-cython-ext":
        build_local_image()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = ROOT / "reproduction" / stamp
    run_root.mkdir(parents=True)
    if args.task:
        parts = args.task.split("/", 1)
        if len(parts) != 2 or parts[0] not in {"terminal-bench", "deep-swe"}:
            parser.error("--task must be terminal-bench/<name> or deep-swe/<name>")
        task_dir = ROOT / "tasks" / parts[0] / parts[1]
        if not task_dir.is_dir():
            parser.error(f"unknown task: {args.task}")
        selected = ((args.task, task_dir),)
    else:
        selected = FAMILIES if args.family == "all" else tuple(row for row in FAMILIES if row[0] == args.family)
    ledger: list[dict] = []
    for family, task_dir in selected:
        job = run_family(harbor, run_root, family, task_dir, api_key)
        ledger.append({
            "family": family,
            "job": job,
            "provider": MODEL_PROVIDER,
            "model": MODEL_ID,
            "effort_override": None,
            "effective_effort": "max",
            "k": 1,
            "n_concurrent": 1,
        })
        (run_root / "runs.json").write_text(
            json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "runs": ledger}, indent=2) + "\n",
            encoding="utf-8",
        )

    print(f"\nReproduction data: {run_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
