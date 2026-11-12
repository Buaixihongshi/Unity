import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend.app.domain.models import DiscussionConfig, Mission, MissionStatus
from backend.app.main import create_app
from backend.app.runtime_control import activity
from backend.app.settings import get_settings, snapshot_settings
from backend.app.storage import hub_settings


def client_for(settings, monkeypatch):
    monkeypatch.setenv("YIREN_LIFECYCLE_TOKEN", "test-private-control")
    app = create_app(settings)
    return TestClient(app), app


def test_drain_requires_private_token_and_blocks_writes_only(temp_settings, monkeypatch):
    client, app = client_for(temp_settings, monkeypatch)
    assert client.post("/api/runtime/drain").status_code == 403
    assert client.post("/api/runtime/drain", headers={"X-AgentClub-Control": "wrong"}).status_code == 403
    headers = {"X-AgentClub-Control": "test-private-control"}
    assert client.post("/api/runtime/drain", headers=headers).status_code == 200
    assert client.post("/api/conversations", json={"title": "blocked"}).status_code == 503
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["runtime"]["draining"] is True
    assert "test-private-control" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/api/runtime/resume", headers=headers).status_code == 200
    assert client.post("/api/conversations", json={"title": "allowed"}).status_code == 201


def test_busy_drain_does_not_disable_new_requests(temp_settings, monkeypatch):
    client, app = client_for(temp_settings, monkeypatch)
    from backend.app.storage.repository import Repository
    repo = Repository(temp_settings.database_path)
    repo.create_mission(Mission(id="busy", title="test", goal="unfinished", status=MissionStatus.AWAITING_INPUT))
    result = client.post("/api/runtime/drain", headers={"X-AgentClub-Control": "test-private-control"})
    assert result.status_code == 409
    assert client.get("/api/health").json()["runtime"]["draining"] is False
    assert activity(repo)["active_missions"] == 1


def test_drain_waits_for_an_inflight_mutation_and_sees_its_work(temp_settings, monkeypatch):
    client, app = client_for(temp_settings, monkeypatch)
    from backend.app.storage.repository import Repository
    repo = Repository(temp_settings.database_path)
    entered = threading.Event()
    release = threading.Event()

    @app.post("/test/admission")
    def admission():
        entered.set()
        assert release.wait(5)
        repo.create_mission(Mission(id="admitted", title="test", goal="unfinished"))
        return {"ok": True}

    results = []
    with client:
        writer = threading.Thread(target=lambda: results.append(client.post("/test/admission")))
        writer.start()
        assert entered.wait(5)
        drainer = threading.Thread(target=lambda: results.append(client.post("/api/runtime/drain", headers={"X-AgentClub-Control": "test-private-control"})))
        drainer.start()
        release.set()
        writer.join(5)
        drainer.join(5)
    assert sorted(r.status_code for r in results) == [200, 409]
    assert not app.state.draining


def test_demo_only_survives_saved_runtime_overrides_and_blocks_real_verification(temp_settings, monkeypatch):
    monkeypatch.setenv("YIREN_DATABASE_PATH", str(temp_settings.database_path))
    monkeypatch.setenv("YIREN_DEMO_ONLY", "true")
    hub_settings.save(hub_settings.default_path(temp_settings), {"runtime": {"use_fake_cli": False, "maintenance_enabled": True}})
    settings = get_settings()
    assert settings.demo_only and settings.use_fake_cli and not settings.maintenance_enabled
    snap = snapshot_settings(settings)
    assert snap.demo_only and snap.use_fake_cli and not snap.maintenance_enabled
    client = TestClient(create_app(settings))
    assert client.post("/api/onboarding/providers/codex/verify").status_code == 409
    assert client.post("/api/onboarding/first-chat", json={"provider_id": "codex", "confirmed_real_call": True}).status_code == 409
    assert client.post("/api/onboarding/providers/codex/activate").status_code == 409
    assert client.post("/api/providers/codex/probe").status_code == 409
    assert client.get("/api/onboarding").json()["real_cli_blocked"] is True


def test_historical_blocked_task_is_idle_but_its_live_grant_still_blocks_drain(temp_settings, monkeypatch):
    from backend.app.storage.repository import Repository
    from backend.app.storage.execution import ExecutionLedger
    client, _ = client_for(temp_settings, monkeypatch)
    repo = Repository(temp_settings.database_path)
    mission = Mission(id="history", title="old task", goal="old task", status=MissionStatus.BLOCKED)
    repo.create_mission(mission)
    headers = {"X-AgentClub-Control": "test-private-control"}
    assert client.post("/api/runtime/drain", headers=headers).status_code == 200
    assert client.post("/api/runtime/resume", headers=headers).status_code == 200
    repo.update_mission(mission.model_copy(update={"status": MissionStatus.RUNNING}))
    ledger = ExecutionLedger(repo)
    ledger.register("worker")
    ledger.begin("worker", mission.id, "chulan", "turn", "fake", timeout=30)
    repo.update_mission(mission)
    assert activity(repo) == {"active_missions": 0, "active_maintenance": 0, "active_grants": 1}
    assert client.post("/api/runtime/drain", headers=headers).status_code == 409


@pytest.mark.parametrize("phase,expected", [("independent", 409), ("ended", 200)])
def test_blocked_v2_discussion_requires_ended_state_before_drain(temp_settings, monkeypatch, phase, expected):
    from backend.app.storage.repository import Repository
    client, _ = client_for(temp_settings, monkeypatch)
    repo = Repository(temp_settings.database_path)
    repo.create_mission(Mission(id="discussion", title="paused", goal="paused", room_mode="roundtable",
                               status=MissionStatus.BLOCKED, discussion=DiscussionConfig(
                                   protocol="deliberation_v2", participant_ids=["chulan", "wangye"], max_turns=7)))
    repo.add_event("discussion", "discussion.v2.state", {"phase": phase})
    assert client.post("/api/runtime/drain", headers={"X-AgentClub-Control": "test-private-control"}).status_code == expected


def test_health_reports_idle_time_that_tooling_probes_do_not_reset(temp_settings, monkeypatch):
    from backend.app import runtime_control
    clock = [1000.0]
    monkeypatch.setattr(runtime_control.time, "time", lambda: clock[0])
    client, app = client_for(temp_settings, monkeypatch)
    probe = {"X-AgentClub-Probe": "1"}
    clock[0] += 600
    assert client.get("/api/health", headers=probe).json()["runtime"]["idle_seconds"] == 600
    # Supervisor/status polls and lifecycle control are bookkeeping, not use.
    clock[0] += 60
    client.post("/api/runtime/resume", headers={"X-AgentClub-Control": "wrong"})
    assert client.get("/api/health", headers=probe).json()["runtime"]["idle_seconds"] == 660
    # A visible page refreshes health without the probe header; that is use.
    client.get("/api/health")
    assert client.get("/api/health", headers=probe).json()["runtime"]["idle_seconds"] == 0
    clock[0] += 30
    client.get("/api/conversations")
    clock[0] += 5
    assert client.get("/api/health", headers=probe).json()["runtime"]["idle_seconds"] == 5
