import time

from fastapi.testclient import TestClient

from backend.app.adapters.base import CLIAdapter, TurnOutcome
from backend.app.domain.models import (
    Conversation,
    Message,
    MessageRole,
    Mission,
    MissionStatus,
    TurnStatus,
)
from backend.app.main import create_app


def _wait_for_onboarding(client: TestClient, expected: str) -> dict:
    for _ in range(100):
        response = client.get("/api/onboarding")
        response.raise_for_status()
        state = response.json()
        if state["state"] == expected:
            return state
        time.sleep(0.02)
    raise AssertionError("onboarding did not reach {state}".format(state=expected))


def _installed(monkeypatch, *provider_ids):
    from backend.app.api import onboarding as onboarding_api

    def readiness(settings, provider_id):
        if provider_id not in provider_ids:
            return {
                "available": False,
                "version": None,
                "path": None,
                "error": "{provider} 未安装".format(provider=provider_id),
            }
        return {
            "available": True,
            "version": "{provider} 9.9.9".format(provider=provider_id),
            "path": "/test/bin/{provider}".format(provider=provider_id),
            "error": None,
        }

    monkeypatch.setattr(onboarding_api, "_provider_readiness", readiness)


class _ScriptedCLI(CLIAdapter):
    """Stands in for the real CLI router and records what it was asked to run."""

    def __init__(self):
        self.calls = []

    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        self.calls.append((agent_id, provider_id, prompt))
        tools.publish_message(
            mission_id,
            agent_id,
            turn_id,
            "{agent} 通过 {provider} 打招呼".format(agent=agent_id, provider=provider_id),
            provider_id,
        )
        return TurnOutcome(status=TurnStatus.COMPLETED)


class _BlockedCLI(CLIAdapter):
    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        return TurnOutcome(
            status=TurnStatus.BLOCKED,
            summary="Codex authentication required",
        )


class _PublishingThenBlockedCLI(CLIAdapter):
    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        tools.publish_message(
            mission_id,
            agent_id,
            turn_id,
            "我错误地声称已经通过 Codex CLI 接入。",
            provider_id,
        )
        return TurnOutcome(
            status=TurnStatus.BLOCKED,
            summary="Antigravity authentication required",
        )


def _start_first_chat(client: TestClient, provider_id: str = "codex", **extra):
    return client.post(
        "/api/onboarding/first-chat",
        json={"provider_id": provider_id, "confirmed_real_call": True, **extra},
    )


def _agent_messages(client: TestClient, conversation_id: str) -> list:
    detail = client.get(
        "/api/conversations/{conversation_id}".format(conversation_id=conversation_id)
    ).json()
    return [message for message in detail["messages"] if message["role"] == "agent"]


def test_health_reports_local_bootstrap_dependencies(temp_settings):
    client = TestClient(create_app(temp_settings))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert {key: response.json()[key] for key in ('status', 'version', 'database', 'config', 'fake_provider')} == {
        "status": "ok",
        "version": "1.0.0",
        "database": "ready",
        "config": "ready",
        "fake_provider": "ready",
    }
    assert 'deliberation_v2' in response.json()['discussion_protocols']
    assert len(response.json()['discussion_rules_hash']) == 64


def test_first_run_detects_clis_and_offers_install_guidance_without_demo(temp_settings, monkeypatch):
    _installed(monkeypatch)
    client = TestClient(create_app(temp_settings, onboarding_verification_adapter=_ScriptedCLI()))

    initial = client.get("/api/onboarding")
    started = _start_first_chat(client)

    assert initial.status_code == 200
    body = initial.json()
    assert body["state"] == "ready_for_first_chat"
    assert body["real_cli_blocked"] is False
    assert [item["id"] for item in body["providers"]] == ["codex", "claude", "antigravity"]
    assert all(item["available"] is False for item in body["providers"])
    assert all(item["install_command"] and item["install_url"] for item in body["providers"])
    assert body["recommended_provider_id"] == "codex"
    # Without an installed CLI there is no demo chat to fall back to.
    assert started.status_code == 409
    assert "未安装" in started.json()["detail"]
    assert client.get("/api/conversations").json() == []


def test_first_chat_requires_confirmation_and_a_real_cli(temp_settings, monkeypatch):
    _installed(monkeypatch, "codex")
    client = TestClient(create_app(temp_settings, onboarding_verification_adapter=_ScriptedCLI()))

    missing_body = client.post("/api/onboarding/first-chat")
    not_confirmed = client.post(
        "/api/onboarding/first-chat",
        json={"provider_id": "codex", "confirmed_real_call": False},
    )
    demo = _start_first_chat(client, "fake")

    assert missing_body.status_code == 422
    assert not_confirmed.status_code == 400
    assert demo.status_code == 404
    assert client.get("/api/onboarding").json()["state"] == "ready_for_first_chat"


def test_first_chat_is_idempotent_and_verifies_the_selected_cli(temp_settings, monkeypatch):
    from backend.app.storage import hub_settings

    _installed(monkeypatch, "codex")
    # Even when this process runs the offline demo, the first chat must run
    # through the CLI the user selected.
    assert temp_settings.use_fake_cli is True
    adapter = _ScriptedCLI()
    client = TestClient(create_app(temp_settings, onboarding_verification_adapter=adapter))

    first = _start_first_chat(client)
    second = _start_first_chat(client)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["conversation"]["conversation"]["id"] == second.json()["conversation"]["conversation"]["id"]
    assert first.json()["conversation"]["latest_mission_detail"]["mission"]["id"] == second.json()["conversation"]["latest_mission_detail"]["mission"]["id"]

    state = _wait_for_onboarding(client, "first_chat_succeeded")
    messages = _agent_messages(client, state["conversation_id"])

    assert [(agent, provider) for agent, provider, _ in adapter.calls] == [("chulan", "codex")]
    assert [(m["agent_id"], m["provider_id"]) for m in messages] == [("chulan", "codex")]
    assert state["first_chat_provider_id"] == "codex"
    assert state["first_reply_completed_at"]
    assert state["verified_provider_ids"] == ["codex"]
    codex = next(item for item in state["providers"] if item["id"] == "codex")
    assert codex["verified"] is True
    assert codex["version"] == "codex 9.9.9"
    # Verification alone never changes the saved team configuration.
    persisted = hub_settings.load(hub_settings.default_path(temp_settings))
    assert "runtime" not in persisted
    assert "agents" not in persisted


def test_failed_first_chat_reports_the_error_and_can_retry_another_cli(temp_settings, monkeypatch):
    from backend.app.storage import hub_settings

    _installed(monkeypatch, "codex", "claude")
    client = TestClient(create_app(temp_settings, onboarding_verification_adapter=_BlockedCLI()))

    _start_first_chat(client).raise_for_status()
    failed = _wait_for_onboarding(client, "first_chat_failed")

    assert "authentication required" in failed["first_chat_error"]
    assert failed["verified_provider_ids"] == []
    assert client.post("/api/onboarding/providers/codex/activate").status_code == 409
    persisted = hub_settings.load(hub_settings.default_path(temp_settings))
    assert persisted.get("runtime", {}).get("use_fake_cli") is not False

    retried = _start_first_chat(client, "claude")
    assert retried.status_code == 202
    assert retried.json()["conversation"]["conversation"]["id"] == failed["conversation_id"]
    assert retried.json()["onboarding"]["mission_id"] != failed["mission_id"]
    assert retried.json()["onboarding"]["first_chat_provider_id"] == "claude"


def test_activation_moves_the_whole_team_to_the_verified_cli(temp_settings, monkeypatch):
    from backend.app.storage import hub_settings

    _installed(monkeypatch, "codex")
    settings_path = hub_settings.default_path(temp_settings)
    hub_settings.save(
        settings_path,
        {
            "schema_version": 1,
            "providers": {"codex": {"enabled": False}},
            "runtime": {"max_turns": 4},
            "agents": {"wangye": {"provider_models": {"codex": "gpt-test"}}},
        },
    )
    adapter = _ScriptedCLI()
    client = TestClient(create_app(temp_settings, onboarding_verification_adapter=adapter))

    _start_first_chat(client).raise_for_status()
    _wait_for_onboarding(client, "first_chat_succeeded")

    activated = client.post("/api/onboarding/providers/codex/activate")
    assert activated.status_code == 202
    intro_mission_id = activated.json()["onboarding"]["team_intro_mission_id"]
    # Repeated activation attaches to the same introduction mission.
    repeated = client.post("/api/onboarding/providers/codex/activate")
    assert repeated.json()["onboarding"]["team_intro_mission_id"] == intro_mission_id

    introduced = _wait_for_onboarding(client, "team_intro_completed")
    assert introduced["activated_provider_id"] == "codex"
    messages = _agent_messages(client, introduced["conversation_id"])
    assert [(m["agent_id"], m["provider_id"]) for m in messages] == [
        ("chulan", "codex"),
        ("baobao", "codex"),
        ("wangye", "codex"),
    ]
    assert [(agent, provider) for agent, provider, _ in adapter.calls] == [
        ("chulan", "codex"),
        ("baobao", "codex"),
        ("wangye", "codex"),
    ]

    client.patch("/api/onboarding", json={"completed": True}).raise_for_status()
    persisted = hub_settings.load(settings_path)
    assert persisted["runtime"] == {"max_turns": 4, "use_fake_cli": False}
    assert persisted["providers"] == {"codex": {"enabled": True}}
    assert persisted["agents"] == {
        "chulan": {"default_provider_id": "codex", "fallback_provider_ids": []},
        "baobao": {"default_provider_id": "codex", "fallback_provider_ids": []},
        "wangye": {
            "provider_models": {"codex": "gpt-test"},
            "default_provider_id": "codex",
            "fallback_provider_ids": [],
        },
    }
    assert persisted["onboarding"]["team_intro_completed_at"]


def test_legacy_demo_first_chat_still_requires_a_real_cli(temp_settings, monkeypatch, repository):
    from backend.app.storage import hub_settings

    _installed(monkeypatch, "codex")
    conversation = Conversation(id="c_legacy", title="欢迎使用 Unity")
    repository.create_conversation(conversation)
    repository.create_mission(
        Mission(
            id="m_legacy",
            title="欢迎使用 Unity",
            goal="legacy",
            conversation_id=conversation.id,
            status=MissionStatus.COMPLETED,
        )
    )
    repository.add_message(
        Message(
            id="msg_legacy",
            mission_id="m_legacy",
            conversation_id=conversation.id,
            role=MessageRole.AGENT,
            agent_id="chulan",
            provider_id="fake",
            content="演示回复",
        )
    )
    hub_settings.save(
        hub_settings.default_path(temp_settings),
        {
            "schema_version": 1,
            "onboarding": {
                "conversation_id": conversation.id,
                "mission_id": "m_legacy",
                "first_reply_completed_at": "2026-10-01T00:00:00+00:00",
            },
        },
    )
    client = TestClient(create_app(temp_settings, onboarding_verification_adapter=_ScriptedCLI()))

    legacy = client.get("/api/onboarding").json()
    assert legacy["state"] == "ready_for_first_chat"
    assert legacy["conversation_id"] == conversation.id
    assert legacy["first_reply_completed_at"] is None

    started = _start_first_chat(client)
    assert started.json()["conversation"]["conversation"]["id"] == conversation.id
    succeeded = _wait_for_onboarding(client, "first_chat_succeeded")
    assert succeeded["verified_provider_ids"] == ["codex"]


def test_onboarding_can_be_completed_and_reset_without_losing_other_settings(temp_settings):
    from backend.app.storage import hub_settings

    settings_path = hub_settings.default_path(temp_settings)
    hub_settings.save(
        settings_path,
        {
            "schema_version": 1,
            "providers": {"codex": {"enabled": True}},
        },
    )
    client = TestClient(create_app(temp_settings))

    completed = client.patch(
        "/api/onboarding",
        json={"completed": True, "dismissed_real_cli_setup": True},
    )
    reset = client.post("/api/onboarding/reset")

    assert completed.status_code == 200
    assert completed.json()["state"] == "completed"
    assert reset.status_code == 200
    assert reset.json()["state"] == "ready_for_first_chat"
    persisted = hub_settings.load(settings_path)
    assert persisted["providers"] == {"codex": {"enabled": True}}


def test_completed_onboarding_can_verify_an_extension_provider(temp_settings, monkeypatch):
    _installed(monkeypatch, "codex", "claude")
    client = TestClient(create_app(temp_settings, onboarding_verification_adapter=_ScriptedCLI()))

    _start_first_chat(client).raise_for_status()
    _wait_for_onboarding(client, "first_chat_succeeded")
    client.post("/api/onboarding/providers/codex/activate").raise_for_status()
    _wait_for_onboarding(client, "team_intro_completed")
    client.patch("/api/onboarding", json={"completed": True}).raise_for_status()

    not_confirmed = client.post(
        "/api/onboarding/providers/claude/verify",
        json={"confirmed_real_call": False},
    )
    verify = client.post(
        "/api/onboarding/providers/claude/verify",
        json={"confirmed_real_call": True},
    )

    assert not_confirmed.status_code == 400
    assert verify.status_code == 202
    assert verify.json()["onboarding"]["state"] in {
        "provider_verifying",
        "provider_verified",
    }
    extended = _wait_for_onboarding(client, "provider_verified")
    assert extended["activated_provider_id"] == "codex"
    assert extended["verification_provider_id"] == "claude"
    assert extended["verified_provider_ids"] == ["claude", "codex"]
    assert extended["completed_at"] is None

    completed = client.patch("/api/onboarding", json={"completed": True})
    assert completed.status_code == 200
    assert completed.json()["state"] == "completed"


def test_failed_provider_verification_keeps_saved_settings(temp_settings, monkeypatch):
    from backend.app.storage import hub_settings

    _installed(monkeypatch, "codex")
    client = TestClient(create_app(temp_settings, onboarding_verification_adapter=_BlockedCLI()))

    # Verification from settings works before any first chat and creates the
    # onboarding conversation on demand.
    client.post(
        "/api/onboarding/providers/codex/verify",
        json={"confirmed_real_call": True},
    ).raise_for_status()
    failed = _wait_for_onboarding(client, "provider_verification_failed")

    assert "authentication required" in failed["verification_error"]
    assert failed["conversation_id"]
    persisted = hub_settings.load(hub_settings.default_path(temp_settings))
    assert persisted.get("runtime", {}).get("use_fake_cli") is not False
    assert persisted.get("providers", {}).get("codex", {}).get("enabled") is not True
    assert client.post("/api/onboarding/providers/codex/activate").status_code == 409


def test_verification_prompt_and_messages_are_isolated_from_chat(temp_settings, monkeypatch):
    _installed(monkeypatch, "codex", "claude")
    adapter = _ScriptedCLI()
    client = TestClient(create_app(temp_settings, onboarding_verification_adapter=adapter))
    _start_first_chat(client).raise_for_status()
    first = _wait_for_onboarding(client, "first_chat_succeeded")

    client.post(
        "/api/onboarding/providers/claude/verify",
        json={"confirmed_real_call": True},
    ).raise_for_status()
    _wait_for_onboarding(client, "provider_verified")

    _, provider_id, prompt = adapter.calls[-1]
    assert provider_id == "claude"
    assert "最近对话" not in prompt
    assert "不要声明当前 Provider 名称" in prompt

    detail = client.get(
        "/api/conversations/{conversation_id}".format(
            conversation_id=first["conversation_id"],
        )
    ).json()
    assert len(detail["missions"]) == 1
    assert [m["provider_id"] for m in detail["messages"] if m["role"] == "agent"] == ["codex"]


def test_blocked_verification_cannot_pollute_conversation(temp_settings, monkeypatch):
    _installed(monkeypatch, "antigravity")
    client = TestClient(
        create_app(
            temp_settings,
            onboarding_verification_adapter=_PublishingThenBlockedCLI(),
        )
    )
    client.post(
        "/api/onboarding/providers/antigravity/verify",
        json={"confirmed_real_call": True},
    ).raise_for_status()
    failed = _wait_for_onboarding(client, "provider_verification_failed")

    assert "authentication required" in failed["verification_error"]
    assert "antigravity" not in failed["verified_provider_ids"]
    detail = client.get(
        "/api/conversations/{conversation_id}".format(
            conversation_id=failed["conversation_id"],
        )
    ).json()
    assert detail["missions"] == []
    assert all("错误地声称" not in message["content"] for message in detail["messages"])
