"""The diagnostic part of a failed CLI's output.

A CLI that dies prints its startup chatter first and its fatal error last.
Claude's stream-json opens with an `init` line and ends with a `result` line
that holds the real message; Claude and Codex both print banners to stderr
before doing any work. Keeping the head of that output records the chatter and
drops the cause, so this module keeps the end and skips known noise.
"""
from __future__ import annotations

import json
import re
from typing import Optional

LIMIT = 200

# Lines every run prints regardless of what went wrong.
_BANNER = re.compile(
    r"^(⚠\s*claude\.ai connectors are disabled"
    r"|Reading additional input from stdin"
    r"|\[notice\]"
    r")",
    re.IGNORECASE,
)
# A stream event, possibly cut off by an earlier truncation.
_EVENT_HEAD = re.compile(r'^\{\s*"type"\s*:\s*"([\w.]+)"')
_ERROR_EVENTS = ("result", "error", "turn.failed")
# What build_failure_summary puts in front of the detail.
_PREFIX = re.compile(r"^\[[\w.-]+\]\s*(exit_code=-?\d+\s*)?")


def _json(line: str) -> Optional[dict]:
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _event_error(event: dict) -> str:
    """The error a stream event carries, or "" when it carries none."""
    kind = event.get("type")
    if kind == "result" and (event.get("is_error") or event.get("subtype") not in (None, "success")):
        return str(event.get("result") or event.get("error") or event.get("terminal_reason") or "")
    if kind in ("error", "turn.failed"):
        error = event.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or "")
        return str(event.get("message") or error or "")
    return ""


def is_noise(line: str) -> bool:
    """A line that says nothing about why this run failed."""
    text = line.strip().rstrip("…").strip()
    if not text or _BANNER.search(text):
        return True
    event = _json(text)
    if event is not None:
        return not _event_error(event)
    head = _EVENT_HEAD.match(text)
    return bool(head) and head.group(1) not in _ERROR_EVENTS


def diagnostic_detail(stderr: str = "", stdout: str = "", *, limit: int = LIMIT) -> str:
    """The most useful failure text from a CLI's streams, at most `limit` chars.

    A structured error event wins; otherwise the last non-noise lines, stderr
    first because that is where CLIs report fatal errors.
    """
    for stream in (stdout, stderr):
        for line in reversed((stream or "").splitlines()):
            event = _json(line.strip())
            message = _event_error(event).strip() if event else ""
            if message:
                return message if len(message) <= limit else message[:limit] + "…"
    for stream in (stderr, stdout):
        lines = [line.strip() for line in (stream or "").splitlines() if not is_noise(line)]
        if lines:
            return _tail(lines, limit)
    return ""


def _tail(lines: list[str], limit: int) -> str:
    """Whole lines from the end, as many as fit; a single long line keeps its end."""
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        if kept and size + len(line) + 1 > limit:
            break
        kept.append(line)
        size += len(line) + 1
    text = "\n".join(reversed(kept))
    return text if len(text) <= limit else "…" + text[-limit:]


def is_informative(reason: str) -> bool:
    """A recorded failure reason says more than an exit code or startup noise.

    "timeout after 600s" is a reason; "[claude] exit_code=1" followed by the
    init event is not.
    """
    rest = _PREFIX.sub("", (reason or "").strip(), count=1)
    return any(not is_noise(line) for line in rest.splitlines())
