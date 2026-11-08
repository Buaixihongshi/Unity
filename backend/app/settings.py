import os
from pathlib import Path
from typing import Dict, Optional

from pydantic import BaseModel, Field


class ProviderOverride(BaseModel):
    enabled: Optional[bool] = None


class AgentOverride(BaseModel):
    fallback_context_approved: bool = False
    fallback_context_targets: Dict[str, str] = Field(default_factory=dict)
    default_provider_id: Optional[str] = None
    fallback_provider_ids: Optional[list[str]] = None
    provider_models: Optional[Dict[str, str]] = None
    provider_efforts: Optional[Dict[str, str]] = None


_AGENT_OVERRIDE_KEYS = {"default_provider_id", "fallback_provider_ids", "provider_models", "provider_efforts",
                        "fallback_context_approved", "fallback_context_targets"}


class Settings(BaseModel):
    database_path: Path = Path("data/yirenhub.sqlite3")
    artifact_root: Path = Path("artifacts")
    memory_root: Path = Path("data/memory")
    config_dir: Path = Path("config")
    # Desktop builds serve the prebuilt frontend from the backend origin.
    frontend_dist: Optional[Path] = None
    use_fake_cli: bool = False
    demo_only: bool = False
    max_turns: int = 6
    roundtable_max_turns: int = Field(default=12, ge=3, le=12)
    deliberation_max_turns: int = Field(default=16, ge=5, le=16)
    max_a2a_invocations: int = 8
    cli_timeout: int = 300
    a2a_idle_timeout: int = Field(default=90, ge=0)
    antigravity_persistent: bool = False
    # Optional extensions on top of the shared-context core; off by default.
    discussion_enabled: bool = False
    tasks_enabled: bool = False
    maintenance_enabled: bool = False
    maintenance_worktree_root: Path = Path("data/maintenance-worktrees")
    maintenance_command_timeout: int = Field(default=900, ge=1)
    provider_overrides: Dict[str, ProviderOverride] = {}
    agent_overrides: Dict[str, AgentOverride] = {}
    api_connections: Dict[str, dict] = Field(default_factory=dict, exclude=True, repr=False)


_RUNTIME_FIELDS = {
    "use_fake_cli",
    "max_turns",
    "max_a2a_invocations",
    "cli_timeout",
    "a2a_idle_timeout",
    "discussion_enabled",
    "tasks_enabled",
    "maintenance_enabled",
    "maintenance_command_timeout",
}

EXTENSION_FIELDS = {
    "discussion": "discussion_enabled",
    "tasks": "tasks_enabled",
    "maintenance": "maintenance_enabled",
}

EXTENSION_DISABLED = {
    "discussion": "讨论扩展未开启，可在设置 → 扩展中开启",
    "tasks": "任务扩展未开启，可在设置 → 扩展中开启",
    "maintenance": "维护扩展未开启，可在设置 → 扩展中开启",
}


def get_settings() -> Settings:
    database_path = _env_path("YIREN_DATABASE_PATH", default=Path("data/yirenhub.sqlite3"))

    kwargs: dict = dict(
        database_path=database_path,
        artifact_root=_env_path("YIREN_ARTIFACT_ROOT", default=Path("artifacts")),
        memory_root=_env_path("YIREN_MEMORY_ROOT", default=Path("data/memory")),
        config_dir=_env_path("YIREN_CONFIG_DIR", default=Path("config")),
        frontend_dist=_env_path("YIREN_FRONTEND_DIST", default=None),
        use_fake_cli=_env_bool("YIREN_USE_FAKE_CLI", default=False),
        demo_only=_env_bool("YIREN_DEMO_ONLY", default=False),
        max_turns=_env_int("YIREN_MAX_TURNS", default=6),
        roundtable_max_turns=_env_int("YIREN_ROUNDTABLE_MAX_TURNS", default=12),
        deliberation_max_turns=_env_int("YIREN_DELIBERATION_MAX_TURNS", default=16),
        max_a2a_invocations=_env_int("YIREN_MAX_A2A_INVOCATIONS", default=8),
        cli_timeout=_env_int("YIREN_CLI_TIMEOUT", default=300),
        a2a_idle_timeout=_env_int("YIREN_A2A_IDLE_TIMEOUT", default=90),
        antigravity_persistent=_env_bool("YIREN_ANTIGRAVITY_PERSISTENT", default=False),
        discussion_enabled=_env_bool("YIREN_DISCUSSION_ENABLED", default=False),
        tasks_enabled=_env_bool("YIREN_TASKS_ENABLED", default=False),
        maintenance_enabled=_env_bool("YIREN_MAINTENANCE_ENABLED", default=False),
        maintenance_worktree_root=_env_path(
            "YIREN_MAINTENANCE_WORKTREE_ROOT",
            default=Path("data/maintenance-worktrees"),
        ),
        maintenance_command_timeout=_env_int("YIREN_MAINTENANCE_COMMAND_TIMEOUT", default=900),
    )

    from backend.app.storage.hub_settings import default_path, load, retire_provider
    settings_path = default_path(Settings(database_path=database_path))
    retire_provider(settings_path, "gemini", "antigravity")
    file_data = load(settings_path)

    runtime = file_data.get("runtime")
    if isinstance(runtime, dict):
        for key, value in runtime.items():
            if key not in _RUNTIME_FIELDS:
                continue
            kwargs[key] = value

    provider_overrides: Dict[str, ProviderOverride] = {}
    providers_raw = file_data.get("providers")
    if isinstance(providers_raw, dict):
        for pid, pdata in providers_raw.items():
            if isinstance(pdata, dict):
                provider_overrides[pid] = ProviderOverride(**{
                    k: v for k, v in pdata.items() if k in {"enabled"}
                })
    kwargs["provider_overrides"] = provider_overrides

    agent_overrides: Dict[str, AgentOverride] = {}
    agents_raw = file_data.get("agents")
    if isinstance(agents_raw, dict):
        for aid, adata in agents_raw.items():
            if isinstance(adata, dict):
                agent_overrides[aid] = AgentOverride(**{
                    k: v for k, v in adata.items()
                    if k in _AGENT_OVERRIDE_KEYS
                })
    kwargs["agent_overrides"] = agent_overrides

    kwargs["api_connections"] = file_data.get("connections") or {}
    if kwargs["demo_only"]:
        kwargs["use_fake_cli"] = True
        kwargs["maintenance_enabled"] = False
    return Settings(**kwargs)


def snapshot_settings(app_settings: "Settings") -> "Settings":
    """Re-read ``hub_settings.json`` for fresh runtime / provider / agent
    overrides, preserving the infrastructure paths from the app-level Settings.

    Called at mission start so UI toggles take effect on the next mission
    without restarting the backend. Every code path that spawns a
    ``MissionRunner`` must go through this — otherwise the runner sees the
    settings that were cached at process start.
    """
    from backend.app.storage.hub_settings import default_path, load, retire_provider
    settings_path = default_path(app_settings)
    retire_provider(settings_path, "gemini", "antigravity")
    file_data = load(settings_path)

    kwargs: dict = {
        "database_path": app_settings.database_path,
        "artifact_root": app_settings.artifact_root,
        "memory_root": app_settings.memory_root,
        "config_dir": app_settings.config_dir,
        "frontend_dist": app_settings.frontend_dist,
        "use_fake_cli": app_settings.use_fake_cli,
        "demo_only": app_settings.demo_only,
        "max_turns": app_settings.max_turns,
        "roundtable_max_turns": app_settings.roundtable_max_turns,
        "deliberation_max_turns": app_settings.deliberation_max_turns,
        "max_a2a_invocations": app_settings.max_a2a_invocations,
        "cli_timeout": app_settings.cli_timeout,
        "a2a_idle_timeout": app_settings.a2a_idle_timeout,
        "antigravity_persistent": app_settings.antigravity_persistent,
        "discussion_enabled": app_settings.discussion_enabled,
        "tasks_enabled": app_settings.tasks_enabled,
        "maintenance_enabled": app_settings.maintenance_enabled,
        "maintenance_worktree_root": app_settings.maintenance_worktree_root,
        "maintenance_command_timeout": app_settings.maintenance_command_timeout,
    }

    runtime = file_data.get("runtime") or {}
    for key, value in runtime.items():
        if key in _RUNTIME_FIELDS:
            kwargs[key] = value

    provider_overrides: Dict[str, ProviderOverride] = {}
    for pid, pdata in (file_data.get("providers") or {}).items():
        if isinstance(pdata, dict):
            provider_overrides[pid] = ProviderOverride(**{
                k: v for k, v in pdata.items() if k in {"enabled"}
            })
    kwargs["provider_overrides"] = provider_overrides

    agent_overrides: Dict[str, AgentOverride] = {}
    for aid, adata in (file_data.get("agents") or {}).items():
        if isinstance(adata, dict):
            agent_overrides[aid] = AgentOverride(**{
                k: v for k, v in adata.items()
                if k in _AGENT_OVERRIDE_KEYS
            })
    kwargs["agent_overrides"] = agent_overrides

    kwargs["api_connections"] = file_data.get("connections") or {}
    if app_settings.demo_only:
        kwargs["use_fake_cli"] = True
        kwargs["maintenance_enabled"] = False
    return Settings(**kwargs)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_path(name: str, default: Optional[Path]) -> Optional[Path]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return Path(value)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)
