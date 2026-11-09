"""Complete, private per-execution records of what a provider CLI saw and did.

One JSON-lines file per CLI process: the launch environment, the exact prompt
the CLI received, every stdout/stderr line as it arrived, and the exit. Unlike
``cli_diagnostics`` (bounded tails for failure triage), records keep the whole
stream so a turn can be reviewed after the fact.

Records stay local: directories are 0700, files 0600, every line is redacted
before it is written, and env values are never stored (only which names Unity
changed). Writing is best-effort and must never change a turn's outcome. A
single record stops growing at ``MAX_RECORD_BYTES``; the whole store prunes the
oldest records beyond ``MAX_TOTAL_BYTES``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from backend.app.adapters.cli_diagnostics import _redact

SCHEMA = 1
DIRECTORY_NAME = "turn-records"
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
VERSION_TIMEOUT_SECONDS = 3
# Offline tests switch this off so they never execute the user's installed CLIs.
VERSION_PROBE = True
PROMPT_PLACEHOLDER = "<prompt>"
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")
_LOGGER = logging.getLogger(__name__)

_versions: dict = {}
_versions_lock = threading.Lock()
_prune_lock = threading.Lock()


def records_root(database_path: Path) -> Path:
    return Path(database_path).resolve().parent / DIRECTORY_NAME


def _safe(value: str) -> str:
    return _SAFE_ID.sub("_", value or "")[:120] or "_"


def _now() -> float:
    return round(time.time(), 3)


def cli_version(command: str) -> Optional[str]:
    """``<cli> --version``, cached per installed binary (path, mtime, size)."""
    if not VERSION_PROBE:
        return None
    resolved = shutil.which(command) or command
    try:
        path = Path(resolved).resolve()
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _versions_lock:
        if key in _versions:
            return _versions[key]
    version = None
    try:
        result = subprocess.run([str(path), "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=VERSION_TIMEOUT_SECONDS, start_new_session=True)
        if result.returncode == 0:
            version = next((line.strip() for line in result.stdout.splitlines() if line.strip()), None)
    except (OSError, subprocess.SubprocessError, ValueError):
        version = None
    version = _redact(version)[:200] if version else None
    with _versions_lock:
        _versions[key] = version
    return version


def redact_prompt_args(argv: Iterable[str], prompt: str) -> list[str]:
    """The launch argv with the prompt itself replaced (it is stored separately)."""
    result = []
    for arg in argv:
        arg = str(arg)
        if prompt and arg == prompt:
            arg = PROMPT_PLACEHOLDER
        elif prompt and arg.endswith("=" + prompt):
            arg = arg[: -len(prompt)] + PROMPT_PLACEHOLDER
        result.append(_redact(arg))
    return result


def env_changes(launch_env: dict, base_env: Optional[dict] = None) -> dict:
    """Names of variables Unity added, changed or removed; never their values."""
    base = dict(os.environ if base_env is None else base_env)
    return {
        "set": sorted(k for k, v in launch_env.items() if base.get(k) != v),
        "removed": sorted(k for k in base if k not in launch_env),
    }


class TurnRecord:
    """Writer for one CLI execution; every method is thread-safe and non-raising."""

    def __init__(self, database_path: Path, *, mission_id: str, turn_id: str, agent_id: str,
                 provider_id: str, route: str, conversation_id: Optional[str] = None,
                 max_bytes: int = MAX_RECORD_BYTES, max_total_bytes: int = MAX_TOTAL_BYTES):
        self.record_id = "tr_" + os.urandom(8).hex()
        self.root = records_root(database_path)
        self.path = self.root / _safe(mission_id) / f"{_safe(turn_id)}.{self.record_id}.jsonl"
        self.max_bytes = max_bytes
        self.max_total_bytes = max_total_bytes
        self._lock = threading.Lock()
        self._stream = None
        self._written = 0
        self._dropped_lines = 0
        self._dropped_bytes = 0
        self._lines = {"stdout": 0, "stderr": 0}
        self._started = time.monotonic()
        self._version_thread: Optional[threading.Thread] = None
        self._version: list = [None]
        self._closed = False
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
            self.path.parent.mkdir(exist_ok=True, mode=0o700)
            self.path.parent.chmod(0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self._stream = os.fdopen(fd, "w", encoding="utf-8")
        except OSError:
            _LOGGER.warning("Could not open turn record", exc_info=True)
            self._stream = None
            return
        self._write({"kind": "header", "schema": SCHEMA, "record_id": self.record_id,
                     "mission_id": mission_id, "conversation_id": conversation_id, "turn_id": turn_id,
                     "agent_id": agent_id, "provider_id": provider_id, "route": route, "t": _now(),
                     "started_ns": time.time_ns()},
                    force=True)

    @property
    def active(self) -> bool:
        return self._stream is not None

    def _write(self, entry: dict, *, force: bool = False) -> bool:
        if self._stream is None:
            return False
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        size = len(line.encode("utf-8"))
        if not force and self._written + size > self.max_bytes:
            self._dropped_lines += 1
            self._dropped_bytes += size
            return False
        try:
            self._stream.write(line)
            self._stream.flush()
            self._written += size
            return True
        except (OSError, ValueError):
            _LOGGER.warning("Could not write turn record", exc_info=True)
            self._close_stream()
            return False

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def launch(self, *, command: str, argv: Iterable[str], prompt: str, cwd: Optional[Path],
               launch_env: dict, isolation: str = "none", sandbox_profile: Optional[str] = None,
               model: Optional[str] = None) -> None:
        """The environment and the exact prompt, written before the CLI starts."""
        if not self.active:
            return
        executable = shutil.which(command) or command
        try:
            executable = str(Path(executable).resolve())
        except OSError:
            pass
        environment = {
            "kind": "environment", "t": _now(), "command": os.path.basename(command), "executable": executable,
            "argv": redact_prompt_args(argv, prompt), "cwd": str(cwd) if cwd else os.getcwd(),
            "env": env_changes(launch_env), "isolation": isolation, "model": model or None,
        }
        if sandbox_profile is not None:
            environment["sandbox_profile_sha256"] = hashlib.sha256(sandbox_profile.encode()).hexdigest()
            environment["sandbox_profile"] = sandbox_profile
        with self._lock:
            self._write(environment, force=True)
            self._write({"kind": "input", "t": _now(), "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                         "chars": len(prompt), "prompt": _redact(prompt)}, force=True)

        def probe() -> None:
            self._version[0] = cli_version(command)

        self._version_thread = threading.Thread(target=probe, daemon=True, name="turn-record-version")
        self._version_thread.start()

    def stream(self, name: str, line: str) -> None:
        if not self.active:
            return
        with self._lock:
            self._lines[name] = self._lines.get(name, 0) + 1
            self._write({"kind": name, "t": _now(), "line": _redact(line)})

    def session(self, session_id: str, model: Optional[str] = None) -> None:
        with self._lock:
            self._write({"kind": "session", "t": _now(), "session_id": session_id, "model": model or None},
                        force=True)

    def finish(self, *, exit_code: Optional[int] = None, error: Optional[str] = None, **details) -> None:
        if self._closed:
            return
        self._closed = True
        if self._version_thread is not None:
            self._version_thread.join(timeout=1)
        with self._lock:
            self._write({"kind": "exit", "t": _now(), "exit_code": exit_code,
                         "error": _redact(error)[:4000] if error else None,
                         "duration_ms": int((time.monotonic() - self._started) * 1000),
                         "cli_version": self._version[0], "lines": dict(self._lines),
                         "truncated": self._dropped_lines > 0, "dropped_lines": self._dropped_lines,
                         "dropped_bytes": self._dropped_bytes, **details}, force=True)
            self._close_stream()
        prune(self.root, self.max_total_bytes, keep=self.path)


def prune(root: Path, max_total_bytes: int, *, keep: Optional[Path] = None) -> None:
    """Remove the oldest finished records once the store exceeds its budget."""
    if not _prune_lock.acquire(blocking=False):
        return
    try:
        files = []
        for path in root.glob("*/*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files, key=lambda item: item[0]):
            if total <= max_total_bytes:
                break
            if keep is not None and path == keep:
                continue
            try:
                path.unlink()
                total -= size
            except OSError:
                continue
    except OSError:
        _LOGGER.warning("Could not prune turn records", exc_info=True)
    finally:
        _prune_lock.release()


def read_records(database_path: Path, mission_id: str, turn_id: str, *, max_entries: int = 20000,
                 strict: bool = False) -> list[dict]:
    """Every record of one turn, oldest first; each is header + entries + exit.

    A file named for the turn that does not read back as its record is skipped,
    and so is a line that is not an entry. ``strict`` keeps each such file as a
    record with an ``error`` and no entries instead, for readers that must not
    silently discard a matching file (the closed-book audit, #161).
    """
    directory = records_root(database_path) / _safe(mission_id)
    records = []
    for path in directory.glob(f"{_safe(turn_id)}.tr_*.jsonl"):
        record, error = _read_record(path, mission_id, turn_id, max_entries, strict)
        if error is None:
            records.append(record)
        elif strict:
            records.append({"record_id": None, "error": error, "_order": (0, 0), "finished": False, "entries": []})
    records.sort(key=lambda record: record.pop("_order"))
    return records


def _read_record(path: Path, mission_id: str, turn_id: str, max_entries: int,
                 strict: bool) -> tuple[Optional[dict], Optional[str]]:
    """One file named for the turn: its record, or why it does not read back as one."""
    entries = []
    try:
        if path.is_symlink() or not path.is_file():
            return None, "not a regular file"
        modified = path.stat().st_mtime_ns
        with path.open(encoding="utf-8") as stream:
            for index, line in enumerate(stream):
                if index >= max_entries:
                    entries.append({"kind": "omitted", "reason": "entry_limit"})
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    entry = None
                if isinstance(entry, dict):
                    entries.append(entry)
                elif strict:
                    return None, "a line is not an entry"
    except (OSError, UnicodeDecodeError):
        return None, "could not be read"
    header = next((e for e in entries if e.get("kind") == "header"), None)
    if header is None:
        return None, "no header"
    if header.get("turn_id") != turn_id:
        return None, "header names another turn"
    if strict and (
        entries[0] is not header
        or sum(e.get("kind") == "header" for e in entries) != 1
        or type(header.get("schema")) is not int or header["schema"] != SCHEMA
        or header.get("mission_id") != mission_id
        or not isinstance(header.get("record_id"), str)
        or path.name != f"{_safe(turn_id)}.{header.get('record_id')}.jsonl"
        or type(header.get("started_ns")) is not int or header["started_ns"] <= 0
        or type(header.get("t")) not in (int, float)
        or any(not isinstance(header.get(key), str) or not header[key]
               for key in ("agent_id", "provider_id", "route"))
    ):
        return None, "invalid header"
    return {
        "record_id": header.get("record_id"),
        "provider_id": header.get("provider_id"),
        "route": header.get("route"),
        "started_at": header.get("t"),
        "_order": (header.get("started_ns") or 0, modified),
        "finished": any(e.get("kind") == "exit" for e in entries),
        "entries": entries,
    }, None
