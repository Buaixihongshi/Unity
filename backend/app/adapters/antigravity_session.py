"""Bounded process reuse with per-turn MCP grants and durable reconciliation.

One retained worker per conversation and persona. Each worker runs in its own
workspace with its own MCP server, so workers for different conversations run
concurrently; turns for the same key share one stdin stream and queue. The
pool caps live workers and evicts idle ones first. It never replays a request
after stdin delivery: native side effects may have happened even when no
activity event reached the host.
"""
import atexit
from collections import deque
import json
import logging
import os
from pathlib import Path
import queue
import select
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from backend.app import extension_points
from backend.app import host_rules
from backend.app.adapters.antigravity_config import WaitCancelled, legacy_config_excluded, write_workspace_plugin
from backend.app.adapters.base import TurnOutcome
from backend.app.adapters.cli_diagnostics import write_cli_diagnostic
from backend.app.adapters.mcp_subprocess import MCPSubprocessAdapter, _augment_prompt_for_mcp, _mcp_published_message, _max_event_id, _emit_turn_usage
from backend.app.adapters.stream_events import StreamEventType, UsageRecord
from backend.app.adapters.stream_parsers import StreamLineParser
from backend.app.adapters.subprocess_cli import kill_process_tree
from backend.app.domain.models import TurnStatus
from backend.app.orchestration import interruption
from backend.app.storage.execution import ExecutionLedger


class TurnStopped(Exception):
    """The user paused the mission while this turn was running."""


def stream_args(args):
    """Preserve model/effort arguments; input framing and timeout belong to host."""
    result = []
    index = 0
    value_flags = {'--input-format', '--output-format', '--print-timeout'}
    while index < len(args):
        arg = args[index]
        if arg in {'-p', '--print', '--prompt'}:
            index += 1; continue
        if arg in value_flags:
            index += 2; continue
        if any(arg.startswith(flag + '=') for flag in value_flags):
            index += 1; continue
        if arg in {'--continue', '-c', '--conversation', '--project', '--new-project'} or any(
                arg.startswith(flag + '=') for flag in {'--conversation', '--project'}):
            raise ValueError('Persistent workers cannot inherit an external conversation or project')
        result.append(arg); index += 1
    return [*result, '--input-format', 'stream-json', '--output-format', 'stream-json', '--print-timeout', '0']


class _Worker:
    def __init__(self, adapter, key, ledger, timeout, *, cancelled=None, on_wait=None):
        self.adapter, self.key, self.ledger = adapter, key, ledger
        self.id = 'agy_' + uuid4().hex
        self.proc = None
        self.watchdog = None
        self.watchdog_fd = None
        self.tmp = None
        self.excluded = None
        self.events = queue.Queue(maxsize=512)
        self.stderr = deque(maxlen=50)
        self.readers = []
        self.last_used = time.monotonic()
        self.turns = 0
        self.session_id = None
        self.usage_totals = {}
        self.closed = False
        ledger.register(self.id, cwd=os.getcwd(), created_at=time.time())
        deadline = time.monotonic() + timeout
        try:
            self.tmp = tempfile.TemporaryDirectory(prefix='yiren-agy-session-')
            workspace = Path(self.tmp.name)
            env = {
                'YIREN_MCP_DB_PATH': str(adapter.db_path),
                'YIREN_MCP_ARTIFACT_ROOT': str(adapter.artifact_root),
                'YIREN_MCP_WORKSPACE_ROOT': str(Path.cwd().resolve()),
                'YIREN_MCP_WORKER_ID': self.id,
                'YIREN_MCP_EXTENSIONS': ','.join(extension_points.modules()),
                'PYTHONPATH': os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[3]), os.environ.get('PYTHONPATH')])),
            }
            spec = {'command': adapter.mcp_python, 'args': ['-m', 'backend.app.mcp.session_server'], 'env': env}
            write_workspace_plugin(workspace, spec)
            self.excluded = legacy_config_excluded(timeout, cancelled=cancelled, on_wait=on_wait)
            self.excluded.__enter__()
            if time.monotonic() >= deadline:
                raise TimeoutError('Antigravity initialization deadline exceeded waiting for an older Unity instance')
            args = stream_args(adapter.args)
            if not any(arg == '--add-dir' or arg.startswith('--add-dir=') for arg in args):
                args = ['--add-dir', os.getcwd(), *args]
            if '--dangerously-skip-permissions' not in args:
                args.insert(0, '--dangerously-skip-permissions')
            launch_env = {k: v for k, v in os.environ.items() if not k.startswith('YIREN_MCP_')}
            launch_env.update({k: v for k, v in adapter.extra_env.items() if not k.startswith('YIREN_MCP_')})
            self.proc = subprocess.Popen([adapter.command, *args], cwd=workspace, env=launch_env,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, bufsize=1, start_new_session=True)
            read_fd, self.watchdog_fd = os.pipe()
            try:
                self.watchdog = subprocess.Popen(
                    [adapter.mcp_python, str(Path(__file__).with_name('process_watchdog.py')), str(read_fd), str(self.proc.pid)],
                    pass_fds=(read_fd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                )
            finally:
                os.close(read_fd)
            ledger.checkpoint(self.id, pid=self.proc.pid)
            self.readers = [threading.Thread(target=target, daemon=True) for target in (self._stdout, self._stderr)]
            for reader in self.readers:
                reader.start()
            while time.monotonic() < deadline:
                event = self.next(deadline)
                if event is None:
                    raise RuntimeError('Antigravity exited before initialization')
                if event.get('event') == 'init':
                    self.session_id = event.get('conversation_id')
                    ledger.checkpoint(self.id, native_session_id=self.session_id)
                    break
            else:
                raise TimeoutError('Antigravity initialization timed out')
        except BaseException:
            self.close('startup_failed')
            raise

    def _stdout(self):
        def put(value):
            while not self.closed:
                try:
                    self.events.put(value, timeout=0.2)
                    return
                except queue.Full:
                    continue
        try:
            pending = b''
            while not self.closed:
                if not select.select([self.proc.stdout], [], [], 0.2)[0]:
                    continue
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                if len(pending) > 4 * 1024 * 1024:
                    break
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    try:
                        value = json.loads(line)
                        if isinstance(value, dict):
                            put(value)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
        except OSError:
            pass
        finally:
            put(None)

    def _stderr(self):
        try:
            while not self.closed:
                if not select.select([self.proc.stderr], [], [], 0.2)[0]:
                    continue
                chunk = os.read(self.proc.stderr.fileno(), 4096)
                if not chunk:
                    break
                self.stderr.append(chunk.decode('utf-8', errors='replace')[-1000:])
        except OSError:
            pass

    def next(self, deadline):
        while time.monotonic() < deadline:
            try:
                return self.events.get(timeout=min(0.2, max(0.001, deadline-time.monotonic())))
            except queue.Empty:
                if self.proc.poll() is not None:
                    # stdout reader may still be enqueueing the terminal frame.
                    try:
                        return self.events.get(timeout=0.2)
                    except queue.Empty:
                        return None
        raise TimeoutError('Antigravity turn deadline exceeded')

    def alive(self):
        return not self.closed and self.proc is not None and self.proc.poll() is None

    def close(self, reason):
        if self.closed:
            return
        self.closed = True
        retry_revoke = False
        try:
            self.ledger.revoke(self.id, reason)
        except Exception:
            # A dying MCP child can still hold SQLite's write lock. Its group
            # must be terminated even when fencing cannot commit immediately.
            retry_revoke = True
        if self.proc is not None:
            # The group can contain an MCP/command child even after agy exits.
            import signal
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass
            kill_process_tree(self.proc)
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass
            for reader in self.readers:
                reader.join(timeout=0.5)
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        if self.watchdog_fd is not None:
            os.close(self.watchdog_fd)
            self.watchdog_fd = None
        if self.watchdog is not None:
            try:
                self.watchdog.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.watchdog.kill(); self.watchdog.wait(timeout=3)
        if self.excluded is not None:
            self.excluded.__exit__(None, None, None)
            self.excluded = None
        if self.tmp is not None:
            self.tmp.cleanup()
        if retry_revoke:
            try:
                self.ledger.revoke(self.id, reason)
            except Exception:
                logging.getLogger(__name__).warning('Could not persist worker revocation after termination')

    def turn_usage(self, result, duration_ms):
        usage = result.get('usage')
        if not isinstance(usage, dict):
            return None
        counts = {k: v for k, v in usage.items() if isinstance(v, int) and not isinstance(v, bool) and v >= 0}
        delta = {k: max(0, v-self.usage_totals.get(k, 0)) for k, v in counts.items()}
        self.usage_totals = counts
        return UsageRecord(input_tokens=delta.get('input_tokens', 0),
                           cached_input_tokens=delta.get('cache_read_tokens', 0),
                           output_tokens=delta.get('output_tokens', 0),
                           reasoning_output_tokens=delta.get('thinking_tokens', 0), duration_ms=duration_ms)


class AntigravitySessionPool:
    def __init__(self, *, idle_seconds=15, max_turns=12, max_workers=4):
        self.idle_seconds, self.max_turns, self.max_workers = idle_seconds, max_turns, max_workers
        self.lock = threading.Lock()  # guards the maps below, never held across a turn
        self.workers = {}  # key -> retained _Worker
        self.slots = {}    # key -> Lock held by the turn using that key
        self.stopped = threading.Event()
        threading.Thread(target=self._reap, daemon=True).start()

    def _try_reserve(self, key):
        """Take the key's slot if a worker may run for it; returns workers evicted to make room."""
        with self.lock:
            slot = self.slots.setdefault(key, threading.Lock())
            if not slot.acquire(blocking=False):
                return None
            busy = {k for k, v in self.slots.items() if v.locked()}
            if key in self.workers or len(busy | set(self.workers)) <= self.max_workers:
                return []
            for other, worker in sorted(self.workers.items(), key=lambda item: item[1].last_used):
                # Hold the victim's slot until it is closed so no turn picks it up.
                if other not in busy and self.slots[other].acquire(blocking=False):
                    del self.workers[other]
                    return [(other, worker)]
            slot.release()
            return None

    def _release(self, key, slot=None):
        with self.lock:
            current = self.slots.get(key)
            slot = slot or current
            slot.release()
            if slot is current and key not in self.workers:
                del self.slots[key]

    def _retire(self, victims, reason):
        for key, worker in victims:
            worker.close(reason)
            self._release(key)

    def _reap(self):
        while not self.stopped.wait(min(1, max(0.02, self.idle_seconds / 2))):
            victims = []
            with self.lock:
                for key, worker in list(self.workers.items()):
                    # Never block an active turn; its deadline is supervised separately.
                    if self.slots[key].acquire(blocking=False):
                        if not worker.alive() or time.monotonic()-worker.last_used >= self.idle_seconds:
                            del self.workers[key]
                            victims.append((key, worker))
                        else:
                            self.slots[key].release()
            for key, worker in victims:
                worker.close('idle_expired' if worker.alive() else 'process_lost')
                self._release(key)

    def close(self, database_path=None):
        with self.lock:
            keys = [key for key in self.workers
                    if database_path is None or str(Path(database_path).resolve()) == key[0]]
            slots = [(key, self.slots[key]) for key in keys]
        for key, slot in slots:
            slot.acquire()  # let a running turn finish first
            with self.lock:
                # A slot replaced meanwhile no longer guards this key's worker.
                worker = self.workers.pop(key, None) if self.slots.get(key) is slot else None
            if worker:
                worker.close('service_shutdown')
            self._release(key, slot)

    def stop(self):
        self.stopped.set(); self.close()

    def run_turn(self, adapter, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        from backend.app.adapters.turn_deadline import ToolResultDeadline, is_hub_work_result, is_tool_return
        started = time.monotonic()
        timeout_code = 'hard_timeout'
        repo = tools.repository
        mission = repo.get_mission(mission_id)
        key = (str(adapter.db_path), str(adapter.artifact_root), mission.conversation_id or mission_id,
               agent_id, provider_id, adapter.command, tuple(adapter.args), os.getcwd(),
               tuple(sorted(adapter.extra_env.items())))
        # Surface the waiting bubble while queued and during cold initialization.
        adapter.live_progress.mark_active(mission_id, turn_id)
        wait_until = time.monotonic() + max(0, adapter.timeout)
        queued = None
        while (victims := self._try_reserve(key)) is None:
            if queued is None:
                queued = {'turn_id': turn_id, 'agent_id': agent_id, 'provider_id': provider_id,
                          'activity_id': turn_id + ':queue', 'tool_name': 'worker_queue'}
                repo.add_event(mission_id, 'provider.activity', {
                    **queued, 'phase': 'started', 'summary': '排队等待 Antigravity：同一会话的上一轮未结束或并行会话已满'})
            if interruption.pause_requested(mission_id):
                repo.add_event(mission_id, 'provider.activity', {**queued, 'phase': 'failed', 'summary': '已停止排队'})
                adapter.live_progress.clear(mission_id, turn_id)
                return TurnOutcome(TurnStatus.BLOCKED, interruption.STOPPED_SUMMARY, failure_code='stopped', retryable=False)
            if time.monotonic() >= wait_until:
                repo.add_event(mission_id, 'provider.activity', {**queued, 'phase': 'failed', 'summary': '排队超时'})
                adapter.live_progress.clear(mission_id, turn_id)
                return TurnOutcome(TurnStatus.BLOCKED, '等待 Antigravity 工作进程超时（同一会话的上一轮未结束或并行会话已满）；本轮未发送。')
            time.sleep(0.05)
        if queued:
            repo.add_event(mission_id, 'provider.activity', {**queued, 'phase': 'completed', 'summary': '排队结束'})
        self._retire(victims, 'capacity_evicted')
        # The turn budget starts once the worker is ours; queueing has its own limit.
        progress = ToolResultDeadline(adapter.timeout, check_interval=adapter.progress_check_interval)
        worker = None
        token = None
        initializing = False
        try:
            ledger = ExecutionLedger(repo)
            worker = self.workers.get(key)
            if worker and (not worker.alive() or worker.turns >= self.max_turns):
                worker.close('worker_replaced')
                self._forget(key)
                worker = None
            if worker is None:
                initializing = True
                repo.add_event(mission_id, 'provider.activity', {
                    'turn_id': turn_id, 'agent_id': agent_id, 'provider_id': provider_id,
                    'activity_id': turn_id + ':startup', 'tool_name': 'worker_startup',
                    'phase': 'started', 'summary': '启动 Antigravity 工作进程',
                })
                def legacy_wait():
                    repo.add_event(mission_id, 'provider.activity', {
                        'turn_id': turn_id, 'agent_id': agent_id, 'provider_id': provider_id,
                        'activity_id': turn_id + ':legacy', 'tool_name': 'worker_queue',
                        'phase': 'started', 'summary': '等待旧版 Unity 实例结束 Antigravity 轮次'})
                worker = _Worker(adapter, key, ledger, max(0.001, progress.remaining()),
                                 cancelled=lambda: interruption.pause_requested(mission_id), on_wait=legacy_wait)
                with self.lock:
                    self.workers[key] = worker
                repo.add_event(mission_id, 'provider.activity', {
                    'turn_id': turn_id, 'agent_id': agent_id, 'provider_id': provider_id,
                    'activity_id': turn_id + ':startup', 'tool_name': 'worker_startup',
                    'phase': 'completed', 'summary': 'Antigravity 已就绪',
                })
                initializing = False
            cursor = _max_event_id(repo, mission_id)
            token, receipt = ledger.begin(worker.id, mission_id, agent_id, turn_id, provider_id,
                                         timeout=max(0.001, progress.remaining()),
                                         valid_agent_ids=sorted(tools.valid_agent_ids or []),
                                         max_a2a_invocations=tools.max_a2a_invocations,
                                         scheduled_agent_ids=tools.scheduled_targets(),
                                         tasks_enabled=getattr(tools, 'tasks_enabled', True))
            instruction = (
                '\n【本轮执行凭证】context_token=' + token + '\n'
                + host_rules.CONTEXT_TOKEN.text + host_rules.OPERATION_ID.text + host_rules.HOST_OWNS_STATUS.text
                + host_rules.AGY_SERVER_NAME.text
            )
            # Scope data comes from the host, never from previous model memory.
            instruction += '\n当前任务：' + mission_id + '；当前回合：' + turn_id + '。\n'
            recovery = ledger.recovery_hint(mission.conversation_id, agent_id)
            message = {'event': 'user', 'message': {'content': _augment_prompt_for_mcp(prompt) + recovery + instruction}}
            tools.repository.add_event(mission_id, 'execution.delivery_started', {'turn_id': turn_id, 'worker_id': worker.id})
            worker.proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
            worker.proc.stdin.flush()
            parser = StreamLineParser('antigravity')
            last_activity = time.monotonic()
            activity_cursor = cursor
            while True:
                if interruption.pause_requested(mission_id):
                    raise TurnStopped(interruption.STOPPED_SUMMARY)
                # MCP calls count as activity even if the provider's stream is quiet.
                for record in repo.list_events_after(mission_id, activity_cursor):
                    activity_cursor = max(activity_cursor, int(record.id))
                    if is_hub_work_result(record, turn_id):
                        progress.record_result(('hub', record.id))
                    if record.payload.get('turn_id') == turn_id and record.event_type in {'tool.call', 'tool.result', 'message', 'agent.thought'}:
                        last_activity = time.monotonic()
                extension = progress.check()
                if extension and progress.remaining() > 0:
                    ledger.renew(worker.id, token, timeout=progress.remaining())
                    repo.add_event(mission_id, 'turn.deadline_extended', {
                        'turn_id': turn_id, 'agent_id': agent_id, 'provider_id': provider_id,
                        **extension,
                    })
                until = progress.remaining()
                if adapter.idle_timeout:
                    until = min(until, max(0, last_activity + adapter.idle_timeout - time.monotonic()))
                if until <= 0:
                    timeout_code = 'hard_timeout' if progress.remaining() <= 0 else 'idle_timeout'
                    raise TimeoutError('Antigravity turn deadline exceeded')
                # Poll periodically so MCP activity refreshes an idle deadline.
                try:
                    event = worker.events.get(timeout=min(0.2, until))
                except queue.Empty:
                    if not worker.alive():
                        raise RuntimeError('Antigravity process exited before result')
                    continue
                if event is None:
                    raise RuntimeError('Antigravity output stream closed before result')
                if event.get('event') == 'result':
                    result = event.get('result') or {}
                    if not isinstance(result, dict):
                        raise RuntimeError('Malformed Antigravity result')
                    published = _mcp_published_message(repo, mission_id, turn_id, cursor, receipt, agent_id, provider_id)
                    ledger.checkpoint(worker.id, last_turn_id=turn_id, last_status=result.get('status'))
                    if result.get('status') != 'SUCCESS' or not published:
                        raise RuntimeError('Antigravity ended without a verified MCP publication')
                    _emit_turn_usage(repo, mission_id, agent_id, provider_id, turn_id,
                                     worker.turn_usage(result, int((time.monotonic()-started)*1000)))
                    ledger.release(worker.id, token, outcome='completed')
                    worker.turns += 1; worker.last_used = time.monotonic()
                    repo.add_event(mission_id, 'execution.completed', {
                        'turn_id': turn_id, 'agent_id': agent_id, 'worker_id': worker.id,
                        'reused': worker.turns > 1, 'duration_ms': int((time.monotonic()-started)*1000),
                    })
                    return TurnOutcome(TurnStatus.COMPLETED, 'via MCP; persistent Antigravity', session_id=worker.session_id)
                parsed = parser.parse(json.dumps(event))
                if parsed and is_tool_return(parsed):
                    progress.record_result(('native', parsed.activity_id) if parsed.activity_id else None)
                if parsed and parsed.type in {StreamEventType.SESSION_STARTED, StreamEventType.TOOL_ATTEMPT}:
                    last_activity = time.monotonic()
                if parsed and parsed.tool_name and parsed.tool_name != 'call_mcp_tool':
                    summary = parsed.tool_summary
                    repo.add_event(mission_id, 'provider.activity', {
                        'turn_id': turn_id, 'agent_id': agent_id, 'provider_id': provider_id,
                        'activity_id': worker.id + ':' + turn_id + ':' + (parsed.activity_id or ''),
                        'tool_name': parsed.tool_name, 'phase': parsed.tool_phase, 'summary': summary,
                    })
                    ledger.checkpoint(worker.id, last_tool=parsed.tool_name, last_summary=summary, last_turn_id=turn_id)
                # Text deltas count as progress; formal replies still require MCP.
                step = event.get('step_update')
                if event.get('event') == 'step_update' and isinstance(step, dict) and step.get('text_delta'):
                    last_activity = time.monotonic()
        except (TurnStopped, WaitCancelled):
            # The user paused: stop the worker group now; a later turn starts a fresh one.
            if worker:
                worker.close('user_stopped')
            self._forget(key)
            return TurnOutcome(TurnStatus.BLOCKED, interruption.STOPPED_SUMMARY, failure_code='stopped', retryable=False)
        except Exception as exc:
            write_cli_diagnostic(
                adapter.db_path.parent / 'cli-diagnostics',
                mission_id=mission_id, turn_id=turn_id, provider_id=provider_id,
                worker_id=worker.id if worker else None,
                native_session_id=worker.session_id if worker else None,
                timeout_seconds=adapter.timeout, duration_ms=int((time.monotonic()-started)*1000),
                exit_code=worker.proc.poll() if worker and worker.proc else None,
                interrupted=True, stdout='',
                **progress.diagnostic(),
                stderr=(''.join(worker.stderr) if worker else '') + '\n' + str(exc),
            )
            if initializing:
                tools.repository.add_event(mission_id, 'provider.activity', {
                    'turn_id': turn_id, 'agent_id': agent_id, 'provider_id': provider_id,
                    'activity_id': turn_id + ':startup', 'tool_name': 'worker_startup',
                    'phase': 'failed', 'summary': 'Antigravity 启动失败',
                })
            published = bool(worker and token and _mcp_published_message(
                tools.repository, mission_id, turn_id, cursor, receipt, agent_id, provider_id))
            if worker:
                worker.close('process_interrupted')
            self._forget(key)
            if published:
                tools.repository.add_event(mission_id, 'turn.warning', {
                    'turn_id': turn_id, 'agent_id': agent_id, 'provider_id': provider_id,
                    'code': 'worker_lost_after_publish', 'summary': '回复已保存；工作进程异常退出，已撤销旧凭证。',
                })
                return TurnOutcome(TurnStatus.COMPLETED, 'via MCP; worker lost after publication')
            return TurnOutcome(TurnStatus.BLOCKED,
                               'Antigravity 执行中断，已保留消息与活动记录；请核对工作区后继续，未自动重放操作。' + ' ' + str(exc),
                               failure_code=timeout_code if isinstance(exc, TimeoutError) else None)
        finally:
            adapter.live_progress.clear(mission_id, turn_id)
            self._release(key)

    def _forget(self, key):
        with self.lock:
            self.workers.pop(key, None)


SESSION_POOL = AntigravitySessionPool()
atexit.register(SESSION_POOL.stop)


def run_persistent_turn(provider, *, database_path, artifact_root, timeout, idle_timeout,
                        mission_id, agent_id, turn_id, prompt, tools):
    adapter = MCPSubprocessAdapter(provider.command, provider.args, db_path=database_path,
                                   artifact_root=artifact_root, timeout=timeout, idle_timeout=idle_timeout)
    return SESSION_POOL.run_turn(adapter, mission_id, agent_id, turn_id, provider.id, prompt, tools)
