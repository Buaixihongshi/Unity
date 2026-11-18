"""Per-workspace MCP injection for Antigravity CLI, plus legacy cleanup.

agy 1.4 loads plugins listed in a workspace's ``.agents/plugins.json``, and a
plugin may bundle MCP servers. Every Unity agy process already runs from its
own throwaway workspace, so the Unity server is written there as a plugin:
each process sees only its own server, nothing outside the workspace changes,
and concurrent agy processes need no shared lock.

agy namespaces plugin servers as ``<plugin>_<server>``; :data:`SERVER_NAME`
is what the model passes to ``call_mcp_tool``. It is deliberately not
``yiren_hub``: older Unity builds still inject ``yiren_hub`` into the global
``~/.gemini/config/mcp_config.json`` while their turns run, and agy drops a
plugin server whose namespaced name collides with a global one.

Older builds still rewrite that global file under an exclusive ``fcntl.flock``
for each turn, and agy loads global servers next to plugin ones. A new agy
started meanwhile would see the older instance's ``yiren_hub``, which writes
into that instance's database. :func:`legacy_config_excluded` therefore holds
the same lock *shared* while an agy process runs: new instances never block
each other, older ones wait until no new agy is running and vice versa. Under
the shared lock no older instance can be mid-turn, so any sentinel left is a
crash leftover and is restored before agy starts.

Paths are computed via functions (not module-level constants) so tests can
monkeypatch ``Path.home``.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

PLUGIN_NAME = "unity"
PLUGIN_SERVER = "hub"
SERVER_NAME = f"{PLUGIN_NAME}_{PLUGIN_SERVER}"


class WaitCancelled(Exception):
    """The caller gave up waiting for the global config lock (e.g. user stop)."""

# agy 1.0.3 swallows several fatal conditions into silent ``exit 0`` (no stdout
# / no stderr) — notably "not logged into Antigravity" and Google-side quota
# 429 ``RESOURCE_EXHAUSTED``. Without context the BLOCKED summary just reads
# ``agent did not call publish_message via MCP``, which is true but useless.
# ``read_agy_log_hint`` peeks the most recent agy log file written during the
# current run and lifts a one-line hint out, so the BLOCKED summary surfaces
# the real cause.
_AGY_LOG_TAIL_BYTES = 256 * 1024
_QUOTA_PATTERN = re.compile(r"RESOURCE_EXHAUSTED[^\n]*", re.IGNORECASE)
_EXECUTOR_PATTERN = re.compile(r"agent executor error:\s*([^\n]{1,240})")


def _config_dir() -> Path:
    return Path.home() / ".gemini" / "config"


def global_mcp_config_path() -> Path:
    return _config_dir() / "mcp_config.json"


def _lock_path() -> Path:
    return _config_dir() / ".yiren-mcp.lock"


def _sentinel_path() -> Path:
    return _config_dir() / "mcp_config.json.yiren-bak"


def _restore(original: Optional[str]) -> None:
    target = global_mcp_config_path()
    if original is None:
        target.unlink(missing_ok=True)
    else:
        target.write_text(original, encoding="utf-8")


def _recover_if_crashed() -> None:
    """If a sentinel exists, a previous run was hard-killed before restoring.

    The sentinel holds the true-original content; restore from it and delete
    the sentinel. Must be called while holding the lock.
    """
    sentinel = _sentinel_path()
    if not sentinel.is_file():
        return
    try:
        payload = json.loads(sentinel.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        sentinel.unlink(missing_ok=True)
        return
    original = payload.get("content") if payload.get("existed") else None
    _restore(original)
    sentinel.unlink(missing_ok=True)


@contextlib.contextmanager
def _flock(timeout: Optional[float] = None, *, shared: bool = False,
           cancelled: Optional[Callable[[], bool]] = None,
           on_wait: Optional[Callable[[], None]] = None) -> Iterator[None]:
    """Cross-process lock on ~/.gemini/config/.yiren-mcp.lock."""
    cfg_dir = _config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_lock_path()), os.O_CREAT | os.O_RDWR, 0o600)
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    try:
        if timeout is None and cancelled is None and on_wait is None:
            fcntl.flock(fd, mode)
        else:
            deadline = None if timeout is None else time.monotonic() + timeout
            waited = False
            while True:
                try:
                    fcntl.flock(fd, mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waited and on_wait is not None:
                        on_wait()
                    waited = True
                    if cancelled is not None and cancelled():
                        raise WaitCancelled('Stopped while waiting for the Antigravity MCP configuration')
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError('Antigravity MCP configuration is in use')
                    time.sleep(0.05 if deadline is None else min(0.05, max(0, deadline - time.monotonic())))
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write_workspace_plugin(workspace: Path, server_spec: dict[str, Any]) -> Path:
    """Register ``server_spec`` as this workspace's Unity MCP server.

    Returns the plugin's ``mcp_config.json`` (useful for diagnostics). The
    ``plugins.json`` schema is ``{"entries": [{"path", "include_only"}]}``;
    agy ignores the file entirely when it does not parse.
    """
    plugins = workspace / ".unity-plugins"
    root = plugins / PLUGIN_NAME
    (root / ".antigravity-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".antigravity-plugin" / "plugin.json").write_text(json.dumps({
        "name": PLUGIN_NAME, "version": "1.0.0", "description": "Unity host tools for this turn",
    }), encoding="utf-8")
    config = root / "mcp_config.json"
    config.write_text(json.dumps({"mcpServers": {PLUGIN_SERVER: server_spec}}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    agents = workspace / ".agents"
    agents.mkdir(exist_ok=True)
    (agents / "plugins.json").write_text(json.dumps({
        "entries": [{"path": str(plugins), "include_only": [PLUGIN_NAME]}],
    }), encoding="utf-8")
    return config


@contextlib.contextmanager
def legacy_config_excluded(timeout: Optional[float] = None, *,
                           cancelled: Optional[Callable[[], bool]] = None,
                           on_wait: Optional[Callable[[], None]] = None) -> Iterator[None]:
    """Hold while an agy process runs so no older build injects the global config."""
    with _flock(timeout, shared=True, cancelled=cancelled, on_wait=on_wait):
        # Restoring under a shared lock is safe: only crashed older owners leave a
        # sentinel, and concurrent new holders restore the same original content.
        _recover_if_crashed()
        yield


def recover_antigravity_global_config() -> None:
    """Restore a global config an older Unity build left behind when killed.

    No-op when ~/.gemini/config does not exist.

    Fast path: if no sentinel exists there is nothing to recover, so we skip
    acquiring the lock entirely — this keeps the common (no-crash) case
    side-effect-free (no lock file created) on every ProviderRouterAdapter
    construction. The authoritative check still happens under the lock.
    """
    if not _config_dir().exists():
        return
    if not _sentinel_path().is_file():
        return
    # A live older instance can legitimately own the sentinel; never wait for it.
    try:
        with _flock(timeout=0):
            _recover_if_crashed()
    except TimeoutError:
        return


def _agy_log_dir() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "log"


def read_agy_log_hint(run_start: float) -> Optional[str]:
    """Best-effort: pull a one-line diagnostic out of the most recent agy log.

    Returns ``None`` when no log was written during this run, the directory is
    missing, or no known pattern matches. All I/O errors are swallowed — this
    is purely a UX enrichment, never a failure source.

    ``run_start`` is the wall-clock time (``time.time()``) captured right
    before launching the agy subprocess; we ignore logs older than that
    (with a 1s grace for clock skew). Patterns in priority order:

    1. ``RESOURCE_EXHAUSTED`` (Google quota 429) — most actionable.
    2. ``not logged into Antigravity`` (OAuth missing).
    3. Generic ``agent executor error:`` line as a fallback.
    """
    try:
        log_dir = _agy_log_dir()
        if not log_dir.is_dir():
            return None
        candidates = [
            p for p in log_dir.glob("cli-*.log")
            if p.is_file() and p.stat().st_mtime >= run_start - 1.0
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        try:
            size = latest.stat().st_size
            with latest.open("rb") as fh:
                if size > _AGY_LOG_TAIL_BYTES:
                    fh.seek(-_AGY_LOG_TAIL_BYTES, os.SEEK_END)
                data = fh.read()
            text = data.decode("utf-8", errors="replace")
        except OSError:
            return None

        m = _QUOTA_PATTERN.search(text)
        if m:
            return f"agy quota exhausted: {m.group(0).strip()[:240]}"
        # Authentication probes can fail before keyring/silent OAuth succeeds.
        # The last observed auth event decides the diagnostic.
        lower = text.lower()
        last_failure = lower.rfind("not logged into antigravity")
        last_success = max(lower.rfind(marker) for marker in (
            "authenticated via keyring", "oauth: authenticated successfully",
            "silent auth succeeded",
        ))
        if last_failure >= 0 and last_failure > last_success:
            return "agy not authenticated — run `agy` interactively to OAuth login"
        m = _EXECUTOR_PATTERN.search(text)
        if m:
            return f"agy agent executor error: {m.group(1).strip()}"
        return None
    except Exception:
        return None
