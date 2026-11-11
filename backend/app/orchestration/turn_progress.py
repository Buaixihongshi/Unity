"""Host-authored progress notes for turns that stop on a timeout.

When a turn times out the agent usually has not published anything, so the chat
would only say that it timed out. The runner snapshots what the turn did from its
own durable events (tools, artifacts, last activity) into one ``turn.progress_note``
event. It is a host record, never an agent message: it is not written to the
message table and is shown as a labelled system card (iron law L1).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Iterable, Optional

from backend.app.storage.repository import Repository

PROGRESS_NOTE_EVENT = "turn.progress_note"
TIMEOUT_CODES = ("hard_timeout", "idle_timeout", "provider_timeout", "timeout")
# What the chat projection exposes from a note (the full payload stays in events).
HOST_PROGRESS_FIELDS = ("source", "reason", "error", "tool_calls", "tools", "artifacts",
                        "published", "last_activity", "stopped_at")
MAX_TOOLS = 5
MAX_ARTIFACTS = 10
MAX_ERROR_CHARS = 300

# Turn-scoped events that show the agent was doing something.
_ACTIVITY_TYPES = {
    "tool.call", "tool.result", "tool.error", "provider.activity",
    "artifact", "message", "agent.thought", "checkpoint.saved",
}
_TIMEOUT_WORDS = ("timeout", "timed out", "deadline exceeded", "超时")


def timeout_reason(failure_code: Optional[str], summary: Optional[str]) -> Optional[str]:
    """The timeout kind of a blocked outcome, or None when it did not time out."""
    if failure_code in TIMEOUT_CODES:
        return failure_code
    if failure_code:
        return None
    text = (summary or "").lower()
    if not any(word in text for word in _TIMEOUT_WORDS):
        return None
    return "idle_timeout" if "idle timeout" in text else "timeout"


def _aware(value) -> Optional[datetime]:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _activity_label(event) -> str:
    payload = event.payload
    if event.event_type == "artifact":
        return payload.get("path") or payload.get("title") or "产物"
    if event.event_type == "message":
        return "发言"
    if event.event_type == "agent.thought":
        return "工作摘要"
    if event.event_type == "checkpoint.saved":
        return "检查点"
    return payload.get("tool_name") or event.event_type


def summarize_turn_progress(events: Iterable, turn_id: str, *,
                            stopped_at: Optional[datetime] = None) -> dict:
    """What one turn did, from its durable events; deterministic and side-effect free."""
    tools: Counter = Counter()
    order: dict[str, int] = {}
    native_seen: set[str] = set()
    artifacts: list[str] = []
    published = False
    last = None
    for event in events:
        payload = event.payload or {}
        if payload.get("turn_id") != turn_id or event.event_type not in _ACTIVITY_TYPES:
            continue
        last = event
        name = payload.get("tool_name")
        if event.event_type == "tool.call" and name:
            tools[name] += 1
            order.setdefault(name, len(order))
        elif event.event_type == "provider.activity" and name:
            # Native activity emits started/completed pairs under one id.
            key = payload.get("activity_id") or f"{event.id}"
            if key not in native_seen:
                native_seen.add(key)
                tools[name] += 1
                order.setdefault(name, len(order))
        elif event.event_type == "artifact":
            path = payload.get("path") or payload.get("title")
            if path and path not in artifacts:
                artifacts.append(path)
        elif event.event_type == "message" and payload.get("role", "agent") == "agent":
            published = True
    ranked = sorted(tools.items(), key=lambda item: (-item[1], order[item[0]]))
    summary = {
        "tool_calls": sum(tools.values()),
        "tools": [{"name": name, "count": count} for name, count in ranked[:MAX_TOOLS]],
        "artifacts": artifacts[:MAX_ARTIFACTS],
        "published": published,
        "last_activity": None,
    }
    if last is not None:
        at = _aware(last.created_at)
        entry = {"kind": last.event_type, "label": _activity_label(last),
                 "at": at.isoformat() if at else None}
        stopped = _aware(stopped_at)
        if at and stopped:
            entry["seconds_before_stop"] = max(0, int(round((stopped - at).total_seconds())))
        summary["last_activity"] = entry
    return summary


def record_turn_progress_note(repo: Repository, mission_id: str, *, turn_id: str, agent_id: str,
                              provider_id: str, attempt: int, reason: str, error: str,
                              events: Iterable) -> str:
    stopped_at = datetime.now(timezone.utc)
    payload = {
        "turn_id": turn_id, "agent_id": agent_id, "provider_id": provider_id,
        "attempt": attempt, "source": "host", "reason": reason,
        "error": (error or "")[:MAX_ERROR_CHARS],
        "stopped_at": stopped_at.isoformat(),
        **summarize_turn_progress(events, turn_id, stopped_at=stopped_at),
    }
    return repo.add_event(mission_id, PROGRESS_NOTE_EVENT, payload)
