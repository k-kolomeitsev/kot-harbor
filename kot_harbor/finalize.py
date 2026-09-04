#!/usr/bin/env python3
"""Best-effort finalizer for the KOT Harbor runner (FrontierHarness Eval).

Used when the main runner died mid-turn (external timeout) but `kot web` is
still alive: flush the virtual-file overlay (the verifier reads the disk),
snapshot usage (the trial's cost record), close the session. Always exits 0 —
the runner's own exit code decides the trial; this only preserves data.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
from http.client import HTTPConnection
from pathlib import Path

BASE_HOST = os.environ.get("KOT_BASE_HOST", "127.0.0.1:18080")
LOG_DIR = Path(os.environ.get("KOT_LOG_DIR", "/logs/agent"))


def _http_json(method: str, path: str, payload: dict | None = None, timeout: float = 10.0) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    conn = HTTPConnection(BASE_HOST, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
            return {}
        return json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except (OSError, json.JSONDecodeError):
        return {}
    finally:
        conn.close()


def main() -> int:
    try:
        session = json.loads((LOG_DIR / "session.json").read_text(encoding="utf-8"))
        session_id = str(session.get("session_id") or "").strip()
    except (OSError, json.JSONDecodeError):
        session_id = ""
    if not session_id:
        return 0

    if not _http_json("GET", "/api/info", timeout=3.0):
        return 0

    sync_report = _http_json("POST", "/api/sync", {"session_id": session_id}, timeout=20.0)
    if sync_report:
        try:
            (LOG_DIR / "sync.json").write_text(json.dumps(sync_report, indent=2), encoding="utf-8")
        except OSError:
            pass

    usage = _http_json("GET", "/api/usage?" + urllib.parse.urlencode({"session_id": session_id}), timeout=10.0)
    if usage:
        try:
            tmp = (LOG_DIR / "usage.json").with_suffix(".json.tmp")
            tmp.write_text(json.dumps(usage, indent=2), encoding="utf-8")
            os.replace(tmp, LOG_DIR / "usage.json")
        except OSError:
            pass

    _http_json("POST", "/api/session/close", {"session_id": session_id}, timeout=5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
