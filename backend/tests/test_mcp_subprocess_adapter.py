import sys
from pathlib import Path

import pytest

from backend.app.adapters.mcp_subprocess import (
    MCPSubprocessAdapter,
    ProcessWaitReason,
    _augment_prompt_for_mcp,
    _wait_for_process_with_activity,
)
from backend.app.domain.models import (
    Conversation,
    Message,
    MessageRole,
    Mission,
    MissionKind,
    TurnStatus,
)
from backend.app.mcp.tools import MCPTools


FAKE_CLI = Path(__file__).resolve().parent / "_fake_mcp_cli.py"


@pytest.fixture()
def seeded_mission(repository):
    conv = Conversation(id="c_adapter", title="t")
    repository.create_conversation(conv)
    mission = Mission(
        id="m_adapter",
        title="Plan",
        goal="ship feature",
        conversation_id=conv.id,
    )
    repository.create_mission(mission)
    return mission


def _adapter(temp_settings, **extra) -> MCPSubprocessAdapter:
    return MCPSubprocessAdapter(
        command=sys.executable,
        args=[str(FAKE_CLI)],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
        extra_env=extra,
    )


def test_completes_via_mcp_when_fake_cli_publishes(repository, temp_settings, seeded_mission):
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = _adapter(temp_settings, YIREN_FAKE_MCP_MODE="publish", YIREN_FAKE_MCP_CONTENT="MCP路径成功")

    outcome = adapter.run_turn(seeded_mission.id, "baobao", "t_mcp", "codex", "请发声", tools)

    assert outcome.status == TurnStatus.COMPLETED
    assert outcome.summary and "via MCP" in outcome.summary

    agent_messages = [m for m in repository.list_messages(seeded_mission.id) if m.role.value == "agent"]
    assert len(agent_messages) == 1
    assert agent_messages[0].content == "MCP路径成功"
    assert agent_messages[0].turn_id == "t_mcp"
    assert agent_messages[0].provider_id == "codex"


def test_blocks_when_cli_skips_publish_message_envelope(repository, temp_settings, seeded_mission):
    """ADR Decision 5: stdout JSON envelope must NOT be synthesised as agent message.
    MCPSubprocessAdapter must return BLOCKED when publish_message was not called."""
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = _adapter(temp_settings, YIREN_FAKE_MCP_MODE="envelope", YIREN_FAKE_MCP_CONTENT="走 stdout 回退")

    outcome = adapter.run_turn(seeded_mission.id, "baobao", "t_envelope", "codex", "请发声", tools)

    assert outcome.status == TurnStatus.BLOCKED
    assert "publish_message" in (outcome.summary or "")
    agent_messages = [m for m in repository.list_messages(seeded_mission.id) if m.role.value == "agent"]
    assert len(agent_messages) == 0, "stdout envelope must not be synthesised as agent message"


def test_blocks_when_cli_outputs_nothing(repository, temp_settings, seeded_mission):
    """ADR Decision 5: empty stdout must NOT produce a fake agent message."""
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = _adapter(temp_settings, YIREN_FAKE_MCP_MODE="noop")

    outcome = adapter.run_turn(seeded_mission.id, "baobao", "t_noop", "codex", "请发声", tools)

    assert outcome.status == TurnStatus.BLOCKED
    assert "publish_message" in (outcome.summary or "")
    agent_messages = [m for m in repository.list_messages(seeded_mission.id) if m.role.value == "agent"]
    assert len(agent_messages) == 0, "empty stdout must not be synthesised as agent message"


def test_blocks_when_cli_exits_nonzero(repository, temp_settings, seeded_mission):
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = _adapter(temp_settings, YIREN_FAKE_MCP_MODE="exit_nonzero")

    outcome = adapter.run_turn(seeded_mission.id, "baobao", "t_fail", "codex", "请发声", tools)

    assert outcome.status == TurnStatus.BLOCKED
    assert "[codex]" in (outcome.summary or "")


def test_writes_codex_config_with_mcp_server_block(tmp_path, repository, temp_settings, seeded_mission):
    """The adapter must materialise a codex-style config TOML pointing at the MCP server."""
    tools = MCPTools(repository, temp_settings.artifact_root)
    captured = {}

    def capture_inspector(config_path: Path) -> None:
        captured["text"] = config_path.read_text(encoding="utf-8")

    adapter = MCPSubprocessAdapter(
        command=sys.executable,
        args=[str(FAKE_CLI)],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
        extra_env={"YIREN_FAKE_MCP_MODE": "publish"},
        on_config_written=capture_inspector,
    )

    adapter.run_turn(seeded_mission.id, "baobao", "t_cfg", "codex", "请发声", tools)

    text = captured.get("text", "")
    assert "[mcp_servers.yiren_hub]" in text
    assert "command =" in text
    assert "backend.app.mcp.server" in text
    assert "YIREN_MCP_MISSION_ID" in text
    assert seeded_mission.id in text


def _make_mock_proc(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Return a mock subprocess.Popen-like object."""
    import unittest.mock as _mock
    proc = _mock.MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    return proc


def _make_streaming_proc(stdout_lines: list[str], returncode: int = 0):
    """Mock Popen whose stdout.readline yields the given lines then EOF."""
    import unittest.mock as _mock
    proc = _mock.MagicMock()
    proc.returncode = returncode
    lines = [(line if line.endswith("\n") else line + "\n") for line in stdout_lines] + [""]
    proc.stdout.readline.side_effect = lines
    proc.stderr.readline.side_effect = [""]
    return proc


def test_activity_wait_uses_idle_deadline(monkeypatch):
    import subprocess as _subprocess

    times = iter([100.0, 100.0, 191.0])
    monkeypatch.setattr(
        "backend.app.adapters.mcp_subprocess.time.time",
        lambda: next(times),
    )

    class SilentProc:
        def wait(self, timeout):
            raise _subprocess.TimeoutExpired(["fake"], timeout)

    reason = _wait_for_process_with_activity(
        SilentProc(),
        hard_timeout=240,
        idle_timeout=90,
        last_activity_at=lambda: 100.0,
        refresh_activity=lambda: None,
    )
    assert reason is ProcessWaitReason.IDLE_TIMEOUT


def test_activity_wait_renews_idle_deadline(monkeypatch):
    import subprocess as _subprocess

    times = iter([100.0, 100.0, 150.0, 170.0, 241.0])
    monkeypatch.setattr(
        "backend.app.adapters.mcp_subprocess.time.time",
        lambda: next(times),
    )
    activity = {"at": 100.0, "refreshes": 0}

    def refresh():
        activity["refreshes"] += 1
        if activity["refreshes"] == 2:
            activity["at"] = 150.0

    class ActiveProc:
        def wait(self, timeout):
            raise _subprocess.TimeoutExpired(["fake"], timeout)

    reason = _wait_for_process_with_activity(
        ActiveProc(),
        hard_timeout=300,
        idle_timeout=90,
        last_activity_at=lambda: activity["at"],
        refresh_activity=refresh,
    )
    assert reason is ProcessWaitReason.IDLE_TIMEOUT
    assert activity["refreshes"] == 4


def test_activity_wait_can_disable_idle_timeout(monkeypatch):
    import subprocess as _subprocess

    times = iter([100.0, 100.0, 221.0])
    monkeypatch.setattr(
        "backend.app.adapters.mcp_subprocess.time.time",
        lambda: next(times),
    )

    class SilentProc:
        def wait(self, timeout):
            raise _subprocess.TimeoutExpired(["fake"], timeout)

    reason = _wait_for_process_with_activity(
        SilentProc(),
        hard_timeout=120,
        idle_timeout=0,
        last_activity_at=lambda: 0.0,
        refresh_activity=lambda: None,
    )
    assert reason is ProcessWaitReason.HARD_TIMEOUT


def test_adapter_blocks_and_kills_process_on_a2a_idle_timeout(
    repository, temp_settings, seeded_mission,
):
    import unittest.mock as _mock

    tools = MCPTools(repository, temp_settings.artifact_root)
    proc = _make_streaming_proc([], returncode=-15)
    adapter = MCPSubprocessAdapter(
        command=sys.executable,
        args=[str(FAKE_CLI)],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=240,
        idle_timeout=90,
    )

    with _mock.patch(
        "backend.app.adapters.mcp_subprocess.subprocess.Popen",
        return_value=proc,
    ), _mock.patch(
        "backend.app.adapters.mcp_subprocess._wait_for_process_with_activity",
        return_value=ProcessWaitReason.IDLE_TIMEOUT,
    ), _mock.patch(
        "backend.app.adapters.mcp_subprocess.kill_process_tree",
    ) as kill:
        outcome = adapter.run_turn(
            seeded_mission.id,
            "baobao",
            "t_idle",
            "codex",
            "请发声",
            tools,
        )

    assert outcome.status == TurnStatus.BLOCKED
    assert "idle timeout after 90s" in (outcome.summary or "")
    assert outcome.failure_code == "idle_timeout"
    kill.assert_called_once_with(proc)


def test_claude_writes_json_mcp_config(repository, temp_settings, seeded_mission):
    """Claude branch writes mcp.json (JSON) instead of a codex TOML."""
    import json as _json
    import unittest.mock as _mock

    tools = MCPTools(repository, temp_settings.artifact_root)
    captured = {}

    def capture_inspector(config_path: Path) -> None:
        captured["path"] = str(config_path)
        captured["text"] = config_path.read_text(encoding="utf-8")

    adapter = MCPSubprocessAdapter(
        command="claude",
        args=["-p"],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
        on_config_written=capture_inspector,
    )

    with _mock.patch(
        "backend.app.adapters.mcp_subprocess.subprocess.Popen",
        return_value=_make_mock_proc(),
    ):
        adapter.run_turn(seeded_mission.id, "chulan", "t_claude", "claude", "请发声", tools)

    assert captured.get("path", "").endswith("mcp.json"), (
        "claude should write mcp.json, not config.toml. got: {path}".format(path=captured.get("path"))
    )
    config = _json.loads(captured["text"])
    assert "mcpServers" in config
    assert "yiren_hub" in config["mcpServers"]
    server = config["mcpServers"]["yiren_hub"]
    assert "backend.app.mcp.server" in " ".join(server.get("args", []))
    assert "YIREN_MCP_MISSION_ID" in server.get("env", {})
    assert seeded_mission.id in server["env"]["YIREN_MCP_MISSION_ID"]


def test_claude_cmd_injects_mcp_config_flags(repository, temp_settings, seeded_mission):
    """Claude adapter must pass --mcp-config and --strict-mcp-config to the CLI."""
    import unittest.mock as _mock

    tools = MCPTools(repository, temp_settings.artifact_root)
    captured_cmd: list = []

    def spy_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return _make_mock_proc()

    adapter = MCPSubprocessAdapter(
        command="claude",
        args=["--no-session-persistence", "-p"],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
    )

    with _mock.patch("backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen):
        adapter.run_turn(seeded_mission.id, "chulan", "t_claude_cmd", "claude", "请发声", tools)

    assert "--mcp-config" in captured_cmd, "--mcp-config missing from cmd: {cmd}".format(cmd=captured_cmd)
    assert "--strict-mcp-config" in captured_cmd, "--strict-mcp-config missing from cmd: {cmd}".format(cmd=captured_cmd)
    # claude binary should be the first token.
    assert captured_cmd[0] == "claude"


def test_verification_cli_env_and_workspace_are_isolated(repository, temp_settings):
    import unittest.mock as _mock

    mission = Mission(
        id="m_verify_isolated",
        title="验证 Claude Code 接入",
        goal="接入验证",
        kind=MissionKind.PROVIDER_VERIFICATION,
    )
    repository.create_mission(mission)
    tools = MCPTools(repository, temp_settings.artifact_root)
    captured: dict = {}

    def spy_popen(cmd, **kwargs):
        captured["env"] = dict(kwargs.get("env", {}))
        captured["cwd"] = kwargs.get("cwd")
        return _make_mock_proc()

    adapter = MCPSubprocessAdapter(
        command="claude",
        args=["-p"],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
    )
    with _mock.patch(
        "backend.app.adapters.mcp_subprocess.subprocess.Popen",
        side_effect=spy_popen,
    ):
        adapter.run_turn(
            mission.id,
            "chulan",
            "t_verify",
            "claude",
            "连接诊断",
            tools,
        )

    assert captured["cwd"]
    assert Path(captured["cwd"]).name == "verification-workspace"
    assert not any(key.startswith("YIREN_MCP_") for key in captured["env"])


def test_direct_message_write_without_mcp_receipt_does_not_complete(
    repository,
    temp_settings,
    seeded_mission,
):
    import unittest.mock as _mock

    tools = MCPTools(repository, temp_settings.artifact_root)

    def spy_popen(cmd, **kwargs):
        repository.add_message(
            Message(
                id="msg_forged",
                mission_id=seeded_mission.id,
                conversation_id=seeded_mission.conversation_id,
                role=MessageRole.AGENT,
                agent_id="chulan",
                turn_id="t_forged",
                provider_id="claude",
                content="直接写库伪造成功",
            )
        )
        return _make_mock_proc()

    adapter = MCPSubprocessAdapter(
        command="claude",
        args=["-p"],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
    )
    with _mock.patch(
        "backend.app.adapters.mcp_subprocess.subprocess.Popen",
        side_effect=spy_popen,
    ):
        outcome = adapter.run_turn(
            seeded_mission.id,
            "chulan",
            "t_forged",
            "claude",
            "请发声",
            tools,
        )

    assert outcome.status == TurnStatus.BLOCKED
    assert "publish_message" in (outcome.summary or "")


def test_gemini_writes_settings_json_under_temp_home(repository, temp_settings, seeded_mission):
    """Gemini injects MCP via GEMINI_CLI_HOME without touching real ~/.gemini."""
    import json as _json
    import unittest.mock as _mock

    tools = MCPTools(repository, temp_settings.artifact_root)
    captured = {}
    captured_env: dict = {}
    captured_cmd: list[str] = []

    def capture_inspector(config_path: Path) -> None:
        captured["path"] = str(config_path)
        captured["text"] = config_path.read_text(encoding="utf-8")

    def spy_popen(cmd, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        captured_cmd.extend(cmd)
        return _make_mock_proc()

    adapter = MCPSubprocessAdapter(
        command="gemini",
        args=["--approval-mode", "yolo", "--prompt"],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
        on_config_written=capture_inspector,
    )

    with _mock.patch("backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen):
        adapter.run_turn(seeded_mission.id, "wangye", "t_gemini", "gemini", "请发声", tools)

    assert "settings.json" in captured.get("path", ""), (
        "gemini should write settings.json, got: {path}".format(path=captured.get("path"))
    )
    config = _json.loads(captured["text"])
    assert "mcpServers" in config
    assert "yiren_hub" in config["mcpServers"]
    server = config["mcpServers"]["yiren_hub"]
    assert "backend.app.mcp.server" in " ".join(server.get("args", []))
    assert seeded_mission.id in server["env"].get("YIREN_MCP_MISSION_ID", "")

    assert "GEMINI_CLI_HOME" in captured_env, "must redirect Gemini via GEMINI_CLI_HOME"
    assert captured["path"].startswith(captured_env["GEMINI_CLI_HOME"])
    assert "--skip-trust" in captured_cmd, "gemini cmd must include --skip-trust"


def test_antigravity_registers_mcp_as_workspace_plugin_not_global_config(
    repository, temp_settings, seeded_mission, tmp_path, monkeypatch
):
    """agy gets the hub through its cwd's .agents/plugins.json; ~/.gemini is never touched,
    so concurrent agy runs cannot see each other's server."""
    import json as _json
    import unittest.mock as _mock
    from pathlib import Path as _Path
    from backend.app.adapters import antigravity_config as _agc

    monkeypatch.setattr(_Path, "home", classmethod(lambda cls: tmp_path))

    tools = MCPTools(repository, temp_settings.artifact_root)
    captured_cmd: list[str] = []
    captured_env: dict = {}
    captured: dict = {}

    def capture_inspector(config_path: Path) -> None:
        captured["config_path"] = config_path

    def spy_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        captured_env.update(kwargs.get("env", {}))
        cwd = Path(kwargs["cwd"])
        captured["cwd"] = cwd
        # Resolve the server exactly as agy does, while the run is live.
        entry = _json.loads((cwd / ".agents" / "plugins.json").read_text())["entries"][0]
        plugin = Path(entry["path"]) / entry["include_only"][0]
        captured["servers"] = _json.loads((plugin / "mcp_config.json").read_text())["mcpServers"]
        # Older Unity builds need the lock exclusively; they must be shut out while agy runs.
        try:
            with _agc._flock(timeout=0):
                captured["legacy_could_inject"] = True
        except TimeoutError:
            captured["legacy_could_inject"] = False
        return _make_mock_proc()

    adapter = MCPSubprocessAdapter(
        command="agy",
        args=[],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=120,
        on_config_written=capture_inspector,
    )

    with _mock.patch("backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen):
        adapter.run_turn(seeded_mission.id, "wangye", "t_agy", "antigravity", "请发声", tools)

    server = captured["servers"][_agc.PLUGIN_SERVER]
    assert "backend.app.mcp.server" in " ".join(server.get("args", []))
    assert seeded_mission.id in server["env"].get("YIREN_MCP_MISSION_ID", "")
    assert "PYTHONPATH" in server["env"]
    assert captured["config_path"].is_relative_to(captured["cwd"])
    assert not _agc.global_mcp_config_path().exists()
    assert captured["legacy_could_inject"] is False
    # The prompt names the namespaced server agy shows the model.
    assert _agc.SERVER_NAME in captured_cmd[-1]

    assert "GEMINI_CLI_HOME" not in captured_env
    assert "--dangerously-skip-permissions" in captured_cmd
    assert "-p" in captured_cmd
    assert "--add-dir" in captured_cmd
    # The host owns the renewable deadline; disable the CLI's fixed timer.
    assert "--print-timeout" in captured_cmd
    assert captured_cmd[captured_cmd.index("--print-timeout") + 1] == "0"
    assert captured_cmd[captured_cmd.index("--output-format") + 1] == "stream-json"


def test_gemini_home_override_used_when_provided(
    repository, temp_settings, seeded_mission, tmp_path
):
    """When gemini_home_override is set, settings.json lands under it (not a per-turn tempdir)."""
    import unittest.mock as _mock

    tools = MCPTools(repository, temp_settings.artifact_root)
    persistent = tmp_path / "persistent-gemini-home"
    captured = {}
    captured_env: dict = {}

    def capture_inspector(config_path: Path) -> None:
        captured["path"] = str(config_path)

    def spy_popen(cmd, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return _make_mock_proc()

    adapter = MCPSubprocessAdapter(
        command="gemini",
        args=["--approval-mode", "yolo", "--prompt"],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
        on_config_written=capture_inspector,
        gemini_home_override=persistent,
    )

    with _mock.patch("backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen):
        adapter.run_turn(seeded_mission.id, "wangye", "t_pers", "gemini", "请发声", tools)

    assert captured["path"].startswith(str(persistent))
    assert captured_env["GEMINI_CLI_HOME"] == str(persistent)


def test_gemini_resume_session_id_injects_resume_flag(
    repository, temp_settings, seeded_mission
):
    """When gemini_resume_session_id is supplied, cmd includes --resume <uuid>."""
    import unittest.mock as _mock

    tools = MCPTools(repository, temp_settings.artifact_root)
    captured_cmd: list[str] = []

    def spy_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return _make_mock_proc()

    adapter = MCPSubprocessAdapter(
        command="gemini",
        args=["--approval-mode", "yolo", "--prompt"],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
        gemini_resume_session_id="prev-sid-xyz",
    )

    with _mock.patch("backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen):
        adapter.run_turn(seeded_mission.id, "wangye", "t_resume", "gemini", "请发声", tools)

    assert "--resume" in captured_cmd
    resume_idx = captured_cmd.index("--resume")
    assert captured_cmd[resume_idx + 1] == "prev-sid-xyz"


def test_gemini_outcome_captures_session_id_from_init_event(
    repository, temp_settings, seeded_mission
):
    """When gemini emits a stream-json init line, session_id flows into TurnOutcome."""
    import json as _json
    import unittest.mock as _mock

    tools = MCPTools(repository, temp_settings.artifact_root)
    init_line = _json.dumps({
        "type": "init",
        "session_id": "abc-123-uuid",
        "model": "gemini-3-flash-preview",
    })

    def spy_popen(cmd, **kwargs):
        return _make_streaming_proc([init_line])

    adapter = MCPSubprocessAdapter(
        command="gemini",
        args=["--approval-mode", "yolo", "--prompt"],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
    )

    with _mock.patch("backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen):
        outcome = adapter.run_turn(
            seeded_mission.id, "wangye", "t_session", "gemini", "请发声", tools
        )

    assert outcome.session_id == "abc-123-uuid"


def test_gemini_cli_receives_the_same_persona_prompt_as_other_providers(repository, temp_settings, seeded_mission):
    import unittest.mock as _mock

    tools = MCPTools(repository, temp_settings.artifact_root)
    captured_cmd: list[str] = []
    long_history = "\n".join(f"- 历史消息 {idx}: " + ("很长的上下文 " * 20) for idx in range(30))
    prompt = "\n".join(
        [
            "聊天室模式：只允许通过工具发声。",
            "你的角色是王也。",
            "最近对话：",
            long_history,
            "Mission: 真实 CLI 演示",
            "Goal: 请三位 Agent 各用一句话说明当前联调状态。",
            "Turn purpose: 分析风险",
            "如需接力，可以 invoke_agent。",
        ]
    )

    def spy_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return _make_mock_proc()

    adapter = MCPSubprocessAdapter(
        command="gemini",
        args=["--approval-mode", "yolo", "--prompt"],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
    )

    with _mock.patch("backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen):
        adapter.run_turn(seeded_mission.id, "wangye", "t_gemini_compact", "gemini", prompt, tools)

    # The prompt is already bounded upstream; trimming it again per provider
    # used to drop the persona and rules for Gemini only.
    assert captured_cmd[-1] == _augment_prompt_for_mcp(prompt)


def test_mcp_prompt_guidance_is_added_even_when_user_mentions_publish_message():
    prompt = "请解释 publish_message 这个词，但不要执行。"

    augmented = _augment_prompt_for_mcp(prompt)

    assert augmented != prompt
    assert "【发声】" in augmented
    assert "yiren_hub.publish_message" in augmented


def test_debug_files_are_provider_specific_for_fallback_attempts(
    monkeypatch,
    tmp_path,
    repository,
    temp_settings,
    seeded_mission,
):
    monkeypatch.setenv("YIREN_MCP_DEBUG_DIR", str(tmp_path))
    tools = MCPTools(repository, temp_settings.artifact_root)

    first = _adapter(temp_settings, YIREN_FAKE_MCP_MODE="noop")
    second = _adapter(temp_settings, YIREN_FAKE_MCP_MODE="publish", YIREN_FAKE_MCP_CONTENT="fallback ok")

    first.run_turn(seeded_mission.id, "wangye", "turn_debug", "gemini", "请发声", tools)
    second.run_turn(seeded_mission.id, "wangye", "turn_debug", "codex", "请发声", tools)

    assert (tmp_path / "turn_debug.gemini.stdout").exists()
    assert (tmp_path / "turn_debug.gemini.stderr").exists()
    assert (tmp_path / "turn_debug.gemini.config").exists()
    assert (tmp_path / "turn_debug.codex.stdout").exists()
    assert (tmp_path / "turn_debug.codex.stderr").exists()
    assert (tmp_path / "turn_debug.codex.config").exists()


def test_invoke_mode_rejects_unknown_target_via_mcp_server(repository, temp_settings, seeded_mission):
    """When valid_agent_ids is passed to the MCP server via env, invoking an
    unknown target must return isError=true and exit nonzero → BLOCKED."""
    tools = MCPTools(
        repository,
        temp_settings.artifact_root,
        valid_agent_ids={"chulan", "baobao", "wangye"},
        max_a2a_invocations=8,
    )
    adapter = _adapter(
        temp_settings,
        YIREN_FAKE_MCP_MODE="invoke",
        YIREN_FAKE_MCP_TARGET="unknown_bot",
        YIREN_FAKE_MCP_PURPOSE="非法目标",
    )

    outcome = adapter.run_turn(seeded_mission.id, "chulan", "t_bad_target", "codex", "请召唤", tools)

    assert outcome.status == TurnStatus.BLOCKED
    # No a2a.request event should have been written
    events = repository.list_events_after(seeded_mission.id, 0)
    a2a_request_events = [e for e in events if e.event_type == "a2a.request"]
    assert len(a2a_request_events) == 0, "a2a.request must not be written for unknown target"


def test_invoke_mode_budget_overflow_blocks_turn(repository, temp_settings, seeded_mission):
    """When max_a2a_invocations=1 and the CLI calls invoke_agent twice, the
    second call returns isError (a2a 深度耗尽), the CLI exits nonzero → BLOCKED."""
    tools = MCPTools(
        repository,
        temp_settings.artifact_root,
        valid_agent_ids={"chulan", "baobao", "wangye"},
        max_a2a_invocations=1,
    )
    adapter = _adapter(
        temp_settings,
        YIREN_FAKE_MCP_MODE="invoke",
        YIREN_FAKE_MCP_INVOKE_COUNT="2",  # call invoke_agent twice
        YIREN_FAKE_MCP_TARGET="baobao",
        YIREN_FAKE_MCP_PURPOSE="核对档案",
    )

    outcome = adapter.run_turn(seeded_mission.id, "chulan", "t_budget", "codex", "请召唤两次", tools)

    assert outcome.status == TurnStatus.BLOCKED
    # Exactly one a2a.request should exist (first call succeeded)
    events = repository.list_events_after(seeded_mission.id, 0)
    a2a_request_events = [e for e in events if e.event_type == "a2a.request"]
    assert len(a2a_request_events) == 1, "only first invoke_agent should have written a2a.request"
    # The summary should reference the budget error
    assert outcome.summary is not None
    assert "a2a 深度耗尽" in (outcome.summary or ""), (
        "expected 'a2a 深度耗尽' in summary, got: {s}".format(s=outcome.summary)
    )


def test_invoke_mode_writes_a2a_request_without_runner_queue(repository, temp_settings, seeded_mission):
    """Without a runner queue (subprocess path), invoke_agent persists an a2a.request
    event to the DB and the turn completes normally via publish_message."""
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = _adapter(temp_settings, YIREN_FAKE_MCP_MODE="invoke")

    outcome = adapter.run_turn(seeded_mission.id, "chulan", "t_invoke", "codex", "请召唤宝宝", tools)

    assert outcome.status == TurnStatus.COMPLETED

    # The a2a.request event should be persisted in the DB
    events = repository.list_events_after(seeded_mission.id, 0)
    a2a_request_events = [e for e in events if e.event_type == "a2a.request"]
    assert len(a2a_request_events) == 1
    assert a2a_request_events[0].payload["target_agent_id"] == "baobao"
    assert a2a_request_events[0].payload["sender_turn_id"] == "t_invoke"


def test_invoke_mode_runner_picks_up_a2a_request(repository, temp_settings, seeded_mission):
    """Full integration: fake CLI calls invoke_agent via MCP subprocess, then the
    runner picks up the a2a.request event and executes a baobao turn."""
    from backend.app.adapters.base import CLIAdapter, TurnOutcome
    from backend.app.domain.models import TurnStatus as TS
    from backend.app.orchestration.runner import MissionRunner

    class SubprocessInvokingAdapter(CLIAdapter):
        """For chulan: use real MCP subprocess to call invoke_agent + publish_message.
        For baobao: use simple in-process publish_message."""

        def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools_arg):
            if agent_id == "chulan":
                inner_adapter = _adapter(
                    temp_settings,
                    YIREN_FAKE_MCP_MODE="invoke",
                    YIREN_FAKE_MCP_TARGET="baobao",
                    YIREN_FAKE_MCP_PURPOSE="核对异闻物档案",
                    YIREN_FAKE_MCP_CONTENT="召唤完成",
                )
                return inner_adapter.run_turn(mission_id, agent_id, turn_id, provider_id, prompt, tools_arg)
            # baobao turn: just publish
            tools_arg.publish_message(mission_id, agent_id, turn_id, "宝宝收到", provider_id)
            return TurnOutcome(status=TS.COMPLETED)

    runner = MissionRunner(repository, temp_settings, SubprocessInvokingAdapter())
    result = runner.run(seeded_mission.id)

    # mission should complete (not block)
    assert result.status.value == "completed"

    messages = repository.list_messages(seeded_mission.id)
    agent_messages = [m for m in messages if m.role.value == "agent"]
    agent_ids_spoken = {m.agent_id for m in agent_messages}
    assert "chulan" in agent_ids_spoken, "chulan should have spoken"
    assert "baobao" in agent_ids_spoken, "baobao should have been invoked and spoken"


def test_gemini_outcome_drops_session_id_when_final_result_is_error(
    repository, temp_settings, seeded_mission,
):
    """If Gemini's stream ends with {type:result, status:error}, the on-disk
    session file is likely not persisted cleanly, so the next turn's
    ``--resume <sid>`` would fail with 'Invalid session identifier'. The
    adapter must NOT surface that sid in TurnOutcome — return None instead so
    the router drops the cached entry and the next turn cold-starts.
    """
    import json as _json
    import unittest.mock as _mock

    tools = MCPTools(repository, temp_settings.artifact_root)
    init_line = _json.dumps({"type": "init", "session_id": "broken-sid"})
    err_line = _json.dumps({
        "type": "result", "status": "error", "error": "RESOURCE_EXHAUSTED",
    })

    def spy_popen(cmd, **kwargs):
        return _make_streaming_proc([init_line, err_line])

    adapter = MCPSubprocessAdapter(
        command="gemini",
        args=["--approval-mode", "yolo", "--prompt"],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
    )

    with _mock.patch("backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen):
        outcome = adapter.run_turn(
            seeded_mission.id, "wangye", "t_err_drop", "gemini", "请发声", tools
        )

    assert outcome.session_id is None, (
        "session_id from init must be dropped when stream ends with "
        "result.status=error; got: {sid}".format(sid=outcome.session_id)
    )


def test_gemini_retries_without_resume_on_invalid_session_identifier(
    repository, temp_settings, seeded_mission,
):
    """When Gemini exits with 'Invalid session identifier' (the resume target
    file is gone/corrupted), the adapter must retry once within the same
    run_turn, this time WITHOUT --resume, so the agent recovers as a cold
    start rather than propagating a hard BLOCKED to the runner.
    """
    import json as _json
    import unittest.mock as _mock

    tools = MCPTools(repository, temp_settings.artifact_root)
    captured_cmds: list[list[str]] = []

    def proc_invalid_sid():
        proc = _mock.MagicMock()
        proc.returncode = 42
        proc.stdout.readline.side_effect = [""]
        proc.stderr.readline.side_effect = [
            "Error resuming session: Invalid session identifier \"bad-sid\".\n",
            "",
        ]
        return proc

    def proc_cold_success():
        init = _json.dumps({"type": "init", "session_id": "fresh-sid"})
        return _make_streaming_proc([init])

    procs = iter([proc_invalid_sid(), proc_cold_success()])

    def spy_popen(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        return next(procs)

    adapter = MCPSubprocessAdapter(
        command="gemini",
        args=["--approval-mode", "yolo", "--prompt"],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=10,
        gemini_resume_session_id="bad-sid",
    )

    with _mock.patch("backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen):
        outcome = adapter.run_turn(
            seeded_mission.id, "wangye", "t_invalid_retry", "gemini", "请发声", tools
        )

    assert len(captured_cmds) == 2, (
        "adapter must retry once without --resume; saw {n} Popen call(s)".format(
            n=len(captured_cmds),
        )
    )
    assert "--resume" in captured_cmds[0], "first attempt must include the cached --resume sid"
    assert "--resume" not in captured_cmds[1], (
        "second attempt must drop --resume to recover from corrupted session"
    )
    # Retry's init event produced 'fresh-sid' — that should flow into the outcome
    # so the router can cache it for future turns.
    assert outcome.session_id == "fresh-sid"


@pytest.mark.parametrize("timeout_stream", ["legacy_stdout", "current_stderr"])
def test_antigravity_exit0_selftimeout_blocks_with_timeout_summary(
    repository, temp_settings, seeded_mission, tmp_path, monkeypatch, timeout_stream
):
    """agy may exit 0 after its own --print-timeout fires without publishing.
    We surface a timeout-flavoured BLOCK, not the generic 'did not publish'."""
    import unittest.mock as _mock
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", classmethod(lambda cls: tmp_path))
    tools = MCPTools(repository, temp_settings.artifact_root)

    def spy_popen(cmd, **kwargs):
        import io
        proc = _make_streaming_proc(
            ["timed out waiting for response"] if timeout_stream == "legacy_stdout" else [],
            returncode=0,
        )
        if timeout_stream == "current_stderr":
            proc.stderr = io.StringIO("[agy] print timeout after 3m50s with turn in progress; returning partial output\n")
        return proc

    adapter = MCPSubprocessAdapter(
        command="agy", args=[],
        db_path=temp_settings.database_path,
        artifact_root=temp_settings.artifact_root,
        timeout=120,
    )
    with _mock.patch("backend.app.adapters.mcp_subprocess.subprocess.Popen", side_effect=spy_popen):
        outcome = adapter.run_turn(seeded_mission.id, "wangye", "t_to", "antigravity", "请发声", tools)

    assert outcome.status == TurnStatus.BLOCKED
    assert "timeout" in (outcome.summary or "").lower()
    assert outcome.failure_code == "provider_timeout"
    assert outcome.session_id is None  # no SESSION_STARTED event from agy self-timeout


def test_adapter_emits_turn_usage_event_when_parser_returns_usage(
    repository, temp_settings, seeded_mission
):
    """S5: when the parser extracts usage from stdout, the adapter should persist a turn.usage event."""
    tools = MCPTools(repository, temp_settings.artifact_root)
    adapter = _adapter(temp_settings, YIREN_FAKE_MCP_MODE="usage")

    outcome = adapter.run_turn(seeded_mission.id, "baobao", "t_usage", "codex", "请发声", tools)

    assert outcome.status == TurnStatus.COMPLETED

    events = repository.list_events_after(seeded_mission.id, 0)
    usage_events = [e for e in events if e.event_type == "turn.usage"]
    assert len(usage_events) == 1, f"expected 1 turn.usage event, got {len(usage_events)}"
    payload = usage_events[0].payload
    assert payload["agent_id"] == "baobao"
    assert payload["provider_id"] == "codex"
    assert payload["turn_id"] == "t_usage"
    usage = payload["usage"]
    assert usage["input_tokens"] == 100
    assert usage["cached_input_tokens"] == 10
    assert usage["output_tokens"] == 50
    assert usage["reasoning_output_tokens"] == 5


@pytest.mark.parametrize("published,tool_error,recovered", [
    (True, False, True), (False, False, False), (True, True, False),
])
def test_host_timeout_preserves_published_message_only_after_successful_tools(
    repository, temp_settings, seeded_mission, monkeypatch,
    published, tool_error, recovered,
):
    tools = MCPTools(repository, temp_settings.artifact_root)
    proc = _make_streaming_proc([], returncode=-15)
    turn_id = "t_timeout_publish"

    def wait_for_timeout(*args, **kwargs):
        if published:
            tools.publish_message(seeded_mission.id, "baobao", turn_id, "已完成", "codex")
        if tool_error:
            repository.add_event(seeded_mission.id, "tool.error", {
                "turn_id": turn_id, "tool_name": "invoke_agent", "error": "handoff failed",
            })
        return ProcessWaitReason.HARD_TIMEOUT

    monkeypatch.setattr("backend.app.adapters.mcp_subprocess.subprocess.Popen", lambda *a, **kw: proc)
    monkeypatch.setattr("backend.app.adapters.mcp_subprocess._wait_for_process_with_activity", wait_for_timeout)
    monkeypatch.setattr("backend.app.adapters.mcp_subprocess._mcp_published_message", lambda *a: published)
    monkeypatch.setattr("backend.app.adapters.mcp_subprocess.kill_process_tree", lambda proc: None)
    outcome = _adapter(temp_settings).run_turn(
        seeded_mission.id, "baobao", turn_id, "codex", "请发声", tools,
    )
    assert outcome.status == (TurnStatus.COMPLETED if recovered else TurnStatus.BLOCKED)
    assert outcome.failure_code == (None if recovered else "hard_timeout")
    warnings = [e for e in repository.list_events_after(seeded_mission.id, 0)
                if e.event_type == "turn.warning"]
    assert len(warnings) == int(recovered)
    if recovered:
        assert warnings[0].payload["code"] == "timeout_after_publish"
    records = list((temp_settings.database_path.parent / "cli-diagnostics").glob("*.json"))
    assert len(records) == 1
    import json
    assert json.loads(records[0].read_text())["hard_timeout"] is True
