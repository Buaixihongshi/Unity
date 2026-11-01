"""Adapter that runs a CLI provider as an MCP client of the Unity MCP server.

Per-provider config rendering:
- codex: materialises a TOML at ``<CODEX_HOME>/config.toml`` and sets
  ``CODEX_HOME`` to an ephemeral tmpdir that is seeded with codex auth files.
- claude: writes an MCP JSON config file and passes ``--mcp-config <path>
  --strict-mcp-config`` flags so the server is injected without touching
  ``~/.claude``.
- gemini: writes a ``settings.json`` under ``<HOME>/.gemini/`` in an
  ephemeral tmpdir and sets ``HOME`` so that Gemini reads our MCP
  server without touching ``~/.gemini``.
- agy: Antigravity CLI 1.0.2 reads MCP only from the global
  ``~/.gemini/config/mcp_config.json``; we guard-mutate-restore it for the
  subprocess lifetime (see ``antigravity_config.guarded_global_mcp_config``)
  and run from a throwaway workspace cwd.

After the CLI exits we inspect the repository event log: if the agent message
was published via MCP we mark the turn ``COMPLETED`` with summary ``"via MCP"``;
otherwise the turn is marked ``BLOCKED`` with summary
``"agent did not call publish_message via MCP"`` (ADR Decision 5 — stdout is
never synthesised into an agent message for MCP-enabled providers).
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, List, Optional
from uuid import uuid4

from backend.app import extension_points
from backend.app import host_rules
from backend.app.adapters.antigravity_config import (
    guarded_global_mcp_config,
    read_agy_log_hint,
)
from backend.app.adapters.base import CLIAdapter, TurnOutcome
from backend.app.adapters.cli_diagnostics import write_cli_diagnostic
from backend.app.adapters.native_activity import NativeActivityRecorder, is_mcp_tool
from backend.app.adapters.stream_events import StreamEventType, UsageRecord
from backend.app.adapters.turn_deadline import (
    PROGRESS_CHECK_SECONDS, ToolResultDeadline, is_hub_work_result, is_tool_return,
)
from backend.app.adapters.failure_text import diagnostic_detail
from backend.app.adapters.stream_parsers import StreamLineParser
from backend.app.adapters.turn_record import TurnRecord
from backend.app.adapters.subprocess_cli import (
    _IS_WINDOWS,
    build_failure_summary,
    kill_process_tree,
)
from backend.app.adapters.subprocess_cli import _parse_result_envelope
from backend.app.domain.models import TurnStatus, is_provider_verification_mission
from backend.app.orchestration import interruption
from backend.app.mcp.tools import MCPToolError, MCPTools
from backend.app.orchestration.live_progress import (
    LiveProgressRegistry,
    get_live_progress_registry,
)
from backend.app.storage.repository import Repository


MCP_SERVER_NAME = "yiren_hub"

_MEANINGFUL_TURN_EVENT_TYPES = {
    "agent.thought",
    "message",
    "mcp.receipt",
    "tool.call",
    "tool.error",
    "tool.result",
}
_MEANINGFUL_STREAM_EVENT_TYPES = {
    StreamEventType.SESSION_STARTED,
    StreamEventType.PARTIAL_TEXT,
    StreamEventType.TOOL_ATTEMPT,
}


class ProcessWaitReason(str, Enum):
    EXITED = "exited"
    HARD_TIMEOUT = "hard_timeout"
    IDLE_TIMEOUT = "idle_timeout"
    STOPPED = "stopped"


class TurnActivityLease:
    """Thread-safe wall-clock timestamp for meaningful provider activity."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_activity_at = time.time()

    def touch(self) -> None:
        with self._lock:
            self._last_activity_at = time.time()

    def last_activity_at(self) -> float:
        with self._lock:
            return self._last_activity_at


class MCPSubprocessAdapter(CLIAdapter):
    def __init__(
        self,
        command: str,
        args: Optional[List[str]] = None,
        *,
        db_path: Path,
        artifact_root: Path,
        memory_root: Optional[Path] = None,
        mcp_python: Optional[str] = None,
        mcp_module: str = "backend.app.mcp.server",
        timeout: int = 300,
        idle_timeout: int = 0,
        extra_env: Optional[dict] = None,
        on_config_written: Optional[Callable[[Path], None]] = None,
        live_progress: Optional[LiveProgressRegistry] = None,
        gemini_home_override: Optional[Path] = None,
        gemini_resume_session_id: Optional[str] = None,
        progress_check_interval: float = PROGRESS_CHECK_SECONDS,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.db_path = Path(db_path).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self.memory_root = Path(memory_root).resolve() if memory_root else None
        self.mcp_python = mcp_python or sys.executable
        self.mcp_module = mcp_module
        self.timeout = timeout
        self.idle_timeout = max(0, idle_timeout)
        self.extra_env = dict(extra_env or {})
        self.on_config_written = on_config_written
        self.live_progress = live_progress or get_live_progress_registry()
        self.gemini_home_override = Path(gemini_home_override) if gemini_home_override else None
        self.gemini_resume_session_id = gemini_resume_session_id
        self.progress_check_interval = progress_check_interval

    def run_turn(
        self,
        mission_id: str,
        agent_id: str,
        turn_id: str,
        provider_id: str,
        prompt: str,
        tools: MCPTools,
    ) -> TurnOutcome:
        outcome = self._run_attempt(
            mission_id, agent_id, turn_id, provider_id, prompt, tools,
            resume_session_id=self.gemini_resume_session_id,
        )
        # Plan A: if Gemini rejected our --resume target (its session file was
        # not flushed cleanly last turn, e.g. final result.status=error), retry
        # once in the same run_turn without --resume so the agent recovers via
        # cold start instead of propagating a hard BLOCKED to the runner.
        if (
            self.gemini_resume_session_id is not None
            and outcome.status == TurnStatus.BLOCKED
            and "Invalid session identifier" in (outcome.summary or "")
        ):
            outcome = self._run_attempt(
                mission_id, agent_id, turn_id, provider_id, prompt, tools,
                resume_session_id=None,
            )
        return outcome

    def _run_attempt(
        self,
        mission_id: str,
        agent_id: str,
        turn_id: str,
        provider_id: str,
        prompt: str,
        tools: MCPTools,
        *,
        resume_session_id: Optional[str],
    ) -> TurnOutcome:
        repository = tools.repository
        cursor = _max_event_id(repository, mission_id)
        activity_cursor = [cursor]
        activity = TurnActivityLease()

        def _record_extension(extension) -> None:
            repository.add_event(mission_id, "turn.deadline_extended", {
                "turn_id": turn_id, "agent_id": agent_id, "provider_id": provider_id,
                **extension,
            })

        def _refresh_mcp_activity() -> None:
            events = repository.list_events_after(mission_id, activity_cursor[0])
            for event in events:
                activity_cursor[0] = max(activity_cursor[0], int(event.id))
                if is_hub_work_result(event, turn_id):
                    progress.record_result(("hub", event.id))
                if (
                    event.event_type in _MEANINGFUL_TURN_EVENT_TYPES
                    and event.payload.get("turn_id") == turn_id
                ):
                    activity.touch()

        with tempfile.TemporaryDirectory(prefix="yiren-mcp-") as tmpdir, \
                contextlib.ExitStack() as guard_stack:
            tmp = Path(tmpdir)
            mcp_env = self._mcp_env(
                mission_id,
                agent_id,
                turn_id,
                provider_id,
                valid_agent_ids=tools.valid_agent_ids,
                max_a2a_invocations=tools.max_a2a_invocations,
                scheduled_agent_ids=tools.scheduled_targets(),
                tasks_enabled=getattr(tools, "tasks_enabled", True),
            )
            receipt_id = "mcp_{suffix}".format(suffix=uuid4().hex)
            mcp_env["YIREN_MCP_RECEIPT_ID"] = receipt_id
            mcp_prompt = _augment_prompt_for_mcp(prompt)
            launch_env = os.environ.copy()
            launch_env.update(self.extra_env)
            # Runtime routing data belongs to the MCP server process only. It
            # must not leak into the provider CLI where it could be used to
            # bypass stdio MCP and write directly to Unity persistence.
            for key in list(launch_env):
                if key.startswith("YIREN_MCP_"):
                    launch_env.pop(key, None)

            cli_basename = os.path.basename(self.command)
            mission = repository.get_mission(mission_id)
            is_verification = bool(
                mission and is_provider_verification_mission(mission)
            )

            cli_cwd: Optional[Path] = None
            if is_verification:
                cli_cwd = tmp / "verification-workspace"
                cli_cwd.mkdir(parents=True, exist_ok=True)
            agy_run_start: Optional[float] = None  # set in agy branch below
            if cli_basename == "claude":
                cmd, config_path = self._setup_claude(tmp, mcp_env, mcp_prompt)
            elif cli_basename == "gemini":
                gemini_home = self.gemini_home_override or tmp
                if self.gemini_home_override is not None:
                    self.gemini_home_override.mkdir(parents=True, exist_ok=True)
                cmd, config_path = self._setup_gemini(
                    gemini_home, mcp_env, mcp_prompt, launch_env,
                    resume_session_id=resume_session_id,
                )
            elif cli_basename == "agy":
                cmd, config_path, cli_cwd, agy_server_spec = self._setup_antigravity(
                    tmp,
                    mcp_env,
                    mcp_prompt,
                    include_repo_context=not is_verification,
                )
                # Inject yiren_hub into the global ~/.gemini/config/mcp_config.json
                # for the agy subprocess lifetime; released (restored) when the
                # ExitStack (guard_stack) unwinds at the end of this `with`, after
                # the subprocess has fully finished.
                guard_stack.enter_context(guarded_global_mcp_config(agy_server_spec))
                # Captured so read_agy_log_hint can filter to logs written during
                # this run only. agy 1.0.3 swallows fatal conditions (no auth /
                # Google quota 429) into silent exit 0, so we mine its own log
                # for context to enrich the BLOCKED summary below.
                agy_run_start = time.time()
            else:
                # Default: codex-style TOML config.
                config_path = tmp / "config.toml"
                config_path.write_text(
                    self._render_codex_config(
                        mcp_env,
                        project_path=cli_cwd or Path.cwd(),
                    ),
                    encoding="utf-8",
                )
                _seed_codex_auth(tmp)
                launch_env["CODEX_HOME"] = str(tmp)
                cmd = [self.command, *self.args, mcp_prompt]

            if self.on_config_written is not None:
                self.on_config_written(config_path)

            record = TurnRecord(
                self.db_path, mission_id=mission_id, turn_id=turn_id, agent_id=agent_id,
                provider_id=provider_id, route="chat",
                conversation_id=mission.conversation_id if mission else None,
            )
            record.launch(command=self.command, argv=cmd, prompt=mcp_prompt, cwd=cli_cwd,
                          launch_env=launch_env)
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    start_new_session=not _IS_WINDOWS,
                    env=launch_env,
                    cwd=str(cli_cwd) if cli_cwd is not None else None,
                )
            except OSError as exc:
                record.finish(error=str(exc))
                return TurnOutcome(
                    status=TurnStatus.BLOCKED,
                    summary=build_failure_summary(provider_id, os_error=str(exc)),
                )

            self.live_progress.mark_active(mission_id, turn_id)

            def _augment_agy_summary(summary: str) -> str:
                """Mine the most recent agy log for a quota/auth hint and append
                it to the BLOCKED summary. No-op for non-agy providers and when
                the log yields nothing recognisable."""
                if cli_basename != "agy" or agy_run_start is None:
                    return summary
                hint = read_agy_log_hint(agy_run_start)
                return f"{summary} | {hint}" if hint else summary

            process_started = time.monotonic()
            # Initialization/configuration time is not part of the CLI execution budget.
            progress = ToolResultDeadline(self.timeout, check_interval=self.progress_check_interval)
            wait_reason = ProcessWaitReason.EXITED
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            last_usage: list[Optional[UsageRecord]] = [None]
            captured_session_id: list[Optional[str]] = [None]
            stream_ended_with_error: list[bool] = [False]
            stream_parser = StreamLineParser(provider_id)
            native = NativeActivityRecorder(
                lambda kind, payload: repository.add_event(mission_id, kind, payload),
                turn_id=turn_id, agent_id=agent_id, provider_id=provider_id,
                on_session=record.session,
            )

            def _drain_stdout() -> None:
                if proc.stdout is None:
                    return
                try:
                    for raw in iter(proc.stdout.readline, ""):
                        if not raw or not isinstance(raw, str):
                            break
                        line = raw.rstrip("\n")
                        stdout_lines.append(line)
                        record.stream("stdout", line)
                        ev = stream_parser.parse(line)
                        if ev is None:
                            continue
                        if ev.type in _MEANINGFUL_STREAM_EVENT_TYPES:
                            activity.touch()
                        if is_tool_return(ev):
                            progress.record_result(("native", ev.activity_id) if ev.activity_id else None)
                        if ev.type is StreamEventType.PARTIAL_TEXT and ev.text:
                            if not self.live_progress.get_partial_text(mission_id, turn_id):
                                repository.add_event(mission_id, "provider.output_started", {"turn_id": turn_id, "provider_id": provider_id})
                            self.live_progress.append_text(mission_id, turn_id, ev.text)
                        if ev.type is StreamEventType.SESSION_STARTED and ev.session_id:
                            captured_session_id[0] = ev.session_id
                        native.handle(ev)
                        if ev.type is StreamEventType.ERROR:
                            stream_ended_with_error[0] = True
                        if ev.type is StreamEventType.FINISHED:
                            stream_ended_with_error[0] = False
                        if ev.usage is not None:
                            last_usage[0] = ev.usage
                except Exception:
                    pass

            def _drain_stderr() -> None:
                if proc.stderr is None:
                    return
                try:
                    for raw in iter(proc.stderr.readline, ""):
                        if not raw or not isinstance(raw, str):
                            break
                        stderr_lines.append(raw.rstrip("\n"))
                        record.stream("stderr", raw.rstrip("\n"))
                except Exception:
                    pass

            t_out = threading.Thread(target=_drain_stdout, daemon=True)
            t_err = threading.Thread(target=_drain_stderr, daemon=True)
            t_out.start()
            t_err.start()

            try:
                try:
                    wait_reason = _wait_for_process_with_activity(
                        proc,
                        hard_timeout=self.timeout,
                        idle_timeout=self.idle_timeout,
                        last_activity_at=activity.last_activity_at,
                        refresh_activity=_refresh_mcp_activity,
                        progress_deadline=progress,
                        on_extension=_record_extension,
                        stopped=lambda: interruption.pause_requested(mission_id),
                    )
                except subprocess.TimeoutExpired:
                    wait_reason = ProcessWaitReason.HARD_TIMEOUT
                if wait_reason is not ProcessWaitReason.EXITED:
                    kill_process_tree(proc)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    t_out.join(timeout=2)
                    t_err.join(timeout=2)
                    stdout = "\n".join(stdout_lines)
                    stderr = "\n".join(stderr_lines)
                    _write_debug_files(turn_id, provider_id, stdout, stderr, config_path)
                    # Plan B: a final result.status=error means the on-disk session
                    # was likely not persisted — do not surface that sid upstream.
                    if stream_ended_with_error[0]:
                        captured_session_id[0] = None
                    if wait_reason is ProcessWaitReason.STOPPED:
                        return TurnOutcome(
                            status=TurnStatus.BLOCKED,
                            summary=interruption.STOPPED_SUMMARY,
                            session_id=captured_session_id[0],
                            failure_code="stopped",
                            retryable=False,
                        )
                    if wait_reason is ProcessWaitReason.IDLE_TIMEOUT:
                        detail = "A2A idle deadline exceeded without meaningful activity"
                        tail = diagnostic_detail(stderr, stdout, limit=140)
                        if tail:
                            detail = f"{detail}\n{tail}"
                        summary = (
                            f"[{provider_id}] idle timeout after "
                            f"{self.idle_timeout}s {detail}"
                        )
                    else:
                        summary = build_failure_summary(
                            provider_id,
                            timeout=round(progress.deadline - progress.started_at),
                            stderr_snippet=stderr.strip(),
                            stdout_snippet=stdout.strip(),
                        )
                    summary = _augment_agy_summary(summary)
                    if (
                        wait_reason is ProcessWaitReason.HARD_TIMEOUT
                        and _mcp_published_message(
                            repository, mission_id, turn_id, cursor, receipt_id,
                            agent_id, provider_id,
                        )
                        and not any(
                            event.event_type == "tool.error"
                            and event.payload.get("turn_id") == turn_id
                            for event in repository.list_events_after(mission_id, cursor)
                        )
                    ):
                        warning = "发言已保存，CLI 退出超时；继续处理已登记的接力。 " + summary
                        repository.add_event(mission_id, "turn.warning", {
                            "turn_id": turn_id, "agent_id": agent_id,
                            "provider_id": provider_id,
                            "code": "timeout_after_publish", "summary": warning,
                        })
                        return TurnOutcome(
                            status=TurnStatus.COMPLETED, summary="via MCP; " + warning,
                            session_id=captured_session_id[0],
                        )
                    return TurnOutcome(
                        status=TurnStatus.BLOCKED,
                        summary=summary,
                        session_id=captured_session_id[0],
                        failure_code=wait_reason.value,
                    )

                t_out.join(timeout=2)
                t_err.join(timeout=2)
                stdout = "\n".join(stdout_lines).strip()
                stderr = "\n".join(stderr_lines).strip()

                _write_debug_files(turn_id, provider_id, stdout, stderr, config_path)

                if stream_ended_with_error[0]:
                    captured_session_id[0] = None

                if proc.returncode != 0:
                    return TurnOutcome(
                        status=TurnStatus.BLOCKED,
                        summary=_augment_agy_summary(build_failure_summary(
                            provider_id,
                            exit_code=proc.returncode,
                            stderr_snippet=stderr,
                            stdout_snippet=stdout,
                        )),
                        session_id=captured_session_id[0],
                    )

                if _mcp_published_message(
                    repository,
                    mission_id,
                    turn_id,
                    cursor,
                    receipt_id,
                    agent_id,
                    provider_id,
                ):
                    return TurnOutcome(
                        status=TurnStatus.COMPLETED,
                        summary="via MCP",
                        session_id=captured_session_id[0],
                    )

                # agy versions report their own timeout on either stream.
                if cli_basename == "agy" and _agy_self_timed_out(stdout, stderr):
                    return TurnOutcome(
                        status=TurnStatus.BLOCKED,
                        summary=_augment_agy_summary(
                            "[antigravity] provider internal timeout before MCP publish_message"
                        ),
                        session_id=captured_session_id[0],
                        failure_code="provider_timeout",
                    )

                # ADR Decision 5: MCP-enabled providers must call publish_message via MCP.
                # stdout is never synthesised into an agent message when mcp_enabled=true.
                return TurnOutcome(
                    status=TurnStatus.BLOCKED,
                    summary=_augment_agy_summary("agent did not call publish_message via MCP"),
                    session_id=captured_session_id[0],
                )
            finally:
                # A charged model turn can fail its MCP publish, exit nonzero,
                # or time out after usage was emitted. Preserve that accounting
                # exactly once instead of only charging successful turns.
                _emit_turn_usage(repository, mission_id, agent_id, provider_id, turn_id, last_usage[0])
                record.finish(exit_code=proc.returncode, wait_reason=wait_reason.value,
                              session_id=native.session_id)
                write_cli_diagnostic(
                    self.db_path.parent / "cli-diagnostics",
                    mission_id=mission_id, turn_id=turn_id, provider_id=provider_id,
                    timeout_seconds=self.timeout,
                    duration_ms=int((time.monotonic() - process_started) * 1000),
                    exit_code=proc.returncode,
                    hard_timeout=wait_reason is ProcessWaitReason.HARD_TIMEOUT,
                    idle_timeout=wait_reason is ProcessWaitReason.IDLE_TIMEOUT,
                    provider_timeout=cli_basename == "agy" and _agy_self_timed_out(
                        "\n".join(stdout_lines), "\n".join(stderr_lines)
                    ),
                    stdout="\n".join(stdout_lines), stderr="\n".join(stderr_lines),
                    **progress.diagnostic(),
                )
                self.live_progress.clear(mission_id, turn_id)

    def _setup_claude(
        self, tmp: Path, mcp_env: dict, mcp_prompt: str
    ) -> tuple:
        """Write an MCP JSON config file and return (cmd, config_path).

        Uses ``--mcp-config <path> --strict-mcp-config`` so that no MCP
        servers from the user's global ~/.claude are loaded — ephemeral and
        non-polluting.  Auth is handled by claude itself (keychain /
        ANTHROPIC_API_KEY), so we do not need to seed credentials.
        """
        config_path = tmp / "mcp.json"
        config_path.write_text(
            self._render_claude_mcp_config(mcp_env), encoding="utf-8"
        )
        cmd = [
            self.command,
            "--mcp-config", str(config_path),
            "--strict-mcp-config",
            *self.args,
            mcp_prompt,
        ]
        return cmd, config_path

    def _setup_gemini(
        self, gemini_home: Path, mcp_env: dict, mcp_prompt: str, launch_env: dict,
        *, resume_session_id: Optional[str] = None,
    ) -> tuple:
        """Inject MCP into Gemini via ``GEMINI_CLI_HOME``.

        Gemini CLI reads ``$GEMINI_CLI_HOME/.gemini/settings.json`` when the
        env var is set (otherwise ``~/.gemini``); pointing it at a tempdir
        injects our MCP server without touching the user's real ``~/.gemini``.
        Vertex-AI / API-key auth uses ambient credentials (gcloud config,
        env vars) so redirecting only the Gemini home does not break login.
        OAuth-personal credential files are copied in for completeness.

        Adds ``--skip-trust`` because the workspace is the runtime cwd, not
        a directory the user has interactively trusted. When
        ``resume_session_id`` is supplied, ``--resume <uuid>`` is injected so
        gemini reattaches to the previously-written session file under
        ``$GEMINI_CLI_HOME/.gemini/tmp/<bucket>/chats/``.
        """
        gemini_dir = gemini_home / ".gemini"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        settings_path = gemini_dir / "settings.json"
        _seed_gemini_auth(gemini_home)
        settings_path.write_text(
            self._render_gemini_settings(mcp_env, settings_path=settings_path), encoding="utf-8"
        )
        launch_env["GEMINI_CLI_HOME"] = str(gemini_home)
        resume_args: list = ["--resume", resume_session_id] if resume_session_id else []
        cmd = [
            self.command,
            "--skip-trust",
            *resume_args,
            *self.args,
            mcp_prompt,
        ]
        return cmd, settings_path

    def _setup_antigravity(
        self,
        tmp: Path,
        mcp_env: dict,
        mcp_prompt: str,
        *,
        include_repo_context: bool = True,
    ) -> tuple:
        """Prepare the agy command + a server_spec for the global-config guard.

        Antigravity CLI 1.0.2 reads MCP servers ONLY from the global
        ``~/.gemini/config/mcp_config.json`` (it ignores GEMINI_CLI_HOME /
        XDG_CONFIG_HOME / --app_data_dir, has no --mcp-config flag, and
        ``.agents/`` is agent metadata only). So we do NOT write any config
        here — the caller injects ``server_spec`` into the global file via
        ``guarded_global_mcp_config`` for the agy subprocess lifetime. We run
        from a throwaway workspace so agy's ``.antigravitycli/`` project
        registration lands in tmp, not the repo. ``--add-dir <repo>`` gives agy
        the repo as read-only context. The host owns the renewable deadline;
        agy's fixed ``--print-timeout`` is disabled.

        Returns ``(cmd, debug_config_path, workspace_cwd, server_spec)``.
        """
        workspace = tmp / "antigravity-workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        server_spec = self._antigravity_server_spec(mcp_env)

        # Observability-only copy of what we inject globally (never read by agy).
        debug_config_path = workspace / "yiren_mcp_config.debug.json"
        debug_config_path.write_text(
            json.dumps(
                {"mcpServers": {MCP_SERVER_NAME: server_spec}},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        cmd_args = list(self.args)
        # Explicit text/json output would hide native tool progress. This
        # transport consumes newline-delimited activity events from stdout.
        cmd_args = [arg for i, arg in enumerate(cmd_args)
                    if arg != "--output-format"
                    and not arg.startswith("--output-format=")
                    and not (i > 0 and cmd_args[i - 1] == "--output-format")]
        cmd_args[0:0] = ["--output-format", "stream-json"]
        if "--dangerously-skip-permissions" not in cmd_args:
            cmd_args.insert(0, "--dangerously-skip-permissions")
        if not any(arg in ("-p", "--print", "--prompt") for arg in cmd_args):
            cmd_args.append("-p")
        if (
            include_repo_context
            and not any(a == "--add-dir" or a.startswith("--add-dir=") for a in cmd_args)
        ):
            cmd_args[0:0] = ["--add-dir", os.getcwd()]
        # The host owns the renewable deadline; a fixed native timer would kill
        # valid work after the host has already granted an extension.
        cmd_args = [arg for i, arg in enumerate(cmd_args)
                    if arg != "--print-timeout" and not arg.startswith("--print-timeout=")
                    and not (i > 0 and cmd_args[i - 1] == "--print-timeout")]
        cmd_args[0:0] = ["--print-timeout", "0"]

        cmd = [self.command, *cmd_args, mcp_prompt]
        return cmd, debug_config_path, workspace, server_spec

    def _mcp_env(
        self,
        mission_id: str,
        agent_id: str,
        turn_id: str,
        provider_id: str,
        valid_agent_ids: "Optional[set]" = None,
        max_a2a_invocations: int = 0,
        scheduled_agent_ids: "Optional[list]" = None,
        tasks_enabled: bool = True,
    ) -> dict:
        env: dict = {
            "YIREN_MCP_DB_PATH": str(self.db_path),
            "YIREN_MCP_ARTIFACT_ROOT": str(self.artifact_root),
            **({"YIREN_MCP_MEMORY_ROOT": str(self.memory_root)} if self.memory_root else {}),
            "YIREN_MCP_WORKSPACE_ROOT": str(Path.cwd().resolve()),
            "YIREN_MCP_MISSION_ID": mission_id,
            "YIREN_MCP_AGENT_ID": agent_id,
            "YIREN_MCP_TURN_ID": turn_id,
            "YIREN_MCP_PROVIDER_ID": provider_id,
            "YIREN_MCP_TASKS_ENABLED": "1" if tasks_enabled else "0",
            "YIREN_MCP_EXTENSIONS": ",".join(extension_points.modules()),
        }
        if valid_agent_ids:
            env["YIREN_MCP_VALID_AGENTS"] = ",".join(sorted(valid_agent_ids))
        if max_a2a_invocations > 0:
            env["YIREN_MCP_MAX_A2A_INVOCATIONS"] = str(max_a2a_invocations)
        if scheduled_agent_ids:
            env["YIREN_MCP_SCHEDULED_AGENTS"] = ",".join(sorted(scheduled_agent_ids))
        return env

    def _render_claude_mcp_config(self, env: dict) -> str:
        """Return a JSON string for ``--mcp-config`` consumed by claude CLI.

        Format: ``{"mcpServers": {"yiren_hub": {"command": ..., "args": [...], "env": {...}}}}``
        """
        server = {
            "command": self.mcp_python,
            "args": ["-m", self.mcp_module],
            "env": {k: str(v) for k, v in env.items()},
        }
        return json.dumps(
            {"mcpServers": {MCP_SERVER_NAME: server}},
            ensure_ascii=False,
            indent=2,
        )

    def _render_gemini_settings(self, env: dict, *, settings_path: Optional[Path] = None) -> str:
        """Return a JSON string for Gemini's settings.json.

        Reads the seeded settings.json (a copy of the user's real one) but
        keeps only auth-related and model-selection fields.  ``mcpServers``
        is replaced entirely with our injected entry — the user's global
        MCP server registry must not leak into agent turns.
        """
        settings: dict = {}
        if settings_path is not None and settings_path.is_file():
            try:
                loaded = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for key in ("security", "model"):
                        if key in loaded:
                            settings[key] = loaded[key]
            except json.JSONDecodeError:
                settings = {}
        server = {
            "command": self.mcp_python,
            "args": ["-m", self.mcp_module],
            "env": {k: str(v) for k, v in env.items()},
        }
        settings["mcpServers"] = {MCP_SERVER_NAME: server}
        return json.dumps(settings, ensure_ascii=False, indent=2)

    def _antigravity_server_spec(self, env: dict) -> dict:
        """Return the inner MCP server dict injected into the global config."""
        server_env = {k: str(v) for k, v in env.items()}
        python_path_parts = [os.getcwd()]
        existing_pythonpath = os.environ.get("PYTHONPATH")
        if existing_pythonpath:
            python_path_parts.append(existing_pythonpath)
        server_env["PYTHONPATH"] = os.pathsep.join(python_path_parts)
        return {
            "command": self.mcp_python,
            "args": ["-m", self.mcp_module],
            "env": server_env,
        }

    def _render_codex_config(
        self,
        env: dict,
        *,
        project_path: Optional[Path] = None,
    ) -> str:
        # Codex CLI reads MCP servers from a TOML config block. We render only
        # what we need; codex tolerates additional unknown blocks.
        # Trust the current working directory: without an explicit project
        # trust entry, non-interactive `codex exec` rejects every MCP tool
        # call as "user cancelled MCP tool call".
        lines = [
            '[projects.{path}]'.format(
                path=_toml_string(str(project_path or Path.cwd()))
            ),
            'trust_level = "trusted"',
            "",
            "[mcp_servers.{name}]".format(name=MCP_SERVER_NAME),
        ]
        lines.append("command = {value}".format(value=_toml_string(self.mcp_python)))
        lines.append(
            "args = [{value}]".format(value=", ".join([_toml_string("-m"), _toml_string(self.mcp_module)]))
        )
        lines.append("[mcp_servers.{name}.env]".format(name=MCP_SERVER_NAME))
        for key, value in env.items():
            lines.append("{key} = {value}".format(key=key, value=_toml_string(str(value))))
        lines.append("")
        return "\n".join(lines)

    def _fallback_via_stdout(
        self,
        tools: MCPTools,
        mission_id: str,
        agent_id: str,
        turn_id: str,
        provider_id: str,
        stdout: str,
    ) -> TurnOutcome:
        envelope = _parse_result_envelope(stdout) if stdout else None
        if envelope is not None:
            message = envelope.message.strip() or "(CLI returned no output)"
            tools.publish_message(mission_id, agent_id, turn_id, message, provider_id)
            for artifact in envelope.artifacts:
                try:
                    tools.write_artifact(
                        mission_id,
                        agent_id,
                        turn_id,
                        artifact.path,
                        artifact.content,
                        provider_id=provider_id,
                    )
                except MCPToolError as exc:
                    return TurnOutcome(status=TurnStatus.BLOCKED, summary=str(exc))
            return TurnOutcome(status=TurnStatus.COMPLETED, summary="stdout envelope")

        text = stdout.strip() or "(CLI returned no output)"
        tools.publish_message(mission_id, agent_id, turn_id, text, provider_id)
        return TurnOutcome(status=TurnStatus.COMPLETED, summary="stdout")


_MCP_PROMPT_GUIDANCE = "\n" + host_rules.SPEAK.text + host_rules.HANDOFF_BEFORE_SPEAK.text


def _augment_prompt_for_mcp(prompt: str) -> str:
    return prompt + "\n" + _MCP_PROMPT_GUIDANCE


def _write_debug_files(
    turn_id: str,
    provider_id: str,
    stdout: str,
    stderr: str,
    config_path: Path,
) -> None:
    debug_dir = os.environ.get("YIREN_MCP_DEBUG_DIR")
    if not debug_dir:
        return
    debug_path = Path(debug_dir)
    debug_path.mkdir(parents=True, exist_ok=True)
    provider_stem = "{tid}.{provider}".format(tid=turn_id, provider=provider_id)
    for stem in (provider_stem, turn_id):
        (debug_path / "{stem}.stdout".format(stem=stem)).write_text(stdout, encoding="utf-8")
        (debug_path / "{stem}.stderr".format(stem=stem)).write_text(stderr, encoding="utf-8")
        try:
            (debug_path / "{stem}.config".format(stem=stem)).write_text(
                config_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        except Exception:
            pass


def _seed_codex_auth(codex_home: Path) -> None:
    real_home = Path(os.path.expanduser("~/.codex"))
    for name in ("auth.json", "config.json"):
        src = real_home / name
        if src.is_file():
            try:
                shutil.copy2(src, codex_home / name)
            except OSError:
                pass


def _seed_gemini_auth(gemini_home: Path) -> None:
    """Copy Gemini auth files from the real ~/.gemini into the ephemeral home.

    Copies only the minimal credential files so the CLI can authenticate
    without reading the user's full ~/.gemini directory.
    """
    real_gemini = Path(os.path.expanduser("~/.gemini"))
    dest_dir = gemini_home / ".gemini"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "settings.json",
        "google_accounts.json",
        "state.json",
        "trustedFolders.json",
        "installation_id",
        "credentials.json",
        "oauth_credentials.json",
        ".google-credentials.json",
    ):
        src = real_gemini / name
        if src.is_file():
            try:
                shutil.copy2(src, dest_dir / name)
            except OSError:
                pass


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"{v}"'.format(v=escaped)


def _wait_for_process_with_activity(
    proc: subprocess.Popen,
    *,
    hard_timeout: int,
    idle_timeout: int,
    last_activity_at: Callable[[], float],
    refresh_activity: Callable[[], None],
    progress_deadline: Optional[ToolResultDeadline] = None,
    on_extension: Optional[Callable[[dict], None]] = None,
    stopped: Callable[[], bool] = lambda: False,
) -> ProcessWaitReason:
    """Wait until exit, a user stop, hard deadline, or an optional inactivity deadline."""
    hard_deadline = time.time() + max(0, hard_timeout)
    while True:
        if stopped():
            return ProcessWaitReason.STOPPED
        if idle_timeout > 0 or progress_deadline is not None:
            refresh_activity()
        if progress_deadline is not None:
            extension = progress_deadline.check()
            if extension and on_extension:
                on_extension(extension)
            hard_deadline = progress_deadline.deadline
        now = time.time()
        if now >= hard_deadline:
            return ProcessWaitReason.HARD_TIMEOUT

        idle_deadline: Optional[float] = None
        if idle_timeout > 0:
            idle_deadline = last_activity_at() + idle_timeout
            if now >= idle_deadline:
                return ProcessWaitReason.IDLE_TIMEOUT

        from backend.app.adapters.turn_budget import remaining_timeout
        remaining = remaining_timeout(hard_deadline - now)
        if remaining <= 0:
            return ProcessWaitReason.HARD_TIMEOUT
        if idle_deadline is not None:
            remaining = min(remaining, idle_deadline - now)
        try:
            proc.wait(timeout=min(remaining, 1.0))
            return ProcessWaitReason.EXITED
        except subprocess.TimeoutExpired:
            continue


_is_mcp_tool = is_mcp_tool


def _max_event_id(repository: Repository, mission_id: str) -> int:
    events = repository.list_events_after(mission_id, 0)
    if not events:
        return 0
    return max(int(event.id) for event in events)


def _emit_turn_usage(
    repository: Repository,
    mission_id: str,
    agent_id: str,
    provider_id: str,
    turn_id: str,
    usage: Optional[UsageRecord],
) -> None:
    if usage is None:
        return
    payload = {
        "agent_id": agent_id,
        "provider_id": provider_id,
        "turn_id": turn_id,
        "usage": dataclasses.asdict(usage),
    }
    if usage.model:
        payload["model"] = usage.model
    if usage.stop_reason:
        payload["stop_reason"] = usage.stop_reason
    if usage.duration_ms:
        payload["duration_ms"] = usage.duration_ms
    repository.add_event(mission_id, "turn.usage", payload)


def _mcp_published_message(
    repository: Repository,
    mission_id: str,
    turn_id: str,
    cursor: int,
    receipt_id: str,
    agent_id: str,
    provider_id: str,
) -> bool:
    new_events: Iterable = repository.list_events_after(mission_id, cursor)
    messages_by_id = {
        event.payload.get("id"): event.payload
        for event in new_events
        if event.event_type == "message"
        and (event.payload or {}).get("role") == "agent"
        and (event.payload or {}).get("turn_id") == turn_id
        and (event.payload or {}).get("agent_id") == agent_id
        and (event.payload or {}).get("provider_id") == provider_id
    }
    new_events = repository.list_events_after(mission_id, cursor)
    for event in new_events:
        if event.event_type != "mcp.receipt":
            continue
        payload = event.payload or {}
        if payload.get("receipt_id") != receipt_id:
            continue
        if (
            payload.get("turn_id") == turn_id
            and payload.get("agent_id") == agent_id
            and payload.get("provider_id") == provider_id
            and payload.get("tool") == "publish_message"
            and payload.get("message_id") in messages_by_id
        ):
            return True
    return False


def _agy_self_timed_out(stdout: str, stderr: str) -> bool:
    output = (stdout + "\n" + stderr).lower()
    return "timed out waiting for response" in output or "print timeout after" in output
