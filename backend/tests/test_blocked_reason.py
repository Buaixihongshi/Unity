"""#155: observable failure cards, provider classification and history boundaries."""
import json
import sys

import pytest
from fastapi.testclient import TestClient

from backend.app.adapters.base import TurnOutcome
from backend.app.adapters.deliberation_errors import failure_code
from backend.app.adapters.failure_text import cli_limit_failure_code, public_failure_text
from backend.app.adapters.mcp_subprocess import MCPSubprocessAdapter
from backend.app.api.mission_detail import build_mission_detail
from backend.app.domain.models import AgentProfile, Conversation, MessageRole, Mission, MissionStatus, TurnStatus
from backend.app.main import create_app
from backend.app.mcp.tools import MCPTools
from backend.app.orchestration.runner import MissionRunner

LIMIT = "You've hit your 5-hour limit · resets 17:25:52"


@pytest.mark.parametrize('text,code', [
    (LIMIT, 'usage_limit'), ('usage_limit_reached', 'usage_limit'),
    ('HTTP 429 rate_limit', 'rate_limit'), ('HTTP/1.1 429 Too Many Requests', 'rate_limit'),
    ('status_code=429', 'rate_limit'), ('worker exited with code 1308', 'execution_failed'),
    ('Request 429 failed', 'execution_failed'), ('resets tomorrow', 'execution_failed'),
])
def test_only_evidenced_limits_are_classified(text, code):
    assert failure_code(text) == code


@pytest.mark.parametrize('provider,event,code', [
    ('claude', {'type': 'result', 'api_error': 'usage_limit_reached', 'result': 'Try later'}, 'usage_limit'),
    ('codex', {'type': 'turn.failed', 'error': {'message': 'HTTP 429: retry later'}}, 'rate_limit'),
])
def test_real_subprocess_fixture_carries_typed_limit(repository, temp_settings, tmp_path, provider, event, code):
    cli = tmp_path / 'fixture_cli.py'
    cli.write_text('import sys\nprint(' + repr(json.dumps(event)) + ')\nsys.exit(1)\n')
    repository.create_mission(Mission(id='m_cli_limit', title='t', goal='t'))
    adapter = MCPSubprocessAdapter(command=sys.executable, args=[str(cli)],
                                  db_path=temp_settings.database_path, artifact_root=temp_settings.artifact_root, timeout=10)
    outcome = adapter.run_turn('m_cli_limit', 'chulan', 't_limit', provider, 't',
                               MCPTools(repository, temp_settings.artifact_root))
    assert outcome.status == TurnStatus.BLOCKED and outcome.failure_code == code


def test_tool_output_mentioning_limit_is_not_terminal_evidence():
    stream = json.dumps({'type': 'assistant', 'message': {'text': 'HTTP 429 usage_limit_reached'}})
    assert cli_limit_failure_code('', stream) is None


@pytest.mark.parametrize('activity,published,raises', [(False, False, False), (True, False, False),
                                                     (True, True, False), (True, False, True)])
def test_runner_card_preserves_progress_and_reply_without_history_pollution(repository, temp_settings, activity, published, raises):
    repository.create_conversation(Conversation(id='c_limit', title='t'))
    repository.create_mission(Mission(id='m_limit', title='t', goal='t', conversation_id='c_limit', target_agent_id='chulan'))

    class Adapter:
        def run_turn(self, mid, aid, tid, pid, prompt, tools):
            if activity:
                repository.add_event(mid, 'tool.call', {'turn_id': tid, 'tool_name': 'read_context'})
                repository.add_event(mid, 'artifact', {'turn_id': tid, 'path': 'acceptance/result.md'})
            if published:
                tools.publish_message(mid, aid, tid, '部分结果', pid)
            if raises:
                raise RuntimeError('fixture process crashed')
            return TurnOutcome(status=TurnStatus.BLOCKED, summary=LIMIT, failure_code='usage_limit')

    MissionRunner(repository, temp_settings, adapter=Adapter()).run('m_limit')
    detail = build_mission_detail(repository, temp_settings, 'm_limit')
    failure = detail['failure_events'][0]
    assert '超时' not in failure['content']
    assert ('已发布的回复已保留' in failure['content']) == published
    progress = failure['metadata']['host_progress']
    assert progress['reason'] == ('execution_failed' if raises else 'usage_limit')
    assert progress['tool_calls'] == int(activity) + int(published)
    assert progress['artifacts'] == (['acceptance/result.md'] if activity else [])
    if not raises:
        assert progress['error'] == LIMIT  # No inferred timezone or reset state.
    assert all(m.role != MessageRole.SYSTEM for m in repository.list_messages('m_limit'))
    assert len([m for m in repository.list_messages('m_limit') if m.role == MessageRole.AGENT]) == int(published)
    with TestClient(create_app(temp_settings)) as client:
        assert client.get('/api/missions/m_limit').json()['failure_events'] == detail['failure_events']
        history = client.get('/api/conversations/c_limit').json()['messages']
        assert all(LIMIT not in m['content'] for m in history)


def test_limit_then_successful_fallback_has_no_failure_card_or_reason_in_next_prompt(repository, temp_settings):
    repository.create_mission(Mission(id='m_success', title='t', goal='t', target_agent_id='chulan'))
    prompts = []

    class Adapter:
        def run_turn(self, mid, aid, tid, pid, prompt, tools):
            prompts.append(prompt)
            if pid == 'claude':
                return TurnOutcome(status=TurnStatus.BLOCKED, summary=LIMIT, failure_code='usage_limit')
            tools.publish_message(mid, aid, tid, '完成', pid)
            return TurnOutcome(status=TurnStatus.COMPLETED)

    runner = MissionRunner(repository, temp_settings, adapter=Adapter())
    runner.agents = [AgentProfile(id='chulan', display_name='张楚岚', role='开发', persona='务实',
                                 default_provider_id='claude', fallback_provider_ids=['codex'])]
    assert runner.run('m_success').status == MissionStatus.COMPLETED
    assert len(prompts) == 2 and all(LIMIT not in p for p in prompts)
    assert build_mission_detail(repository, temp_settings, 'm_success')['failure_events'] == []


def test_legacy_api_projection_scopes_activity_and_reason_to_latest_attempt(repository, temp_settings):
    repository.create_mission(Mission(id='m_legacy', title='t', goal='t', status=MissionStatus.BLOCKED))
    base = {'turn_id': 't', 'agent_id': 'chulan'}
    add = lambda kind, **payload: repository.add_event('m_legacy', kind, {**base, **payload})
    add('turn.started', provider_id='claude', attempt=1)
    add('tool.call', tool_name='old_attempt')
    add('artifact', path='old.md')
    add('turn.blocked', provider_id='claude', attempt=1, error='timeout after 5s')
    add('turn.progress_note', provider_id='claude', attempt=1, source='host', reason='hard_timeout')
    add('turn.started', provider_id='codex', attempt=2)
    add('tool.call', tool_name='new_attempt')
    add('turn.blocked', provider_id='codex', attempt=2, error='HTTP 429: retry later')
    before = len(repository.list_events_after('m_legacy', 0))
    progress = build_mission_detail(repository, temp_settings, 'm_legacy')['failure_events'][0]['metadata']['host_progress']
    assert progress['reason'] == 'rate_limit' and progress['error'] == 'HTTP 429: retry later'
    assert progress['tools'] == [{'name': 'new_attempt', 'count': 1}] and progress['artifacts'] == []
    assert len(repository.list_events_after('m_legacy', 0)) == before  # Read-only compatibility.


@pytest.mark.parametrize('has_note', [False, True])
def test_card_error_redacts_credentials_urls_and_paths_before_trimming(repository, temp_settings, has_note):
    secret = 'fixture-' + 'private-value'
    raw = f'HTTP 429 Authorization: Bearer {secret} api_key="{secret}" /tmp/private/example.txt C:\\private\\cli.log https://user:{secret}@example.test/error?token={secret}'
    repository.create_mission(Mission(id='m_redact', title='t', goal='t', status=MissionStatus.BLOCKED))
    base = {'turn_id': 't', 'agent_id': 'chulan', 'provider_id': 'claude', 'attempt': 1}
    repository.add_event('m_redact', 'turn.blocked', {**base, 'error': raw})
    if has_note:
        repository.add_event('m_redact', 'turn.progress_note', {**base, 'source': 'host', 'reason': 'rate_limit', 'error': raw,
                                                              'tools': [], 'artifacts': [], 'tool_calls': 0})
    error = build_mission_detail(repository, temp_settings, 'm_redact')['failure_events'][0]['metadata']['host_progress']['error']
    assert secret not in error and '/tmp/private' not in error and 'C:\\private' not in error
    assert 'example.test' not in error and 'HTTP 429' in error
    assert '[PATH]' in error and '[URL]' in error
    assert secret not in public_failure_text('x' * 280 + ' api_key="' + secret * 20 + '"')


def test_final_terminal_error_wins_over_earlier_limit():
    stream = "\n".join(json.dumps(e) for e in [
        {"type": "error", "message": "HTTP 429"},
        {"type": "turn.failed", "error": {"message": "not logged in"}},
    ])
    assert cli_limit_failure_code('', stream) is None
    assert cli_limit_failure_code('', json.dumps({"type": "turn.failed", "error": {
        "message": "retry later", "code": "rate_limit_exceeded"}})) == 'rate_limit'


@pytest.mark.parametrize('code', ['usage_limit', 'rate_limit'])
def test_limits_are_environment_failures_in_observability(code):
    from backend.app.observability.turns import Turn
    turn = Turn(turn_id='t', mission_id='m', mission_title='t', index=1, agent_id='chulan',
                provider_id='claude', status='blocked', failure_code=code)
    assert turn.failure_class == code and turn.attribution == 'environment'


def test_public_error_redacts_basic_auth_and_preserves_reset_timezone():
    assert 'YWJjOmRlZg==' not in public_failure_text('Authorization: Basic YWJjOmRlZg==')
    assert public_failure_text('resets 2:40pm (Asia/Shanghai)') == 'resets 2:40pm (Asia/Shanghai)'
