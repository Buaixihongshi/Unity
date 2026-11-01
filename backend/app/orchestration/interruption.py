"""Close a core mission whose run was cut short, so its conversation is free again.

An interrupted run is never replayed: native CLI effects are not transactional.
The unfinished turn is recorded as blocked, published replies stay, and the
notice names who was still scheduled to speak so the user can continue by
sending a message. Extensions recover the missions they own themselves.

Only a run whose owning process is gone counts as interrupted: tests and smoke
scripts also build the app, sometimes against a database another live backend
is serving, and must not end that backend's work.

A user pause ends a live run the same way. The request is durable (an event)
and also flagged in memory, because the adapters poll it every second while a
CLI turn runs and stop the process group when it is set.
"""
import os
import threading
from datetime import timedelta
from typing import Iterable, List, Optional
from uuid import uuid4

from backend.app import extension_points
from backend.app.domain.models import Mission, MissionStatus, utc_now
from backend.app.orchestration.events import record_turn_blocked
from backend.app.orchestration.live_progress import get_live_progress_registry
from backend.app.storage.repository import Repository

INTERRUPTED_EVENT = "mission.interrupted"
OWNER_EVENT = "mission.owner"
PAUSE_EVENT = "mission.pause_requested"
PLANNED_EVENT = "orchestration.planned"
STEP_EVENT = "orchestration.step"

REASON_LABELS = {"service_restart": "服务重启", "user_pause": "用户停止"}
STOPPED_SUMMARY = "用户停止，本回合已中止"
# Enough of an interrupted reply for the next turn to pick up from.
MAX_PARTIAL_CHARS = 1500

_TERMINAL_TURN_EVENTS = {"turn.completed", "turn.blocked", "turn.failed"}

# One per process: a restarted backend may get the old pid back, never this.
INSTANCE_ID = uuid4().hex
# A mission whose runner has not claimed it yet may still be starting.
UNCLAIMED_GRACE = timedelta(seconds=60)

_pauses: set = set()
_pauses_lock = threading.Lock()
_FIND = object()


def record_owner(repository: Repository, mission_id: str) -> None:
    repository.add_event(mission_id, OWNER_EVENT, {"pid": os.getpid(), "instance": INSTANCE_ID})


def _owner_alive(owner: dict) -> bool:
    pid = owner.get("pid")
    if not isinstance(pid, int):
        return False
    if pid == os.getpid():
        return owner.get("instance") == INSTANCE_ID
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_orphaned(repository: Repository, mission: Mission) -> bool:
    if mission.status not in {MissionStatus.PENDING, MissionStatus.RUNNING}:
        return False
    if extension_points.any('mission_owned', repository, mission):
        return False
    owners = [e.payload for e in repository.list_events_after(mission.id, 0) if e.event_type == OWNER_EVENT]
    if owners:
        return not _owner_alive(owners[-1])
    return utc_now() - mission.updated_at > UNCLAIMED_GRACE


def request_pause(repository: Repository, mission: Mission) -> None:
    """Ask the runner to stop now; a run with no live owner is closed right here."""
    if pause_requested(mission.id, repository):
        return
    partial = "\n".join(get_live_progress_registry().snapshot_for_mission(mission.id).values())
    repository.add_event(mission.id, PAUSE_EVENT, {"partial_text": partial[-MAX_PARTIAL_CHARS:]})
    with _pauses_lock:
        _pauses.add(mission.id)
    if is_orphaned(repository, repository.get_mission(mission.id)):
        close_interrupted(repository, repository.get_mission(mission.id), "user_pause")


def pause_requested(mission_id: str, repository: Optional[Repository] = None) -> bool:
    """Cheap in-memory check for the adapters' poll; pass the repository for the durable one."""
    if mission_id in _pauses:
        return True
    return repository is not None and any(
        e.event_type == PAUSE_EVENT for e in repository.list_events_after(mission_id, 0))


def record_plan(repository: Repository, mission_id: str, steps) -> None:
    repository.add_event(mission_id, PLANNED_EVENT, {
        "steps": [{"agent_id": s.agent_id, "is_closing": s.is_closing} for s in steps],
    })


def record_step(repository: Repository, mission_id: str, index: int, turn_id: str) -> None:
    repository.add_event(mission_id, STEP_EVENT, {"index": index, "turn_id": turn_id})


def unfinished_turn(events) -> Optional[dict]:
    """The latest attempt that started and never reached a terminal event."""
    open_attempts = {}
    for event in events:
        turn_id = event.payload.get("turn_id")
        if not turn_id:
            continue
        if event.event_type == "turn.started":
            open_attempts[turn_id] = event.payload
        elif event.event_type in _TERMINAL_TURN_EVENTS:
            started = open_attempts.get(turn_id)
            if started and (event.payload.get("attempt") or 1) >= (started.get("attempt") or 1):
                del open_attempts[turn_id]
    return list(open_attempts.values())[-1] if open_attempts else None


def remaining_agents(events) -> List[str]:
    """Agents of the declared workflow that had not been given a turn yet."""
    steps: list = []
    claimed = -1
    for event in events:
        if event.event_type == PLANNED_EVENT:
            steps, claimed = event.payload.get("steps") or [], -1
        elif event.event_type == STEP_EVENT:
            claimed = max(claimed, int(event.payload.get("index", -1)))
    return list(dict.fromkeys(s["agent_id"] for s in steps[claimed + 1:] if s.get("agent_id")))


def close_interrupted(repository: Repository, mission: Mission, reason: str, *, turn=_FIND) -> Mission:
    """End the run as interrupted. ``turn`` is the stopped turn when the runner
    already recorded its outcome; by default an unfinished turn is looked up and
    recorded as blocked here."""
    events = repository.list_events_after(mission.id, 0)
    remaining = remaining_agents(events)
    label = REASON_LABELS.get(reason, reason)
    if turn is _FIND:
        turn = unfinished_turn(events)
        if turn is not None:
            record_turn_blocked(
                repository, mission.id, turn["turn_id"], turn.get("agent_id"), turn.get("provider_id"),
                turn.get("attempt") or 1, 0, "{}，本回合已中断，未自动重放".format(label),
            )
    pauses = [e.payload for e in events if e.event_type == PAUSE_EVENT]
    repository.add_event(mission.id, INTERRUPTED_EVENT, {
        "reason": reason,
        "turn_id": turn["turn_id"] if turn else None,
        "agent_id": turn.get("agent_id") if turn else None,
        "remaining_agent_ids": remaining,
        "partial_text": pauses[-1].get("partial_text", "") if pauses and turn else "",
    })
    with _pauses_lock:
        _pauses.discard(mission.id)
    closed = mission.model_copy(update={"status": MissionStatus.BLOCKED, "updated_at": utc_now()})
    repository.update_mission(closed)
    repository.add_event(mission.id, "status", {
        "mission_id": mission.id,
        "status": MissionStatus.BLOCKED.value,
        "summary": "{}中断；已发布的回复已保留，可直接发消息继续".format(label),
    })
    return closed


def recover(repository: Repository, missions: Optional[Iterable[Mission]] = None) -> None:
    """End core runs whose process is gone: at startup, and again whenever a
    conversation is found occupied, so a missed sweep never locks it for good."""
    for mission in repository.list_missions() if missions is None else missions:
        if is_orphaned(repository, mission):
            close_interrupted(repository, mission, "service_restart")
