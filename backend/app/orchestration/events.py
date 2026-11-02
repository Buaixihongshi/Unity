import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from backend.app.storage.repository import Repository


def record_turn_started(
    repo: Repository,
    mission_id: str,
    turn_id: str,
    agent_id: str,
    provider_id: str,
    attempt: int,
    purpose: str,
    started_at: Optional[datetime] = None,
    a2a_sender_turn_id: Optional[str] = None,
) -> None:
    payload: dict = {
        "mission_id": mission_id,
        "turn_id": turn_id,
        "agent_id": agent_id,
        "provider_id": provider_id,
        "attempt": attempt,
        "purpose": purpose,
        "started_at": (started_at or datetime.now(timezone.utc)).isoformat(),
    }
    if a2a_sender_turn_id is not None:
        payload["a2a_sender_turn_id"] = a2a_sender_turn_id
    repo.add_event(mission_id, "turn.started", payload)


def record_turn_completed(
    repo: Repository,
    mission_id: str,
    turn_id: str,
    agent_id: str,
    provider_id: str,
    attempt: int,
    duration_ms: int,
    summary: str = "",
) -> None:
    repo.add_event(
        mission_id,
        "turn.completed",
        {
            "mission_id": mission_id,
            "turn_id": turn_id,
            "agent_id": agent_id,
            "provider_id": provider_id,
            "attempt": attempt,
            "duration_ms": duration_ms,
            "summary": summary,
        },
    )


def record_turn_blocked(
    repo: Repository,
    mission_id: str,
    turn_id: str,
    agent_id: str,
    provider_id: str,
    attempt: int,
    duration_ms: int,
    error: str,
    failure_code: Optional[str] = None,
) -> None:
    payload = {
        "mission_id": mission_id,
        "turn_id": turn_id,
        "agent_id": agent_id,
        "provider_id": provider_id,
        "attempt": attempt,
        "duration_ms": duration_ms,
        "error": error,
    }
    if failure_code:
        payload["failure_code"] = failure_code
    repo.add_event(mission_id, "turn.blocked", payload)


def record_turn_failed(
    repo: Repository,
    mission_id: str,
    turn_id: str,
    agent_id: str,
    provider_id: str,
    attempt: int,
    duration_ms: int,
    error: str,
) -> None:
    repo.add_event(
        mission_id,
        "turn.failed",
        {
            "mission_id": mission_id,
            "turn_id": turn_id,
            "agent_id": agent_id,
            "provider_id": provider_id,
            "attempt": attempt,
            "duration_ms": duration_ms,
            "error": error,
        },
    )


def record_tool_call(
    repo: Repository,
    mission_id: str,
    turn_id: str,
    agent_id: str,
    tool_name: str,
    tool_input: Optional[dict] = None,
    provider_id: Optional[str] = None,
) -> str:
    """Record a tool call and return its id, used to pair the later result."""
    call_id = "tc_" + uuid4().hex[:12]
    key = _pending_key(mission_id, turn_id, agent_id, tool_name)
    with _PENDING_LOCK:
        queue = _PENDING_CALLS.setdefault(key, [])
        queue.append((call_id, time.monotonic()))
        if len(queue) > _PENDING_LIMIT:
            queue.pop(0)
    sanitized_input = _sanitize_tool_input(tool_name, tool_input)
    repo.add_event(
        mission_id,
        "tool.call",
        {
            "mission_id": mission_id,
            "turn_id": turn_id,
            "agent_id": agent_id,
            "tool_name": tool_name,
            "tool_input": sanitized_input,
            "call_id": call_id,
            "provider_id": provider_id,
        },
    )
    return call_id


TOOL_OUTPUT_MAX_CHARS = 2000

# A tool call and its result are recorded by two adjacent function calls, so
# pairing them here gives every `tool.result` a stable `call_id` and a
# duration without changing any tool implementation.
_PENDING_LOCK = threading.Lock()
_PENDING_CALLS: dict[tuple[str, str, str, str], list[tuple[str, float]]] = {}
_PENDING_LIMIT = 200


def _pending_key(mission_id: str, turn_id: str, agent_id: str, tool_name: str):
    return (mission_id, turn_id, agent_id, tool_name)


def _pop_pending_call(key, call_id: Optional[str]) -> tuple[Optional[str], Optional[float]]:
    with _PENDING_LOCK:
        queue = _PENDING_CALLS.get(key) or []
        if call_id:
            for index, (pending_id, started) in enumerate(queue):
                if pending_id == call_id:
                    queue.pop(index)
                    return pending_id, started
        if queue:
            return queue.pop(0)
    return None, None

# Values that look like credentials never reach the trace. `summary` is written
# by our own code, but `output` is tool-produced text and can echo anything.
_SECRET_VALUE = re.compile(
    r"(?i)(?:Bearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{16,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\b(token|api[_-]?key|secret|password|passwd)\s*=\s*[^\s&\"']+)"
)
SECRET_KEY = re.compile(r"(authorization|apikey|token|secret|password|passwd|cookie|"
                        r"credential|privatekey|accesskey|x-api-key|bearer)", re.IGNORECASE)
ARGS_MAX_CHARS = 800


def sanitize_tool_output(output: str) -> str:
    """Mask key-shaped strings, then trim. The trace is for reading, not dumping."""
    text = _SECRET_VALUE.sub(lambda m: m.group(1) + "=***" if m.group(1) else "***", output or "")
    if len(text) <= TOOL_OUTPUT_MAX_CHARS:
        return text
    return text[: TOOL_OUTPUT_MAX_CHARS - 1] + "…"


def sanitize_tool_args(args: Optional[dict]) -> Optional[dict]:
    """A bounded, credential-free copy of a tool's arguments.

    Native CLIs echo whole command lines and absolute paths; keeping them is
    what makes a step readable, so mask the values that look secret instead of
    dropping the structure.
    """
    if not isinstance(args, dict):
        return None
    out: dict = {}
    for key, value in list(args.items())[:20]:
        name = re.sub(r"[^A-Za-z]", "", str(key))
        if SECRET_KEY.search(name) and value not in (None, ""):
            out[key] = "***"
        elif isinstance(value, str):
            out[key] = sanitize_tool_output(value)[:ARGS_MAX_CHARS]
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = sanitize_tool_output(json.dumps(value, ensure_ascii=False, default=str))[:ARGS_MAX_CHARS]
    return out


def record_tool_result(
    repo: Repository,
    mission_id: str,
    turn_id: str,
    agent_id: str,
    tool_name: str,
    summary: str = "",
    artifact_id: Optional[str] = None,
    provider_id: Optional[str] = None,
    output: Optional[str] = None,
    call_id: Optional[str] = None,
) -> None:
    payload = {
        "mission_id": mission_id,
        "turn_id": turn_id,
        "agent_id": agent_id,
        "tool_name": tool_name,
        "summary": summary,
        "artifact_id": artifact_id,
        "provider_id": provider_id,
        "ok": True,
    }
    resolved, started = _pop_pending_call(
        _pending_key(mission_id, turn_id, agent_id, tool_name), call_id)
    if resolved:
        payload["call_id"] = resolved
    if started is not None:
        payload["duration_ms"] = int((time.monotonic() - started) * 1000)
    if output:
        payload["output"] = sanitize_tool_output(output)
    repo.add_event(mission_id, "tool.result", payload)


def record_tool_error(
    repo: Repository,
    mission_id: str,
    turn_id: str,
    agent_id: str,
    tool_name: str,
    error: str,
    provider_id: Optional[str] = None,
    call_id: Optional[str] = None,
) -> None:
    payload = {
        "mission_id": mission_id,
        "turn_id": turn_id,
        "agent_id": agent_id,
        "tool_name": tool_name,
        "error": error,
        "provider_id": provider_id,
        "ok": False,
    }
    resolved, started = _pop_pending_call(
        _pending_key(mission_id, turn_id, agent_id, tool_name), call_id)
    if resolved:
        payload["call_id"] = resolved
    if started is not None:
        payload["duration_ms"] = int((time.monotonic() - started) * 1000)
    repo.add_event(mission_id, "tool.error", payload)


def _sanitize_tool_input(tool_name: str, tool_input: Optional[dict]) -> Optional[dict]:
    if tool_input is None:
        return None
    sanitized = dict(tool_input)
    if tool_name == "write_artifact":
        sanitized.pop("content", None)
    for key in list(sanitized.keys()):
        value = sanitized[key]
        if isinstance(value, str) and len(value) > 200:
            sanitized[key] = value[:200] + "...[truncated]"
    return sanitized


def record_interrupt_handled(
    repo: Repository,
    mission_id: str,
    interrupt_event_id: str,
    target_agent_id: Optional[str],
    handled_by_agent_id: str,
    turn_id: str,
) -> None:
    repo.add_event(
        mission_id,
        "interrupt.handled",
        {
            "mission_id": mission_id,
            "interrupt_event_id": interrupt_event_id,
            "target_agent_id": target_agent_id,
            "handled_by_agent_id": handled_by_agent_id,
            "turn_id": turn_id,
            "handled_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def record_a2a_invoke(
    repo: Repository,
    mission_id: str,
    sender_agent_id: str,
    sender_turn_id: str,
    target_agent_id: str,
    purpose: str,
    mode: str,
    queued_position: int,
) -> None:
    repo.add_event(
        mission_id,
        "a2a.invoke",
        {
            "mission_id": mission_id,
            "sender_agent_id": sender_agent_id,
            "sender_turn_id": sender_turn_id,
            "target_agent_id": target_agent_id,
            "purpose": purpose,
            "mode": mode,
            "queued_position": queued_position,
        },
    )


def record_a2a_skipped(
    repo: Repository,
    mission_id: str,
    sender_agent_id: str,
    sender_turn_id: str,
    target_agent_id: str,
    purpose: str,
    reason: str,
) -> None:
    """A fire-and-forget invoke that was absorbed instead of queued, because the
    target is already scheduled to speak (avoids a redundant duplicate turn)."""
    repo.add_event(
        mission_id,
        "a2a.skipped",
        {
            "mission_id": mission_id,
            "sender_agent_id": sender_agent_id,
            "sender_turn_id": sender_turn_id,
            "target_agent_id": target_agent_id,
            "purpose": purpose,
            "reason": reason,
        },
    )


def record_a2a_cancelled(
    repo: Repository,
    mission_id: str,
    pending_count: int,
    reason: str,
    cancelled_steps: Optional[list] = None,
) -> None:
    payload: dict = {
        "mission_id": mission_id,
        "pending_count": pending_count,
        "reason": reason,
    }
    if cancelled_steps is not None:
        payload["cancelled_steps"] = cancelled_steps
    repo.add_event(mission_id, "a2a.cancelled", payload)


def record_a2a_reply(
    repo: Repository,
    mission_id: str,
    *,
    callee_turn_id: str,
    callee_agent_id: str,
    invoke_event_id: str,
    sender_agent_id: str,
    sender_resume_step_id: str,
    reply_text: str,
    reply_message_id: Optional[str],
    truncated: bool,
) -> None:
    repo.add_event(
        mission_id,
        "a2a.reply",
        {
            "mission_id": mission_id,
            "turn_id": callee_turn_id,
            "agent_id": callee_agent_id,
            "invoke_event_id": invoke_event_id,
            "sender_agent_id": sender_agent_id,
            "sender_resume_step_id": sender_resume_step_id,
            "reply_text": reply_text,
            "reply_message_id": reply_message_id,
            "truncated": truncated,
        },
    )


def record_a2a_reply_failed(
    repo: Repository,
    mission_id: str,
    *,
    callee_turn_id: str,
    callee_agent_id: str,
    invoke_event_id: str,
    reason: str,
    fallback_reply_text: str,
) -> None:
    repo.add_event(
        mission_id,
        "a2a.reply.failed",
        {
            "mission_id": mission_id,
            "turn_id": callee_turn_id,
            "agent_id": callee_agent_id,
            "invoke_event_id": invoke_event_id,
            "reason": reason,
            "fallback_reply_text": fallback_reply_text,
        },
    )


def record_a2a_request(
    repo: Repository,
    mission_id: str,
    sender_agent_id: str,
    sender_turn_id: str,
    target_agent_id: str,
    purpose: str,
    mode: str,
) -> None:
    """Persists an A2A request from a subprocess MCP call.

    Written when MCPTools.invoke_agent is called without an in-memory
    a2a_enqueue callback (i.e. from a real CLI subprocess).  The runner
    polls for these events after each turn and promotes them into the
    worklist.
    """
    repo.add_event(
        mission_id,
        "a2a.request",
        {
            "mission_id": mission_id,
            "sender_agent_id": sender_agent_id,
            "sender_turn_id": sender_turn_id,
            "target_agent_id": target_agent_id,
            "purpose": purpose,
            "mode": mode,
        },
    )


def fetch_a2a_requests_for_turn(repo: Repository, mission_id: str, sender_turn_id: str) -> list:
    """Return all a2a.request events whose sender_turn_id matches the given turn."""
    return [
        event
        for event in repo.list_events_after(mission_id, 0)
        if event.event_type == "a2a.request"
        and event.payload.get("sender_turn_id") == sender_turn_id
    ]


def find_pending_interrupts(repo: Repository, mission_id: str) -> list:
    """Return interrupt events that have no matching interrupt.handled event."""
    events = repo.list_events_after(mission_id, 0)
    interrupt_ids = set()
    handled_ids = set()
    interrupt_events = []
    for event in events:
        if event.event_type == "interrupt":
            interrupt_ids.add(str(event.id))
            interrupt_events.append(event)
        elif event.event_type == "interrupt.handled":
            handled_ids.add(str(event.payload.get("interrupt_event_id", "")))
    return [e for e in interrupt_events if str(e.id) not in handled_ids]
