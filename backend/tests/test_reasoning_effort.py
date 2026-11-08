"""Per-role, per-connection reasoning effort: stored like provider_models and
passed to each CLI with its own flag; an empty effort adds nothing."""

import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from backend.app.adapters import cli_catalog, provider_router
from backend.app.adapters.model_selection import with_effort, with_model_and_effort
from backend.app.domain.agent_registry import AgentRegistry
from backend.app.domain.models import CLIProvider
from backend.app.main import create_app
from backend.app.orchestration.runner import _apply_agent_overrides
from backend.app.settings import AgentOverride, snapshot_settings


def _provider(pid, args):
    return CLIProvider(id=pid, label=pid, command=pid, args=args, enabled=True)


@pytest.mark.parametrize('pid, args, expected', [
    ('codex', ['exec', '--json', '-c', 'model_reasoning_effort="low"', '-c', 'other=1'],
     ['exec', '--json', '-c', 'other=1', '--model', 'gpt-6.1-sol', '-c', 'model_reasoning_effort="high"']),
    ('codex', ['exec', '--config=model_reasoning_effort=low'],
     ['exec', '--model', 'gpt-6.1-sol', '-c', 'model_reasoning_effort="high"']),
    ('claude', ['--effort', 'low', '--verbose', '-p'], ['--verbose', '--model', 'gpt-6.1-sol', '--effort', 'high', '-p']),
    ('antigravity', ['--effort=max', '-p'], ['--model', 'gpt-6.1-sol', '--effort', 'high', '-p']),
])
def test_effort_replaces_existing_flag_and_stays_before_prompt(pid, args, expected):
    provider = _provider(pid, args)
    selected = with_model_and_effort(provider, 'gpt-6.1-sol', 'high')
    assert selected.args == expected
    assert provider.args == args


def test_empty_unknown_or_unsupported_effort_keeps_cli_args():
    provider = _provider('claude', ['--effort', 'low', '-p'])
    assert with_effort(provider, '') is provider
    assert with_effort(provider, 'ultra') is provider  # Codex-only level
    api = _provider('api-x', ['-p'])
    assert with_effort(api, 'high') is api


def test_override_merges_efforts_beside_models():
    base = AgentRegistry.builtin().all()
    overrides = {'wangye': AgentOverride(provider_efforts={'antigravity': 'max'})}
    wangye = next(a for a in _apply_agent_overrides(base, overrides) if a.id == 'wangye')
    assert wangye.provider_efforts == {'antigravity': 'max'}
    assert wangye.provider_models == next(a for a in base if a.id == 'wangye').provider_models


def test_all_cli_routes_receive_role_effort(temp_settings, monkeypatch):
    from backend.app.extensions.discussion import deliberation_cli
    from backend.app.extensions.discussion.hooks import run_deliberation_turn
    router = provider_router.ProviderRouterAdapter(temp_settings.config_dir, agent_overrides={
        'wangye': AgentOverride(provider_models={'codex': 'gpt-6.1-sol'}, provider_efforts={'codex': 'xhigh'})})
    monkeypatch.setattr(provider_router, 'get_cli_provider', lambda *a, **kw: _provider('codex', ['exec']))
    subprocess = Mock()
    monkeypatch.setattr(provider_router, 'SubprocessCLIAdapter', subprocess)
    router.run_turn('mission', 'wangye', 'turn', 'codex', 'prompt', Mock())
    assert subprocess.call_args.args[1] == ['exec', '--model', 'gpt-6.1-sol', '-c', 'model_reasoning_effort="xhigh"']
    isolated = Mock()
    monkeypatch.setattr(deliberation_cli, 'DeliberationCLIAdapter', isolated)
    run_deliberation_turn(router, 'mission', 'wangye', 'turn', 'codex', 'prompt', Mock())
    assert isolated.call_args.kwargs['effort'] == 'xhigh'
    # Another connection of the same role has no effort, so nothing is added.
    assert router._effort_for('wangye', 'claude') == ''


def test_settings_api_saves_lists_and_resets_efforts(temp_settings):
    client = TestClient(create_app(temp_settings))
    response = client.put('/api/hub/settings', json={'agents': {'wangye': {'provider_efforts': {'antigravity': 'high'}}}})
    assert response.status_code == 200, response.text
    view = next(a for a in response.json()['agents'] if a['id'] == 'wangye')
    assert view['provider_efforts'] == {'value': {'antigravity': 'high'}, 'source': 'user'}
    assert view['base_provider_efforts'] == {}
    assert snapshot_settings(temp_settings).agent_overrides['wangye'].provider_efforts == {'antigravity': 'high'}
    client.put('/api/hub/settings', json={'agents': {'wangye': {'clear': True}}})
    view = next(a for a in client.get('/api/hub/settings').json()['agents'] if a['id'] == 'wangye')
    assert view['provider_efforts'] == {'value': {}, 'source': 'committed'}


@pytest.mark.parametrize('efforts', [{'codex': 'turbo'}, {'claude': 'ultra'}, {'fake': 'high'}, {'bogus': 'high'}])
def test_settings_api_rejects_invalid_effort(temp_settings, efforts):
    client = TestClient(create_app(temp_settings))
    response = client.put('/api/hub/settings', json={'agents': {'wangye': {'provider_efforts': efforts}}})
    assert response.status_code == 400


def test_custom_role_keeps_route_efforts(temp_settings):
    client = TestClient(create_app(temp_settings))
    role = {'display_name': '陆玲珑', 'role': '测试', 'persona': '细致', 'default_provider_id': 'codex',
            'fallback_provider_ids': ['claude'], 'provider_efforts': {'codex': 'ultra', 'claude': ''}}
    response = client.post('/api/hub/agents', json=role)
    assert response.status_code == 200, response.text
    view = next(a for a in response.json()['agents'] if a['display_name'] == '陆玲珑')
    assert view['provider_efforts']['value'] == {'codex': 'ultra'}
    for bad in ({'antigravity': 'high'}, {'claude': 'ultra'}):
        response = client.post('/api/hub/agents', json=dict(role, display_name='别名', provider_efforts=bad))
        assert response.status_code == 400, response.text


def test_codex_catalog_lists_supported_efforts_per_model():
    cli_catalog._cache.clear()
    output = json.dumps({'models': [
        {'slug': 'a', 'visibility': 'list', 'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'xhigh'},
                                                                            {'effort': 'future'}]},
        {'slug': 'b', 'visibility': 'list'},
    ]})
    models = cli_catalog.parse_codex_json(output)
    assert models[0]['efforts'] == ['low', 'xhigh']
    assert 'efforts' not in models[1]
    cli_catalog._cache.clear()


def test_settings_view_lists_effort_levels_per_cli_without_a_model_listing(temp_settings):
    client = TestClient(create_app(temp_settings))
    providers = {p['id']: p['efforts'] for p in client.get('/api/hub/settings').json()['providers']}
    assert providers['claude'] == ['low', 'medium', 'high', 'xhigh', 'max']
    assert providers['codex'][-1] == 'ultra'
    assert providers['fake'] == []


def test_antigravity_listed_models_fix_their_own_effort(monkeypatch):
    # agy 1.5.3 rejects `--model gemini-3.8-flash-low --effort high` and any
    # --effort on its listed Claude models; only its default/base model takes one.
    cli_catalog._cache.clear()
    monkeypatch.setattr(cli_catalog.shutil, 'which', lambda command: '/usr/bin/' + command)
    monkeypatch.setattr(cli_catalog.subprocess, 'run', lambda *a, **kw: __import__('subprocess').CompletedProcess(
        [], 0, stdout=b'gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\nclaude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n'))
    result = cli_catalog.cli_catalog(CLIProvider(id='antigravity', label='Antigravity', command='agy'))
    assert [m['efforts'] for m in result['models']] == [[], []]
    cli_catalog._cache.clear()


def _list_models(monkeypatch, stdout):
    cli_catalog._cache.clear()
    monkeypatch.setattr(cli_catalog.shutil, 'which', lambda command: '/usr/bin/' + command)
    monkeypatch.setattr(cli_catalog.subprocess, 'run', lambda *a, **kw: __import__('subprocess').CompletedProcess(
        [], 0, stdout=stdout))


def _put_agent(client, agent_id, override):
    return client.put('/api/hub/settings', json={'agents': {agent_id: override}})


@pytest.mark.parametrize('first, second', [
    ({}, {'provider_models': {'antigravity': 'gemini-3.8-flash-high'}, 'provider_efforts': {'antigravity': 'low'}}),
    ({'provider_efforts': {'antigravity': 'low'}}, {'provider_models': {'antigravity': 'gemini-3.8-flash-high'}}),
    ({'provider_models': {'antigravity': 'gemini-3.8-flash-high'}}, {'provider_efforts': {'antigravity': 'low'}}),
])
def test_settings_api_rejects_effort_on_model_with_fixed_effort(temp_settings, first, second):
    # Issue #132: agy rejects `--model gemini-3.8-flash-high --effort low`; the
    # merged model/effort pair is checked even when the model listing is not loaded.
    cli_catalog._cache.clear()
    client = TestClient(create_app(temp_settings))
    if first:
        assert _put_agent(client, 'wangye', first).status_code == 200
    response = _put_agent(client, 'wangye', second)
    assert response.status_code == 400, response.text
    assert '已固定推理强度' in response.json()['detail']
    view = next(a for a in client.get('/api/hub/settings').json()['agents'] if a['id'] == 'wangye')
    saved = (view['provider_models']['value'].get('antigravity'), view['provider_efforts']['value'].get('antigravity'))
    assert saved != ('gemini-3.8-flash-high', 'low')


def test_settings_api_narrows_effort_to_listed_model(temp_settings, monkeypatch):
    _list_models(monkeypatch, json.dumps({'models': [
        {'slug': 'gpt-5.5', 'visibility': 'list', 'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'xhigh'}]},
    ]}).encode())
    client = TestClient(create_app(temp_settings))
    rejected = _put_agent(client, 'wangye', {'provider_models': {'codex': 'gpt-5.5'}, 'provider_efforts': {'codex': 'ultra'}})
    assert rejected.status_code == 400
    assert '不支持推理强度 ultra' in rejected.json()['detail']
    accepted = _put_agent(client, 'wangye', {'provider_models': {'codex': 'gpt-5.5'}, 'provider_efforts': {'codex': 'xhigh'}})
    assert accepted.status_code == 200, accepted.text
    # A hand-typed model the listing does not know keeps the CLI's own levels.
    unknown = _put_agent(client, 'wangye', {'provider_models': {'codex': 'gpt-next'}, 'provider_efforts': {'codex': 'ultra'}})
    assert unknown.status_code == 200, unknown.text
    cli_catalog._cache.clear()


def test_settings_api_rejects_effort_on_listed_antigravity_model(temp_settings, monkeypatch):
    _list_models(monkeypatch, b'claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n')
    client = TestClient(create_app(temp_settings))
    response = _put_agent(client, 'wangye', {'provider_models': {'antigravity': 'claude-sonnet-4-6'},
                                             'provider_efforts': {'antigravity': 'high'}})
    assert response.status_code == 400
    base = _put_agent(client, 'wangye', {'provider_models': {'antigravity': 'gemini-3.8-flash'},
                                         'provider_efforts': {'antigravity': 'high'}})
    assert base.status_code == 200, base.text
    cli_catalog._cache.clear()


def test_custom_role_rejects_effort_on_model_with_fixed_effort(temp_settings):
    cli_catalog._cache.clear()
    client = TestClient(create_app(temp_settings))
    role = {'display_name': '陆玲珑', 'role': '测试', 'persona': '细致', 'default_provider_id': 'antigravity',
            'provider_models': {'antigravity': 'gemini-3.8-flash-high'}, 'provider_efforts': {'antigravity': 'low'}}
    response = client.post('/api/hub/agents', json=role)
    assert response.status_code == 400, response.text
    assert '已固定推理强度' in response.json()['detail']
