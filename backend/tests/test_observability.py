"""Tests for the turn-level observability layer.

The point of these tests is the contract, not the wording: a turn is folded
from events in order, a cost prefers the provider's own number, and a check
fails only on a contradiction it can point at.
"""
from __future__ import annotations

import pytest

from backend.app.domain.models import Message, MessageRole, Mission, MissionEvent
from backend.app.observability import checks, pricing
from backend.app.observability.service import build_payload, collect_turns
from backend.app.observability.turns import A2A, ARTIFACT, NATIVE, TOOL, Turn, Step, build_turns


def mission() -> Mission:
    return Mission(id="m_1", title="测试任务", goal="测试任务")


def event(event_id: int, kind: str, **payload) -> MissionEvent:
    return MissionEvent(id=str(event_id), mission_id="m_1", event_type=kind, payload=payload)


def message(turn_id: str, content: str) -> Message:
    return Message(id=f"msg_{turn_id}", mission_id="m_1", role=MessageRole.AGENT,
                   content=content, turn_id=turn_id, agent_id="chulan")


# ---------------------------------------------------------------------------
# turns
# ---------------------------------------------------------------------------

def test_build_turns_pairs_calls_results_and_errors():
    events = [
        event(1, "turn.started", turn_id="t1", agent_id="chulan", provider_id="codex",
              purpose="实现", started_at="2026-10-20T10:00:00+00:00"),
        event(2, "tool.call", turn_id="t1", agent_id="chulan", tool_name="read_context", tool_input={}),
        event(3, "tool.result", turn_id="t1", agent_id="chulan", tool_name="read_context",
              summary="Returned context"),
        event(4, "tool.call", turn_id="t1", agent_id="chulan", tool_name="write_artifact",
              tool_input={"relative_path": "a.md"}),
        event(5, "tool.error", turn_id="t1", agent_id="chulan", tool_name="write_artifact",
              error="path outside"),
        event(6, "turn.completed", turn_id="t1", agent_id="chulan", duration_ms=1234),
    ]
    turns = build_turns(events, [message("t1", "做完了")], mission())

    assert len(turns) == 1
    turn = turns[0]
    assert turn.status == "completed"
    assert turn.duration_ms == 1234
    assert turn.reply == "做完了"
    assert [step.tool for step in turn.steps] == ["read_context", "write_artifact"]
    assert turn.steps[0].ok is True and turn.steps[0].summary == "Returned context"
    assert turn.steps[1].ok is False and turn.steps[1].error == "path outside"
    assert turn.steps[1].args == {"relative_path": "a.md"}


def test_build_turns_keeps_unfinished_turn_as_running():
    events = [event(1, "turn.started", turn_id="t1", agent_id="baobao", provider_id="claude")]
    turns = build_turns(events, [], mission())
    assert turns[0].status == "running"
    assert turns[0].ended_at is None


def test_build_turns_orders_events_by_id_even_when_input_is_shuffled():
    events = [
        event(3, "tool.result", turn_id="t1", tool_name="x", summary="done"),
        event(1, "turn.started", turn_id="t1", agent_id="a", provider_id="codex"),
        event(2, "tool.call", turn_id="t1", tool_name="x", tool_input={}),
        event(4, "turn.completed", turn_id="t1", duration_ms=5),
    ]
    turn = build_turns(events, [], mission())[0]
    assert [s.tool for s in turn.steps] == ["x"]
    assert turn.steps[0].ok


def test_native_activity_and_a2a_are_steps_not_tool_calls():
    events = [
        event(1, "turn.started", turn_id="t1", agent_id="wangye", provider_id="antigravity"),
        event(2, "provider.activity", turn_id="t1", agent_id="wangye", provider_id="antigravity",
              tool_name="view_file", phase="completed", summary="读取文件 · app.py"),
        event(3, "a2a.skipped", sender_turn_id="t1", target_agent_id="baobao", reason="already scheduled"),
        event(4, "artifact", turn_id="t1", agent_id="wangye", title="方案.md", path="方案.md"),
    ]
    turn = build_turns(events, [], mission())[0]
    kinds = [s.kind for s in turn.steps]
    assert kinds == [NATIVE, A2A, ARTIFACT]
    assert turn.native_visible is True
    assert turn.a2a[0].tool == "skipped" and turn.a2a[0].ok is False
    assert turn.artifacts[0].output == "方案.md"


def test_orphan_result_is_kept():
    events = [
        event(1, "turn.started", turn_id="t1", agent_id="a", provider_id="codex"),
        event(2, "tool.result", turn_id="t1", tool_name="read_artifact", summary="page"),
    ]
    turn = build_turns(events, [], mission())[0]
    assert turn.steps[0].phase == "orphan"
    assert turn.steps[0].summary == "page"


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------

def test_provider_reported_cost_wins_over_estimate():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "total_cost_usd": 0.42}
    usd, source = pricing.turn_cost("claude", usage)
    assert (usd, source) == (0.42, "provider")


def test_estimate_prices_cached_tokens_cheaper():
    fresh = pricing.estimate_cost("claude", {"input_tokens": 1_000_000, "output_tokens": 0})
    cached = pricing.estimate_cost("claude", {"cached_input_tokens": 1_000_000, "output_tokens": 0})
    assert fresh == pytest.approx(3.0)
    assert cached == pytest.approx(0.3)


def test_unknown_provider_has_no_cost_not_zero():
    assert pricing.turn_cost("mystery", {"input_tokens": 10, "output_tokens": 5}) == (None, None)
    assert pricing.estimate_cost("mystery", {"input_tokens": 10}) is None


def test_total_tokens_counts_cache_and_reasoning():
    usage = {"input_tokens": 10, "cached_input_tokens": 90, "output_tokens": 5,
             "reasoning_output_tokens": 3}
    assert pricing.total_tokens(usage) == 108


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def turn_with(reply: str = "", status: str = "completed", steps: list[Step] | None = None) -> Turn:
    turn = Turn(turn_id="t1", mission_id="m_1", mission_title="任务", index=1,
                agent_id="chulan", provider_id="codex", status=status, reply=reply)
    turn.steps = steps or []
    return turn


def test_claims_fail_when_delegation_was_actually_absorbed():
    turn = turn_with("我已经让冯宝宝复核过了。",
                     steps=[Step(id="1", kind=A2A, tool="skipped", ts="", error="already scheduled")])
    result = checks.check_claims_match_events(turn)
    assert result["value"] == checks.FAIL
    assert "skipped" in result["note"]


def test_claims_pass_when_delegation_has_an_invoke():
    turn = turn_with("我已经让冯宝宝复核过了。", steps=[Step(id="1", kind=A2A, tool="invoke", ts="")])
    assert checks.check_claims_match_events(turn)["value"] == checks.PASS


def test_claims_are_na_when_the_trace_cannot_settle_them():
    turn = turn_with("我核对了代码，结论是接口没问题。")
    assert checks.check_claims_match_events(turn)["value"] == checks.NA


def test_claims_ignore_plans_and_denials():
    turn = turn_with("建议下一步让冯宝宝复核。我没有写入任何文件。")
    assert checks.check_claims_match_events(turn)["value"] == checks.NA


def test_claims_fail_when_reply_says_done_but_turn_blocked():
    turn = turn_with("已经修复好了。", status="blocked")
    result = checks.check_claims_match_events(turn)
    assert result["value"] == checks.FAIL and "blocked" in result["note"]


def test_tool_errors_fail_when_neither_retried_nor_mentioned():
    turn = turn_with("都做完了。", steps=[
        Step(id="1", kind=TOOL, tool="write_artifact", ts="", ok=False, error="boom"),
    ])
    assert checks.check_tool_errors_handled(turn)["value"] == checks.FAIL


def test_tool_errors_pass_when_retried_in_the_same_turn():
    turn = turn_with("", steps=[
        Step(id="1", kind=TOOL, tool="write_artifact", ts="", ok=False, error="boom"),
        Step(id="2", kind=TOOL, tool="write_artifact", ts="", ok=True),
    ])
    assert checks.check_tool_errors_handled(turn)["value"] == checks.PASS


def test_tool_errors_pass_when_the_reply_names_the_tool():
    turn = turn_with("write_artifact 失败了，路径越界。", steps=[
        Step(id="1", kind=TOOL, tool="write_artifact", ts="", ok=False, error="boom"),
    ])
    assert checks.check_tool_errors_handled(turn)["value"] == checks.PASS


def test_blocked_turn_must_explain_itself():
    assert checks.check_blocked_explained(turn_with("", status="blocked"))["value"] == checks.FAIL
    assert checks.check_blocked_explained(turn_with("CLI 超时了", status="blocked"))["value"] == checks.PASS
    assert checks.check_blocked_explained(turn_with("", status="completed"))["value"] == checks.NA


def test_run_checks_never_raises(monkeypatch):
    def boom(_turn):
        raise RuntimeError("bad check")

    monkeypatch.setitem(checks.CHECKS, "claims_match_events", boom)
    result = {c["name"]: c for c in checks.run_checks(turn_with("hi"))}
    assert result["claims_match_events"] == {"name": "claims_match_events", "source": "code",
                                             "layer": "agent", "value": None, "note": "check error"}


# ---------------------------------------------------------------------------
# service + API
# ---------------------------------------------------------------------------

def _seed_mission(repository, mission_id: str = "m_1") -> Mission:
    target = Mission(id=mission_id, title="观测测试", goal="观测测试")
    repository.create_mission(target)
    return target


def test_payload_covers_checks_coverage_and_spend(repository):
    mission = _seed_mission(repository)
    repository.add_event(mission.id, "turn.started", {
        "turn_id": "t1", "agent_id": "chulan", "provider_id": "claude",
        "started_at": "2026-10-20T10:00:00+00:00"})
    repository.add_event(mission.id, "tool.call", {"turn_id": "t1", "agent_id": "chulan",
                                                   "tool_name": "publish_message"})
    repository.add_event(mission.id, "tool.result", {"turn_id": "t1", "agent_id": "chulan",
                                                     "tool_name": "publish_message", "summary": "ok"})
    repository.add_event(mission.id, "turn.usage", {
        "turn_id": "t1", "agent_id": "chulan", "provider_id": "claude",
        "usage": {"input_tokens": 1000, "output_tokens": 100, "total_cost_usd": 0.02}})
    repository.add_event(mission.id, "turn.completed", {"turn_id": "t1", "duration_ms": 900})
    repository.add_message(Message(id="msg_1", mission_id=mission.id, role=MessageRole.AGENT,
                                   content="已完成。", turn_id="t1", agent_id="chulan"))

    payload = build_payload(repository, mission_id=mission.id)

    assert payload["scope"]["turns"] == 1
    turn = payload["turns"][0]
    assert turn["agent_name"] == "张楚岚"
    assert turn["cost_usd"] == 0.02 and turn["cost_source"] == "provider"
    assert turn["coverage"]["cost_known"] is True
    assert turn["coverage"]["tool_calls"] == 1
    assert payload["summary"]["spend"]["reported_usd"] == 0.02
    assert payload["summary"]["checks"]["turns"] == 1


def test_api_returns_payload_and_404(temp_settings):
    from fastapi.testclient import TestClient

    from backend.app.main import create_app

    client = TestClient(create_app(temp_settings))
    response = client.get("/api/observability", params={"mission_id": "missing"})
    assert response.status_code == 404

    response = client.get("/api/observability", params={"limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert {"scope", "summary", "turns"} <= set(body)


# ---------------------------------------------------------------------------
# tool output capture (G1)
# ---------------------------------------------------------------------------

def test_record_tool_result_keeps_output_trimmed_and_masked(repository):
    from backend.app.orchestration.events import TOOL_OUTPUT_MAX_CHARS, record_tool_result

    record_tool_result(repository, "m_1", "t1", "chulan", "read_artifact",
                       summary="Read a.md", output="key sk-live-1234567890 and " + "x" * 4000)
    event = repository.list_events_after("m_1", 0)[-1]

    assert event.event_type == "tool.result"
    assert "sk-live-1234567890" not in event.payload["output"]
    assert "***" in event.payload["output"]
    assert len(event.payload["output"]) == TOOL_OUTPUT_MAX_CHARS
    assert event.payload["summary"] == "Read a.md"


def test_record_tool_result_without_output_keeps_payload_small(repository):
    from backend.app.orchestration.events import record_tool_result

    record_tool_result(repository, "m_1", "t1", "chulan", "publish_message", summary="Published")
    assert "output" not in repository.list_events_after("m_1", 0)[-1].payload


def test_artifact_io_lands_in_the_trace_and_in_the_turn(repository, temp_settings):
    """G1: a tool's output is what makes 'was the reply faithful' answerable."""
    from backend.app.mcp.tools import MCPTools

    repository.create_mission(Mission(id="m_art", title="产物任务", goal="写一个方案"))
    tools = MCPTools(repository, temp_settings.artifact_root)

    tools.write_artifact("m_art", "chulan", "t1", "notes.md", "# 结论\n细节" * 400)
    tools.read_artifact("m_art", "chulan", "t1", "notes.md")

    turns = collect_turns(repository, mission_id="m_art")
    assert len(turns) == 1
    steps = {step.tool: step for step in turns[0].steps}

    assert "sha256:" in steps["write_artifact"].output
    assert steps["read_artifact"].output.startswith("# 结论")
    assert len(steps["read_artifact"].output) <= 2000
    assert turns[0].to_dict()["coverage"]["tool_outputs"] == 2


# ---------------------------------------------------------------------------
# frozen v2 context: the agent's real input belongs in the trace
# ---------------------------------------------------------------------------

def _v2_mission() -> Mission:
    from backend.app.domain.models import DiscussionConfig, RoomMode

    return Mission(
        id="m_v2", title="讨论", goal="设计咱们的记忆",
        room_mode=RoomMode.ROUNDTABLE,
        discussion=DiscussionConfig(participant_ids=["baobao"], max_turns=4, protocol="deliberation_v2"),
    )


def test_v2_read_context_records_a_readable_digest(repository, temp_settings, monkeypatch):
    from backend.app.mcp.tools import MCPTools
    from backend.app.extensions.discussion.deliberation import Deliberation

    repository.create_mission(_v2_mission())
    view = {
        "phase": "independent", "round_index": 0,
        "snapshot": {"goal": "设计咱们的记忆",
                     "materials": {"artifact:m:a.md": {"path": "a.md"}},
                     "history_count": 7, "messages": []},
        "results": [{"id": "r1", "agent_id": "baobao", "kind": "opinion",
                     "data": {"position_summary": "先明确写入与召回边界。"}}],
        "issues": {},
    }
    monkeypatch.setattr(Deliberation, "read_context", lambda self, *a, **k: view)

    tools = MCPTools(repository, temp_settings.artifact_root, lease_token="lease")
    tools.read_context("m_v2", agent_id="baobao", turn_id="t1", provider_id="claude")

    payload = repository.list_events_after("m_v2", 0)[-1].payload
    assert payload["tool_name"] == "read_context"
    assert "phase=independent" in payload["output"]
    assert "任务：设计咱们的记忆" in payload["output"]
    assert "baobao/opinion" in payload["output"]
    assert "先明确写入与召回边界" in payload["output"]
    assert "材料：a.md" in payload["output"]


def test_v2_read_artifact_records_the_material_body(repository, temp_settings, monkeypatch):
    from backend.app.mcp.tools import MCPTools
    from backend.app.extensions.discussion.deliberation import Deliberation

    repository.create_mission(_v2_mission())
    monkeypatch.setattr(Deliberation, "read_material",
                        lambda self, *a, **k: {"content": "# 记忆设计\n召回与写入分界", "offset": 0})

    tools = MCPTools(repository, temp_settings.artifact_root, lease_token="lease")
    tools.read_artifact("m_v2", "baobao", "t1", "note.md", provider_id="claude")

    events = [e for e in repository.list_events_after("m_v2", 0)
              if e.event_type in ("tool.call", "tool.result")]
    assert [e.event_type for e in events] == ["tool.call", "tool.result"]
    assert events[1].payload["output"] == "# 记忆设计\n召回与写入分界"


def test_tool_events_share_a_call_id_and_duration(repository):
    from backend.app.orchestration.events import record_tool_call, record_tool_result

    call_id = record_tool_call(repository, "m_1", "t1", "chulan", "read_context", {"limit": 20})
    record_tool_result(repository, "m_1", "t1", "chulan", "read_context",
                       summary="Returned", output="body")

    events = [e for e in repository.list_events_after("m_1", 0)
              if e.event_type in ("tool.call", "tool.result")]
    assert events[0].payload["call_id"] == call_id
    assert events[1].payload["call_id"] == call_id
    assert events[1].payload["ok"] is True
    assert isinstance(events[1].payload["duration_ms"], int)


def test_turns_pair_steps_by_call_id_even_when_results_reorder():
    from backend.app.observability.turns import build_turns

    events = [
        event(1, "turn.started", turn_id="t1", agent_id="chulan", provider_id="claude"),
        event(2, "tool.call", turn_id="t1", tool_name="read_context", call_id="tc_a"),
        event(3, "tool.call", turn_id="t1", tool_name="read_context", call_id="tc_b"),
        event(4, "tool.result", turn_id="t1", tool_name="read_context", call_id="tc_b", output="B"),
        event(5, "tool.result", turn_id="t1", tool_name="read_context", call_id="tc_a", output="A"),
    ]
    steps = build_turns(events, [], mission())[0].steps
    assert [(step.call_id, step.output) for step in steps] == [("tc_a", "A"), ("tc_b", "B")]


def test_native_phases_merge_into_one_step_with_output_and_ok():
    from backend.app.observability.turns import build_turns

    events = [
        event(1, "turn.started", turn_id="t1", agent_id="chulan", provider_id="claude"),
        event(2, "provider.activity", turn_id="t1", activity_id="call_1", tool_name="Bash",
              phase="started", summary=None),
        event(3, "provider.activity", turn_id="t1", activity_id="call_1", tool_name="Bash",
              phase="completed", summary="Bash · ls", args={"command": "ls"}, duration_ms=120),
        event(4, "provider.activity", turn_id="t1", activity_id="call_1", tool_name="Bash",
              phase="result", output="a.py\nb.py", ok=True),
    ]
    steps = build_turns(events, [], mission())[0].steps
    assert len(steps) == 1
    step = steps[0]
    assert step.tool == "Bash"
    assert step.summary == "Bash · ls"
    assert step.args == {"command": "ls"}
    assert step.output == "a.py\nb.py"
    assert step.ok is True
    assert step.duration_ms == 120


def test_provider_session_records_model_on_the_turn():
    from backend.app.observability.turns import build_turns

    events = [
        event(1, "turn.started", turn_id="t1", agent_id="baobao", provider_id="claude"),
        event(2, "provider.session", turn_id="t1", session_id="s_1", model="claude-opus-5[1m]"),
    ]
    turn = build_turns(events, [], mission())[0]
    assert turn.model == "claude-opus-5[1m]"
    assert turn.session_id == "s_1"


# ---------------------------------------------------------------------------
# global overview (settings page)
# ---------------------------------------------------------------------------

def _seed_overview(repository):
    from datetime import datetime, timezone

    old = Mission(id="m_old", title="旧任务", goal="旧", conversation_id="c_old",
                  created_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    new = Mission(id="m_new", title="新任务", goal="新", conversation_id="c_new",
                  created_at=datetime(2026, 10, 20, tzinfo=timezone.utc))
    repository.create_mission(old)
    repository.create_mission(new)

    for mission, turn_id, started in (
        (old, "t_old", "2026-10-01T10:00:00+00:00"),
        (new, "t_new", "2026-10-20T10:00:00+00:00"),
    ):
        repository.add_event(mission.id, "turn.started", {
            "turn_id": turn_id, "agent_id": "chulan", "provider_id": "codex",
            "started_at": started})
        repository.add_event(mission.id, "turn.usage", {
            "turn_id": turn_id, "agent_id": "chulan", "provider_id": "codex",
            "usage": {"input_tokens": 1000, "output_tokens": 100, "total_cost_usd": 0.02}})
        repository.add_event(mission.id, "turn.completed", {"turn_id": turn_id, "duration_ms": 900})
        repository.add_message(Message(id=f"msg_{turn_id}", mission_id=mission.id,
                                       role=MessageRole.AGENT, content="已完成。", turn_id=turn_id))

    # a tool failure the reply never mentions → check fails
    repository.add_event("m_new", "tool.call", {"turn_id": "t_new", "agent_id": "chulan",
                                                "tool_name": "write_artifact", "tool_input": {}})
    repository.add_event("m_new", "tool.error", {"turn_id": "t_new", "agent_id": "chulan",
                                                 "tool_name": "write_artifact", "error": "boom"})


def test_overview_aggregates_conversations(repository):
    from datetime import datetime, timezone

    from backend.app.observability.overview import collect_overview

    _seed_overview(repository)
    data = collect_overview(repository, window="all",
                            now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))

    assert data["totals"]["conversations"] == 2
    assert data["totals"]["missions"] == 2
    assert data["totals"]["turns"] == 2
    assert data["totals"]["cost"] == 0.04
    assert [row["agent"] for row in data["by_agent"]] == ["chulan"]
    assert len(data["daily"]) == 14
    assert [row["turn_id"] for row in data["recent_turns"]] == ["t_new", "t_old"]
    assert any(row["check"] == "tool_errors_handled" for row in data["failures"])


def test_overview_window_filters_turns_and_conversations(repository):
    from datetime import datetime, timezone

    from backend.app.observability.overview import collect_overview

    _seed_overview(repository)
    data = collect_overview(repository, window="7d",
                            now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))

    assert data["totals"]["turns"] == 1
    assert data["totals"]["conversations"] == 1
    assert [row["turn_id"] for row in data["recent_turns"]] == ["t_new"]


def test_overview_api_serves_global_scope_and_rejects_bad_window(temp_settings):
    from fastapi.testclient import TestClient

    from backend.app.main import create_app

    client = TestClient(create_app(temp_settings))
    response = client.get("/api/observability/overview", params={"window": "all"})
    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "all"
    assert body["totals"]["turns"] == 0
    assert {"checks", "coverage", "spend", "by_agent", "recent_turns", "failures"} <= set(body)

    assert client.get("/api/observability/overview", params={"window": "nope"}).status_code == 422


def test_turn_detail_finds_a_turn_across_missions(repository):
    from backend.app.observability.service import turn_detail

    _seed_overview(repository)
    detail = turn_detail(repository, "t_new")

    assert detail is not None
    assert detail["mission"]["id"] == "m_new"
    assert detail["turn"]["turn_id"] == "t_new"
    assert any(check["name"] == "tool_errors_handled" for check in detail["turn"]["checks"])
    assert turn_detail(repository, "missing") is None


def test_turn_api_returns_detail_and_404(temp_settings):
    from fastapi.testclient import TestClient

    from backend.app.main import create_app
    from backend.app.storage.repository import Repository

    repository = Repository(temp_settings.database_path)
    repository.initialize()
    _seed_overview(repository)
    client = TestClient(create_app(temp_settings))

    response = client.get("/api/observability/turns/t_new")
    assert response.status_code == 200
    assert response.json()["turn"]["turn_id"] == "t_new"
    assert client.get("/api/observability/turns/missing").status_code == 404
