"""KOT agent for Harbor (FrontierHarness Eval run): the rust-agent (`kot web`)
installed into the task container and driven over its own HTTP+SSE API by an
in-container runner.

Model: anthropic-oauth / claude-fable-5-1 (Claude subscription OAuth). The
credential travels as a FILE uploaded from the host (`~/.kot/auth/
anthropic-oauth.json`), never via env: before every upload the host performs a
locked refresh preflight so a container always receives an access token whose
remaining lifetime covers the whole task — containers then never refresh
concurrently (refresh tokens rotate, parallel refreshes would invalidate each
other). The preflight takes the same `.lock` sidecar domain the kot daemon's
fs2 guard uses, so host daemon refreshes serialize with ours.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar, override

from harbor.agents.installed.base import (
    AgentAuthenticationError,
    ApiOverloadedError,
    BaseInstalledAgent,
    ContextWindowExceededError,
    ErrorPattern,
)
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.result import AgentInfo, ModelInfo

_AGENT_DIR = Path(__file__).resolve().parent
_KOT_BIN = _AGENT_DIR / "kot"          # linux x86_64 musl-static release binary
_RUNNER = _AGENT_DIR / "runner.py"
_FINALIZE = _AGENT_DIR / "finalize.py"
_REMOTE_DIR = "/opt/kot"

_HOST_OAUTH = Path(os.environ.get("KOT_HOST_OAUTH", str(Path.home() / ".kot" / "auth" / "anthropic-oauth.json")))
_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_OAUTH_UA = "claude-cli/2.1.252 (external, cli)"
# Refresh when the remaining access-token lifetime cannot cover a whole task
# (deep-swe agent timeout is 5400 s) plus startup/cleanup margin.
_OAUTH_MIN_TTL_SEC = 2 * 3600
_OAUTH_LOCK_TIMEOUT_SEC = 60.0


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _SidecarLock:
    """Cross-process exclusive lock on the token file's `.lock` sidecar — the
    same lock domain as the daemon's fs2 guard (LockFile range lock on Windows).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh = None

    def __enter__(self):
        import msvcrt

        self._fh = open(self._path, "a+b")
        deadline = time.monotonic() + _OAUTH_LOCK_TIMEOUT_SEC
        while True:
            try:
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError(f"oauth lock busy: {self._path}")
                time.sleep(0.2)

    def __exit__(self, *_exc) -> None:
        if self._fh is not None:
            import msvcrt

            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._fh.close()
                self._fh = None


def _parse_rfc3339(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _oauth_refresh(payload: dict) -> dict:
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": payload["refresh_token"],
        "client_id": _OAUTH_CLIENT_ID,
        "scope": " ".join(payload.get("scopes") or []),
    }).encode()
    req = urllib.request.Request(
        _OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": _OAUTH_UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ensure_fresh_oauth() -> None:
    """Locked read → refresh-if-needed → atomic replace on the HOST auth file."""
    lock_path = _HOST_OAUTH.with_suffix(_HOST_OAUTH.suffix + ".lock")
    with _SidecarLock(lock_path):
        data = json.loads(_HOST_OAUTH.read_text(encoding="utf-8"))
        try:
            expires_at = _parse_rfc3339(data["expires_at"])
        except (KeyError, ValueError) as exc:
            raise AgentAuthenticationError(f"oauth file malformed: {exc}") from exc
        if expires_at - datetime.now(timezone.utc) > timedelta(seconds=_OAUTH_MIN_TTL_SEC):
            return
        try:
            refreshed = _oauth_refresh(data)
        except Exception as exc:
            raise AgentAuthenticationError(f"oauth preflight refresh failed: {exc}") from exc
        now = datetime.now(timezone.utc)
        data["access_token"] = refreshed["access_token"]
        if refreshed.get("refresh_token"):
            data["refresh_token"] = refreshed["refresh_token"]
        data["expires_at"] = _rfc3339(now + timedelta(seconds=int(refreshed.get("expires_in", 3600))))
        fd, tmp = tempfile.mkstemp(dir=_HOST_OAUTH.parent, prefix=".oauth-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, _HOST_OAUTH)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class KotAgent(BaseInstalledAgent):
    """Harbor `BaseInstalledAgent` for KOT (see module docstring)."""

    ERROR_PATTERNS: ClassVar[list[ErrorPattern]] = [
        ErrorPattern(r"KOT_AUTH_MISSING|invalid_grant|InvalidApiKey|invalid_token", AgentAuthenticationError),
        ErrorPattern(r"PromptTooLong", ContextWindowExceededError),
        ErrorPattern(r'"http_status":\s*(429|529)\b|rate_limit_error', ApiOverloadedError),
        *BaseInstalledAgent.ERROR_PATTERNS,
    ]

    def _bench(self, key: str, default: str) -> str:
        return (self._get_env(f"KOT_BENCH_{key}") or default).strip() or default

    @property
    def bench_provider(self) -> str:
        return self._bench("PROVIDER", "anthropic-oauth")

    @property
    def bench_model(self) -> str:
        return self._bench("MODEL", "claude-fable-5-1")

    @property
    def bench_effort(self) -> str:
        return self._bench("EFFORT", "")

    @staticmethod
    @override
    def name() -> str:
        return "KOT"

    @override
    def to_agent_info(self) -> AgentInfo:
        info = super().to_agent_info()
        if info.model_info is None:
            return AgentInfo(
                name=info.name,
                version=info.version,
                model_info=ModelInfo(name=self.bench_model, provider=self.bench_provider),
            )
        return info

    @override
    def get_version_command(self) -> str | None:
        return f"{_REMOTE_DIR}/kot --version"

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip().splitlines()[0] if stdout.strip() else "unknown"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(environment, ("python3", "ca_certificates", "procps"))
        await environment.exec(command=f"mkdir -p {_REMOTE_DIR}/auth /logs/agent", user="root")
        await environment.upload_file(_KOT_BIN, f"{_REMOTE_DIR}/kot")
        await environment.upload_file(_RUNNER, f"{_REMOTE_DIR}/runner.py")
        await environment.upload_file(_FINALIZE, f"{_REMOTE_DIR}/finalize.py")
        chmod = f"chmod 0755 {_REMOTE_DIR}/kot && chmod 0644 {_REMOTE_DIR}/runner.py {_REMOTE_DIR}/finalize.py"
        if self.bench_provider == "anthropic-oauth":
            # Refresh preflight runs on the host, serialized across concurrent
            # installs and against the daemon, then the fresh file goes up.
            await asyncio.to_thread(_ensure_fresh_oauth)
            await environment.upload_file(_HOST_OAUTH, f"{_REMOTE_DIR}/auth/anthropic-oauth.json")
            chmod += f" && chmod 0600 {_REMOTE_DIR}/auth/anthropic-oauth.json"
        await environment.exec(command=chmod, user="root")

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        await self._upload_config_text(
            environment,
            content=instruction,
            remote_path=_REMOTE_DIR,
            filename="instruction.txt",
        )
        workdir = "/app"
        probe = await environment.exec(command="pwd", user="root")
        if probe.return_code == 0 and probe.stdout and probe.stdout.strip():
            workdir = probe.stdout.strip()

        env: dict[str, str] = {
            "KOT_PROVIDER": self.bench_provider,
            "KOT_MODEL": self.bench_model,
            "KOT_EFFORT": self.bench_effort,
        }
        key_env = {"deepseek": "DEEPSEEK_API_KEY", "zai": "ZAI_API_KEY", "moonshotai": "MOONSHOT_API_KEY"}.get(self.bench_provider)
        if key_env:
            api_key = self._get_env(key_env)
            if api_key:
                env[key_env] = api_key

        command = f"python3 {_REMOTE_DIR}/runner.py"
        try:
            await self.exec_as_agent(environment, command=command, env=env, cwd=workdir)
        finally:
            # The runner may have died mid-turn (timeout): if `kot web` still
            # answers, flush the overlay and snapshot usage before teardown.
            try:
                await self._finalize_remote(environment)
            except Exception:
                pass
            try:
                await self._collect_usage(environment, context)
            except Exception:
                pass  # usage reporting never fails the trial
            await self._finish_cleanup(environment)

    async def _finalize_remote(self, environment: BaseEnvironment) -> None:
        await environment.exec(
            command=f"python3 {_REMOTE_DIR}/finalize.py",
            user="root",
            timeout_sec=45,
        )

    async def _collect_usage(self, environment: BaseEnvironment, context: AgentContext) -> None:
        usage_result = await environment.exec(command="cat /logs/agent/usage.json", user="root")
        if usage_result.return_code != 0 or not usage_result.stdout:
            return
        try:
            usage = json.loads(usage_result.stdout)
            total = usage.get("total") or {}
            uncached = int(total.get("input_tokens") or 0)
            cache_read = int(total.get("cache_read_input_tokens") or 0)
            cache_creation = int(total.get("cache_creation_input_tokens") or 0)
            output = int(total.get("output_tokens") or 0)
            # Harbor convention: n_input_tokens INCLUDES cache tokens.
            context.n_input_tokens = uncached + cache_read + cache_creation
            context.n_cache_tokens = cache_read
            context.n_output_tokens = output
            context.metadata = {
                "uncached_input_tokens": uncached,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "output_tokens": output,
                "reasoning_tokens": int(total.get("reasoning_tokens") or 0),
                "turns": int(usage.get("turns") or 0),
            }
        except (ValueError, TypeError):
            pass  # usage reporting never fails the trial

    async def _finish_cleanup(self, environment: BaseEnvironment) -> None:
        task = asyncio.ensure_future(self._cleanup_container_processes(environment))
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=30)
            except asyncio.CancelledError:
                continue
            except (asyncio.TimeoutError, Exception):
                break

    async def _cleanup_container_processes(self, environment: BaseEnvironment) -> None:
        command = (
            "command -v pkill >/dev/null && command -v pgrep >/dev/null || "
            "echo 'kot-cleanup: WARNING: pkill/pgrep missing — agent processes may leak'; "
            "pkill -TERM -f '/opt/kot/[r]unner.py'; "
            "for i in $(seq 1 18); do "
            "pgrep -f '/opt/kot/[r]unner.py' >/dev/null || break; sleep 1; "
            "done; "
            "pkill -KILL -f '/opt/kot/[r]unner.py'; "
            "pkill -KILL -f '/opt/kot/[k]ot'; "
            "true"
        )
        try:
            await environment.exec(command=command, user="root", timeout_sec=30)
        except Exception:
            pass  # teardown is best-effort; the original error/result stands
