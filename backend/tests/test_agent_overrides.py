import json

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.orchestration.runner import _apply_agent_overrides
from backend.app.settings import AgentOverride
from backend.app.storage.hub_settings import default_path, save


def _make_client(temp_settings):
    return TestClient(create_app(temp_settings))


def test_apply_overrides_replaces_default_provider():
    from backend.app.domain.agent_registry import AgentRegistry
    base = AgentRegistry.builtin().all()
    overrides = {"wangye": AgentOverride(default_provider_id="claude")}
    result = _apply_agent_overrides(base, overrides)
    wangye = next(a for a in result if a.id == "wangye")
    assert wangye.default_provider_id == "claude"
    # fallback preserved
    assert wangye.fallback_provider_ids == ["codex"]


def test_apply_overrides_replaces_fallbacks_independently():
    from backend.app.domain.agent_registry import AgentRegistry
    base = AgentRegistry.builtin().all()
    overrides = {"wangye": AgentOverride(fallback_provider_ids=[])}
    result = _apply_agent_overrides(base, overrides)
    wangye = next(a for a in result if a.id == "wangye")
    assert wangye.default_provider_id == "antigravity"
    assert wangye.fallback_provider_ids == []


def test_apply_overrides_leaves_unmentioned_agents_alone():
    from backend.app.domain.agent_registry import AgentRegistry
    base = AgentRegistry.builtin().all()
    overrides = {"wangye": AgentOverride(default_provider_id="claude")}
    result = _apply_agent_overrides(base, overrides)
    baobao = next(a for a in result if a.id == "baobao")
    assert baobao.default_provider_id == "codex"


def test_hub_settings_view_includes_agents(temp_settings):
    client = _make_client(temp_settings)
    body = client.get("/api/hub/settings").json()
    assert "agents" in body
    wangye = next(a for a in body["agents"] if a["id"] == "wangye")
    assert wangye["default_provider_id"]["value"] == "antigravity"
    assert wangye["default_provider_id"]["source"] == "committed"


def test_put_agent_override_sets_default_and_persists(temp_settings):
    client = _make_client(temp_settings)
    resp = client.put("/api/hub/settings", json={
        "agents": {"wangye": {"default_provider_id": "claude"}},
    })
    assert resp.status_code == 200
    body = resp.json()
    wangye = next(a for a in body["agents"] if a["id"] == "wangye")
    assert wangye["default_provider_id"] == {"value": "claude", "source": "user"}
    assert wangye["base_default_provider_id"] == "antigravity"

    persisted = json.loads(default_path(temp_settings).read_text("utf-8"))
    assert persisted["agents"]["wangye"]["default_provider_id"] == "claude"


def test_put_agent_override_rejects_unknown_agent(temp_settings):
    client = _make_client(temp_settings)
    resp = client.put("/api/hub/settings", json={
        "agents": {"luyi": {"default_provider_id": "claude"}},
    })
    assert resp.status_code == 400
    assert "luyi" in resp.json()["detail"]


def test_put_agent_override_rejects_unknown_provider(temp_settings):
    client = _make_client(temp_settings)
    resp = client.put("/api/hub/settings", json={
        "agents": {"wangye": {"default_provider_id": "bogus"}},
    })
    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]


def test_put_agent_override_clear_removes_entry(temp_settings):
    client = _make_client(temp_settings)
    client.put("/api/hub/settings", json={
        "agents": {"wangye": {"default_provider_id": "claude"}},
    })
    resp = client.put("/api/hub/settings", json={
        "agents": {"wangye": {"clear": True}},
    })
    assert resp.status_code == 200
    body = resp.json()
    wangye = next(a for a in body["agents"] if a["id"] == "wangye")
    assert wangye["default_provider_id"] == {"value": "antigravity", "source": "committed"}


def test_mission_runner_uses_overrides_from_disk(temp_settings):
    save(default_path(temp_settings), {
        "schema_version": 1,
        "agents": {"wangye": {"default_provider_id": "claude", "fallback_provider_ids": []}},
    })
    # Re-read via _snapshot_settings to mimic the mission start path
    from backend.app.api.missions import _snapshot_settings
    snap = _snapshot_settings(temp_settings)
    assert "wangye" in snap.agent_overrides
    assert snap.agent_overrides["wangye"].default_provider_id == "claude"
    assert snap.agent_overrides["wangye"].fallback_provider_ids == []

    from backend.app.adapters.fake import FakeCLIAdapter
    from backend.app.orchestration.runner import MissionRunner
    from backend.app.storage.repository import Repository
    repo = Repository(temp_settings.database_path)
    repo.initialize()
    runner = MissionRunner(repo, snap, FakeCLIAdapter())
    wangye = next(a for a in runner.agents if a.id == "wangye")
    assert wangye.default_provider_id == "claude"
    assert wangye.fallback_provider_ids == []


def test_agent_models_persist_snapshot_and_reset(temp_settings):
    from backend.app.settings import snapshot_settings
    from backend.app.adapters.provider_router import ProviderRouterAdapter
    client = _make_client(temp_settings)
    models = {"codex": "gpt-6.1-sol", "claude": "sonnet"}
    response = client.put('/api/hub/settings', json={"agents": {"wangye": {"provider_models": models}}})
    assert response.status_code == 200
    agent = next(a for a in response.json()['agents'] if a['id'] == 'wangye')
    assert agent['provider_models'] == {"value": models, "source": "user"}
    snapshot = snapshot_settings(temp_settings)
    router = ProviderRouterAdapter(snapshot.config_dir, agent_overrides=snapshot.agent_overrides)
    assert router._model_for('wangye', 'codex') == 'gpt-6.1-sol'
    assert router._model_for('wangye', 'claude') == 'sonnet'
    assert router._model_for('baobao', 'codex') == ''
    assert client.put('/api/hub/settings', json={"agents": {"wangye": {"provider_models": {}}}}).status_code == 200
    assert snapshot_settings(temp_settings).agent_overrides['wangye'].provider_models == {}
    client.put('/api/hub/settings', json={"agents": {"wangye": {"clear": True}}})
    assert 'wangye' not in snapshot_settings(temp_settings).agent_overrides


def test_agent_models_reject_invalid_provider_and_control_characters(temp_settings):
    client = _make_client(temp_settings)
    for models in ({'bogus': 'abc'}, {'codex': 'abc\nxyz'}, {'fake': 'abc'}):
        response = client.put('/api/hub/settings', json={"agents": {"wangye": {"provider_models": models}}})
        assert response.status_code == 400


def test_model_argument_overrides_shared_config_without_mutation():
    from backend.app.adapters.model_selection import with_model
    from backend.app.domain.models import CLIProvider
    for provider_id, args in [('codex', ['exec', '--model=old', '--json']), ('claude', ['-m', 'old', '-p'])]:
        provider = CLIProvider(id=provider_id, label=provider_id, command=provider_id, args=args)
        selected = with_model(provider, 'gpt-6.1-sol')
        assert 'old' not in selected.args and '--model=old' not in selected.args
        assert selected.args[selected.args.index('--model') + 1] == 'gpt-6.1-sol'
        assert provider.args == args
        assert with_model(provider, '') is provider


def test_both_cli_routes_receive_role_model(temp_settings, repository, monkeypatch):
    from unittest.mock import Mock
    from backend.app.adapters import provider_router
    from backend.app.extensions.discussion import deliberation_cli
    from backend.app.domain.models import CLIProvider
    router = provider_router.ProviderRouterAdapter(temp_settings.config_dir, agent_overrides={
        'wangye': AgentOverride(provider_models={'codex': 'gpt-6.1-sol'})})
    monkeypatch.setattr(provider_router, 'get_cli_provider', lambda *a, **kw: CLIProvider(
        id='codex', label='Codex', command='codex', args=['exec'], enabled=True))
    subprocess = Mock()
    monkeypatch.setattr(provider_router, 'SubprocessCLIAdapter', subprocess)
    router.run_turn('mission', 'wangye', 'turn', 'codex', 'prompt', Mock(repository=repository))
    assert subprocess.call_args.args[1] == ['exec', '--model', 'gpt-6.1-sol']
    isolated = Mock()
    monkeypatch.setattr(deliberation_cli, 'DeliberationCLIAdapter', isolated)
    from backend.app.extensions.discussion.hooks import run_deliberation_turn
    run_deliberation_turn(router, 'mission', 'wangye', 'turn', 'codex', 'prompt', Mock())
    assert isolated.call_args.kwargs['model'] == 'gpt-6.1-sol'
