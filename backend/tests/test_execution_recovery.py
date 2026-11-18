import json
from pathlib import Path
import sys
import time
import subprocess
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.app.adapters.antigravity_session import AntigravitySessionPool
from backend.app.adapters.mcp_subprocess import MCPSubprocessAdapter
from backend.app.domain.models import Conversation, Mission, MissionStatus, TurnStatus
from backend.app.mcp.session_server import dispatch
from backend.app.mcp.tools import MCPTools
from backend.app.orchestration import interruption
from backend.app.storage.execution import ExecutionLedger


def mission(repo, mid='m1', conversation='c1'):
    if not repo.get_conversation(conversation):
        repo.create_conversation(Conversation(id=conversation, title='test'))
    value = Mission(id=mid, title='test', goal='test', conversation_id=conversation, status=MissionStatus.RUNNING)
    repo.create_mission(value)
    return value


def call(repo, root, worker, token, content='hello', op='final'):
    return dispatch(repo, root, worker, {'id': 1, 'method': 'tools/call', 'params': {
        'name': 'publish_message', 'arguments': {'context_token': token, 'operation_id': op, 'content': content},
    }})['result']


def test_fencing_rotation_dedup_and_conflict(repository, temp_settings):
    mission(repository)
    ledger = ExecutionLedger(repository); ledger.register('w')
    token, _ = ledger.begin('w', 'm1', 'wangye', 't1', 'antigravity', timeout=30)
    first = call(repository, temp_settings.artifact_root, 'w', token)
    assert not first['isError']
    assert call(repository, temp_settings.artifact_root, 'w', token, op='retry') == first
    assert call(repository, temp_settings.artifact_root, 'w', token, content='changed')['isError']
    assert len(repository.list_messages('m1')) == 1
    ledger.release('w', token, outcome='completed')
    new, _ = ledger.begin('w', 'm1', 'wangye', 't2', 'antigravity', timeout=30)
    assert call(repository, temp_settings.artifact_root, 'w', token)['isError']
    assert call(repository, temp_settings.artifact_root, 'different-worker', new)['isError']
    assert not call(repository, temp_settings.artifact_root, 'w', new)['isError']
    assert {message.turn_id for message in repository.list_messages('m1')} == {'t1', 't2'}


def test_atomic_publication_and_operation_receipt(repository, temp_settings, monkeypatch):
    mission(repository)
    ledger = ExecutionLedger(repository); ledger.register('w')
    token, _ = ledger.begin('w', 'm1', 'wangye', 't1', 'antigravity', timeout=30)
    add = repository.add_event
    def fail_receipt(mid, kind, payload, **kwargs):
        if kind == 'mcp.receipt':
            raise ValueError('simulated commit failure')
        return add(mid, kind, payload, **kwargs)
    monkeypatch.setattr(repository, 'add_event', fail_receipt)
    assert call(repository, temp_settings.artifact_root, 'w', token)['isError']
    assert repository.list_messages('m1') == []
    monkeypatch.setattr(repository, 'add_event', add)
    assert not call(repository, temp_settings.artifact_root, 'w', token)['isError']
    assert len(repository.list_messages('m1')) == 1


def test_concurrent_publication_is_deduplicated(repository, temp_settings):
    mission(repository)
    ledger = ExecutionLedger(repository); ledger.register('w')
    token, _ = ledger.begin('w', 'm1', 'wangye', 't1', 'antigravity', timeout=30)
    with ThreadPoolExecutor(max_workers=4) as executor:
        answers = list(executor.map(lambda _: call(repository, temp_settings.artifact_root, 'w', token), range(8)))
    assert all(answer == answers[0] and not answer['isError'] for answer in answers)
    assert len(repository.list_messages('m1')) == 1


def test_tool_cannot_commit_after_its_grant_deadline(repository):
    mission(repository)
    ledger = ExecutionLedger(repository); ledger.register('w')
    token, _ = ledger.begin('w', 'm1', 'wangye', 't1', 'antigravity', timeout=0.05)
    from backend.app.mcp.tools import MCPToolError
    def slow_handler(grant):
        repository.add_event('m1', 'should_rollback', {})
        time.sleep(0.07)
        return {'isError': False}
    with pytest.raises(MCPToolError, match='expired'):
        ledger.call('w', token, 'log_thought', {}, 'slow', slow_handler)
    assert not any(e.event_type == 'should_rollback' for e in repository.list_events_after('m1', 0))


def test_expired_grant_and_service_restart_are_not_replayed(repository, temp_settings):
    mission(repository)
    ledger = ExecutionLedger(repository); ledger.register('w')
    token, _ = ledger.begin('w', 'm1', 'wangye', 't1', 'antigravity', timeout=-1)
    assert call(repository, temp_settings.artifact_root, 'w', token)['isError']
    ledger.checkpoint('w', last_summary='读取文件 · App.tsx')
    ledger.recover()
    assert repository.get_mission('m1').status == MissionStatus.BLOCKED
    assert call(repository, temp_settings.artifact_root, 'w', token)['isError']
    assert '读取文件 · App.tsx' in ledger.recovery_hint('c1', 'wangye')
    assert ledger.recovery_hint('another-conversation', 'wangye') == ''
    assert ledger.recovery_hint('c1', 'baobao') == ''
    assert repository.list_messages('m1') == []


def test_execution_capability_is_redacted_from_diagnostics():
    from backend.app.adapters.cli_diagnostics import _redact
    for text in ['context_token=private-grant', '{"context_token":"private-grant"}']:
        assert 'private-grant' not in _redact(text)


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    value = AntigravitySessionPool(idle_seconds=30)
    yield value
    value.stop()


def adapter(temp_settings, tmp_path, mode='normal', timeout=8):
    launcher = tmp_path/'agy'
    target = str(Path(__file__).with_name('_fake_persistent_agy.py').resolve())
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" -c "import runpy; runpy.run_path({repr(target)}, run_name=\'__main__\')" "$@"\n')
    launcher.chmod(0o700)
    return MCPSubprocessAdapter(str(launcher), [],
        db_path=temp_settings.database_path, artifact_root=temp_settings.artifact_root, timeout=timeout,
        extra_env={'AGY_TEST_RECORD': str(tmp_path/'calls.jsonl'), 'AGY_TEST_MODE': mode,
                   'AGY_TEST_GLOBAL': str(tmp_path/'home/.gemini/config/mcp_config.json')})


def only(pool):
    (worker,) = pool.workers.values()
    return worker


def calls(tmp_path):
    return [json.loads(line) for line in (tmp_path/'calls.jsonl').read_text().splitlines()]


def run(pool, cli, repository, temp_settings, mid='m1', turn='t1', agent='wangye', conversation='c1'):
    if not repository.get_mission(mid):
        mission(repository, mid, conversation)
    tools = MCPTools(repository, temp_settings.artifact_root)
    return pool.run_turn(cli, mid, agent, turn, 'antigravity', 'reply', tools)


def test_real_stdio_reuse_rotates_identity_and_keeps_history_isolated(pool, repository, temp_settings, tmp_path):
    cli = adapter(temp_settings, tmp_path)
    assert run(pool, cli, repository, temp_settings).status == TurnStatus.COMPLETED
    first_pid = only(pool).proc.pid
    assert run(pool, cli, repository, temp_settings, mid='m2', turn='t2').status == TurnStatus.COMPLETED
    assert only(pool).proc.pid == first_pid
    assert len(repository.list_messages('m1')) == len(repository.list_messages('m2')) == 1
    usages = [e.payload['usage']['input_tokens'] for mid in ['m1', 'm2']
              for e in repository.list_events_after(mid, 0) if e.event_type == 'turn.usage']
    assert usages == [100, 100]  # CLI result usage is cumulative across the session.
    assert run(pool, cli, repository, temp_settings, mid='m3', turn='t3', agent='baobao').status == TurnStatus.COMPLETED
    # Another persona gets its own worker; the first one is kept, not evicted.
    pids = {key[3]: worker.proc.pid for key, worker in pool.workers.items()}
    assert pids['wangye'] == first_pid and pids['baobao'] != first_pid
    assert repository.list_messages('m3')[0].agent_id == 'baobao'


def test_progress_renews_persistent_worker_deadline_and_mcp_grant(pool, repository, temp_settings, tmp_path):
    # Cold startup has its own real-pipe overhead; start once before the short budget.
    cli = adapter(temp_settings, tmp_path, 'long_tools', timeout=8)
    assert run(pool, cli, repository, temp_settings).status == TurnStatus.COMPLETED
    cli.timeout = 1.2
    cli.progress_check_interval = 0.2
    before = time.monotonic()
    result = run(pool, cli, repository, temp_settings, mid='m2', turn='t2')
    assert time.monotonic() - before > 1.2
    assert result.status == TurnStatus.COMPLETED  # Final publication requires the renewed grant.
    extensions = [e for e in repository.list_events_after('m2', 0) if e.event_type == 'turn.deadline_extended']
    assert extensions and extensions[-1].payload['extension'] >= 2


def test_worker_loss_before_publish_blocks_without_replay(pool, repository, temp_settings, tmp_path):
    outcome = run(pool, adapter(temp_settings, tmp_path, 'crash_before'), repository, temp_settings)
    assert outcome.status == TurnStatus.BLOCKED
    assert not pool.workers
    assert len((tmp_path/'calls.jsonl').read_text().splitlines()) == 1
    assert repository.list_messages('m1') == []
    assert any(e.event_type == 'execution.interrupted' for e in repository.list_events_after('m1', 0))
    next_outcome = run(pool, adapter(temp_settings, tmp_path), repository, temp_settings, mid='m2', turn='t2')
    assert next_outcome.status == TurnStatus.COMPLETED
    assert json.loads((tmp_path/'calls.jsonl').read_text().splitlines()[-1])['recovery']


def test_published_message_survives_worker_crash(pool, repository, temp_settings, tmp_path):
    outcome = run(pool, adapter(temp_settings, tmp_path, 'publish_then_crash'), repository, temp_settings)
    assert outcome.status == TurnStatus.COMPLETED
    assert len(repository.list_messages('m1')) == 1
    assert any(e.event_type == 'turn.warning' for e in repository.list_events_after('m1', 0))
    assert not pool.workers


def test_dead_idle_worker_is_rebuilt_without_touching_global_config(pool, repository, temp_settings, tmp_path):
    cli = adapter(temp_settings, tmp_path)
    assert run(pool, cli, repository, temp_settings).status == TurnStatus.COMPLETED
    old = only(pool).proc
    old.kill(); old.wait(timeout=3)
    assert run(pool, cli, repository, temp_settings, mid='m2', turn='t2').status == TurnStatus.COMPLETED
    assert only(pool).proc.pid != old.pid
    pool.idle_seconds = 0
    deadline = time.monotonic() + 3
    while pool.workers and time.monotonic() < deadline:
        time.sleep(0.03)
    assert not pool.workers
    assert not (tmp_path/'home/.gemini/config/mcp_config.json').exists()


def test_waiting_bubble_is_active_while_cli_is_initializing(pool, repository, temp_settings, tmp_path):
    cli = adapter(temp_settings, tmp_path)
    cli.extra_env['AGY_TEST_INIT_DELAY'] = '0.5'
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run, pool, cli, repository, temp_settings)
        deadline = time.monotonic() + 2
        while 't1' not in cli.live_progress.snapshot_for_mission('m1') and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cli.live_progress.snapshot_for_mission('m1') == {'t1': ''}
        assert not pool.workers  # still waiting for init, before a request is delivered
        assert future.result(timeout=8).status == TurnStatus.COMPLETED
    assert cli.live_progress.snapshot_for_mission('m1') == {}


def test_hung_worker_times_out_and_fences_tools(pool, repository, temp_settings, tmp_path):
    outcome = run(pool, adapter(temp_settings, tmp_path, 'hang', timeout=0.5), repository, temp_settings)
    assert outcome.status == TurnStatus.BLOCKED
    assert not pool.workers
    assert not (tmp_path/'home/.gemini/config/mcp_config.json').exists()


def test_conversations_run_concurrently_in_separate_workers(pool, repository, temp_settings, tmp_path):
    cli = adapter(temp_settings, tmp_path)
    cli.extra_env['AGY_TEST_TURN_DELAY'] = '1'
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, pool, cli, repository, temp_settings, mid, turn, 'wangye', conversation)
                   for mid, turn, conversation in [('m1', 't1', 'c1'), ('m2', 't2', 'c2')]]
        outcomes = [future.result(timeout=15) for future in futures]
    assert [outcome.status for outcome in outcomes] == [TurnStatus.COMPLETED] * 2
    first, second = sorted(calls(tmp_path), key=lambda call: call['started'])
    assert first['pid'] != second['pid']
    assert second['started'] - first['started'] < 1  # the second began before the first finished
    assert len(pool.workers) == 2
    assert not (tmp_path/'home/.gemini/config/mcp_config.json').exists()


def test_stop_while_queued_behind_same_conversation_returns_at_once(pool, repository, temp_settings, tmp_path):
    cli = adapter(temp_settings, tmp_path)
    cli.extra_env['AGY_TEST_TURN_DELAY'] = '2'
    queued = mission(repository, 'm_queued')
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            running = executor.submit(run, pool, cli, repository, temp_settings)
            deadline = time.monotonic() + 8
            while not (tmp_path/'calls.jsonl').exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            waiting = executor.submit(run, pool, cli, repository, temp_settings, 'm_queued', 't_queued')
            time.sleep(0.2)
            stopped_at = time.monotonic()
            interruption.request_pause(repository, queued)
            outcome = waiting.result(timeout=1)
            assert time.monotonic() - stopped_at < 1
            assert running.result(timeout=10).status == TurnStatus.COMPLETED
    finally:
        interruption._pauses.discard('m_queued')
    assert outcome.status == TurnStatus.BLOCKED and outcome.failure_code == 'stopped'
    phases = [e.payload['phase'] for e in repository.list_events_after('m_queued', 0)
              if e.event_type == 'provider.activity' and e.payload.get('tool_name') == 'worker_queue']
    assert phases == ['started', 'failed']
    assert len(calls(tmp_path)) == 1  # the queued turn was never delivered


def test_capacity_evicts_the_idle_worker_first(pool, repository, temp_settings, tmp_path):
    pool.max_workers = 1
    cli = adapter(temp_settings, tmp_path)
    assert run(pool, cli, repository, temp_settings).status == TurnStatus.COMPLETED
    first = only(pool)
    assert run(pool, cli, repository, temp_settings, 'm2', 't2', conversation='c2').status == TurnStatus.COMPLETED
    assert only(pool) is not first and not first.alive()


def test_startup_recovery_does_not_wait_for_live_config_owner(tmp_path, monkeypatch):
    # An older Unity build may still own the global config while its turn runs.
    from backend.app.adapters import antigravity_config as agc
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    cfg = agc.global_mcp_config_path()
    cfg.parent.mkdir(parents=True)
    cfg.write_text('{"mcpServers": {"yiren_hub": {"command": "live"}}}')
    agc._sentinel_path().write_text(json.dumps({'existed': False, 'content': None}))
    with agc._flock():
        before = cfg.read_bytes()
        started = time.monotonic()
        agc.recover_antigravity_global_config()
        assert time.monotonic() - started < 0.5
        assert cfg.read_bytes() == before


def test_watchdog_reaps_child_after_host_sigkill(tmp_path):
    """Exercise parent death through real process/pipe EOF, not a mocked poll."""
    watchdog = Path(__file__).parents[1]/'app/adapters/process_watchdog.py'
    child_code = 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'
    parent_code = '''import os,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c',sys.argv[2]],start_new_session=True)
read_fd,write_fd=os.pipe()
watcher=subprocess.Popen([sys.executable,sys.argv[1],str(read_fd),str(child.pid)],pass_fds=(read_fd,),start_new_session=True)
os.close(read_fd)
print(child.pid,watcher.pid,flush=True)
time.sleep(60)
'''
    parent = subprocess.Popen([sys.executable, '-c', parent_code, str(watchdog), child_code], stdout=subprocess.PIPE, text=True)
    child, watcher = map(int, parent.stdout.readline().split())
    try:
        parent.kill(); parent.wait(timeout=3)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            # Orphans can briefly be zombies until launchd/init reaps them.
            result = subprocess.run(['ps', '-p', str(child), '-o', 'stat='], capture_output=True, text=True)
            if not result.stdout.strip() or result.stdout.strip().startswith('Z'):
                break
            time.sleep(0.05)
        else:
            pytest.fail('watchdog left the native command running')
    finally:
        parent.stdout.close()
        for pid in [child, watcher]:
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def test_http_followups_use_the_enabled_persistent_adapter(pool, temp_settings, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app.main import create_app
    from backend.app.adapters import antigravity_session
    from backend.app.settings import AgentOverride
    import shutil
    cli = adapter(temp_settings, tmp_path)
    config = tmp_path/'config'; shutil.copytree('config', config)
    (config/'cli-providers.json').write_text(json.dumps([
        {'id': 'antigravity', 'label': 'Fake AGY', 'command': cli.command,
         'args': [], 'enabled': True, 'mcp_enabled': True},
    ]))
    monkeypatch.setattr(antigravity_session, 'SESSION_POOL', pool)
    for key, value in cli.extra_env.items():
        monkeypatch.setenv(key, value)
    settings = temp_settings.model_copy(update={
        'use_fake_cli': False, 'antigravity_persistent': True, 'config_dir': config,
        'agent_overrides': {'wangye': AgentOverride(default_provider_id='antigravity', fallback_provider_ids=[])},
    })
    with TestClient(create_app(settings)) as client:
        conversation = None
        pids = []
        for _ in range(2):
            body = {'goal': '你好', 'target_agent_ids': ['wangye']}
            if conversation:
                body['conversation_id'] = conversation
            created = client.post('/api/missions', json=body)
            assert created.status_code == 201, created.text
            mid = created.json()['mission']['id']
            conversation = created.json()['mission']['conversation_id']
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                detail = client.get('/api/missions/' + mid).json()
                if detail['mission']['status'] in {'completed', 'blocked', 'failed'}:
                    break
                time.sleep(0.02)
            assert detail['mission']['status'] == 'completed', detail
            assert [m['content'] for m in detail['messages'] if m['role'] == 'agent'] == ['verified reply']
            pids.append(only(pool).proc.pid)
        assert pids[0] == pids[1]
    assert not pool.workers


def test_worker_waits_out_an_older_build_instead_of_loading_its_server(pool, repository, temp_settings, tmp_path):
    """Regression: a new agy started while an older build had injected yiren_hub
    loaded that server and published into the older instance's database."""
    import threading
    from backend.app.adapters import antigravity_config as agc
    cfg = agc.global_mcp_config_path()
    cfg.parent.mkdir(parents=True)
    injected, release = threading.Event(), threading.Event()

    def legacy_turn():
        with agc._flock():
            cfg.write_text(json.dumps({'mcpServers': {'yiren_hub': {'command': 'legacy'}}}))
            injected.set()
            release.wait(5)
            cfg.unlink()

    legacy = threading.Thread(target=legacy_turn); legacy.start()
    injected.wait(2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run, pool, adapter(temp_settings, tmp_path), repository, temp_settings)
        time.sleep(0.5)
        assert not (tmp_path/'calls.jsonl').exists()  # nothing launched while the older build owns the config
        release.set()
        assert future.result(timeout=10).status == TurnStatus.COMPLETED
    legacy.join(2)
    assert calls(tmp_path)[0]['global_servers'] == []
    waits = [e.payload['summary'] for e in repository.list_events_after('m1', 0)
             if e.event_type == 'provider.activity' and e.payload.get('activity_id', '').endswith(':legacy')]
    assert waits == ['等待旧版 Unity 实例结束 Antigravity 轮次']


def test_stop_while_waiting_for_an_older_build(pool, repository, temp_settings, tmp_path):
    import threading
    from backend.app.adapters import antigravity_config as agc
    release = threading.Event(); held = threading.Event()

    def legacy_turn():
        with agc._flock():
            held.set(); release.wait(5)

    legacy = threading.Thread(target=legacy_turn); legacy.start()
    held.wait(2)
    stopping = mission(repository, 'm_legacy_stop')
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run, pool, adapter(temp_settings, tmp_path), repository, temp_settings, 'm_legacy_stop', 't1')
            time.sleep(0.3)
            interruption.request_pause(repository, stopping)
            outcome = future.result(timeout=2)
    finally:
        interruption._pauses.discard('m_legacy_stop')
        release.set(); legacy.join(2)
    assert outcome.status == TurnStatus.BLOCKED and outcome.failure_code == 'stopped'
    assert not (tmp_path/'calls.jsonl').exists()
    assert not pool.workers
