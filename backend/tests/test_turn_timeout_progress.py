from datetime import datetime, timedelta, timezone

from backend.app.adapters.base import TurnOutcome
from backend.app.api.mission_detail import build_mission_detail
from backend.app.domain.models import Conversation, MessageRole, Mission, MissionStatus, TurnStatus
from backend.app.orchestration.runner import MissionRunner
from backend.app.orchestration.turn_progress import (
    PROGRESS_NOTE_EVENT, summarize_turn_progress, timeout_reason,
)


def _notes(repo, mission_id):
    return [e for e in repo.list_events_after(mission_id, 0) if e.event_type == PROGRESS_NOTE_EVENT]


def _work_then_stop(repo, mission_id, outcome):
    """Adapter whose turn calls tools and saves an artifact, then stops with ``outcome``."""
    class Adapter:
        def run_turn(self, mission_id_, agent_id, turn_id_, provider_id, prompt, tools):
            add = lambda kind, payload: repo.add_event(mission_id, kind, {"turn_id": turn_id_, **payload})
            add("tool.call", {"tool_name": "read_context"})
            add("provider.activity", {"activity_id": "a1", "tool_name": "Bash", "phase": "started"})
            add("provider.activity", {"activity_id": "a1", "tool_name": "Bash", "phase": "completed"})
            add("provider.activity", {"activity_id": "a2", "tool_name": "Bash", "phase": "started"})
            add("artifact", {"path": "demo/index.html", "title": "index.html"})
            add("provider.activity", {"activity_id": "a3", "tool_name": "Read", "phase": "started"})
            add("turn.deadline_extended", {"reason": "not activity"})
            return outcome
    return Adapter()


def _run(repo, settings, outcome):
    repo.create_conversation(Conversation(id="c_timeout", title="timeout"))
    mission = Mission(id="m_timeout", title="demo", goal="做一个 demo", conversation_id="c_timeout",
                      target_agent_id="wangye")
    repo.create_mission(mission)
    runner = MissionRunner(repo, settings, adapter=_work_then_stop(repo, mission.id, outcome))
    return runner.run(mission.id)


def test_timeout_reason_prefers_codes_and_falls_back_to_legacy_text():
    assert timeout_reason("idle_timeout", "whatever") == "idle_timeout"
    assert timeout_reason("hard_timeout", None) == "hard_timeout"
    assert timeout_reason("authentication", "request timed out") is None
    assert timeout_reason(None, "[codex] idle timeout after 90s") == "idle_timeout"
    assert timeout_reason(None, "[claude] timeout after 600s") == "timeout"
    assert timeout_reason(None, "Antigravity turn deadline exceeded") == "timeout"
    assert timeout_reason(None, "agent did not call publish_message via MCP") is None


def test_summary_dedupes_native_activity_and_ignores_other_turns(repository):
    repository.create_mission(Mission(id="m_sum", title="s", goal="s"))
    add = lambda kind, payload: repository.add_event("m_sum", kind, payload)
    add("tool.call", {"turn_id": "t", "tool_name": "write_artifact"})
    add("provider.activity", {"turn_id": "t", "activity_id": "x", "tool_name": "Edit", "phase": "started"})
    add("provider.activity", {"turn_id": "t", "activity_id": "x", "tool_name": "Edit", "phase": "completed"})
    add("provider.activity", {"turn_id": "t", "activity_id": "y", "tool_name": "Edit", "phase": "started"})
    add("artifact", {"turn_id": "t", "path": "a.md"})
    add("artifact", {"turn_id": "t", "path": "a.md"})
    add("tool.call", {"turn_id": "other", "tool_name": "Bash"})
    events = repository.list_events_after("m_sum", 0)
    stopped = events[4].created_at.replace(tzinfo=timezone.utc) + timedelta(seconds=91)
    summary = summarize_turn_progress(events, "t", stopped_at=stopped)
    assert summary["tool_calls"] == 3
    assert summary["tools"] == [{"name": "Edit", "count": 2}, {"name": "write_artifact", "count": 1}]
    assert summary["artifacts"] == ["a.md"]
    assert summary["published"] is False
    assert summary["last_activity"]["kind"] == "artifact"
    assert summary["last_activity"]["label"] == "a.md"
    assert summary["last_activity"]["seconds_before_stop"] == 91
    empty = summarize_turn_progress(events, "missing")
    assert empty["tool_calls"] == 0 and empty["last_activity"] is None


def test_idle_timeout_leaves_host_progress_note_without_agent_or_system_message(repository, temp_settings):
    outcome = TurnOutcome(status=TurnStatus.BLOCKED, summary="[codex] idle timeout after 90s",
                          failure_code="idle_timeout")
    mission = _run(repository, temp_settings, outcome)
    assert mission.status == MissionStatus.BLOCKED

    notes = _notes(repository, mission.id)
    assert len(notes) == 1
    note = notes[0].payload
    assert note["source"] == "host" and note["reason"] == "idle_timeout" and note["attempt"] == 1
    assert note["tool_calls"] == 4
    assert note["tools"][0] == {"name": "Bash", "count": 2}
    assert note["artifacts"] == ["demo/index.html"]
    assert note["last_activity"]["label"] == "Read"

    detail = build_mission_detail(repository, temp_settings, mission.id)
    failure = detail["failure_events"][0]
    assert failure["content"] == "王也本轮超时，未发布回复。"
    progress = failure["metadata"]["host_progress"]
    assert progress["source"] == "host"
    assert progress["artifacts"] == ["demo/index.html"]
    assert progress["last_activity"]["label"] == "Read"
    # L1: the host note never becomes a message, so no agent speaks for the turn.
    assert all(m.role != MessageRole.SYSTEM for m in repository.list_messages(mission.id))
    assert not [m for m in repository.list_messages(mission.id) if m.role == MessageRole.AGENT]


def test_non_timeout_block_records_reason_and_activity(repository, temp_settings):
    outcome = TurnOutcome(status=TurnStatus.BLOCKED, summary="not logged in", failure_code="authentication")
    mission = _run(repository, temp_settings, outcome)
    assert _notes(repository, mission.id)[0].payload["reason"] == "authentication"
    failure = build_mission_detail(repository, temp_settings, mission.id)["failure_events"][0]
    progress = failure["metadata"]["host_progress"]
    assert progress["reason"] == "authentication" and progress["error"] == "not logged in"
    assert progress["tool_calls"] == 4 and progress["artifacts"] == ["demo/index.html"]
    assert failure["content"] == "王也本轮未完成，未发布回复。"


def test_snapshot_from_earlier_fallback_attempt_is_not_reused(repository, temp_settings):
    repository.create_mission(Mission(id="m_fb", title="f", goal="f", status=MissionStatus.BLOCKED))
    base = {"turn_id": "t_fb", "agent_id": "wangye"}
    repository.add_event("m_fb", "turn.blocked", {**base, "provider_id": "claude", "attempt": 1,
                                                  "error": "[claude] timeout after 600s"})
    repository.add_event("m_fb", PROGRESS_NOTE_EVENT, {**base, "attempt": 1, "source": "host",
                                                       "reason": "hard_timeout", "tool_calls": 0, "tools": [],
                                                       "artifacts": [], "published": False})
    repository.add_event("m_fb", "turn.blocked", {**base, "provider_id": "codex", "attempt": 2,
                                                  "error": "not logged in"})
    failure = build_mission_detail(repository, temp_settings, "m_fb")["failure_events"][0]
    progress = failure["metadata"]["host_progress"]
    assert progress["reason"] == "authentication" and progress["error"] == "not logged in"
    assert progress["tool_calls"] == 0 and progress["artifacts"] == []
    assert "超时" not in failure["content"]


def test_a2a_invitee_timeout_note_survives_until_mission_ends(repository, temp_settings):
    """A timed-out fire-and-forget invitee leaves the mission running; its note shows at the end."""
    repository.create_mission(Mission(id="m_a2a", title="a", goal="a", status=MissionStatus.BLOCKED))
    base = {"turn_id": "t_callee", "agent_id": "baobao", "provider_id": "claude"}
    repository.add_event("m_a2a", "provider.activity", {**base, "activity_id": "z", "tool_name": "Grep"})
    repository.add_event("m_a2a", "turn.blocked", {**base, "attempt": 1, "error": "[claude] idle timeout after 90s"})
    repository.add_event("m_a2a", PROGRESS_NOTE_EVENT, {
        **base, "attempt": 1, "source": "host", "reason": "idle_timeout", "tool_calls": 1,
        "tools": [{"name": "Grep", "count": 1}], "artifacts": [], "published": False,
        "last_activity": {"kind": "provider.activity", "label": "Grep",
                          "at": datetime.now(timezone.utc).isoformat(), "seconds_before_stop": 90},
    })
    repository.update_mission(repository.get_mission("m_a2a").model_copy(update={"status": MissionStatus.RUNNING}))
    assert build_mission_detail(repository, temp_settings, "m_a2a")["failure_events"] == []
    repository.update_mission(repository.get_mission("m_a2a").model_copy(update={"status": MissionStatus.BLOCKED}))
    failure = build_mission_detail(repository, temp_settings, "m_a2a")["failure_events"][0]
    assert failure["metadata"]["host_progress"]["reason"] == "idle_timeout"
    assert failure["metadata"]["host_progress"]["last_activity"]["seconds_before_stop"] == 90
