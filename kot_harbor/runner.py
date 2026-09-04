#!/usr/bin/env python3
"""KOT per-task runner (Harbor installed-agent half, FrontierHarness Eval).

Runs INSIDE the task container: installs the uploaded OAuth credential into a
fresh config home, starts `kot web` on localhost, creates one session on the
task workspace, submits the task instruction, and waits until the turn is
REALLY over — terminal `Completed` + authoritative `Idle` + no live background
work + a quiet window. Flushes the virtual-file overlay to the physical
workspace (the verifier reads the disk), snapshots token usage, closes.

Usage is ALSO checkpointed to /logs/agent/usage.json every few seconds while
the turn runs: an external timeout can SIGKILL this runner with no signal
path, and the trial must still carry honest tokens/turns. A `KOT_TERMINAL`
JSON line on stdout carries the terminal reason (+ last error detail) for
outcome classification.

Exit codes: 0 = turn Completed; 1 = anything else (reason on stderr).
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.client import HTTPConnection
from pathlib import Path

PORT = int(os.environ.get("KOT_PORT", "18080"))
BASE_HOST = f"127.0.0.1:{PORT}"
PROVIDER = os.environ.get("KOT_PROVIDER", "anthropic-oauth")
MODEL = os.environ.get("KOT_MODEL", "claude-fable-5-1")
EFFORT = os.environ.get("KOT_EFFORT", "")
LOG_DIR = Path(os.environ.get("KOT_LOG_DIR", "/logs/agent"))
KOT_BIN = os.environ.get("KOT_BIN", "/opt/kot/kot")
INSTRUCTION_FILE = os.environ.get("KOT_INSTRUCTION_FILE", "/opt/kot/instruction.txt")
OAUTH_SRC = Path(os.environ.get("KOT_OAUTH_FILE", "/opt/kot/auth/anthropic-oauth.json"))

# Config home: a per-task dir under the host bind mount when one is present
# (/mnt/kot-homes is mounted by the launcher into every trial container), so
# the full history/telemetry survives the container. Fallback: container-local.
MOUNT_ROOT = Path("/mnt/kot-homes")
CONFIG_DIR = os.environ.get("KOT_CONFIG_HOME", "")

HTTP_TIMEOUT = 30.0
SSE_TIMEOUT = 90.0
READY_TIMEOUT_SEC = 180.0
QUIESCENCE_SEC = 3.0
QUIESCENCE_CAP_SEC = 180.0
STUCK_IDLE_SEC = 45.0
ABORT_GRACE_SEC = 20.0
MAX_SSE_RECONNECTS = 2
USAGE_CHECKPOINT_SEC = 15.0


class RunnerError(RuntimeError):
    pass


def _http_json(method: str, path: str, payload: dict | None = None, timeout: float = HTTP_TIMEOUT) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    conn = HTTPConnection(BASE_HOST, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
            raise RunnerError(f"HTTP {resp.status} {method} {path}: {raw[:400]!r}")
        return json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"{method} {path} failed: {exc}") from exc
    finally:
        conn.close()


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _usage_snapshot(session_id: str) -> dict | None:
    try:
        usage = _http_json("GET", "/api/usage?" + urllib.parse.urlencode({"session_id": session_id}), timeout=10.0)
    except RunnerError:
        return None
    try:
        _write_json_atomic(LOG_DIR / "usage.json", usage)
    except OSError:
        pass
    return usage


class EventStream:
    """GET /api/events reader: a daemon thread drains the SSE socket into a
    queue; close() shuts the socket down so teardown never blocks on a read."""

    def __init__(self, session_id: str, timeout: float) -> None:
        self._url = "/api/events?" + urllib.parse.urlencode({"session_id": session_id})
        self._timeout = timeout
        self._queue: queue.Queue = queue.Queue()
        self._closed = False
        self._conn: HTTPConnection | None = None
        self._connect()

    def _connect(self) -> None:
        conn = HTTPConnection(BASE_HOST, timeout=self._timeout)
        conn.request("GET", self._url, headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()
        if resp.status != 200:
            conn.close()
            raise RunnerError(f"SSE connect failed: HTTP {resp.status}")
        self._conn = conn
        self._resp = resp
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        buf = b""
        try:
            while not self._closed:
                chunk = self._resp.read1(4096) if hasattr(self._resp, "read1") else self._resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line or line.startswith(b":"):
                        continue
                    if line.startswith(b"data:"):
                        data = line[5:].strip()
                        try:
                            self._queue.put(json.loads(data.decode("utf-8", "replace")))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            pass
        except AttributeError:
            pass
        self._queue.put({"kind": "__stream_closed__"})

    def reconnect(self) -> None:
        try:
            self._conn.close() if self._conn else None
        except OSError:
            pass
        self._connect()

    def get(self, timeout: float) -> dict | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._closed = True
        try:
            if self._conn:
                self._conn.sock.shutdown(2) if self._conn.sock else None
                self._conn.close()
        except OSError:
            pass


def _active_work(session_id: str) -> list[str]:
    data = _http_json("GET", "/api/tasks?" + urllib.parse.urlencode({"session_id": session_id}))
    busy: list[str] = []
    for task in data.get("tasks") or []:
        if isinstance(task, dict):
            if str(task.get("status", "")).lower() == "running":
                busy.append(f"task:{task.get('task_id')}")
            for mon in task.get("monitors") or []:
                if isinstance(mon, dict):
                    busy.append(f"monitor:{mon.get('monitor_id')}")
    for delegate in data.get("delegates") or []:
        if isinstance(delegate, dict) and str(delegate.get("status", "")).lower() == "running":
            busy.append(f"delegate:{delegate.get('name') or delegate.get('agent_id')}")
    for spawn in data.get("inline_agents") or []:
        if isinstance(spawn, dict):
            busy.append(f"spawn:{spawn.get('agent_id')}")
    for teammate in data.get("teammates") or []:
        if isinstance(teammate, dict) and str(teammate.get("activity", "")).lower() == "running":
            busy.append(f"teammate:{teammate.get('name')}")
    return busy


def await_turn(stream: EventStream, session_id: str, submitted_at: float, errors: list[str]) -> dict:
    """Success requires ALL of: last terminal Completed; every observed Running
    matched by a terminal; authoritative Idle; a successful EMPTY /api/tasks
    probe; and after a confirmed drain a FULL quiet window ending in a second
    empty probe. Usage checkpoints fire on the 1-second tick regardless of
    event arrival order."""
    state: str | None = None
    runs_started = 0
    turns_ended = 0
    last_terminal: str | None = None
    idle_since: float | None = None
    quiet_since = time.monotonic()
    busy_since: float | None = None
    drain_pending = False
    sse_reconnects = 0
    last_ckpt = 0.0

    while True:
        event = stream.get(timeout=1.0)
        if event is not None:
            kind = str(event.get("kind") or "")
            quiet_since = time.monotonic()
            if kind == "session_state":
                state = str(event.get("state") or "")
                if state == "Running":
                    runs_started += 1
                    idle_since = None
                elif state == "Idle" and idle_since is None:
                    idle_since = time.monotonic()
                continue
            if kind == "turn_ended":
                reason = str(event.get("reason") or "")
                turns_ended += 1
                last_terminal = reason
                if reason in ("Completed", "SoftInterrupted"):
                    continue
                return {"ok": False, "reason": reason, "quiescence": "not-reached"}
            if kind == "error":
                errors.append(str(event.get("message") or "")[:800])
                continue
            if kind in ("__stream_error__", "__stream_closed__"):
                if sse_reconnects >= MAX_SSE_RECONNECTS:
                    raise RunnerError("event stream lost and reconnect budget exhausted")
                sse_reconnects += 1
                time.sleep(1.0)
                stream.reconnect()
                continue
            continue

        now = time.monotonic()
        if now - last_ckpt >= USAGE_CHECKPOINT_SEC:
            last_ckpt = now
            _usage_snapshot(session_id)
        eligible = (
            turns_ended >= 1
            and runs_started == turns_ended
            and last_terminal == "Completed"
            and state == "Idle"
        )
        if not eligible:
            turn_outstanding = runs_started > turns_ended or turns_ended == 0
            if (
                turn_outstanding
                and state == "Idle"
                and idle_since is not None
                and now - idle_since > STUCK_IDLE_SEC
                and now - submitted_at > STUCK_IDLE_SEC
            ):
                return {"ok": False, "reason": "idle_without_turn_end", "quiescence": "not-reached"}
            continue
        if now - quiet_since < QUIESCENCE_SEC:
            continue
        try:
            busy = _active_work(session_id)
        except RunnerError as exc:
            errors.append(f"tasks probe failed: {exc}")
            return {"ok": False, "reason": "tasks_probe_failed", "quiescence": "unprovable"}
        if busy:
            if busy_since is None:
                busy_since = now
            drain_pending = True
            quiet_since = now
            if now - busy_since > QUIESCENCE_CAP_SEC:
                return {"ok": False, "reason": "quiescence_cap", "quiescence": f"live-work: {','.join(busy)}"}
            continue
        if drain_pending:
            drain_pending = False
            quiet_since = now
            continue
        return {"ok": True, "reason": "Completed", "quiescence": "clean"}


def _terminal_line(reason: str, errors: list[str]) -> None:
    detail = errors[-1] if errors else ""
    print(f"KOT_TERMINAL {json.dumps({'reason': reason, 'detail': detail[:400]})}", flush=True)


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    workspace = os.getcwd()
    instruction = Path(INSTRUCTION_FILE).read_text(encoding="utf-8")
    if not instruction.strip():
        print("runner: empty instruction", file=sys.stderr)
        return 1

    config_dir = CONFIG_DIR
    if not config_dir:
        if MOUNT_ROOT.is_dir():
            digest = hashlib.sha256(instruction.encode("utf-8")).hexdigest()[:16]
            config_dir = str(MOUNT_ROOT / digest)
        else:
            config_dir = "/tmp/kot-config"

    if PROVIDER == "anthropic-oauth":
        if not OAUTH_SRC.exists():
            print(f"runner: KOT_AUTH_MISSING {OAUTH_SRC}", file=sys.stderr)
            return 1
        auth_dir = Path(config_dir) / "auth"
        auth_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(OAUTH_SRC, auth_dir / "anthropic-oauth.json")
        os.chmod(auth_dir / "anthropic-oauth.json", 0o600)
    else:
        key_env = {"deepseek": "DEEPSEEK_API_KEY", "zai": "ZAI_API_KEY", "moonshotai": "MOONSHOT_API_KEY"}.get(PROVIDER)
        if key_env and not os.environ.get(key_env):
            print(f"runner: {key_env} is not set", file=sys.stderr)
            return 1

    # Fresh config home per task; proactive auto-compaction ON for the main
    # session (children default on) — long tasks must not die on the window.
    # Retry budget raised to ~30 min of patience: provider 529s come in long
    # waves; the default ~9.5 min budget burns attempts on pure overload.
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    settings_path = Path(config_dir) / "settings.json"
    if not settings_path.exists():
        settings_path.write_text(
            json.dumps({
                "compaction": {"auto_enabled": True},
                "daemon": {
                    "retry": {
                        "max_attempts": 30,
                        "quick_retries": 2,
                        "quick_delay_secs": 30,
                        "slow_delay_secs": 60,
                    }
                },
            }),
            encoding="utf-8",
        )

    kot_log = open(LOG_DIR / "kot-web.log", "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [
            KOT_BIN, "web",
            "--addr", BASE_HOST,
            "--config-dir", config_dir,
            "--provider", PROVIDER,
            "--model", MODEL,
            "--workspace", workspace,
            "--new",
        ],
        stdout=kot_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    def cleanup() -> None:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    session_ref: dict[str, str] = {}
    term_errors: list[str] = []

    def on_sigterm(_sig, _frame) -> None:
        # Bounded graceful path: usage first (the trial's cost record), then
        # flush the overlay so the verifier grades the latest disk, then close.
        sid = session_ref.get("id")
        if sid:
            _usage_snapshot(sid)
            try:
                _http_json("POST", "/api/sync", {"session_id": sid}, timeout=8.0)
            except RunnerError:
                pass
            try:
                _http_json("POST", "/api/session/close", {"session_id": sid}, timeout=5.0)
            except RunnerError:
                pass
        _terminal_line("sigterm", term_errors)
        cleanup()
        sys.exit(1)

    signal.signal(signal.SIGTERM, on_sigterm)

    try:
        deadline = time.monotonic() + READY_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                print(f"runner: kot web exited during startup (code {proc.returncode})", file=sys.stderr)
                _terminal_line("startup_exit", term_errors)
                return 1
            try:
                _http_json("GET", "/api/info", timeout=3.0)
                break
            except RunnerError:
                time.sleep(1.0)
        else:
            print("runner: kot web did not become ready", file=sys.stderr)
            _terminal_line("startup_timeout", term_errors)
            return 1

        session_payload: dict = {"cwd": workspace, "provider": PROVIDER, "model": MODEL}
        if EFFORT:
            session_payload["effort"] = EFFORT
        answer = _http_json("POST", "/api/session", session_payload)
        session_id = str(answer.get("session_id") or "").strip()
        if not session_id:
            print(f"runner: /api/session returned no session_id: {answer}", file=sys.stderr)
            _terminal_line("no_session", term_errors)
            return 1
        session_ref["id"] = session_id
        try:
            _write_json_atomic(LOG_DIR / "session.json", {"session_id": session_id, "cwd": workspace})
        except OSError:
            pass

        errors: list[str] = []
        stream = EventStream(session_id, SSE_TIMEOUT)
        try:
            submitted_at = time.monotonic()
            while True:
                answer = _http_json("POST", "/api/submit", {
                    "session_id": session_id,
                    "text": instruction,
                    "client_msg_id": f"fh-{session_id}",
                })
                if answer.get("ok"):
                    break
                if str(answer.get("reason") or "") == "running":
                    time.sleep(2.0)
                    continue
                print(f"runner: submit refused: {answer}", file=sys.stderr)
                _terminal_line("submit_refused", term_errors)
                return 1

            verdict = await_turn(stream, session_id, submitted_at, errors)
            if errors:
                for msg in errors:
                    print(f"runner: event error: {msg}", file=sys.stderr)
            term_errors.extend(errors)

            if not verdict["ok"]:
                try:
                    _http_json("POST", "/api/abort", {"session_id": session_id}, timeout=5.0)
                    stop_at = time.monotonic() + ABORT_GRACE_SEC
                    while time.monotonic() < stop_at:
                        ev = stream.get(timeout=1.0)
                        if ev is None:
                            continue
                        if ev.get("kind") == "session_state" and ev.get("state") == "Idle":
                            break
                        if ev.get("kind") in ("__stream_error__", "__stream_closed__"):
                            break
                except RunnerError:
                    pass

            # Flush the virtual-file overlay: the verifier reads the disk. A
            # failed/conflicted flush means the disk is STALE — the trial must
            # fail, never let the verifier grade old bytes.
            sync_ok = False
            try:
                sync_report = _http_json("POST", "/api/sync", {"session_id": session_id})
                _write_json_atomic(LOG_DIR / "sync.json", sync_report)
                conflicts = sync_report.get("conflicts") or []
                real_errors = [
                    e
                    for e in (sync_report.get("errors") or [])
                    if not (
                        isinstance(e, dict)
                        and not str(e.get("path") or "").strip()
                        and "no virtual state to sync" in str(e.get("error") or "")
                    )
                ]
                sync_ok = not conflicts and not real_errors
                if not sync_ok:
                    print(
                        f"runner: sync reported conflicts/errors: conflicts={conflicts} errors={real_errors}",
                        file=sys.stderr,
                    )
            except RunnerError as exc:
                print(f"runner: sync failed: {exc}", file=sys.stderr)

            usage = _usage_snapshot(session_id)
            if usage is None:
                print("runner: usage snapshot failed", file=sys.stderr)
            else:
                total = usage.get("total") or {}
                print(
                    "runner: usage input={i} cache_read={cr} cache_write={cw} output={o} reasoning={r}".format(
                        i=total.get("input_tokens", 0),
                        cr=total.get("cache_read_input_tokens", 0),
                        cw=total.get("cache_creation_input_tokens", 0),
                        o=total.get("output_tokens", 0),
                        r=total.get("reasoning_tokens", 0),
                    )
                )

            _http_json("POST", "/api/session/close", {"session_id": session_id}, timeout=10.0)

            if not verdict["ok"]:
                print(f"runner: turn not completed: {verdict['reason']} ({verdict['quiescence']})", file=sys.stderr)
                _terminal_line(verdict["reason"], term_errors)
                return 1
            if not sync_ok:
                print("runner: workspace flush failed — the verifier would read a stale disk", file=sys.stderr)
                _terminal_line("sync_failed", term_errors)
                return 1
            _terminal_line("Completed", term_errors)
            return 0
        finally:
            stream.close()
    finally:
        cleanup()
        kot_log.close()


if __name__ == "__main__":
    sys.exit(main())
