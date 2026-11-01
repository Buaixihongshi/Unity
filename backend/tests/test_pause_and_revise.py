"""Pause a running chat, rewrite the last message, and hand the stopped work over."""
import sys
import threading
import time

from fastapi.testclient import TestClient

from backend.app.adapters.base import TurnOutcome
from backend.app.adapters.fake import FakeCLIAdapter
from backend.app.adapters.subprocess_cli import SubprocessCLIAdapter
from backend.app.domain.models import Mission, MissionStatus, MessageRole, RoomMode, TurnStatus
from backend.app.main import create_app
from backend.app.mcp.tools import MCPTools
from backend.app.orchestration import interruption
from backend.app.orchestration.live_progress import get_live_progress_registry


class _StopsOnSecondTurn(FakeCLIAdapter):
    """The second turn behaves like a CLI that streams a little, then is stopped."""

    def __init__(self):
        super().__init__(response="宝宝的看法")
        self.prompts = []
        self.second_turn_running = threading.Event()

    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        self.prompts.append((agent_id, prompt))
        if len(self.prompts) == 2:
            get_live_progress_registry().append_text(mission_id, turn_id, "楚岚说到一半")
            self.second_turn_running.set()
            while not interruption.pause_requested(mission_id):
                time.sleep(0.02)
            get_live_progress_registry().clear(mission_id, turn_id)
            return TurnOutcome(status=TurnStatus.BLOCKED, summary=interruption.STOPPED_SUMMARY,
                               failure_code="stopped", retryable=False)
        return super().run_turn(mission_id, agent_id, turn_id, provider_id, prompt, tools)


def _wait_status(client, mission_id, statuses, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/missions/{mission_id}").json()
        if detail["mission"]["status"] in statuses:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"mission {mission_id} never reached {statuses}")


def _start_paused_chat(client, adapter):
    created = client.post("/api/missions", json={
        "goal": "周末去爬山", "target_agent_ids": ["baobao", "chulan", "wangye"]}).json()
    mission_id = created["mission"]["id"]
    assert adapter.second_turn_running.wait(10)
    paused = client.post(f"/api/missions/{mission_id}/pause")
    assert paused.status_code == 202
    return _wait_status(client, mission_id, {"blocked"})


def test_pause_stops_the_running_turn_and_frees_the_conversation(temp_settings):
    adapter = _StopsOnSecondTurn()
    with TestClient(create_app(temp_settings, mission_adapter=adapter)) as client:
        detail = _start_paused_chat(client, adapter)
    assert [agent for agent, _ in adapter.prompts] == ["baobao", "chulan"]
    assert [f["content"] for f in detail["failure_events"]] == [
        "张楚岚本轮因用户停止中断，未发布回复。原计划尚未发言：王也。可以直接发消息继续。"
    ]
    assert [m["content"] for m in detail["messages"] if m["role"] == "agent"] == ["宝宝的看法"]


def test_revised_message_supersedes_the_old_one_and_hands_over(temp_settings, repository):
    adapter = _StopsOnSecondTurn()
    with TestClient(create_app(temp_settings, mission_adapter=adapter)) as client:
        paused = _start_paused_chat(client, adapter)
        conversation_id = paused["mission"]["conversation_id"]
        original = next(m for m in paused["messages"] if m["role"] == "user")
        revised = client.post("/api/missions", json={
            "goal": "周末改去露营", "conversation_id": conversation_id,
            "revises_message_id": original["id"]})
        assert revised.status_code == 201
        _wait_status(client, revised.json()["mission"]["id"], {"completed", "completed_with_warnings"})

    agent, prompt = adapter.prompts[2]
    assert agent == "baobao"  # unaddressed: the last agent who actually published continues
    for expected in ("【交接：用户修改了上一条发言】", "原话：周末去爬山", "改为：周末改去露营",
                     "修改前已回应：冯宝宝", "被中止的回合：张楚岚，中止时已输出：「楚岚说到一半」",
                     "原计划尚未发言：王也", "不要沿用已作废的前提"):
        assert expected in prompt
    assert "用户（此条已被用户修改，以后面的新版为准）：周末去爬山" in prompt
    assert "冯宝宝（回应的是修改前的说法）：宝宝的看法" in prompt
    users = [m for m in repository.list_messages_for_conversation(conversation_id) if m.role == MessageRole.USER]
    assert [m.content for m in users] == ["周末去爬山", "周末改去露营"]


def test_only_the_latest_user_message_can_be_rewritten(temp_settings):
    adapter = _StopsOnSecondTurn()
    with TestClient(create_app(temp_settings, mission_adapter=adapter)) as client:
        paused = _start_paused_chat(client, adapter)
        conversation_id = paused["mission"]["conversation_id"]
        original = next(m for m in paused["messages"] if m["role"] == "user")
        later = client.post("/api/missions", json={"goal": "再补一句", "conversation_id": conversation_id})
        _wait_status(client, later.json()["mission"]["id"], {"completed", "completed_with_warnings"})
        stale = client.post("/api/missions", json={
            "goal": "改旧的", "conversation_id": conversation_id, "revises_message_id": original["id"]})
    assert stale.status_code == 409


def test_pause_leaves_finished_and_extension_missions_alone(temp_settings, repository):
    with TestClient(create_app(temp_settings)) as client:
        repository.create_mission(Mission(id="m_done", title="t", goal="t", status=MissionStatus.COMPLETED))
        repository.create_mission(Mission(id="m_rt", title="t", goal="t", status=MissionStatus.RUNNING,
                                          room_mode=RoomMode.ROUNDTABLE))
        assert client.post("/api/missions/m_done/pause").status_code == 202
        assert client.post("/api/missions/m_rt/pause").status_code == 409
    assert not interruption.pause_requested("m_done", repository)


def test_pause_kills_a_real_cli_process_group(repository, temp_settings):
    mission = Mission(id="m_cli", title="t", goal="t", status=MissionStatus.RUNNING)
    repository.create_mission(mission)
    adapter = SubprocessCLIAdapter(sys.executable, ["-c", "import time; time.sleep(60)"], timeout=60)
    threading.Timer(0.5, interruption.request_pause, args=(repository, mission)).start()
    started = time.monotonic()
    outcome = adapter.run_turn("m_cli", "baobao", "t_cli", "local", "prompt", MCPTools(repository, temp_settings.artifact_root))
    assert outcome.failure_code == "stopped"
    assert time.monotonic() - started < 10


def test_mcp_turn_wait_returns_stopped_for_a_pause():
    import subprocess

    from backend.app.adapters.mcp_subprocess import ProcessWaitReason, _wait_for_process_with_activity

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        started = time.monotonic()
        reason = _wait_for_process_with_activity(
            proc, hard_timeout=60, idle_timeout=0, last_activity_at=time.time,
            refresh_activity=lambda: None, stopped=lambda: time.monotonic() - started > 0.3)
        assert reason is ProcessWaitReason.STOPPED
        assert time.monotonic() - started < 5
    finally:
        proc.kill()
        proc.wait()
