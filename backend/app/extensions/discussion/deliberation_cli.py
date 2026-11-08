"""Restricted CLI execution with a host-owned MCP bridge.

The provider never receives a database path or a DB-capable MCP subprocess.
The sandbox can reach its own bridge and remote model APIs, but cannot read
the repository, peer workspaces, transcripts or the local HTTP application.
"""
import json
import hashlib
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from backend.app.adapters.base import TurnOutcome
from backend.app.adapters.deliberation_errors import failure_code, private_error_tail
from backend.app.config.providers import get_cli_provider
from backend.app.domain.models import TurnStatus
from backend.app.mcp.server import _Context, _dispatch
from backend.app.adapters.turn_deadline import HUB_WORK_TOOLS, PROGRESS_CHECK_SECONDS, ToolResultDeadline
from backend.app.adapters.native_activity import NativeActivityRecorder
from backend.app.adapters.stream_parsers import StreamLineParser
from backend.app.adapters.turn_record import TurnRecord


def isolation_supported():
    return platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file()


PROXY_SOURCE = '''import json,socket,sys
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
    s.connect(sys.argv[1])
    stream=s.makefile("rwb")
    for line in sys.stdin.buffer:
        stream.write(line);stream.flush()
        try: frame=json.loads(line)
        except ValueError: frame={"id":None}
        if "id" in frame:
            reply=stream.readline()
            if not reply: break
            sys.stdout.buffer.write(reply);sys.stdout.buffer.flush()
'''


@contextmanager
def mcp_bridge(directory, tools, mission_id, agent_id, turn_id, provider_id, on_tool_return=None):
    path = directory / "mcp.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    os.chmod(path, 0o600)
    listener.listen(4)
    listener.settimeout(0.2)
    stopped = threading.Event()
    connections = []
    context = _Context(mission_id, agent_id, turn_id, provider_id, tools)

    def serve(connection):
        try:
            with connection, connection.makefile("rwb") as stream:
                while line := stream.readline(512_001):
                    if len(line) > 512_000:
                        break
                    try:
                        frame = json.loads(line)
                        reply = _dispatch(context, frame)
                    except (ValueError, TypeError):
                        reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}}
                    if reply is not None:
                        stream.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
                        stream.flush()
                        if (on_tool_return and frame.get('method') == 'tools/call'
                                and (frame.get('params') or {}).get('name') in HUB_WORK_TOOLS
                                and isinstance(reply.get('result'), dict)):
                            on_tool_return()
        except OSError:
            pass

    def accept():
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            connections.append(connection)
            threading.Thread(target=serve, args=(connection,), daemon=True).start()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    proxy = directory / "mcp_proxy.py"
    proxy.write_text(PROXY_SOURCE)
    try:
        yield {"command": str(Path(sys.executable).resolve()), "args": ["-I", "-S", str(proxy), str(path)]}
    finally:
        stopped.set()
        listener.close()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        thread.join(timeout=1)


def sandbox_profile(directory, executable, *, auth_paths=(), protected_paths=(), loopback_ports=()):
    """Protect user/runtime data while allowing the OS runtime to start.

    macOS also reads its boot-volume metadata outside /System and /usr during
    exec. Keep those OS reads available, explicitly excluding user data and
    shared temporary directories. All writes remain default-denied.
    """
    quote = lambda value: json.dumps(str(value))
    # The MCP proxy runs on this interpreter (-I -S: runtime only, no site
    # packages); the packaged app may live under /Users, /Volumes or a
    # translocated /private/var/folders path.
    readable = [str(Path(sys.base_prefix).resolve())]
    executable_path = Path(shutil.which(executable) or executable).resolve()
    # User-installed executables need their own package runtime, never all HOME.
    node = shutil.which("node")
    if node:
        node_root = Path(node).resolve().parent.parent
        if node_root.is_relative_to('/Users') and '/.nvm/versions/node/' in str(node_root):
            readable.append(str(node_root))
    exclusions = [f'(require-not (subpath {quote(directory)}))']
    exclusions.append(f'(require-not (literal {quote(executable_path)}))')
    exclusions += [f'(require-not (subpath {quote(p)}))' for p in readable]
    exclusions += [f'(require-not (literal {quote(p)}))' for p in auth_paths]
    lines = [
        "(version 1)", "(deny default)",
        "(allow process-exec process-fork sysctl-read mach-lookup mach-register)",
        "(allow file-read*)",
        '(allow file-read* file-write* (subpath "/dev"))',
        f"(allow file-read* file-write* (subpath {quote(directory)}))",
        '(allow network-outbound (require-all (remote ip "*:*") (require-not (remote ip "localhost:*"))))',
        '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',
        f'(allow network-outbound (remote unix-socket (path-literal {quote(directory / "mcp.sock")})))',
    ]
    exceptions = ' '.join(f'(require-not (remote ip "localhost:{int(port)}"))' for port in loopback_ports)
    lines.append(f'(deny network-outbound (require-all (remote ip "localhost:*") {exceptions}))')
    lines += [f'(allow network-outbound (remote ip "localhost:{int(port)}"))' for port in loopback_ports]
    if Path(executable).name == 'codex':
        # CFPreferencesAppSynchronize needs the OS preference cache, even
        # when no managed values exist. No preference writes are permitted.
        lines += ['(allow user-preference-read (preference-domain "com.openai.codex"))',
                  '(allow ipc-posix-shm-read* (ipc-posix-name-prefix "apple.cfprefs."))']
    if Path(executable).name == 'agy':
        lines.append('(allow network-bind network-inbound (local ip "localhost:*"))')
    for protected in ['/Users', '/Volumes', '/private/var/folders', '/private/tmp', '/tmp', '/private/var/root', *protected_paths]:
        lines.append(f'(deny file-read-data (require-all (subpath {quote(protected)}) {" ".join(exclusions)}))')
    return "\n".join(lines)


def prompt_arguments(basename, args, prompt):
    if basename == 'codex':
        return [*args, prompt]
    flags = {'-p', '--print'} | ({'--prompt'} if basename == 'agy' else set())
    if any(arg.startswith(tuple(flag + '=' for flag in flags)) for arg in args):
        raise ValueError('V2 supplies its own print prompt; remove configured prompt values')
    options = [arg for arg in args if arg not in flags]
    # agy consumes the next token after -p; bind explicitly so additional
    # options can never become its prompt. Claude's -p is a boolean flag.
    return [*options, '-p=' + prompt] if basename == 'agy' else [*options, '-p', prompt]


def claude_oauth_token(env):
    """Read only the selected CLI credential on the host, never its sessions.

    The child receives an access token, not access to the user's Keychain.
    Existing API-key/token configuration always takes precedence.
    """
    if any(env.get(key) for key in ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')):
        return None
    config = Path(env.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    try:
        data = json.loads((config / '.credentials.json').read_text())
    except (OSError, ValueError):
        secure_dir = env.get('CLAUDE_SECURESTORAGE_CONFIG_DIR', env.get('CLAUDE_CONFIG_DIR', ''))
        suffix = '-' + hashlib.sha256(secure_dir.encode()).hexdigest()[:8] if secure_dir else ''
        try:
            result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-a', env.get('USER', ''),
                                     '-w', '-s', 'Claude Code-credentials' + suffix],
                                    capture_output=True, text=True, timeout=5)
            data = json.loads(result.stdout) if result.returncode == 0 else {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            data = {}
    oauth = data.get('claudeAiOauth') if isinstance(data, dict) else None
    token = oauth.get('accessToken') if isinstance(oauth, dict) else None
    return token if isinstance(token, str) and token else None


def claude_provider_environment(env):
    """Retain provider connection settings without loading hooks or projects."""
    config = Path(env.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    try:
        values = json.loads((config / 'settings.json').read_text()).get('env', {})
    except (OSError, ValueError, AttributeError):
        return
    allowed = {'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL',
               'ANTHROPIC_MODEL', 'ANTHROPIC_SMALL_FAST_MODEL'}
    if isinstance(values, dict):
        for key, value in values.items():
            if (key in allowed or key.startswith('ANTHROPIC_DEFAULT_')) and isinstance(value, str):
                env.setdefault(key, value)


class DeliberationCLIAdapter:
    def __init__(self, config_dir, timeout, provider_overrides, *, model="", effort="",
                 progress_check_interval=PROGRESS_CHECK_SECONDS, work_context=None):
        self.config_dir = config_dir
        self.timeout = timeout
        self.provider_overrides = provider_overrides
        self.model = model
        self.effort = effort
        self.progress_check_interval = progress_check_interval
        self.work_context = work_context

    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        progress = ToolResultDeadline(self.timeout, check_interval=self.progress_check_interval)
        if not isolation_supported():
            return TurnOutcome(status=TurnStatus.BLOCKED, summary="isolation_not_supported")
        provider = get_cli_provider(self.config_dir, provider_id, overrides=self.provider_overrides)
        from backend.app.adapters.model_selection import with_model_and_effort
        provider = with_model_and_effort(provider, self.model, self.effort)
        if not provider.enabled:
            return TurnOutcome(status=TurnStatus.BLOCKED, summary="Provider is disabled")
        basename = Path(provider.command).name
        if basename not in {"codex", "claude", "agy"}:
            return TurnOutcome(status=TurnStatus.BLOCKED, summary="isolation_not_supported for this CLI")
        with tempfile.TemporaryDirectory(prefix="acv2-", dir="/tmp") as temp, ExitStack() as cleanup:
            directory = Path(temp).resolve()
            with mcp_bridge(directory, tools, mission_id, agent_id, turn_id, provider_id,
                            on_tool_return=lambda: progress.record_result()) as server:
                env = {k: v for k, v in os.environ.items() if not k.startswith("YIREN_")}
                env.pop("PYTHONPATH", None)
                env.pop("NODE_OPTIONS", None)
                env.update(TMPDIR=str(directory), TMP=str(directory), TEMP=str(directory),
                           CLAUDE_CODE_TMPDIR=str(directory), XDG_CACHE_HOME=str(directory / 'cache'),
                           XDG_STATE_HOME=str(directory / 'state'), XDG_DATA_HOME=str(directory / 'data'))
                auth = []
                loopback_ports = []
                args = list(provider.args)
                if basename == "claude":
                    config = directory / "mcp.json"
                    config.write_text(json.dumps({"mcpServers": {"yiren_hub": server}}))
                    settings = directory / 'claude-settings.json'
                    settings.write_text(json.dumps({'disableAllHooks': True}))
                    args = ["--mcp-config", str(config), "--strict-mcp-config", "--tools", ("default" if self.work_context else ""),
                            '--setting-sources', '', '--settings', str(settings), *args]
                    if '--no-session-persistence' not in args:
                        args.append('--no-session-persistence')
                    claude_provider_environment(env)
                    endpoint = urlsplit(env.get('ANTHROPIC_BASE_URL', ''))
                    if endpoint.hostname in {'localhost', '127.0.0.1', '::1'}:
                        port = endpoint.port or (443 if endpoint.scheme == 'https' else 80)
                        if port in {8000, 5173}:
                            raise ValueError('Provider endpoint cannot be the local Unity application')
                        loopback_ports.append(port)
                    token = claude_oauth_token(env)
                    if token:
                        env['CLAUDE_CODE_OAUTH_TOKEN'] = token
                    claude_home = directory / 'claude'
                    claude_home.mkdir()
                    env['CLAUDE_CONFIG_DIR'] = str(claude_home)
                    env['CLAUDE_SECURESTORAGE_CONFIG_DIR'] = str(claude_home)
                elif basename == "codex":
                    env["CODEX_HOME"] = str(directory)
                    credentials = Path.home() / ".codex/auth.json"
                    if credentials.is_file():
                        shutil.copy2(credentials, directory / "auth.json")
                    config = directory / "config.toml"
                    config.write_text(
                        f'[projects.{json.dumps(str(Path(self.work_context["workspace_root"]) if self.work_context else directory))}]\ntrust_level="trusted"\n'
                        f'[mcp_servers.yiren_hub]\ncommand={json.dumps(server["command"])}\n'
                        f'args={json.dumps(server["args"])}\n'
                    )
                    if "--skip-git-repo-check" not in args:
                        args.append("--skip-git-repo-check")
                else:
                    args = [arg for i, arg in enumerate(args)
                            if arg != '--print-timeout' and not arg.startswith('--print-timeout=')
                            and not (i > 0 and args[i - 1] == '--print-timeout')]
                    args = ['--print-timeout', '0', *args]
                    agy_home = directory / 'home'
                    agy_config = agy_home / '.gemini/config'
                    agy_config.mkdir(parents=True)
                    (agy_config / 'mcp_config.json').write_text(json.dumps({'mcpServers': {'yiren_hub': server}}))
                    for name in ('installation_id', 'jetski_state.pbtxt'):
                        identity = Path.home() / '.gemini/antigravity-cli' / name
                        if identity.is_file():
                            target = agy_home / '.gemini/antigravity-cli' / name
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(identity, target)
                    try:
                        user_settings = json.loads((Path.home() / '.gemini/antigravity-cli/settings.json').read_text())
                        routing = {key: user_settings[key] for key in ('model', 'useG1Credits')
                                   if key in user_settings and isinstance(user_settings[key], (str, bool))}
                        target = agy_home / '.gemini/antigravity-cli/settings.json'
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(json.dumps(routing))
                    except (OSError, ValueError, TypeError):
                        pass
                    env['HOME'] = str(agy_home)
                    # Security.framework derives the default search path from
                    # HOME. Keep its existing unlocked login Keychain visible
                    # through a read-only alias; session/config data stays local.
                    keychain = Path.home() / 'Library/Keychains/login.keychain-db'
                    if keychain.is_file():
                        alias = agy_home / 'Library/Keychains/login.keychain-db'
                        alias.parent.mkdir(parents=True)
                        alias.symlink_to(keychain)
                    args += ["--new-project", "--disable-slash-commands", "--log-file", str(directory / "agy.log")]
                    auth += [Path.home() / 'Library/Preferences/com.apple.security.plist',
                             Path.home() / 'Library/Keychains/login.keychain-db']
                profile = directory / "isolation.sb"
                if any(a in {'--resume', '--continue', '--conversation', 'resume'} for a in args) or (
                    basename in {'claude', 'agy'} and any(a in {'-c', '-r'} for a in args)
                ):
                    return TurnOutcome(status=TurnStatus.BLOCKED, summary='V2 cannot resume an existing CLI conversation')
                if self.work_context:
                    from backend.app.extensions.tasks.runtime import profile as work_profile
                    work = self.work_context
                    readonly = work['task']['attempts'][-1]['stage'] in {'review', 'reconcile', 'handoff'}
                    db_path = tools.repository.database_path.resolve()
                    profile.write_text(work_profile(directory, provider.command, Path(work['workspace_root']),
                        delivery=work['authorization']['delivery'], readonly=readonly,
                        protected_paths=[db_path, str(db_path) + '-wal', str(db_path) + '-shm', tools.artifact_root.resolve()],
                        auth_paths=auth, loopback_ports=loopback_ports))
                else:
                    profile.write_text(sandbox_profile(directory, provider.command, auth_paths=auth, protected_paths=[
                        tools.repository.database_path.resolve().parent, tools.artifact_root.resolve(), Path.cwd().resolve()],
                        loopback_ports=loopback_ports))
                command = ["/usr/bin/sandbox-exec", "-f", str(profile), provider.command,
                           *prompt_arguments(basename, args, prompt)]
                cwd = Path(self.work_context['workspace_root']) if self.work_context else directory
                mission = tools.repository.get_mission(mission_id)
                record = TurnRecord(tools.repository.database_path, mission_id=mission_id, turn_id=turn_id,
                                    agent_id=agent_id, provider_id=provider_id,
                                    route='work' if self.work_context else 'discussion',
                                    conversation_id=mission.conversation_id if mission else None)
                cleanup.callback(record.finish, error='execution ended unexpectedly')
                record.launch(command=provider.command, argv=command, prompt=prompt, cwd=cwd, launch_env=env,
                              isolation='macos-sandbox+host-mcp-bridge', sandbox_profile=profile.read_text(),
                              model=self.model)
                try:
                    proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                except OSError as exc:
                    record.finish(error=str(exc))
                    raise
                # Discussion results stay private until their batch is released,
                # so only work turns publish native activity to the trace; both
                # keep the complete stream in the private turn record.
                native = NativeActivityRecorder(
                    (lambda kind, payload: tools.repository.add_event(mission_id, kind, payload))
                    if self.work_context else (lambda kind, payload: None),
                    turn_id=turn_id, agent_id=agent_id, provider_id=provider_id, on_session=record.session)
                live_parser = StreamLineParser(provider_id)
                captured = {'stdout': [], 'stderr': []}

                def drain(stream, name):
                    try:
                        for raw in iter(stream.readline, b''):
                            captured[name].append(raw)
                            line = raw.decode(errors='replace').rstrip('\n')
                            record.stream(name, line)
                            if name == 'stdout':
                                event = live_parser.parse(line)
                                if event is not None:
                                    native.handle(event)
                    except (OSError, ValueError):
                        pass

                readers = [threading.Thread(target=drain, args=(proc.stdout, 'stdout'), daemon=True),
                           threading.Thread(target=drain, args=(proc.stderr, 'stderr'), daemon=True)]
                for reader in readers:
                    reader.start()
                if self.work_context:
                    from backend.app.adapters.subprocess_cli import kill_process_tree
                    cleanup.callback(kill_process_tree, proc)
                    read_fd, write_fd = os.pipe()
                    try:
                        subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[2] / 'adapters' / 'process_watchdog.py'),
                                          str(read_fd), str(proc.pid)], pass_fds=(read_fd,), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                    finally:
                        os.close(read_fd)
                    cleanup.callback(os.close, write_fd)
                    from backend.app.extensions.tasks.adaptive import AdaptiveWork
                    AdaptiveWork(tools.repository, self.work_context['workspace_root']).record_process(mission_id, proc.pid)
                progress = ToolResultDeadline(self.timeout, check_interval=self.progress_check_interval)
                terminated = None
                while True:
                    try:
                        proc.wait(timeout=min(.25, max(.01, progress.remaining())))
                        break
                    except subprocess.TimeoutExpired:
                        if self.work_context:
                            from backend.app.extensions.tasks.service import SharedWork
                            current_work = SharedWork(tools.repository).execution_context(mission_id)
                            cancelled = not current_work or current_work['task']['status'] == 'cancelled'
                        else:
                            from backend.app.extensions.discussion.deliberation import Deliberation
                            policy = Deliberation(tools.repository)
                            state = policy.state(tools.repository.get_mission(mission_id))
                            cancelled = state['pending_control'] == 'stop' or state['phase'] == 'ended'
                        extension = progress.check()
                        if (not self.work_context and extension and not cancelled and progress.remaining() > 0
                                and state['leases'][turn_id]['status'] == 'running'):
                            renewed = policy.renew_turn_lease(mission_id, agent_id, turn_id, provider_id,
                                                             tools.lease_token, progress.remaining() + 30)
                            if renewed:
                                tools.repository.add_event(mission_id, 'turn.deadline_extended', {
                                    'turn_id': turn_id, 'agent_id': agent_id, 'provider_id': provider_id,
                                    **extension,
                                })
                        if cancelled or progress.remaining() <= 0:
                            from backend.app.adapters.subprocess_cli import kill_process_tree
                            kill_process_tree(proc)
                            proc.wait(timeout=5)
                            terminated = 'Discussion stopped' if cancelled else 'Isolated CLI timed out'
                            break
                for reader in readers:
                    reader.join(timeout=5)
                stdout, stderr = b''.join(captured['stdout']), b''.join(captured['stderr'])
                from backend.app.adapters.mcp_subprocess import _emit_turn_usage
                parser = StreamLineParser(provider_id)
                usage = None
                stream_errors = []
                for line in stdout.decode(errors='replace').splitlines():
                    event = parser.parse(line)
                    if event and event.usage:
                        usage = event.usage
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(frame, dict) and frame.get('type') == 'result' and frame.get('is_error'):
                        errors = frame.get('errors') or [frame.get('result')]
                        if isinstance(errors, str):
                            errors = [errors]
                        stream_errors.extend(str(item) for item in errors if item)
                _emit_turn_usage(tools.repository, mission_id, agent_id, provider_id, turn_id, usage)
                if self.work_context:
                    from backend.app.extensions.tasks.service import SharedWork
                    current_work = SharedWork(tools.repository).execution_context(mission_id)
                    accepted = bool(current_work and current_work['task']['attempts'][-1].get('structured'))
                    published = any(m.turn_id == turn_id and m.agent_id == agent_id
                                    for m in tools.repository.list_messages(mission_id))
                    if not published:
                        accepted = False
                else:
                    from backend.app.extensions.discussion.deliberation import Deliberation
                    accepted = Deliberation(tools.repository).state(tools.repository.get_mission(mission_id))['leases'][turn_id]['status'] == 'submitted'
                diagnostic = terminated or '\n'.join([stderr.decode(errors='replace'), *stream_errors])
                if not accepted and basename == 'agy':
                    log = directory / 'agy.log'
                    if log.is_file():
                        diagnostic += '\n' + log.read_text(errors='replace')[-8000:]
                code = None if accepted else failure_code(terminated or diagnostic, exit_code=proc.returncode)
                record.finish(exit_code=proc.returncode, terminated=terminated, accepted=accepted,
                              failure_code=code, session_id=native.session_id)
                tools.repository.add_event(mission_id, "adaptive.provider_execution" if self.work_context else "discussion.provider_execution", {
                    **progress.diagnostic(),
                    "turn_id": turn_id, "provider_id": provider_id, "exit_code": proc.returncode,
                    "isolation": "macos-sandbox+host-mcp-bridge", "usage_available": usage is not None,
                    "failure_code": code,
                })
                # Success requires an accepted structured submission, checked by
                # the runner. stdout never becomes a chat message or a result.
                error = 'Structured result accepted' if accepted else (private_error_tail(diagnostic) or 'No structured result submitted')
                return TurnOutcome(status=TurnStatus.COMPLETED if not proc.returncode and not terminated and (accepted or not self.work_context) else TurnStatus.BLOCKED,
                                   summary=error, failure_code=code, exit_code=proc.returncode)
