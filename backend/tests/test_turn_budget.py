from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from backend.app.adapters.base import TurnOutcome
from backend.app.adapters.turn_budget import budgeted_turn, claim_attempt, remaining_timeout
from backend.app.adapters.turn_deadline import ToolResultDeadline
from backend.app.domain.models import Mission, TurnStatus
from backend.app.mcp.tools import MCPTools
from backend.app.orchestration.runner import MissionRunner


@pytest.mark.parametrize('elapsed,expected', [(0, ['codex', 'gemini']), (301, ['codex'])])
def test_chat_shared_budget(repository, temp_settings, monkeypatch, elapsed, expected):
    now = [1000.0]
    monkeypatch.setattr('backend.app.adapters.turn_budget.time.monotonic', lambda: now[0])
    temp_settings.cli_timeout = 300
    mission = Mission(id='budget', title='budget', goal='test', status='running')
    repository.create_mission(mission)
    calls, remaining = [], []

    def run(*args):
        calls.append(args[3])
        remaining.append(remaining_timeout(600))
        now[0] += elapsed or 40
        return TurnOutcome(status=TurnStatus.BLOCKED, failure_code='network', retryable=True)

    runner = MissionRunner(repository, temp_settings, Mock(run_turn=run))
    outcome = runner._run_turn_with_fallbacks(mission, 'chulan', 'budget-turn',
        ['codex', 'codex', 'gemini', 'claude'], 'test', MCPTools(repository, temp_settings.artifact_root))
    assert calls == expected
    assert remaining == ([300, 260] if not elapsed else [300])
    assert outcome.failure_code == 'budget_exhausted'
    assert remaining_timeout(600) == 600


def test_budget_resets_on_exception_and_isolates_workers():
    @budgeted_turn(300)
    def work():
        assert claim_attempt()
        assert claim_attempt()
        assert not claim_attempt()
        raise RuntimeError('test')

    def worker(_):
        with pytest.raises(RuntimeError):
            work()
        assert claim_attempt()
        assert remaining_timeout(600) == 600

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(worker, range(6)))


def test_tool_progress_renewal_extends_shared_budget(monkeypatch):
    # Issue #136: renewal used to be clamped back to the original budget, so a
    # busy turn always died at cli_timeout while reporting "extended".
    now = [0.0]
    monkeypatch.setattr('backend.app.adapters.turn_budget.time.monotonic', lambda: now[0])

    @budgeted_turn(10)
    def work():
        progress = ToolResultDeadline(10, check_interval=5, clock=lambda: now[0])
        now[0] = 4
        progress.record_result('tool')
        now[0] = 5
        extension = progress.check()
        assert extension['previous_deadline_at'] == 10
        assert extension['deadline_at'] == 15
        assert remaining_timeout(600) == 10
        now[0] = 11
        assert progress.remaining() == 4
        now[0] = 15
        assert progress.remaining() == 0
        assert remaining_timeout(600) == 0
    work()


def test_budget_without_progress_still_expires_at_cli_timeout(monkeypatch):
    now = [0.0]
    monkeypatch.setattr('backend.app.adapters.turn_budget.time.monotonic', lambda: now[0])

    @budgeted_turn(10)
    def work():
        progress = ToolResultDeadline(10, check_interval=5, clock=lambda: now[0])
        now[0] = 5
        assert progress.check() is None
        now[0] = 10
        assert progress.check() is None
        assert progress.remaining() == 0
    work()


def test_exhausted_budget_is_not_revived_by_late_check(monkeypatch):
    now = [0.0]
    monkeypatch.setattr('backend.app.adapters.turn_budget.time.monotonic', lambda: now[0])

    @budgeted_turn(10)
    def work():
        progress = ToolResultDeadline(20, check_interval=5, clock=lambda: now[0])
        now[0] = 4
        progress.record_result('tool')
        now[0] = 12  # Poll arrives after the shared budget already ran out.
        assert progress.check() is None
        assert progress.remaining() == 0
        assert remaining_timeout(600) == 0
    work()


def test_wall_and_monotonic_drift_is_not_reported_as_renewal(monkeypatch):
    # The real incident logged five "extensions" whose deadline_at slowly
    # decreased because the budget (monotonic) and deadline (wall) drifted.
    wall = [1000.0]
    monkeypatch.setattr('backend.app.adapters.turn_budget.time.monotonic', lambda: wall[0] - 0.013)

    @budgeted_turn(10)
    def work():
        progress = ToolResultDeadline(10, check_interval=5, clock=lambda: wall[0])
        wall[0] = 1004
        progress.record_result('tool')
        wall[0] = 1005
        extension = progress.check()
        assert extension['deadline_at'] > extension['previous_deadline_at'] + 1
        assert progress.deadline == 1015
    work()


def test_busy_process_survives_cli_timeout_inside_budgeted_turn(monkeypatch):
    import subprocess
    from backend.app.adapters.mcp_subprocess import ProcessWaitReason, _wait_for_process_with_activity

    clock = [0.0]
    monkeypatch.setattr('backend.app.adapters.turn_budget.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('backend.app.adapters.mcp_subprocess.time.time', lambda: clock[0])
    marks = [100, 400, 700, 1000]  # Tool results in four consecutive windows.
    extensions = []

    class Process:
        def wait(self, timeout):
            clock[0] += timeout
            raise subprocess.TimeoutExpired('fake', timeout)

    @budgeted_turn(600)
    def work():
        progress = ToolResultDeadline(600, check_interval=300, clock=lambda: clock[0])

        def refresh():
            while marks and clock[0] >= marks[0]:
                progress.record_result(marks.pop(0))

        return _wait_for_process_with_activity(
            Process(), hard_timeout=600, idle_timeout=0, last_activity_at=lambda: 0,
            refresh_activity=refresh, progress_deadline=progress, on_extension=extensions.append)

    assert work() is ProcessWaitReason.HARD_TIMEOUT
    assert clock[0] == pytest.approx(1800)
    assert [e['deadline_at'] for e in extensions] == [900, 1200, 1500, 1800]
    assert all(e['deadline_at'] > e['previous_deadline_at'] for e in extensions)


def test_api_budget_expired_before_dns(monkeypatch):
    from backend.app.adapters.api_transport import APIError, request
    resolve = Mock()
    monkeypatch.setattr('backend.app.adapters.api_transport.resolve_endpoint', resolve)
    now = [0.0]
    monkeypatch.setattr('backend.app.adapters.turn_budget.time.monotonic', lambda: now[0])

    @budgeted_turn(10)
    def work():
        now[0] = 11
        with pytest.raises(APIError) as error:
            with request({'base_url': 'https://example.com', 'protocol': 'openai_chat'},
                         'secret', 'GET', '/models'):
                pytest.fail('expired request started')
        assert error.value.code == 'timeout'
        resolve.assert_not_called()
    work()
