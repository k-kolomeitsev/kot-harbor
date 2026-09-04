#!/usr/bin/env python3
"""Validate the submitted FrontierHarness data from raw jobs and KOT histories."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_BINARY_SHA256 = "ac992437515974b8dae5ac96ef78fc5d584d3bbf28d72dac1163e2d1ce2d558e"
EXPECTED_JOBS = {"2026-09-04__00-24-47", "2026-09-04__02-43-47"}
EXPECTED_TOTALS = {
    "input_tokens": 2_027_470,
    "cache_read_input_tokens": 144_773_846,
    "cache_creation_input_tokens": 0,
    "output_tokens": 1_037_745,
    "reasoning_tokens": 656_915,
    "turns": 1_369,
}
EXPECTED_TOTAL_COST = 65.0807388
KEY_PATTERN = re.compile(r"sk-kimi-[A-Za-z0-9_-]{16,}")


def load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def close(actual: float, expected: float, label: str) -> None:
    if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-9):
        raise AssertionError(f"{label}: {actual!r} != {expected!r}")


def normalize_instruction(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\n")


def transcript_identity(path: Path) -> tuple[str, str]:
    session_id = ""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            if not session_id:
                session_id = str(record.get("session_id") or "")
            if record.get("type") == "user" and not record.get("is_meta"):
                content = record.get("content")
                if isinstance(content, str):
                    return session_id, content
    raise AssertionError(f"no initial user message in {path}")


def duration_seconds(result: dict) -> float:
    execution = result["agent_execution"]
    start = datetime.fromisoformat(execution["started_at"].replace("Z", "+00:00"))
    finish = datetime.fromisoformat(execution["finished_at"].replace("Z", "+00:00"))
    return (finish - start).total_seconds()


def verify_manifest() -> None:
    manifest = ROOT / "MANIFEST.sha256"
    if not manifest.exists():
        raise AssertionError("MANIFEST.sha256 is missing")
    listed: set[str] = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        digest, relative = line.split("  ", 1)
        path = ROOT / relative
        if not path.is_file():
            raise AssertionError(f"manifest path is missing: {relative}")
        if sha256(path) != digest:
            raise AssertionError(f"manifest digest mismatch: {relative}")
        listed.add(relative)
    actual = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "worklog" not in path.parts
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and path.name != "MANIFEST.sha256"
        and "reproduction" not in path.parts
    }
    if listed != actual:
        raise AssertionError(
            f"manifest coverage mismatch: unlisted={sorted(actual - listed)} stale={sorted(listed - actual)}"
        )


def verify_no_provider_credentials() -> None:
    offenders: list[str] = []
    for base in (ROOT / "data" / "jobs", ROOT / "data" / "histories"):
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            data = path.read_bytes()
            if b"\x00" in data:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if KEY_PATTERN.search(text):
                offenders.append(path.relative_to(ROOT).as_posix())
    if offenders:
        raise AssertionError(f"provider credential value remains in {offenders}")


def main() -> None:
    provenance = load(ROOT / "data" / "provenance.json")
    raw = load(ROOT / "data" / "results" / "results-raw.json")
    normalized = load(ROOT / "data" / "results" / "eval-data-kot.json")
    submission = load(ROOT / "data" / "submission" / "eval-data.json")
    versions = load(ROOT / "data" / "submission" / "harness-versions.json")
    index = load(ROOT / "data" / "history-index.json")["histories"]
    redactions = load(ROOT / "data" / "redactions.json")

    assert provenance["model"] == {"provider": "moonshotai", "id": "k3", "display": "Kimi K3"}
    assert provenance["harbor"] == {"version": "0.22.0", "python": "3.12.10", "k": 1, "n_concurrent": 1}
    assert set(provenance["jobs"]) == EXPECTED_JOBS
    assert sha256(ROOT / "kot_harbor" / "kot") == EXPECTED_BINARY_SHA256
    assert provenance["kot"]["sha256"] == EXPECTED_BINARY_SHA256
    assert versions["harnesses"]["kot"]["executable_sha256"] == EXPECTED_BINARY_SHA256
    assert versions["harnesses"]["kot"]["version"] == "1.3.1"
    assert versions["harnesses"]["kot"]["source_revision"] == "96328165955504c601b67301d4be89bec5259b6b"

    terminal_tasks = sorted(path.name for path in (ROOT / "tasks" / "terminal-bench").iterdir() if path.is_dir())
    deep_swe_tasks = sorted(path.name for path in (ROOT / "tasks" / "deep-swe").iterdir() if path.is_dir())
    assert len(terminal_tasks) == 21
    assert len(deep_swe_tasks) == 9
    assert len(index) == 30
    assert len({row["task_id"] for row in index}) == 30
    assert len(raw["tasks"]) == 30
    assert not raw["pending_rerun"]

    harness = normalized["harnesses"][0]
    submitted_harness = next(row for row in submission["harnesses"] if row["name"] == "kot")
    assert harness == submitted_harness
    assert harness["successful"] == 23
    assert harness["completed"] == harness["expected"] == 30
    close(harness["pass_rate"], 23 / 30, "pass rate")
    close(harness["effective_cost_per_pass"], EXPECTED_TOTAL_COST / 23, "effective cost per pass")
    close(harness["median_cost_per_success_normalized"], 0.2775336, "median successful-cell cost")
    close(harness["cache_hit_rate_typical"], 0.9405882986747118, "median successful-cell cache rate")
    close(harness["median_duration_seconds"], 300.494982, "median successful-cell duration")
    assert harness["termination_anomalies"] == 2

    pricing = raw["pricing"]
    index_by_task = {row["task_id"]: row for row in index}
    totals = {key: 0 for key in EXPECTED_TOTALS}
    costs: list[float] = []
    successes = 0
    anomaly_tasks: set[str] = set()

    for task_id, item in raw["tasks"].items():
        assert item["status"] == "ok"
        canonical = item["canonical"]
        assert canonical["validity"] == "valid"
        assert canonical["job"] in EXPECTED_JOBS
        if canonical.get("anomaly"):
            anomaly_tasks.add(task_id)
        successes += int(bool(canonical["success"]))

        row = index_by_task[task_id]
        assert row["job"] == canonical["job"]
        assert row["trial"] == canonical["trial"]
        assert row["success"] == bool(canonical["success"])

        job_result_path = ROOT / "data" / "jobs" / row["job"] / row["trial"] / "result.json"
        result = load(job_result_path)
        assert result["task_name"] == task_id
        assert result["agent_info"]["name"] == "KOT"
        assert result["agent_info"]["version"] == "kot 1.3.1"
        assert result["agent_info"]["model_info"] == {"name": "k3", "provider": "moonshotai"}
        reward = result["verifier_result"]["rewards"].get("reward")
        assert (reward == 1) == bool(canonical["success"])

        metrics = item["metrics"]
        metadata = result["agent_result"]["metadata"]
        mapping = {
            "input_tokens": "uncached_input_tokens",
            "cache_read_input_tokens": "cache_read_input_tokens",
            "cache_creation_input_tokens": "cache_creation_input_tokens",
            "output_tokens": "output_tokens",
            "reasoning_tokens": "reasoning_tokens",
            "turns": "turns",
        }
        for metric_key, metadata_key in mapping.items():
            assert metrics[metric_key] == metadata[metadata_key], f"{task_id}: {metric_key}"
            totals[metric_key] += metrics[metric_key]
        close(metrics["duration_seconds"], duration_seconds(result), f"{task_id}: duration")

        usage = load(job_result_path.parent / "agent" / "usage.json")
        usage_total = usage["total"]
        assert usage_total["input_tokens"] == metrics["input_tokens"]
        assert usage_total["cache_read_input_tokens"] == metrics["cache_read_input_tokens"]
        assert usage_total["cache_creation_input_tokens"] == metrics["cache_creation_input_tokens"]
        assert usage_total["output_tokens"] == metrics["output_tokens"]
        assert usage_total["reasoning_tokens"] == metrics["reasoning_tokens"]
        assert usage["turns"] == metrics["turns"]

        expected_cost = (
            metrics["input_tokens"] * pricing["input_per_mtok"]
            + metrics["cache_read_input_tokens"] * pricing["cache_read_per_mtok"]
            + metrics["cache_creation_input_tokens"] * pricing["cache_creation_per_mtok"]
            + metrics["output_tokens"] * pricing["output_per_mtok"]
        ) / 1_000_000
        close(item["cost_first_cold_usd"], expected_cost, f"{task_id}: cost")
        costs.append(expected_cost)

        transcript = ROOT / row["transcript"]
        session_id, initial_user = transcript_identity(transcript)
        assert session_id == row["session_id"]
        instruction = (ROOT / row["instruction"]).read_text(encoding="utf-8")
        assert normalize_instruction(initial_user) == normalize_instruction(instruction)
        session_record = load(job_result_path.parent / "agent" / "session.json")
        assert session_record["session_id"] == session_id
        assert (ROOT / row["frozen_head"]).is_file()

    assert successes == 23
    assert totals == EXPECTED_TOTALS
    assert raw["token_totals"] == EXPECTED_TOTALS
    close(sum(costs), EXPECTED_TOTAL_COST, "total cost")
    close(raw["total_cost_usd"], EXPECTED_TOTAL_COST, "raw total cost")
    assert anomaly_tasks == {
        "terminal-bench/build-cython-ext",
        "datacurve/httpx-multipart-response-parsing",
    }
    assert redactions["total_replacements"] == 1
    assert len(redactions["files"]) == 1

    verify_no_provider_credentials()
    verify_manifest()

    print("PASS: FrontierHarness package is internally consistent")
    print("tasks=30 successes=23 jobs=2 histories=30")
    print(f"tokens={json.dumps(totals, sort_keys=True)}")
    print(f"total_cost_usd={sum(costs):.7f} effective_cost_per_pass_usd={sum(costs)/23:.7f}")


if __name__ == "__main__":
    main()
