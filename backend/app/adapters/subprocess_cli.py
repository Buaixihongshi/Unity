import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional

from backend.app.adapters.base import CLIAdapter, TurnOutcome
from backend.app.adapters.failure_text import diagnostic_detail
from backend.app.domain.models import TurnStatus
from backend.app.orchestration import interruption
from backend.app.mcp.tools import MCPToolError, MCPTools

_IS_WINDOWS = sys.platform == "win32"
_GRACE_PERIOD = 5


@dataclass(frozen=True)
class CLIArtifact:
    path: str
    content: str


@dataclass(frozen=True)
class CLIResultEnvelope:
    message: str
    artifacts: List[CLIArtifact]


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate the entire process group, then force-kill after a grace period."""
    if _IS_WINDOWS:
        proc.kill()
        return
    pgid = None
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pass
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
    try:
        proc.wait(timeout=_GRACE_PERIOD)
    except subprocess.TimeoutExpired:
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=_GRACE_PERIOD)
        except subprocess.TimeoutExpired:
            pass


def build_failure_summary(
    provider_id: str,
    *,
    timeout: Optional[int] = None,
    exit_code: Optional[int] = None,
    stderr_snippet: str = "",
    stdout_snippet: str = "",
    os_error: Optional[str] = None,
) -> str:
    parts = ["[{pid}]".format(pid=provider_id)]
    if os_error is not None:
        parts.append("OSError: {e}".format(e=os_error))
        return " ".join(parts)
    if timeout is not None:
        parts.append("timeout after {t}s".format(t=timeout))
    elif exit_code is not None:
        parts.append("exit_code={c}".format(c=exit_code))
    detail = diagnostic_detail(stderr_snippet, stdout_snippet)
    if detail:
        parts.append(detail)
    return " ".join(parts)


class SubprocessCLIAdapter(CLIAdapter):
    def __init__(
        self,
        command: str,
        args: Optional[List[str]] = None,
        output_parser: Optional[Callable[[str], str]] = None,
        *,
        timeout: int = 300,
    ):
        self.command = command
        self.args = args or []
        self.output_parser = output_parser or (lambda output: output.strip())
        self.timeout = timeout

    def run_turn(
        self,
        mission_id: str,
        agent_id: str,
        turn_id: str,
        provider_id: str,
        prompt: str,
        tools: MCPTools,
    ) -> TurnOutcome:
        cmd = [self.command] + self.args + [prompt]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=not _IS_WINDOWS,
            )
        except OSError as exc:
            summary = build_failure_summary(provider_id, os_error=str(exc))
            return TurnOutcome(status=TurnStatus.BLOCKED, summary=summary)

        finished, paused = threading.Event(), threading.Event()

        def watch_for_pause() -> None:
            # A user pause stops the process group; communicate() then returns.
            while not finished.wait(1.0):
                if interruption.pause_requested(mission_id):
                    paused.set()
                    kill_process_tree(proc)
                    return

        threading.Thread(target=watch_for_pause, daemon=True).start()
        try:
            from backend.app.adapters.turn_budget import remaining_timeout
            stdout, stderr = proc.communicate(timeout=remaining_timeout(self.timeout))
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            stdout, stderr = proc.communicate()
            summary = build_failure_summary(
                provider_id,
                timeout=self.timeout,
                stderr_snippet=stderr.strip() if stderr else "",
                stdout_snippet=stdout.strip() if stdout else "",
            )
            return TurnOutcome(status=TurnStatus.BLOCKED, summary=summary, retryable=False)
        finally:
            finished.set()
        if paused.is_set():
            return TurnOutcome(status=TurnStatus.BLOCKED, summary=interruption.STOPPED_SUMMARY,
                               failure_code="stopped", retryable=False)

        stdout = stdout.strip()
        stderr = stderr.strip()

        if proc.returncode != 0:
            summary = build_failure_summary(
                provider_id,
                exit_code=proc.returncode,
                stderr_snippet=stderr,
                stdout_snippet=stdout,
            )
            return TurnOutcome(status=TurnStatus.BLOCKED, summary=summary, retryable=False)

        content = self.output_parser(stdout) if stdout else ""
        envelope = _parse_result_envelope(content)
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
            return TurnOutcome(status=TurnStatus.COMPLETED)

        content = content.strip() or "(CLI returned no output)"
        tools.publish_message(mission_id, agent_id, turn_id, content, provider_id)
        return TurnOutcome(status=TurnStatus.COMPLETED)


def _parse_result_envelope(output: str) -> Optional[CLIResultEnvelope]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    message = payload.get("message")
    artifacts = payload.get("artifacts", [])
    if not isinstance(message, str) or not isinstance(artifacts, list):
        return None

    parsed_artifacts = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            return None
        path = artifact.get("path")
        content = artifact.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return None
        parsed_artifacts.append(CLIArtifact(path=path, content=content))

    return CLIResultEnvelope(message=message, artifacts=parsed_artifacts)
