import json
import tempfile
from pathlib import Path
from typing import Optional

from backend.app import extension_points
from backend.app.adapters.base import CLIAdapter, TurnOutcome
from backend.app.adapters.turn_budget import remaining_timeout
from backend.app.adapters.fake import FakeCLIAdapter
from backend.app.adapters.mcp_subprocess import MCPSubprocessAdapter
from backend.app.adapters.antigravity_config import recover_antigravity_global_config
from backend.app.adapters.subprocess_cli import SubprocessCLIAdapter
from backend.app.config.providers import ProviderNotFoundError, get_cli_provider
from backend.app.domain.models import TurnStatus
from backend.app.mcp.tools import MCPTools
from backend.app.settings import AgentOverride, ProviderOverride
from backend.app.domain.agent_registry import get_agent_registry


class ProviderRouterAdapter(CLIAdapter):
    def __init__(
        self,
        config_dir: Path,
        *,
        cli_timeout: int = 300,
        a2a_idle_timeout: int = 90,
        antigravity_persistent: bool = False,
        database_path: Optional[Path] = None,
        artifact_root: Optional[Path] = None,
        memory_root: Optional[Path] = None,
        provider_overrides: Optional[dict[str, ProviderOverride]] = None,
        agent_overrides: Optional[dict[str, AgentOverride]] = None,
        api_settings=None,
    ):
        self.api_settings = api_settings
        self.config_dir = config_dir
        self.cli_timeout = cli_timeout
        self.a2a_idle_timeout = max(0, a2a_idle_timeout)
        self.antigravity_persistent = antigravity_persistent
        self.database_path = database_path
        self.artifact_root = artifact_root
        self.memory_root = memory_root
        self.provider_overrides = provider_overrides or {}
        self.agent_overrides = agent_overrides or {}
        self.fake_adapter = FakeCLIAdapter()
        # Per-mission Gemini state: one persistent GEMINI_CLI_HOME so session
        # files survive across turns, plus an (agent_id -> session_id) map so
        # each agent resumes its own session — personas must not share context.
        self._gemini_home_ctx: Optional[tempfile.TemporaryDirectory] = None
        self._gemini_home: Optional[Path] = None
        self._gemini_sessions: dict[str, str] = {}
        # agy crash recovery: a prior hard-kill may have left our temp MCP config
        # in the user's global ~/.gemini/config/mcp_config.json. Restore it before
        # any turn. No-op when ~/.gemini/config does not exist.
        recover_antigravity_global_config()

    def _ensure_gemini_home(self) -> Path:
        if self._gemini_home is None:
            self._gemini_home_ctx = tempfile.TemporaryDirectory(prefix="yiren-gemini-")
            self._gemini_home = Path(self._gemini_home_ctx.name)
        return self._gemini_home

    def close(self) -> None:
        """Release the per-mission Gemini tempdir. Idempotent."""
        if self._gemini_home_ctx is not None:
            try:
                self._gemini_home_ctx.cleanup()
            except OSError:
                pass
            self._gemini_home_ctx = None
            self._gemini_home = None
        self._gemini_sessions.clear()

    def _route_value(self, agent_id, provider_id, field):
        override = self.agent_overrides.get(agent_id)
        agent = get_agent_registry(self.config_dir).get(agent_id)
        if override and getattr(override, field) is not None:
            values = getattr(override, field)
        else:
            values = getattr(agent, field) if agent else {}
        return values.get(provider_id, "")

    def _model_for(self, agent_id, provider_id):
        return self._route_value(agent_id, provider_id, "provider_models")

    def _effort_for(self, agent_id, provider_id):
        return self._route_value(agent_id, provider_id, "provider_efforts")

    def _api_adapter(self, agent_id, provider_id):
        from backend.app.adapters.api_text import APITextAdapter
        if self.api_settings is None or provider_id not in self.api_settings.api_connections:
            return None
        return APITextAdapter(self.api_settings, self.api_settings.api_connections[provider_id],
                              self._model_for(agent_id, provider_id))

    def run_turn(
        self,
        mission_id: str,
        agent_id: str,
        turn_id: str,
        provider_id: str,
        prompt: str,
        tools: MCPTools,
    ) -> TurnOutcome:
        routed = extension_points.first('route_turn', self, mission_id, agent_id, turn_id, provider_id, prompt, tools)
        if routed is not None:
            return routed
        api = self._api_adapter(agent_id, provider_id)
        if api:
            return api.run_turn(mission_id, agent_id, turn_id, provider_id, prompt, tools)
        try:
            provider = get_cli_provider(
                self.config_dir,
                provider_id,
                overrides=self.provider_overrides,
            )
        except ProviderNotFoundError:
            return TurnOutcome(status=TurnStatus.BLOCKED, summary="Unknown connection configuration", failure_code="configuration", retryable=False)

        from backend.app.adapters.model_selection import with_model_and_effort
        provider = with_model_and_effort(provider, self._model_for(agent_id, provider_id),
                                         self._effort_for(agent_id, provider_id))

        if not provider.enabled:
            return TurnOutcome(
                status=TurnStatus.BLOCKED,
                summary="provider is disabled: {provider_id}".format(
                    provider_id=provider_id
                ),
                failure_code="configuration", retryable=False,
            )

        if provider.id == "fake":
            return self.fake_adapter.run_turn(
                mission_id,
                agent_id,
                turn_id,
                provider_id,
                prompt,
                tools,
            )

        if provider.mcp_enabled and self.database_path is not None and self.artifact_root is not None:
            if provider.id == 'antigravity' and self.antigravity_persistent:
                from backend.app.adapters.antigravity_session import run_persistent_turn
                from backend.app.domain.models import is_provider_verification_mission
                mission = tools.repository.get_mission(mission_id)
                if mission and not is_provider_verification_mission(mission):
                    return run_persistent_turn(
                        provider, database_path=self.database_path, artifact_root=self.artifact_root,
                        timeout=remaining_timeout(self.cli_timeout),
                        idle_timeout=self.a2a_idle_timeout if tools.current_step_origin in {'a2a', 'a2a_resume'} else 0,
                        mission_id=mission_id, agent_id=agent_id, turn_id=turn_id, prompt=prompt, tools=tools,
                    )
            gemini_home: Optional[Path] = None
            resume_sid: Optional[str] = None
            if provider.id == "gemini":
                gemini_home = self._ensure_gemini_home()
                resume_sid = self._gemini_sessions.get(agent_id)
            adapter: CLIAdapter = MCPSubprocessAdapter(
                provider.command,
                provider.args,
                db_path=self.database_path,
                artifact_root=self.artifact_root,
                memory_root=self.memory_root,
                timeout=remaining_timeout(self.cli_timeout),
                idle_timeout=(
                    self.a2a_idle_timeout
                    if tools.current_step_origin in {"a2a", "a2a_resume"}
                    else 0
                ),
                gemini_home_override=gemini_home,
                gemini_resume_session_id=resume_sid,
            )
            outcome = adapter.run_turn(
                mission_id,
                agent_id,
                turn_id,
                provider_id,
                prompt,
                tools,
            )
            if provider.id == "gemini":
                if outcome.session_id:
                    self._gemini_sessions[agent_id] = outcome.session_id
                else:
                    # outcome.session_id=None means MCPSubprocessAdapter detected
                    # an end-of-stream error (Plan B) — the on-disk session is
                    # untrustworthy. Drop the cached entry so the next turn
                    # cold-starts instead of hitting "Invalid session identifier".
                    self._gemini_sessions.pop(agent_id, None)
            return outcome

        adapter = SubprocessCLIAdapter(
            provider.command,
            provider.args,
            output_parser=lambda output: parse_provider_output(provider.id, output),
            timeout=remaining_timeout(self.cli_timeout),
        )
        return adapter.run_turn(
            mission_id,
            agent_id,
            turn_id,
            provider_id,
            prompt,
            tools,
        )


def parse_provider_output(provider_id: str, output: str) -> str:
    if provider_id != "codex":
        return output.strip()

    messages = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            messages.append(item["text"].strip())

    return "\n".join(message for message in messages if message) or output.strip()
