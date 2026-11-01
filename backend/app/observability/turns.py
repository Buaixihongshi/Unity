"""Turn reconstruction: the observability unit is one turn, not one mission.

Every agent turn already leaves a trail of events (`turn.started`, `tool.call`,
`tool.result`, `provider.activity`, `a2a.invoke`, `artifact`, `turn.usage`,
`turn.completed`). This module folds that trail, plus the turn's reply, into
one object the UI and the checks can both read.

Design rules (borrowed from waku-agent's tracer):

- **Pure.** `build_turns` takes plain domain objects and returns plain dicts.
  No HTTP, no database, no clock. The API and the tests call the same code.
- **Read-time.** Nothing here is persisted. Restart the server, change a check,
  and every historical turn is re-described on the next read.
- **Honest about gaps.** A tool result with no stored output keeps
  `output=""`; the UI shows "未记录" rather than inventing a summary. A turn
  with no `turn.completed` is `unfinished`, not failed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from backend.app.adapters.deliberation_errors import failure_code
from backend.app.adapters.failure_text import is_informative
from backend.app.domain.models import Message, MessageRole, Mission, MissionEvent
from backend.app.observability import pricing

# Step kinds. Kept small on purpose: these are what a reader can point at.
TOOL = "tool"
NATIVE = "native"
A2A = "a2a"
ARTIFACT = "artifact"

STATUS_RUNNING = "running"


def _ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _event_id(event: MissionEvent) -> str:
    return str(event.id)


def _sort_key(event: MissionEvent) -> tuple:
    """Chronological, with the insertion order as the tie-break.

    Numeric ids sort first because SQLite hands them out monotonically; a
    non-numeric id (a fixture) falls back to its timestamp.
    """
    try:
        return (0, int(event.id), "")
    except (TypeError, ValueError):
        return (1, 0, event.created_at.isoformat())


@dataclass
class Step:
    id: str
    kind: str
    tool: str
    ts: str
    call_id: str = ""
    ok: bool = True
    error: str = ""
    args: Optional[dict] = None
    output: str = ""
    summary: str = ""
    duration_ms: Optional[int] = None
    phase: Optional[str] = None
    agent_id: str = ""
    provider_id: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "tool": self.tool,
            "ts": self.ts,
            "call_id": self.call_id,
            "ok": self.ok,
            "error": self.error,
            "args": self.args,
            "output": self.output,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "phase": self.phase,
            "agent_id": self.agent_id,
            "provider_id": self.provider_id,
            "source": self.source,
        }


@dataclass
class Turn:
    turn_id: str
    mission_id: str
    mission_title: str
    index: int
    agent_id: str
    provider_id: str
    status: str
    model: str = ""
    session_id: str = ""
    purpose: str = ""
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: int = 0
    reply: str = ""
    error: str = ""
    failure_code: str = ""  # what the adapter itself reported, when it knew
    steps: list[Step] = field(default_factory=list)
    usage: Optional[dict] = None
    cost_usd: Optional[float] = None
    cost_source: Optional[str] = None
    checks: list[dict] = field(default_factory=list)

    # ---- derived facts the checks read -------------------------------------
    @property
    def tools(self) -> list[Step]:
        return [s for s in self.steps if s.kind in (TOOL, NATIVE)]

    @property
    def a2a(self) -> list[Step]:
        return [s for s in self.steps if s.kind == A2A]

    @property
    def artifacts(self) -> list[Step]:
        return [s for s in self.steps if s.kind == ARTIFACT]

    @property
    def failed_tools(self) -> list[Step]:
        return [s for s in self.tools if not s.ok]

    @property
    def native_visible(self) -> bool:
        return any(s.kind == NATIVE for s in self.steps)

    @property
    def failure_class(self) -> Optional[str]:
        """Why a blocked/failed turn ended, as a fixed vocabulary; None otherwise.

        Read time, like the checks: improving the classifier reclassifies
        history. A reason that is only an exit code or startup noise is
        "unknown" rather than a guess.
        """
        if self.status not in ("blocked", "failed"):
            return None
        if self.failure_code:
            return self.failure_code
        if not is_informative(self.error):
            return "unknown"
        return failure_code(self.error)

    @property
    def cost_known(self) -> bool:
        return self.cost_usd is not None

    def to_dict(self, *, agent_name: str = "") -> dict:
        return {
            "turn_id": self.turn_id,
            "mission_id": self.mission_id,
            "mission_title": self.mission_title,
            "index": self.index,
            "agent_id": self.agent_id,
            "agent_name": agent_name or self.agent_id,
            "provider_id": self.provider_id,
            "model": self.model,
            "session_id": self.session_id,
            "status": self.status,
            "purpose": self.purpose,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "reply": self.reply,
            "error": self.error,
            "failure_class": self.failure_class,
            "steps": [step.to_dict() for step in self.steps],
            "usage": self.usage,
            "tokens": pricing.total_tokens(self.usage),
            "cost_usd": self.cost_usd,
            "cost_source": self.cost_source,
            "checks": self.checks,
            "coverage": {
                "has_reply": bool(self.reply.strip()),
                "native_visible": self.native_visible,
                "tool_calls": len(self.tools),
                "tool_outputs": sum(1 for s in self.tools if s.output),
                "cost_known": self.cost_known,
            },
        }


def _turn_seed(turn_id: str, mission: Mission, payload: dict) -> Turn:
    started = _ts(payload.get("started_at")) or _ts(payload.get("created_at"))
    return Turn(
        turn_id=turn_id,
        mission_id=mission.id,
        mission_title=mission.title or mission.goal,
        index=0,
        agent_id=str(payload.get("agent_id") or ""),
        provider_id=str(payload.get("provider_id") or ""),
        status=STATUS_RUNNING,
        purpose=str(payload.get("purpose") or ""),
        started_at=_iso(started),
    )


def _pair_tools(
    turn: Turn,
    event: MissionEvent,
    payload: dict,
    open_calls: dict[str, list[Step]],
) -> None:
    """A `tool.result` completes the oldest open call with the same name.

    The event stream carries no call id for MCP tools, so the pairing rule is
    "same turn, same tool, first unmatched". That matches how the tool layer
    emits them: one call, then one result or one error. An unmatched result is
    kept as an `orphan` step rather than dropped — losing evidence is worse
    than showing an odd row.
    """
    name = str(payload.get("tool_name") or "")
    if event.event_type == "tool.call":
        step = Step(
            id=_event_id(event),
            kind=TOOL,
            tool=name,
            ts=event.created_at.isoformat(),
            call_id=str(payload.get("call_id") or ""),
            args=payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else None,
            agent_id=str(payload.get("agent_id") or ""),
            provider_id=str(payload.get("provider_id") or ""),
            source="mcp",
        )
        turn.steps.append(step)
        open_calls.setdefault(name, []).append(step)
        return

    call_id = str(payload.get("call_id") or "")
    step = None
    if call_id:
        step = next((candidate for candidate in turn.steps
                     if candidate.kind == TOOL and candidate.call_id == call_id), None)
        if step is not None:
            queue = open_calls.get(name) or []
            if step in queue:
                queue.remove(step)
    if step is None:
        queue = open_calls.get(name) or []
        if queue:
            step = queue.pop(0)
    if step is not None:
        step.phase = "completed" if event.event_type == "tool.result" else "failed"
        step.ok = bool(payload.get("ok", event.event_type == "tool.result"))
        step.error = "" if step.ok else str(payload.get("error") or "")
        step.output = str(payload.get("output") or "")
        step.summary = str(payload.get("summary") or "")
        duration = payload.get("duration_ms")
        if isinstance(duration, int):
            step.duration_ms = duration
        return

    turn.steps.append(Step(
        id=_event_id(event),
        kind=TOOL,
        tool=name,
        ts=event.created_at.isoformat(),
        ok=event.event_type != "tool.error",
        error=str(payload.get("error") or ""),
        output=str(payload.get("output") or ""),
        summary=str(payload.get("summary") or ""),
        phase="orphan",
        agent_id=str(payload.get("agent_id") or ""),
        provider_id=str(payload.get("provider_id") or ""),
        source="mcp",
    ))


def _append_native(turn: Turn, event: MissionEvent, payload: dict) -> None:
    """One step per native call, merged across started / completed / result.

    A CLI reports the same call two or three times (started, completed, and for
    claude a result carrying the output). The list should read like calls, not
    like protocol frames, so merge on `activity_id` and keep the newest detail.
    """
    activity_id = str(payload.get("activity_id") or "")
    step = next((candidate for candidate in turn.steps
                 if candidate.kind == NATIVE and candidate.call_id == activity_id), None)
    if step is None:
        step = Step(
            id=_event_id(event),
            kind=NATIVE,
            tool=str(payload.get("tool_name") or ""),
            ts=event.created_at.isoformat(),
            call_id=activity_id,
            agent_id=str(payload.get("agent_id") or ""),
            provider_id=str(payload.get("provider_id") or ""),
            source="provider",
        )
        turn.steps.append(step)
    if payload.get("summary"):
        step.summary = str(payload["summary"])
    if isinstance(payload.get("args"), dict):
        step.args = payload["args"]
    if payload.get("output"):
        step.output = str(payload["output"])
    if payload.get("error"):
        step.error = str(payload["error"])
    if isinstance(payload.get("duration_ms"), int):
        step.duration_ms = payload["duration_ms"]
    if isinstance(payload.get("ok"), bool):
        step.ok = payload["ok"]
    phase = str(payload.get("phase") or "")
    if phase:
        step.phase = phase


def _append_a2a(turn: Turn, event: MissionEvent, payload: dict) -> None:
    kind = event.event_type.split(".", 1)[-1]  # invoke / skipped / reply
    target = payload.get("target_agent_id") or payload.get("sender_agent_id") or ""
    ok = kind == "invoke" or (kind == "reply" and not payload.get("failed"))
    if kind == "skipped":
        ok = False
    turn.steps.append(Step(
        id=_event_id(event),
        kind=A2A,
        tool=kind,
        ts=event.created_at.isoformat(),
        ok=ok,
        error=str(payload.get("reason") or "") if kind == "skipped" else "",
        output=str(payload.get("purpose") or ""),
        args={"target": str(target)} if target else None,
        agent_id=str(payload.get("agent_id") or payload.get("sender_agent_id") or ""),
        provider_id=str(payload.get("provider_id") or ""),
        source="a2a",
    ))


def build_turns(
    events: Iterable[MissionEvent],
    messages: Iterable[Message],
    mission: Mission,
) -> list[Turn]:
    """Every turn in one mission, oldest first. Turns without a start are dropped.

    An event that names a `turn_id` but has no `turn.started` (a fixture, or a
    turn whose start was lost) still gets a turn: losing the start must not
    hide the work that followed.
    """
    turns: dict[str, Turn] = {}
    order: list[str] = []
    open_tools: dict[str, dict[str, list[Step]]] = {}

    def turn_for(turn_id: str, payload: dict) -> Turn:
        turn = turns.get(turn_id)
        if turn is None:
            turn = _turn_seed(turn_id, mission, payload)
            turns[turn_id] = turn
            order.append(turn_id)
        return turn

    for event in sorted(events, key=_sort_key):
        payload = event.payload or {}
        kind = event.event_type
        if kind == "turn.started":
            turn_id = str(payload.get("turn_id") or "")
            if not turn_id:
                continue
            turn = turn_for(turn_id, payload)
            turn.agent_id = str(payload.get("agent_id") or turn.agent_id)
            turn.provider_id = str(payload.get("provider_id") or turn.provider_id)
            turn.purpose = str(payload.get("purpose") or turn.purpose)
            started = _ts(payload.get("started_at")) or event.created_at
            turn.started_at = _iso(started)
            continue

        turn_id = str(payload.get("turn_id") or payload.get("sender_turn_id") or "")
        if not turn_id:
            continue

        if kind in ("turn.completed", "turn.blocked", "turn.failed"):
            turn = turn_for(turn_id, payload)
            turn.status = kind.split(".", 1)[1]
            turn.ended_at = event.created_at.isoformat()
            duration = payload.get("duration_ms")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                turn.duration_ms = int(duration)
            turn.error = str(payload.get("error") or "")
            turn.failure_code = str(payload.get("failure_code") or "")
        elif kind == "provider.session":
            turn = turn_for(turn_id, payload)
            model = payload.get("model")
            session_id = payload.get("session_id")
            if isinstance(model, str) and model:
                turn.model = model
            if isinstance(session_id, str) and session_id:
                turn.session_id = session_id
        elif kind == "turn.usage":
            turn = turn_for(turn_id, payload)
            usage = payload.get("usage")
            if isinstance(usage, dict):
                turn.usage = usage
            turn.agent_id = str(payload.get("agent_id") or turn.agent_id)
            turn.provider_id = str(payload.get("provider_id") or turn.provider_id)
        elif kind in ("tool.call", "tool.result", "tool.error"):
            turn = turn_for(turn_id, payload)
            _pair_tools(turn, event, payload, open_tools.setdefault(turn_id, {}))
        elif kind == "provider.activity":
            turn = turn_for(turn_id, payload)
            _append_native(turn, event, payload)
        elif kind in ("a2a.invoke", "a2a.skipped", "a2a.reply"):
            turn = turn_for(turn_id, payload)
            _append_a2a(turn, event, payload)
        elif kind == "artifact":
            turn = turn_for(turn_id, payload)
            turn.steps.append(Step(
                id=_event_id(event),
                kind=ARTIFACT,
                tool="artifact",
                ts=event.created_at.isoformat(),
                output=str(payload.get("title") or payload.get("path") or ""),
                args={"path": payload.get("path")} if payload.get("path") else None,
                agent_id=str(payload.get("agent_id") or ""),
                provider_id=str(payload.get("provider_id") or ""),
                source="artifact",
            ))

    replies: dict[str, str] = {}
    for message in messages:
        if message.role is MessageRole.AGENT and message.turn_id:
            replies[message.turn_id] = message.content or ""
    for turn_id, reply in replies.items():
        if turn_id in turns:
            turns[turn_id].reply = reply

    built = [turns[turn_id] for turn_id in order]
    for index, turn in enumerate(built, start=1):
        turn.index = index
        if not turn.duration_ms and turn.started_at and turn.ended_at:
            start, end = _ts(turn.started_at), _ts(turn.ended_at)
            if start and end:
                turn.duration_ms = max(int((end - start).total_seconds() * 1000), 0)
        turn.cost_usd, turn.cost_source = pricing.turn_cost(turn.provider_id, turn.usage)
    return built
