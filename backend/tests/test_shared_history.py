"""Roundtable prerequisites, exercised without a model or real CLI."""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app.adapters.base import TurnOutcome
from backend.app.api.mission_detail import build_mission_detail
from backend.app.domain.models import (
    Conversation, Message, MessageRole, Mission, MissionKind, MissionStatus, TurnStatus,
)
from backend.app.main import create_app
from backend.app.mcp.tools import MCPTools
from backend.app.orchestration.context import build_conversation_context
from backend.app.orchestration.runner import MissionRunner
from backend.app.storage.repository import Repository


def seed(repository, *, conversation_id="c", mission_id="m", status=MissionStatus.COMPLETED):
    if conversation_id and not repository.get_conversation(conversation_id):
        repository.create_conversation(Conversation(id=conversation_id, title="讨论"))
    mission = Mission(
        id=mission_id, title="讨论", goal="设计并实现系统，包含前端、后端和测试",
        conversation_id=conversation_id, status=status,
    )
    repository.create_mission(mission)
    return mission


def say(repository, mission, message_id, content, role=MessageRole.AGENT, **kwargs):
    message = Message(
        id=message_id, mission_id=mission.id, conversation_id=mission.conversation_id,
        role=role, content=content, **kwargs,
    )
    repository.add_message(message)
    return message


@pytest.mark.parametrize("conversation_id", ["c", None])
@pytest.mark.parametrize("legacy", [True, False])
def test_each_turn_sees_prior_speech_and_handled_interruption(repository, temp_settings, conversation_id, legacy):
    mission = seed(repository, conversation_id=conversation_id)
    prompts, contexts = [], []

    class Adapter:
        def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
            prompts.append(prompt)
            contexts.append(tools.read_context(mission_id)["messages"])
            tools.publish_message(mission_id, agent_id, turn_id, f"事实{len(prompts)}：可以离线讨论", provider_id)
            if len(prompts) == 1:
                # Both formats must survive handling and remain visible to C.
                if legacy:
                    repository.add_event(mission_id, "interrupt", {"content": "补充条件：全程离线"})
                else:
                    repository.add_interruption(mission_id, "补充条件：全程离线")
            return TurnOutcome(status=TurnStatus.COMPLETED)

    repository.update_mission(mission.model_copy(update={"target_agent_ids": ["chulan", "baobao", "wangye"]}))
    MissionRunner(repository, temp_settings, adapter=Adapter()).run(mission.id)
    assert len(prompts) == 3
    for index in (1, 2):
        assert "事实1：可以离线讨论" in prompts[index]
        assert "补充条件：全程离线" in prompts[index]
        assert any(m["content"] == "补充条件：全程离线" for m in contexts[index])
    assert "事实2：可以离线讨论" in prompts[2]


def test_legacy_interrupt_survives_next_mission_with_stable_identity(repository, temp_settings):
    previous = seed(repository)
    say(repository, previous, "before", "先前发言")
    event_id = repository.add_event(previous.id, "interrupt", {"content": "只考虑本地方案"})
    current = seed(repository, mission_id="next")
    say(repository, current, "after", "继续", MessageRole.USER)
    context = MCPTools(repository, temp_settings.artifact_root).read_context(current.id)
    assert [m["content"] for m in context["messages"]] == ["先前发言", "只考虑本地方案", "继续"]
    legacy = context["messages"][1]
    assert legacy["id"] == f"interrupt:{event_id}"
    assert "只考虑本地方案" in build_conversation_context(repository, "c")
    client = TestClient(create_app(temp_settings))
    history = client.get("/api/conversations/c").json()["messages"]
    detail = client.get(f"/api/missions/{previous.id}").json()
    assert legacy["id"] in [m["id"] for m in history]
    assert [m["id"] for m in detail["messages"]] == ["before", legacy["id"]]
    assert [e["id"] for e in detail["events"] if e["role"] == "user"] == [legacy["id"]]
    assert len(repository.list_messages(previous.id)) == 1  # No read-time migration.


def test_new_interrupt_is_atomic_and_appears_once_in_api_sse_and_history(repository, temp_settings):
    mission = seed(repository, status=MissionStatus.RUNNING)
    client = TestClient(create_app(temp_settings))
    for _ in range(2):  # Identical text is two distinct submissions.
        response = client.post(f"/api/missions/{mission.id}/interruptions", json={"content": "限制条件", "target_agent_id": "wangye"})
        assert response.status_code == 202
    # A finished run lets the SSE stream below close after its snapshots.
    repository.update_mission(mission.model_copy(update={"status": MissionStatus.COMPLETED}))
    events = repository.list_events_after(mission.id, 0)
    interrupts = [e for e in events if e.event_type == "interrupt"]
    stored = repository.list_messages(mission.id)
    assert len(stored) == 2
    ids = [m.id for m in stored]
    assert len(set(ids)) == 2
    assert {e.payload["message_id"] for e in interrupts} == set(ids)
    assert all(m.role == MessageRole.USER and m.conversation_id == "c" for m in stored)
    detail = response.json()
    assert len(detail["interrupts"]) == 2
    history = client.get("/api/conversations/c").json()["messages"]
    assert client.get("/api/conversations").json()[0]["latest_message_preview"] == "限制条件"
    snapshots = [json.loads(line[6:]) for line in client.get(f"/api/missions/{mission.id}/stream").text.splitlines() if line.startswith("data: ")]
    for snapshot in [detail, *snapshots]:
        assert [m["id"] for m in snapshot["messages"]] == [m["id"] for m in history]
        assert len([e for e in snapshot["events"] if e["role"] == "user"]) == 2
    assert len(MCPTools(repository, temp_settings.artifact_root).read_context(mission.id)["messages"]) == 2


def test_interrupt_rolls_back_message_and_events_on_failure(repository, temp_settings, monkeypatch):
    mission = seed(repository, status=MissionStatus.RUNNING)
    original = type(repository).add_event

    def fail(self, mission_id, event_type, payload, db=None):
        if event_type == "interrupt":
            raise RuntimeError("injected failure")
        return original(self, mission_id, event_type, payload, db=db)

    monkeypatch.setattr(type(repository), "add_event", fail)
    client = TestClient(create_app(temp_settings), raise_server_exceptions=False)
    assert client.post(f"/api/missions/{mission.id}/interruptions", json={"content": "不能留下半条记录"}).status_code == 500
    assert repository.list_messages(mission.id) == []
    assert [e.event_type for e in repository.list_events_after(mission.id, 0)] == ["mission_created"]


def test_history_reads_one_snapshot_while_interruption_commits(repository, monkeypatch):
    mission = seed(repository)
    # WAL permits a real writer to commit between the reader's SELECTs.
    with repository._transaction() as db:
        db.execute("PRAGMA journal_mode=WAL")
    writer = Repository(repository.database_path)
    connect = repository._connect
    committed = []

    def connect_with_concurrent_write():
        db = connect()

        def trace(sql):
            if sql.startswith("SELECT e.*") and not committed:
                committed.append(writer.add_interruption(mission.id, "读取期间提交的插话"))

        db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(repository, "_connect", connect_with_concurrent_write)
    history = repository.read_message_history(mission_id=mission.id)
    assert len(committed) == 1
    assert history.project() == []
    assert not any(e.event_type == "interrupt" for e in history.events)
    assert [e.message.id for e in repository.read_message_history(mission_id=mission.id).project()] == [committed[0].id]


def test_only_formal_dialogue_enters_prompt_and_mcp(repository, temp_settings):
    mission = seed(repository)
    say(repository, mission, "visible", "正式发言")
    say(repository, mission, "system", "系统控制记录", MessageRole.SYSTEM)
    for kind in ("cli.stdout", "agent.thought", "agent.partial", "tool.call", "status"):
        repository.add_event(mission.id, kind, {"content": f"禁止进入-{kind}"})
    verification = seed(repository, mission_id="verify").model_copy(update={"kind": MissionKind.PROVIDER_VERIFICATION})
    repository.update_mission(verification)
    say(repository, verification, "verify-msg", "接入验证内部记录")
    repository.add_event(verification.id, "interrupt", {"content": "验证插话"})
    tools = MCPTools(repository, temp_settings.artifact_root)
    assert [m["content"] for m in tools.read_context(mission.id)["messages"]] == ["正式发言"]
    prompt = build_conversation_context(repository, "c")
    assert "正式发言" in prompt
    assert all(text not in prompt for text in ("系统控制记录", "禁止进入", "接入验证内部记录", "验证插话"))
    assert "正式发言" not in str(tools.read_context(verification.id))


def test_event_order_wins_over_equal_or_backdated_timestamps(repository, temp_settings):
    mission = seed(repository)
    same_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    say(repository, mission, "z", "第一句", created_at=same_time)
    event_id = repository.add_event(mission.id, "interrupt", {"content": "第二句"})
    say(repository, mission, "a", "第三句", created_at=same_time)
    expected = ["z", f"interrupt:{event_id}", "a"]
    assert [m["id"] for m in MCPTools(repository, temp_settings.artifact_root).read_context(mission.id)["messages"]] == expected
    assert [m["id"] for m in build_mission_detail(repository, temp_settings, mission.id)["messages"]] == expected


def test_bounded_prompt_prioritizes_user_and_reply_then_recent_speech(repository):
    mission = seed(repository)
    say(repository, mission, "user", "关键条件开头" + "长条件" * 600 + "关键条件结尾", MessageRole.USER)
    say(repository, mission, "reply", "需要回应的观点", turn_id="original-turn")
    for index in range(12):
        say(repository, mission, f"recent-{index}", f"普通发言{index}：" + "x" * 1800)
    context = build_conversation_context(repository, "c", limit=4, max_chars=2200, reply_to_turn_id="original-turn")
    assert len(context) <= 2200
    assert "关键条件开头" in context and "关键条件结尾" in context
    assert "已截断" in context and "id=user" in context
    assert "需要回应的观点" in context and "id=reply" in context
    assert "普通发言11" in context
    assert "普通发言0：" not in context
    assert context == build_conversation_context(repository, "c", limit=4, max_chars=2200, reply_to_turn_id="original-turn")
    default_context = build_conversation_context(repository, "c", reply_to_turn_id="original-turn")
    assert len(default_context) <= 6000
    assert "关键条件结尾" in default_context and "需要回应的观点" in default_context


@pytest.mark.parametrize("legacy", [True, False])
def test_latest_interrupt_remains_pinned_when_continuing_a_long_conversation(repository, temp_settings, legacy):
    previous = seed(repository)
    if legacy:
        repository.add_event(previous.id, "interrupt", {"content": "不可遗忘的离线限制"})
    else:
        repository.add_interruption(previous.id, "不可遗忘的离线限制")
    for index in range(25):
        say(repository, previous, f"old-{index}", f"后续讨论{index}")
    current = seed(repository, mission_id="next")
    say(repository, current, "continue", "现在继续讨论", MessageRole.USER)
    assert "不可遗忘的离线限制" in build_conversation_context(repository, "c")
    messages = MCPTools(repository, temp_settings.artifact_root).read_context(current.id)["messages"]
    assert len(messages) == 20
    assert "不可遗忘的离线限制" in [m["content"] for m in messages]


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_context_limit_is_empty(repository, temp_settings, limit):
    mission = seed(repository)
    say(repository, mission, "msg", "发言")
    assert build_conversation_context(repository, "c", limit=limit) == ""
    assert MCPTools(repository, temp_settings.artifact_root).read_context(mission.id, limit=limit)["messages"] == []
