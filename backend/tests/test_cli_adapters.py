import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.app.adapters.base import TurnOutcome
from backend.app.adapters.fake import FakeCLIAdapter
from backend.app.adapters.provider_router import ProviderRouterAdapter
from backend.app.adapters.subprocess_cli import (
    SubprocessCLIAdapter,
    build_failure_summary,
    kill_process_tree,
)
from backend.app.domain.models import (
    AgentProfile,
    Conversation,
    Message,
    MessageRole,
    Mission,
    MissionStatus,
    OrchestrationMode,
    TurnStatus,
)
from backend.app.mcp.tools import MCPTools
from backend.app.orchestration.runner import MissionRunner, _build_prompt


def _make_mock_popen(
    stdout="",
    stderr="",
    returncode=0,
    *,
    timeout_on_communicate=False,
    pid=12345,
):
    """Build a mock Popen instance for testing."""
    mock_proc = MagicMock()
    mock_proc.pid = pid
    mock_proc.returncode = returncode

    if timeout_on_communicate:
        call_count = [0]
        def communicate_side_effect(timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise subprocess.TimeoutExpired(["cli"], timeout or 120)
            return (stdout, stderr)
        mock_proc.communicate = MagicMock(side_effect=communicate_side_effect)
        mock_proc.wait = MagicMock()
    else:
        mock_proc.communicate = MagicMock(return_value=(stdout, stderr))

    return mock_proc


@pytest.mark.parametrize("agent_id", ["chulan", "baobao", "wangye"])
def test_fake_adapter_publishes_message_and_artifact(repository, temp_settings, agent_id):
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = FakeCLIAdapter()

    adapter.run_turn("m_1", agent_id, "t_1", "fake", "Create a report", tools)

    assert repository.list_messages("m_1")[0].agent_id == agent_id
    assert (temp_settings.artifact_root / "m_1" / f"demo-{agent_id}.md").exists()


@pytest.mark.parametrize("agent_id", ["chulan", "baobao", "wangye"])
def test_fake_adapter_does_not_create_artifact_for_discussion(repository, temp_settings, agent_id):
    repository.create_mission(
        Mission(id="m_discuss", title="Discuss", goal="三位 Agent 讨论近期趋势并给我讲讲")
    )
    tools = MCPTools(repository, temp_settings.artifact_root)

    FakeCLIAdapter().run_turn(
        "m_discuss",
        agent_id,
        "t_1",
        "fake",
        "讨论近期趋势",
        tools,
    )

    assert repository.list_messages("m_discuss")[0].agent_id == agent_id
    assert not (temp_settings.artifact_root / "m_discuss" / f"demo-{agent_id}.md").exists()
    assert all(
        event.event_type != "artifact"
        for event in repository.list_events_after("m_discuss", 0)
    )


def test_runner_completes_mission_with_fake_cli(repository, temp_settings):
    mission = Mission(id="m_1", title="Build", goal="设计并实现一个多 Agent 聊天室系统，包含后端、前端、测试和 MCP")
    repository.create_mission(mission)
    runner = MissionRunner(repository, temp_settings, adapter=FakeCLIAdapter())

    completed = runner.run("m_1")

    assert completed.status == MissionStatus.COMPLETED
    assert len(repository.list_messages("m_1")) >= 1


class BlockingAdapter:
    def run_turn(
        self,
        mission_id,
        agent_id,
        turn_id,
        provider_id,
        prompt,
        tools,
    ):
        return TurnOutcome(status=TurnStatus.BLOCKED, summary="adapter blocked")


def test_runner_blocks_mission_when_adapter_blocks(repository, temp_settings):
    mission = Mission(id="m_1", title="Build", goal="设计并实现一个系统，包含后端、前端、测试和 MCP")
    repository.create_mission(mission)
    runner = MissionRunner(repository, temp_settings, adapter=BlockingAdapter())

    completed = runner.run("m_1")
    persisted = repository.get_mission("m_1")
    status_events = [
        event
        for event in repository.list_events_after("m_1", 0)
        if event.event_type == "status"
    ]

    assert completed.status == MissionStatus.BLOCKED
    assert persisted.status == MissionStatus.BLOCKED
    assert [event.payload["status"] for event in status_events].count("completed") == 0


class SelectiveBlockingAdapter:
    def run_turn(
        self,
        mission_id,
        agent_id,
        turn_id,
        provider_id,
        prompt,
        tools,
    ):
        if provider_id == "claude":
            return TurnOutcome(
                status=TurnStatus.BLOCKED,
                summary="temporary provider network failure",
                failure_code="network",
                retryable=True,
            )
        tools.publish_message(mission_id, agent_id, turn_id, "fallback ok", provider_id)
        return TurnOutcome(status=TurnStatus.COMPLETED)


@pytest.mark.parametrize("failure", ["authentication", "configuration"])
def test_runner_does_not_fallback_after_permanent_failure(repository, temp_settings, failure):
    calls = []

    class PermanentFailureAdapter:
        def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
            calls.append(provider_id)
            return TurnOutcome(status=TurnStatus.BLOCKED, failure_code=failure, retryable=False)

    repository.create_mission(Mission(id="m_permanent", title="Plan", goal="设计方案"))
    runner = MissionRunner(repository, temp_settings, adapter=PermanentFailureAdapter())
    runner.agents = [AgentProfile(id="chulan", display_name="张楚岚", role="规划",
                                 persona="拆解", default_provider_id="claude",
                                 fallback_provider_ids=["gemini"])]
    assert runner.run("m_permanent").status == MissionStatus.BLOCKED
    assert calls == ["claude"]


def test_runner_uses_agent_fallback_provider_when_default_blocks(
    repository,
    temp_settings,
):
    mission = Mission(id="m_1", title="Plan", goal="设计方案")
    repository.create_mission(mission)
    runner = MissionRunner(
        repository,
        temp_settings,
        adapter=SelectiveBlockingAdapter(),
    )
    runner.agents = [
        AgentProfile(
            id="chulan",
            display_name="张楚岚",
            role="规划",
            persona="拆解",
            default_provider_id="claude",
            fallback_provider_ids=["gemini"],
        ),
        AgentProfile(
            id="wangye",
            display_name="王也",
            role="分析",
            persona="风险",
            default_provider_id="gemini",
        ),
    ]

    completed = runner.run("m_1")

    messages = repository.list_messages("m_1")
    assert completed.status == MissionStatus.COMPLETED
    assert messages[0].agent_id == "chulan"
    assert messages[0].provider_id == "gemini"
    assert messages[0].content == "fallback ok"


def test_runner_emits_turn_events_during_fallback(repository, temp_settings):
    mission = Mission(id="m_ev1", title="Plan", goal="设计方案")
    repository.create_mission(mission)
    runner = MissionRunner(
        repository,
        temp_settings,
        adapter=SelectiveBlockingAdapter(),
    )
    runner.agents = [
        AgentProfile(
            id="chulan",
            display_name="张楚岚",
            role="规划",
            persona="拆解",
            default_provider_id="claude",
            fallback_provider_ids=["gemini"],
        ),
        AgentProfile(
            id="wangye",
            display_name="王也",
            role="分析",
            persona="风险",
            default_provider_id="gemini",
        ),
    ]
    temp_settings.max_turns = 1

    runner.run("m_ev1")

    events = repository.list_events_after("m_ev1", 0)
    turn_events = [e for e in events if e.event_type.startswith("turn.")]
    turn_types = [e.event_type for e in turn_events]
    assert "turn.started" in turn_types
    assert "turn.blocked" in turn_types
    assert "turn.completed" in turn_types

    blocked = [e for e in turn_events if e.event_type == "turn.blocked"]
    assert blocked[0].payload["provider_id"] == "claude"

    completed = [e for e in turn_events if e.event_type == "turn.completed"]
    assert completed[0].payload["provider_id"] == "gemini"


def test_fallback_derive_turns_aggregates_attempts(repository, temp_settings):
    """After a fallback scenario, _derive_turns should produce exactly 2 attempts
    (1 blocked + 1 completed) per turn, not 3-4 raw event records."""
    from backend.app.api.mission_detail import _derive_turns

    mission = Mission(id="m_agg", title="Plan", goal="设计方案")
    repository.create_mission(mission)
    runner = MissionRunner(
        repository,
        temp_settings,
        adapter=SelectiveBlockingAdapter(),
    )
    runner.agents = [
        AgentProfile(
            id="chulan",
            display_name="张楚岚",
            role="规划",
            persona="拆解",
            default_provider_id="claude",
            fallback_provider_ids=["gemini"],
        ),
        AgentProfile(
            id="wangye",
            display_name="王也",
            role="分析",
            persona="风险",
            default_provider_id="gemini",
        ),
    ]
    temp_settings.max_turns = 1
    runner.run("m_agg")

    events = repository.list_events_after("m_agg", 0)
    turns = _derive_turns(events)

    chulan_turn = [t for t in turns if t["agent_id"] == "chulan"]
    assert chulan_turn
    turn = chulan_turn[0]
    assert turn["status"] == "completed"
    assert len(turn["attempts"]) == 2
    statuses = [a["status"] for a in turn["attempts"]]
    assert "blocked" in statuses
    assert "completed" in statuses


def test_single_success_derive_turns_one_attempt(repository, temp_settings):
    """A turn that succeeds on the first provider should have exactly 1 attempt."""
    from backend.app.api.mission_detail import _derive_turns

    mission = Mission(id="m_one", title="Plan", goal="快速任务")
    repository.create_mission(mission)
    runner = MissionRunner(
        repository,
        temp_settings,
        adapter=FakeCLIAdapter(),
    )
    runner.agents = [
        AgentProfile(
            id="chulan",
            display_name="张楚岚",
            role="规划",
            persona="拆解",
            default_provider_id="fake",
        ),
    ]
    temp_settings.max_turns = 1
    runner.run("m_one")

    events = repository.list_events_after("m_one", 0)
    turns = _derive_turns(events)

    assert len(turns) == 1
    turn = turns[0]
    assert turn["status"] == "completed"
    assert len(turn["attempts"]) == 1
    assert turn["attempts"][0]["status"] == "completed"


def test_directed_mission_routes_to_target_agent_wangye(repository, temp_settings):
    mission = Mission(
        id="m_dir1", title="看风险", goal="看风险",
        target_agent_id="wangye",
    )
    repository.create_mission(mission)
    runner = MissionRunner(repository, temp_settings, adapter=FakeCLIAdapter())
    temp_settings.max_turns = 1

    completed = runner.run("m_dir1")

    assert completed.status == MissionStatus.COMPLETED
    messages = repository.list_messages("m_dir1")
    assert messages[0].agent_id == "wangye"

    events = repository.list_events_after("m_dir1", 0)
    turn_started = [e for e in events if e.event_type == "turn.started"]
    assert turn_started[0].payload["agent_id"] == "wangye"


def test_directed_mission_routes_to_target_agent_baobao(repository, temp_settings):
    mission = Mission(
        id="m_dir2", title="做实现", goal="做实现",
        target_agent_id="baobao",
    )
    repository.create_mission(mission)
    runner = MissionRunner(repository, temp_settings, adapter=FakeCLIAdapter())
    temp_settings.max_turns = 1

    completed = runner.run("m_dir2")

    assert completed.status == MissionStatus.COMPLETED
    messages = repository.list_messages("m_dir2")
    assert messages[0].agent_id == "baobao"

    events = repository.list_events_after("m_dir2", 0)
    turn_started = [e for e in events if e.event_type == "turn.started"]
    assert turn_started[0].payload["agent_id"] == "baobao"


def test_undirected_mission_uses_default_workflow(repository, temp_settings):
    mission = Mission(
        id="m_dir3", title="解释报错", goal="解释 Python 报错",
    )
    repository.create_mission(mission)
    runner = MissionRunner(repository, temp_settings, adapter=FakeCLIAdapter())
    temp_settings.max_turns = 1

    completed = runner.run("m_dir3")

    assert completed.status == MissionStatus.COMPLETED
    messages = repository.list_messages("m_dir3")
    assert messages[0].agent_id == "chulan"


def test_directed_mission_uses_target_agent_providers_for_fallback(
    repository, temp_settings,
):
    mission = Mission(
        id="m_dir4", title="做实现", goal="做实现",
        target_agent_id="chulan",
    )
    repository.create_mission(mission)
    runner = MissionRunner(
        repository, temp_settings,
        adapter=SelectiveBlockingAdapter(),
    )
    runner.agents = [
        AgentProfile(
            id="chulan", display_name="张楚岚", role="规划", persona="拆解",
            default_provider_id="claude",
            fallback_provider_ids=["gemini"],
        ),
        AgentProfile(
            id="baobao", display_name="冯宝宝", role="执行", persona="产物",
            default_provider_id="codex",
        ),
        AgentProfile(
            id="wangye", display_name="王也", role="分析", persona="风险",
            default_provider_id="gemini",
        ),
    ]
    temp_settings.max_turns = 1

    completed = runner.run("m_dir4")

    assert completed.status == MissionStatus.COMPLETED
    messages = repository.list_messages("m_dir4")
    assert messages[0].agent_id == "chulan"
    assert messages[0].provider_id == "gemini"


class PromptCapturingAdapter:
    def __init__(self):
        self.prompts = []

    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        self.prompts.append(prompt)
        tools.publish_message(mission_id, agent_id, turn_id, "ok", provider_id)
        return TurnOutcome(status=TurnStatus.COMPLETED)


def test_runner_prompt_includes_conversation_context(repository, temp_settings):
    repository.create_conversation(Conversation(id="c_ctx", title="ctx"))
    repository.create_mission(Mission(
        id="m_prior", title="first", goal="先做规划", conversation_id="c_ctx",
    ))
    repository.add_message(Message(
        id="msg_u1", mission_id="m_prior", conversation_id="c_ctx",
        role=MessageRole.USER, content="帮我规划一下",
    ))
    repository.add_message(Message(
        id="msg_a1", mission_id="m_prior", conversation_id="c_ctx",
        role=MessageRole.AGENT, content="已规划完成", agent_id="chulan",
    ))

    repository.create_mission(Mission(
        id="m_follow", title="follow", goal="继续实现", conversation_id="c_ctx",
    ))

    adapter = PromptCapturingAdapter()
    runner = MissionRunner(repository, temp_settings, adapter=adapter)
    temp_settings.max_turns = 1
    runner.run("m_follow")

    prompt = adapter.prompts[0]
    assert "最近对话" in prompt
    assert "用户：帮我规划一下" in prompt
    assert "张楚岚：已规划完成" in prompt


def test_runner_prompt_without_conversation_has_no_context(repository, temp_settings):
    repository.create_mission(Mission(
        id="m_noconv", title="solo", goal="解释报错",
    ))

    adapter = PromptCapturingAdapter()
    runner = MissionRunner(repository, temp_settings, adapter=adapter)
    temp_settings.max_turns = 1
    runner.run("m_noconv")

    prompt = adapter.prompts[0]
    assert "最近对话" not in prompt


def test_directed_mission_with_context_still_routes_to_target(repository, temp_settings):
    repository.create_conversation(Conversation(id="c_dir", title="dir"))
    repository.create_mission(Mission(
        id="m_dir_ctx", title="directed", goal="看风险",
        conversation_id="c_dir", target_agent_id="wangye",
    ))

    runner = MissionRunner(repository, temp_settings, adapter=FakeCLIAdapter())
    temp_settings.max_turns = 1
    completed = runner.run("m_dir_ctx")

    assert completed.status == MissionStatus.COMPLETED
    messages = repository.list_messages("m_dir_ctx")
    assert messages[0].agent_id == "wangye"


def test_build_prompt_constrains_cli_to_short_chat_response():
    mission = Mission(id="m_1", title="Plan", goal="系统 后端 前端 测试 MCP 多 Agent")
    agent = AgentProfile(
        id="chulan",
        display_name="张楚岚",
        role="规划",
        persona="拆解",
        default_provider_id="claude",
    )

    prompt = _build_prompt(mission, "拆解任务", agent)

    assert "聊天室" in prompt
    assert "张楚岚" in prompt
    assert "write_artifact" in prompt
    assert "artifacts" in prompt
    # 旧的硬性禁令已移除：让各家 CLI 用自己的原生工具读写文件
    assert "不要读取文件" not in prompt
    assert "120字以内" not in prompt
    assert "短中文回复" not in prompt


def test_build_prompt_separates_discussion_from_file_delivery():
    mission = Mission(id="m_discuss", title="Discuss", goal="讨论近期 Agent 趋势")
    agent = AgentProfile(
        id="baobao",
        display_name="冯宝宝",
        role="执行",
        persona="具体",
        default_provider_id="codex",
    )

    prompt = _build_prompt(mission, "补充具体做法和实例", agent)

    assert "讨论、解释、评审直接在聊天里回答" in prompt
    assert "只有用户要文件，或已获授权的执行任务确实需要时才写文件" in prompt


def test_build_prompt_includes_trigger_points_when_agent_has_them():
    mission = Mission(id="m_1", title="Plan", goal="多 Agent 协作")
    agent = AgentProfile(
        id="wangye",
        display_name="王也",
        role="分析",
        persona="风险",
        default_provider_id="gemini",
        trigger_points=[
            "别人给出方案后还没有人挑过隐含假设 → 你先指出最可能出错的点",
            "讨论开始相互附和 → 你至少给一条反对视角",
        ],
    )

    prompt = _build_prompt(mission, "复核风险", agent)

    assert "【做本职工作时】" in prompt
    assert "别人给出方案后还没有人挑过隐含假设" in prompt
    assert "讨论开始相互附和" in prompt


def test_build_prompt_does_not_double_punctuate_persona():
    mission = Mission(id="m_1", title="Plan", goal="多 Agent 协作")
    agent = AgentProfile(
        id="chulan",
        display_name="张楚岚",
        role="规划",
        persona="圆滑、市侩。",
        default_provider_id="claude",
    )

    prompt = _build_prompt(mission, "拆解任务", agent)

    assert "圆滑、市侩。。" not in prompt


def test_build_prompt_skips_trigger_points_when_agent_has_none():
    mission = Mission(id="m_1", title="Plan", goal="多 Agent 协作")
    agent = AgentProfile(
        id="chulan",
        display_name="张楚岚",
        role="规划",
        persona="拆解",
        default_provider_id="claude",
    )

    prompt = _build_prompt(mission, "拆解任务", agent)

    assert "【做本职工作时】" not in prompt


def test_build_prompt_includes_artifact_index_section():
    mission = Mission(id="m_1", title="Plan", goal="多 Agent 协作")
    agent = AgentProfile(
        id="chulan",
        display_name="张楚岚",
        role="规划",
        persona="拆解",
        default_provider_id="claude",
    )
    index = "【产物清单】（要引用别人写的内容时，先用 read_artifact 读取，不要凭印象引述）：\n- plan.md  by baobao（plan.md）"

    prompt = _build_prompt(mission, "收束", agent, artifact_index=index)

    assert "【产物清单】" in prompt
    assert "plan.md  by baobao" in prompt


def test_build_prompt_routes_handoffs_by_tool_without_exit_checklist():
    mission = Mission(id="m_1", title="Plan", goal="多 Agent 协作")
    agent = AgentProfile(
        id="chulan",
        display_name="张楚岚",
        role="规划",
        persona="拆解",
        default_provider_id="claude",
    )

    prompt = _build_prompt(mission, "收束", agent, max_a2a_invocations=2)

    assert "【发送前出口检查】" not in prompt
    assert "log_thought" not in prompt
    assert "invoke_agent" in prompt
    assert "只在正文里喊名字不会触发接力" in prompt
    assert "invoke_agent" not in _build_prompt(mission, "收束", agent, max_a2a_invocations=0)


# ---------------------------------------------------------------------------
# SubprocessCLIAdapter: Popen-based tests
# ---------------------------------------------------------------------------


def test_subprocess_adapter_passes_prompt_as_final_arg_and_timeout(
    repository,
    temp_settings,
):
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)
    popen_calls = []

    mock_proc = _make_mock_popen(stdout="done\n", stderr="")

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        mock_proc.returncode = 0
        return mock_proc

    adapter = SubprocessCLIAdapter("cli", ["--json"], timeout=120)
    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", side_effect=fake_popen):
        outcome = adapter.run_turn("m_1", "baobao", "t_1", "local", "Create a report", tools)

    assert outcome.status == TurnStatus.COMPLETED
    assert popen_calls[0][0] == ["cli", "--json", "Create a report"]
    assert popen_calls[0][1]["stdin"] == subprocess.DEVNULL
    mock_proc.communicate.assert_called_once_with(timeout=120)


def test_subprocess_adapter_publishes_placeholder_for_empty_stdout(
    repository,
    temp_settings,
):
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)

    mock_proc = _make_mock_popen(stdout=" \n", stderr="")

    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", return_value=mock_proc):
        adapter = SubprocessCLIAdapter("cli")
        outcome = adapter.run_turn("m_1", "baobao", "t_1", "local", "Create a report", tools)

    assert outcome.status == TurnStatus.COMPLETED
    assert repository.list_messages("m_1")[0].content == "(CLI returned no output)"


def test_subprocess_adapter_writes_artifacts_from_json_envelope(
    repository,
    temp_settings,
):
    repository.create_mission(Mission(id="m_env1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)
    envelope = {
        "message": "我写好了摘要。",
        "artifacts": [
            {"path": "notes/summary.md", "content": "# Summary\nDone."},
        ],
    }

    mock_proc = _make_mock_popen(
        stdout=json.dumps(envelope, ensure_ascii=False), stderr="",
    )

    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", return_value=mock_proc):
        adapter = SubprocessCLIAdapter("cli")
        outcome = adapter.run_turn("m_env1", "baobao", "turn_1", "codex", "Create a report", tools)

    messages = repository.list_messages("m_env1")
    assert outcome.status == TurnStatus.COMPLETED
    assert messages[0].content == "我写好了摘要。"
    artifact_path = temp_settings.artifact_root / "m_env1" / "notes" / "summary.md"
    assert artifact_path.read_text(encoding="utf-8") == "# Summary\nDone."

    artifact_events = [
        event for event in repository.list_events_after("m_env1", 0)
        if event.event_type == "artifact"
    ]
    assert len(artifact_events) == 1
    assert artifact_events[0].payload["agent_id"] == "baobao"
    assert artifact_events[0].payload["turn_id"] == "turn_1"
    assert artifact_events[0].payload["path"] == "notes/summary.md"


def test_subprocess_adapter_blocks_on_unsafe_envelope_artifact_after_message(
    repository,
    temp_settings,
):
    repository.create_mission(Mission(id="m_env2", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)
    envelope = {
        "message": "先保留这条回复。",
        "artifacts": [
            {"path": "../escape.md", "content": "bad"},
        ],
    }

    mock_proc = _make_mock_popen(
        stdout=json.dumps(envelope, ensure_ascii=False), stderr="",
    )

    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", return_value=mock_proc):
        adapter = SubprocessCLIAdapter("cli")
        outcome = adapter.run_turn("m_env2", "baobao", "turn_1", "codex", "Create a report", tools)

    messages = repository.list_messages("m_env2")
    assert outcome.status == TurnStatus.BLOCKED
    assert "Artifact path is outside artifact directory" in outcome.summary
    assert messages[0].content == "先保留这条回复。"
    tool_errors = [
        event for event in repository.list_events_after("m_env2", 0)
        if event.event_type == "tool.error"
    ]
    assert len(tool_errors) == 1


def test_subprocess_adapter_keeps_non_envelope_json_as_plain_message(
    repository,
    temp_settings,
):
    repository.create_mission(Mission(id="m_env3", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)

    mock_proc = _make_mock_popen(stdout='{"ok": true}', stderr="")

    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", return_value=mock_proc):
        adapter = SubprocessCLIAdapter("cli")
        outcome = adapter.run_turn("m_env3", "baobao", "turn_1", "codex", "Create a report", tools)

    assert outcome.status == TurnStatus.COMPLETED
    assert repository.list_messages("m_env3")[0].content == '{"ok": true}'


def test_subprocess_adapter_returns_structured_blocked_summary_on_nonzero_exit(
    repository,
    temp_settings,
):
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)

    mock_proc = _make_mock_popen(stdout="", stderr="bad command", returncode=2)

    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", return_value=mock_proc):
        adapter = SubprocessCLIAdapter("cli")
        outcome = adapter.run_turn("m_1", "baobao", "t_1", "local", "Create a report", tools)

    assert outcome.status == TurnStatus.BLOCKED
    assert "[local]" in outcome.summary
    assert "exit_code=2" in outcome.summary
    assert "bad command" in outcome.summary


def test_subprocess_adapter_timeout_returns_structured_blocked_summary(
    repository,
    temp_settings,
):
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)

    mock_proc = _make_mock_popen(
        stdout="partial out", stderr="timeout info",
        timeout_on_communicate=True,
    )

    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", return_value=mock_proc):
        adapter = SubprocessCLIAdapter("cli", timeout=60)
        outcome = adapter.run_turn("m_1", "baobao", "t_1", "local", "Create a report", tools)

    assert outcome.status == TurnStatus.BLOCKED
    assert "[local]" in outcome.summary
    assert "timeout after 60s" in outcome.summary


def test_subprocess_adapter_os_error_returns_structured_blocked_summary(
    repository,
    temp_settings,
):
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)

    with patch(
        "backend.app.adapters.subprocess_cli.subprocess.Popen",
        side_effect=FileNotFoundError("missing cli"),
    ):
        adapter = SubprocessCLIAdapter("cli")
        outcome = adapter.run_turn("m_1", "baobao", "t_1", "local", "Create a report", tools)

    assert outcome.status == TurnStatus.BLOCKED
    assert "[local]" in outcome.summary
    assert "OSError: missing cli" in outcome.summary


def test_subprocess_adapter_timeout_calls_kill_process_tree(
    repository,
    temp_settings,
):
    """On timeout, the adapter must call kill_process_tree to clean up the process group."""
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)

    mock_proc = _make_mock_popen(
        stdout="", stderr="",
        timeout_on_communicate=True,
    )

    killed_procs = []
    original_kill = kill_process_tree

    def tracking_kill(proc):
        killed_procs.append(proc)
        original_kill(proc)

    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", return_value=mock_proc):
        with patch("backend.app.adapters.subprocess_cli.kill_process_tree", side_effect=tracking_kill):
            adapter = SubprocessCLIAdapter("cli", timeout=10)
            adapter.run_turn("m_1", "baobao", "t_1", "local", "Create a report", tools)

    assert len(killed_procs) == 1
    assert killed_procs[0] is mock_proc


def test_subprocess_adapter_uses_start_new_session():
    """SubprocessCLIAdapter must use start_new_session=True for process group isolation."""
    popen_kwargs = []

    mock_proc = _make_mock_popen(stdout="ok", stderr="")

    def capture_popen(cmd, **kwargs):
        popen_kwargs.append(kwargs)
        return mock_proc

    from backend.app.adapters.subprocess_cli import _IS_WINDOWS
    tools = MagicMock()

    adapter = SubprocessCLIAdapter("cli")
    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", side_effect=capture_popen):
        adapter.run_turn("m_1", "agent", "t_1", "prov", "prompt", tools)

    if not _IS_WINDOWS:
        assert popen_kwargs[0]["start_new_session"] is True


def test_kill_process_tree_sends_sigterm_then_sigkill_on_posix():
    """kill_process_tree should send SIGTERM to the process group, then SIGKILL if needed."""
    if sys.platform == "win32":
        return

    mock_proc = MagicMock()
    mock_proc.pid = 99999
    mock_proc.wait = MagicMock(side_effect=subprocess.TimeoutExpired(["x"], 5))
    mock_proc.kill = MagicMock()

    with patch("os.getpgid", return_value=99999) as mock_getpgid, \
         patch("os.killpg") as mock_killpg:
        kill_process_tree(mock_proc)

    mock_getpgid.assert_called_once_with(99999)
    calls = [c[0] for c in mock_killpg.call_args_list]
    assert (99999, signal.SIGTERM) in calls
    assert (99999, signal.SIGKILL) in calls


# ---------------------------------------------------------------------------
# Runner-level tests with structured summaries
# ---------------------------------------------------------------------------


def test_runner_preserves_subprocess_stderr_summary_on_block(repository, temp_settings):
    mission = Mission(id="m_1", title="Build", goal="设计并实现一个系统，包含后端、前端、测试和 MCP")
    repository.create_mission(mission)

    def always_fail(cmd, **kwargs):
        return _make_mock_popen(stdout="", stderr="specific stderr", returncode=2)

    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", side_effect=always_fail):
        runner = MissionRunner(repository, temp_settings, adapter=SubprocessCLIAdapter("cli"))
        completed = runner.run("m_1")

    latest_status = _latest_status_event(repository, "m_1")

    assert completed.status == MissionStatus.BLOCKED
    assert latest_status.payload["status"] == "blocked"
    assert "specific stderr" in latest_status.payload["summary"]


def test_runner_preserves_subprocess_timeout_summary_on_block(
    repository,
    temp_settings,
):
    mission = Mission(id="m_1", title="Build", goal="设计并实现一个系统，包含后端、前端、测试和 MCP")
    repository.create_mission(mission)

    def always_timeout(cmd, **kwargs):
        return _make_mock_popen(stdout="", stderr="", timeout_on_communicate=True)

    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", side_effect=always_timeout):
        runner = MissionRunner(repository, temp_settings, adapter=SubprocessCLIAdapter("cli"))
        completed = runner.run("m_1")

    latest_status = _latest_status_event(repository, "m_1")

    assert completed.status == MissionStatus.BLOCKED
    assert latest_status.payload["status"] == "blocked"
    assert "timeout" in latest_status.payload["summary"]


def test_runner_preserves_subprocess_os_error_summary_on_block(
    repository,
    temp_settings,
):
    mission = Mission(id="m_1", title="Build", goal="设计并实现一个系统，包含后端、前端、测试和 MCP")
    repository.create_mission(mission)

    with patch(
        "backend.app.adapters.subprocess_cli.subprocess.Popen",
        side_effect=FileNotFoundError("missing cli"),
    ):
        runner = MissionRunner(repository, temp_settings, adapter=SubprocessCLIAdapter("cli"))
        completed = runner.run("m_1")

    latest_status = _latest_status_event(repository, "m_1")

    assert completed.status == MissionStatus.BLOCKED
    assert latest_status.payload["status"] == "blocked"
    assert "missing cli" in latest_status.payload["summary"]


# ---------------------------------------------------------------------------
# ProviderRouterAdapter tests
# ---------------------------------------------------------------------------


def test_provider_router_uses_internal_fake_provider(repository, temp_settings):
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = ProviderRouterAdapter(temp_settings.config_dir)

    outcome = adapter.run_turn("m_1", "baobao", "t_1", "fake", "Create a report", tools)

    messages = repository.list_messages("m_1")
    assert outcome.status == TurnStatus.COMPLETED
    assert messages[0].provider_id == "fake"
    assert messages[0].content == "[演示:baobao] Create a report"


def test_provider_router_blocks_disabled_real_provider_without_subprocess(
    repository,
    temp_settings,
    tmp_path,
):
    config_dir = tmp_path / "config"
    _write_provider_config(
        config_dir,
        [
            {
                "id": "claude",
                "label": "Claude Code",
                "command": "claude",
                "args": ["-p"],
                "enabled": False,
            }
        ],
    )
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = ProviderRouterAdapter(config_dir)

    outcome = adapter.run_turn("m_1", "chulan", "t_1", "claude", "Create a report", tools)

    assert outcome.status == TurnStatus.BLOCKED
    assert outcome.summary == "provider is disabled: claude"
    assert repository.list_messages("m_1") == []


def test_provider_router_persists_gemini_home_and_resumes_session(
    repository, temp_settings, tmp_path
):
    """Two consecutive gemini turns share one GEMINI_CLI_HOME and pass --resume.

    Session resume requires session files to survive between turns, so the
    router must reuse one tempdir per mission. The session_id captured from
    turn 1's init event must be threaded into turn 2's cmd as --resume.
    """
    import json as _json
    import unittest.mock as _mock
    from backend.app.adapters.mcp_subprocess import MCPSubprocessAdapter
    from backend.tests.test_mcp_subprocess_adapter import _make_streaming_proc

    config_dir = tmp_path / "config"
    _write_provider_config(
        config_dir,
        [
            {
                "id": "gemini",
                "label": "Gemini",
                "command": "gemini",
                "args": ["--prompt"],
                "enabled": True,
                "mcp_enabled": True,
            }
        ],
    )
    repository.create_mission(Mission(id="m_resume", title="Plan", goal="Resume test"))
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = ProviderRouterAdapter(
        config_dir,
        database_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
    )

    captured_cmds: list[list[str]] = []
    captured_envs: list[dict] = []
    init_lines = [
        _json.dumps({"type": "init", "session_id": "sid-turn-1"}),
        _json.dumps({"type": "init", "session_id": "sid-turn-1"}),  # resume keeps same
    ]
    call_count = {"n": 0}

    def spy_popen(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        captured_envs.append(dict(kwargs.get("env", {})))
        line = init_lines[call_count["n"]] if call_count["n"] < len(init_lines) else ""
        call_count["n"] += 1
        return _make_streaming_proc([line] if line else [])

    with _mock.patch(
        "backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen
    ):
        adapter.run_turn("m_resume", "wangye", "t1", "gemini", "first", tools)
        adapter.run_turn("m_resume", "wangye", "t2", "gemini", "second", tools)

    assert "--resume" not in captured_cmds[0], "turn 1 must NOT pass --resume"
    assert "--resume" in captured_cmds[1], "turn 2 must pass --resume"
    sid_idx = captured_cmds[1].index("--resume")
    assert captured_cmds[1][sid_idx + 1] == "sid-turn-1"
    assert captured_envs[0]["GEMINI_CLI_HOME"] == captured_envs[1]["GEMINI_CLI_HOME"], (
        "both turns must share the same GEMINI_CLI_HOME"
    )

    home = Path(captured_envs[0]["GEMINI_CLI_HOME"])
    adapter.close()
    assert not home.exists(), "close() must release the per-mission tempdir"


def test_provider_router_isolates_session_id_per_agent(
    repository, temp_settings, tmp_path
):
    """wangye and baobao on gemini must NOT share a session — personas differ."""
    import json as _json
    import unittest.mock as _mock
    from backend.tests.test_mcp_subprocess_adapter import _make_streaming_proc

    config_dir = tmp_path / "config"
    _write_provider_config(
        config_dir,
        [{
            "id": "gemini",
            "label": "Gemini",
            "command": "gemini",
            "args": ["--prompt"],
            "enabled": True,
            "mcp_enabled": True,
        }],
    )
    repository.create_mission(Mission(id="m_iso", title="Plan", goal="Isolation"))
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = ProviderRouterAdapter(
        config_dir,
        database_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
    )

    captured_cmds: list[list[str]] = []
    sid_seq = ["wangye-sid", "baobao-sid", "wangye-sid"]
    call_count = {"n": 0}

    def spy_popen(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        sid = sid_seq[call_count["n"]]
        call_count["n"] += 1
        return _make_streaming_proc([_json.dumps({"type": "init", "session_id": sid})])

    try:
        with _mock.patch(
            "backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen
        ):
            adapter.run_turn("m_iso", "wangye", "t1", "gemini", "wangye-1", tools)
            adapter.run_turn("m_iso", "baobao", "t2", "gemini", "baobao-1", tools)
            adapter.run_turn("m_iso", "wangye", "t3", "gemini", "wangye-2", tools)

        # wangye's 2nd turn (3rd overall) must resume wangye-sid, not baobao-sid
        assert "--resume" not in captured_cmds[0]
        assert "--resume" not in captured_cmds[1], "baobao's first turn must not resume"
        assert "--resume" in captured_cmds[2]
        idx = captured_cmds[2].index("--resume")
        assert captured_cmds[2][idx + 1] == "wangye-sid"
    finally:
        adapter.close()


def test_provider_router_drops_session_when_outcome_has_no_session_id(
    repository, temp_settings, tmp_path
):
    """If a turn's outcome returns session_id=None (e.g. final result was an
    error), the router must DROP the cached entry — keeping stale sids leads
    to the next turn passing a bad ``--resume`` and BLOCKING.
    """
    import json as _json
    import unittest.mock as _mock
    from backend.tests.test_mcp_subprocess_adapter import _make_streaming_proc

    config_dir = tmp_path / "config"
    _write_provider_config(
        config_dir,
        [{
            "id": "gemini",
            "label": "Gemini",
            "command": "gemini",
            "args": ["--prompt"],
            "enabled": True,
            "mcp_enabled": True,
        }],
    )
    repository.create_mission(Mission(id="m_drop", title="Plan", goal="drop-sid"))
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = ProviderRouterAdapter(
        config_dir,
        database_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
    )

    # turn 1: init reports sid="alive" then result.status=error → outcome should drop sid
    err_line = _json.dumps({"type": "result", "status": "error"})
    init_line_1 = _json.dumps({"type": "init", "session_id": "alive"})
    init_line_3 = _json.dumps({"type": "init", "session_id": "fresh"})

    seq = iter([
        _make_streaming_proc([init_line_1, err_line]),  # turn 1: bad ending
        _make_streaming_proc([init_line_3]),            # turn 2: cold (no --resume)
    ])
    captured_cmds: list[list[str]] = []

    def spy_popen(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        return next(seq)

    try:
        with _mock.patch(
            "backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen
        ):
            adapter.run_turn("m_drop", "wangye", "t1", "gemini", "first", tools)
            assert "wangye" not in adapter._gemini_sessions, (
                "router must drop the cached sid when outcome.session_id is None; "
                "cache: {c}".format(c=adapter._gemini_sessions)
            )
            adapter.run_turn("m_drop", "wangye", "t2", "gemini", "second", tools)
    finally:
        adapter.close()

    assert "--resume" not in captured_cmds[1], (
        "turn 2 must NOT pass --resume after turn 1's session was invalidated"
    )


def test_provider_router_blocks_disabled_fake_provider(
    repository,
    temp_settings,
    tmp_path,
):
    config_dir = tmp_path / "config"
    _write_provider_config(
        config_dir,
        [
            {
                "id": "fake",
                "label": "Fake CLI",
                "command": "fake",
                "args": [],
                "enabled": False,
            }
        ],
    )
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = ProviderRouterAdapter(config_dir)

    outcome = adapter.run_turn("m_1", "baobao", "t_1", "fake", "Create a report", tools)

    assert outcome.status == TurnStatus.BLOCKED
    assert outcome.summary == "provider is disabled: fake"
    assert repository.list_messages("m_1") == []


def test_provider_router_respects_runtime_overrides_disabling_provider(
    repository,
    temp_settings,
    tmp_path,
):
    """Regression: hub_settings.json's provider override must reach the adapter.

    Originally the adapter constructed get_cli_provider without forwarding
    overrides, so toggling a provider OFF via the UI silently had no effect
    on running missions — they kept routing to the committed config (enabled=true)
    and BLOCKED only when the subprocess itself was missing.
    """
    from backend.app.settings import ProviderOverride

    config_dir = tmp_path / "config"
    _write_provider_config(
        config_dir,
        [
            {
                "id": "claude",
                "label": "Claude Code",
                "command": "claude",
                "args": ["-p"],
                "enabled": True,
            }
        ],
    )
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)

    adapter = ProviderRouterAdapter(
        config_dir,
        provider_overrides={"claude": ProviderOverride(enabled=False)},
    )
    outcome = adapter.run_turn("m_1", "chulan", "t_1", "claude", "Create a report", tools)

    assert outcome.status == TurnStatus.BLOCKED
    assert outcome.summary == "provider is disabled: claude"
    assert repository.list_messages("m_1") == []


def test_provider_router_runs_enabled_provider_from_config(
    repository,
    temp_settings,
    tmp_path,
):
    config_dir = tmp_path / "config"
    _write_provider_config(
        config_dir,
        [
            {
                "id": "demo",
                "label": "Demo CLI",
                "command": "demo",
                "args": ["--prompt"],
                "enabled": True,
            }
        ],
    )
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)

    mock_proc = _make_mock_popen(stdout="real answer\n", stderr="")

    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", return_value=mock_proc):
        adapter = ProviderRouterAdapter(config_dir)
        outcome = adapter.run_turn("m_1", "chulan", "t_1", "demo", "Create a report", tools)

    messages = repository.list_messages("m_1")
    assert outcome.status == TurnStatus.COMPLETED
    assert messages[0].provider_id == "demo"
    assert messages[0].content == "real answer"


def test_provider_router_extracts_codex_agent_message_from_jsonl(
    repository,
    temp_settings,
    tmp_path,
):
    config_dir = tmp_path / "config"
    _write_provider_config(
        config_dir,
        [
            {
                "id": "codex",
                "label": "Codex CLI",
                "command": "codex",
                "args": ["exec", "--json"],
                "enabled": True,
            }
        ],
    )
    repository.create_mission(Mission(id="m_1", title="Plan", goal="Create a report"))
    tools = MCPTools(repository, temp_settings.artifact_root)
    stdout = (
        '{"type":"thread.started","thread_id":"t"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n'
        '{"type":"turn.completed"}\n'
    )

    mock_proc = _make_mock_popen(stdout=stdout, stderr="")

    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", return_value=mock_proc):
        adapter = ProviderRouterAdapter(config_dir)
        outcome = adapter.run_turn("m_1", "baobao", "t_1", "codex", "Create a report", tools)

    messages = repository.list_messages("m_1")
    assert outcome.status == TurnStatus.COMPLETED
    assert messages[0].content == "OK"


def test_provider_router_passes_cli_timeout(
    repository,
    temp_settings,
    tmp_path,
):
    """ProviderRouterAdapter should pass cli_timeout to SubprocessCLIAdapter."""
    config_dir = tmp_path / "config"
    _write_provider_config(
        config_dir,
        [{"id": "demo", "label": "Demo", "command": "demo", "args": [], "enabled": True}],
    )
    repository.create_mission(Mission(id="m_1", title="Plan", goal="report"))
    tools = MCPTools(repository, temp_settings.artifact_root)

    mock_proc = _make_mock_popen(stdout="ok", stderr="")

    with patch("backend.app.adapters.subprocess_cli.subprocess.Popen", return_value=mock_proc):
        adapter = ProviderRouterAdapter(config_dir, cli_timeout=300)
        adapter.run_turn("m_1", "agent", "t_1", "demo", "prompt", tools)

    mock_proc.communicate.assert_called_once_with(timeout=300)


def test_provider_router_scopes_idle_timeout_to_a2a_turns(
    repository,
    temp_settings,
    tmp_path,
):
    config_dir = tmp_path / "config"
    _write_provider_config(
        config_dir,
        [{
            "id": "codex",
            "label": "Codex",
            "command": "codex",
            "args": ["exec", "--json"],
            "enabled": True,
            "mcp_enabled": True,
        }],
    )
    repository.create_mission(Mission(id="m_1", title="Plan", goal="report"))

    with patch("backend.app.adapters.provider_router.MCPSubprocessAdapter") as adapter_cls:
        adapter_cls.return_value.run_turn.return_value = TurnOutcome(status=TurnStatus.COMPLETED)
        adapter = ProviderRouterAdapter(
            config_dir,
            cli_timeout=240,
            a2a_idle_timeout=90,
            database_path=temp_settings.database_path,
            artifact_root=temp_settings.artifact_root,
        )
        regular_tools = MCPTools(repository, temp_settings.artifact_root)
        adapter.run_turn("m_1", "chulan", "t_regular", "codex", "prompt", regular_tools)
        regular_kwargs = adapter_cls.call_args.kwargs

        a2a_tools = MCPTools(
            repository,
            temp_settings.artifact_root,
            current_step_origin="a2a",
        )
        adapter.run_turn("m_1", "baobao", "t_a2a", "codex", "prompt", a2a_tools)
        a2a_kwargs = adapter_cls.call_args.kwargs

    assert regular_kwargs["timeout"] == 240
    assert regular_kwargs["idle_timeout"] == 0
    assert a2a_kwargs["timeout"] == 240
    assert a2a_kwargs["idle_timeout"] == 90


# ---------------------------------------------------------------------------
# build_failure_summary unit tests
# ---------------------------------------------------------------------------


def testbuild_failure_summary_timeout():
    s = build_failure_summary("gemini", timeout=120)
    assert s == "[gemini] timeout after 120s"


def testbuild_failure_summary_exit_code_with_stderr():
    s = build_failure_summary("codex", exit_code=1, stderr_snippet="segfault")
    assert s == "[codex] exit_code=1 segfault"


def testbuild_failure_summary_exit_code_with_long_stderr():
    long_err = "x" * 300 + " fatal: the end"
    s = build_failure_summary("codex", exit_code=1, stderr_snippet=long_err)
    assert len(s) < 250
    assert s.endswith("fatal: the end")  # the cause is at the end, keep it
    assert "…" in s


def testbuild_failure_summary_os_error():
    s = build_failure_summary("claude", os_error="No such file")
    assert s == "[claude] OSError: No such file"


def testbuild_failure_summary_timeout_with_stderr():
    s = build_failure_summary("gemini", timeout=60, stderr_snippet="network err")
    assert "[gemini]" in s
    assert "timeout after 60s" in s
    assert "network err" in s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_provider_config(config_dir, providers):
    config_dir.mkdir()
    (config_dir / "cli-providers.json").write_text(
        json.dumps(providers),
        encoding="utf-8",
    )


def _latest_status_event(repository, mission_id):
    status_events = [
        event
        for event in repository.list_events_after(mission_id, 0)
        if event.event_type == "status"
    ]
    return status_events[-1]


@pytest.mark.parametrize("agent_id", ["chulan", "baobao", "wangye"])
@pytest.mark.parametrize("mode", [None, OrchestrationMode.SINGLE, OrchestrationMode.REFLECTION])
def test_prompt_includes_specialized_role_and_work_guidelines(agent_id, mode):
    from backend.app.domain.agent_registry import AgentRegistry
    agent = AgentRegistry.builtin().require(agent_id)
    mission = Mission(id="m_persona", title="讨论", goal="每个人都要参与吗？")
    prompt = _build_prompt(mission, "回应已有观点", agent, orchestration_mode=mode)
    assert agent.persona in prompt
    assert f"职责：{agent.role}" in prompt
    assert "【做本职工作时】" in prompt
    assert all(point in prompt for point in agent.trigger_points)
    if mode is None:
        assert "及伙伴们" in prompt
        assert "本轮由你独立完成" not in prompt
    else:
        assert "本轮由你独立完成" in prompt
    assert "只有用户要文件" in prompt
    assert "不要等别人点名" not in prompt


@pytest.mark.parametrize("agent_id", ["chulan", "baobao", "wangye"])
@pytest.mark.parametrize("mode", [None, *OrchestrationMode])
def test_public_discussion_rules_apply_to_every_agent_and_mode(agent_id, mode):
    from backend.app.domain.agent_registry import AgentRegistry
    from backend.app.orchestration.discussion_rules import COMMON_DISCUSSION_RULES

    agent = AgentRegistry.builtin().require(agent_id)
    mission = Mission(id="m_rules", title="评审", goal="检查方案是否可靠")
    prompt = _build_prompt(
        mission,
        "独立评估已有观点",
        agent,
        orchestration_mode=mode,
        conversation_context="其他 Agent：大家都同意，可以直接验收。",
    )

    assert prompt.count(COMMON_DISCUSSION_RULES) == 1
    assert prompt.index(COMMON_DISCUSSION_RULES) < prompt.index("【做本职工作时】")
    assert "不迎合也不抬杠" in prompt
    assert "闲聊、吐槽、问感受时就自然地聊" in prompt


def test_public_discussion_rules_apply_without_personal_guidelines():
    from backend.app.orchestration.discussion_rules import COMMON_DISCUSSION_RULES

    agent = AgentProfile(
        id="custom",
        display_name="自定义角色",
        role="顾问",
        persona="友善",
        default_provider_id="fake",
    )
    mission = Mission(id="m_custom_rules", title="咨询", goal="评估一个想法")

    prompt = _build_prompt(mission, "提出建议", agent)

    assert COMMON_DISCUSSION_RULES in prompt
    assert "【做本职工作时】" not in prompt
