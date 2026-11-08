"""GET / PUT /api/hub/settings — resolved settings view with source annotation."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import re
import secrets
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.app.adapters.model_selection import EFFORT_LEVELS
from backend.app.config.providers import load_cli_providers
from backend.app.domain.agent_registry import CUSTOM_AGENTS_KEY, AgentRegistry, get_agent_registry
from backend.app.settings import EXTENSION_FIELDS, AgentOverride, ProviderOverride, Settings
from backend.app.storage import hub_settings
from backend.app.storage import api_connections


_settings_write_lock = threading.RLock()


class RuntimePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_fake_cli: Optional[bool] = None
    max_turns: Optional[int] = None
    max_a2a_invocations: Optional[int] = None
    cli_timeout: Optional[int] = None
    a2a_idle_timeout: Optional[int] = Field(default=None, ge=0)


class ExtensionsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discussion: Optional[bool] = None
    tasks: Optional[bool] = None
    maintenance: Optional[bool] = None


class AgentOverridePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_provider_id: Optional[str] = None
    fallback_provider_ids: Optional[List[str]] = None
    provider_models: Optional[Dict[str, str]] = None
    provider_efforts: Optional[Dict[str, str]] = None
    clear: Optional[bool] = None
    fallback_context_approved: Optional[bool] = None


class CustomAgentInput(BaseModel):
    """A user-created role. Routing here becomes the role's own default."""

    model_config = ConfigDict(extra="forbid")

    id: Optional[str] = None
    display_name: str
    role: str
    persona: str
    aliases: List[str] = Field(default_factory=list)
    trigger_points: List[str] = Field(default_factory=list)
    default_provider_id: str
    fallback_provider_ids: List[str] = Field(default_factory=list)
    provider_models: Dict[str, str] = Field(default_factory=dict)
    provider_efforts: Dict[str, str] = Field(default_factory=dict)


MAX_CUSTOM_AGENTS = 12
_CUSTOM_ID = re.compile(r"[a-z][a-z0-9_-]{1,31}")
_ROUTE_KEYS = ("default_provider_id", "fallback_provider_ids", "provider_models", "provider_efforts")
# Separators the @-mention parsers split on; an alias containing one could never match.
_ALIAS_FORBIDDEN = frozenset("@,，、:：")


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _invalid_effort(provider_id: str, effort: str) -> Optional[str]:
    """Return why an effort cannot be saved for this connection, or None."""
    levels = EFFORT_LEVELS.get(provider_id)
    if levels is None:
        return f"该连接不支持推理强度：{provider_id}"
    if effort not in levels:
        return f"推理强度无效：{effort}"
    return None


def _clean_text(value: str, label: str, limit: int) -> str:
    text = value.strip()
    if not text:
        raise _bad_request(f"{label}不能为空")
    if len(text) > limit or any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise _bad_request(f"{label}过长或包含控制字符（上限 {limit} 字）")
    return text


def _clean_custom_agent(payload: CustomAgentInput, registry: AgentRegistry, agent_id: str) -> dict:
    display_name = _clean_text(payload.display_name, "名称", 24)
    if "\n" in display_name:
        raise _bad_request("名称不能换行")
    aliases: List[str] = []
    for raw in payload.aliases:
        alias = raw.strip()
        if not alias:
            continue
        if len(alias) > 24 or any(c.isspace() or c in _ALIAS_FORBIDDEN for c in alias):
            raise _bad_request("别名不能包含空格、@ 或标点，且不超过 24 字")
        if alias.lower() not in {a.lower() for a in aliases} and alias.lower() != display_name.lower():
            aliases.append(alias)
    if len(aliases) > 8:
        raise _bad_request("别名最多 8 个")
    trigger_points = [line.strip() for line in payload.trigger_points if line.strip()]
    if len(trigger_points) > 8 or any(len(line) > 300 for line in trigger_points):
        raise _bad_request("职责指引最多 8 条，每条不超过 300 字")
    # @mentions resolve names case-insensitively, so every name a role
    # answers to must stay unique across the whole team.
    taken: Dict[str, str] = {}
    for other in registry.all():
        if other.id == agent_id:
            continue
        for name in (other.id, other.display_name, *other.aliases):
            taken[name.lower()] = other.display_name
    for name in (agent_id, display_name, *aliases):
        if name.lower() in taken:
            raise _bad_request(f"“{name}”已被 {taken[name.lower()]} 使用，请换一个名称或别名")
    return {
        "id": agent_id,
        "display_name": display_name,
        "role": _clean_text(payload.role, "职位", 80),
        "persona": _clean_text(payload.persona, "人格", 600),
        "aliases": aliases,
        "trigger_points": trigger_points,
        "capability_hints": [],
    }


class HubSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connections: Optional[Dict[str, Dict[str, Any]]] = None
    providers: Optional[Dict[str, ProviderOverride]] = None
    runtime: Optional[RuntimePatch] = None
    extensions: Optional[ExtensionsPatch] = None
    agents: Optional[Dict[str, AgentOverridePatch]] = None


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/hub", tags=["hub-settings"])

    def _settings_file_path():
        return hub_settings.default_path(settings)

    def _committed_provider_ids() -> set:
        return {p.id for p in load_cli_providers(settings.config_dir)}

    def _build_view() -> dict:
        file_data = hub_settings.load(_settings_file_path())
        file_runtime = file_data.get("runtime") or {}
        file_providers = file_data.get("providers") or {}

        committed = load_cli_providers(settings.config_dir)

        runtime_view = {}
        _RUNTIME_DEFAULTS = {
            "use_fake_cli": False,
            "max_turns": 6,
            "max_a2a_invocations": 8,
            "cli_timeout": 300,
            "a2a_idle_timeout": 90,
        }
        for key, default in _RUNTIME_DEFAULTS.items():
            current = getattr(settings, key)
            if key in file_runtime:
                source = "user"
                value = file_runtime[key]
            elif current != default:
                source = "env"
                value = current
            else:
                source = "default"
                value = default
            runtime_view[key] = {"value": value, "source": source}

        # Extensions default off; a saved choice wins over the process environment.
        extensions_view = {}
        for name, field_name in EXTENSION_FIELDS.items():
            if field_name in file_runtime:
                extensions_view[name] = {"value": bool(file_runtime[field_name]), "source": "user"}
            else:
                current = getattr(settings, field_name)
                extensions_view[name] = {"value": current, "source": "env" if current else "default"}

        provider_list: List[Dict[str, Any]] = []
        for p in committed:
            pid = p.id
            if pid in file_providers and "enabled" in file_providers[pid]:
                source = "user"
                value = file_providers[pid]["enabled"]
            else:
                source = "committed"
                value = p.enabled
            provider_list.append({
                "id": pid,
                "label": p.label,
                "enabled": {"value": value, "source": source},
                "efforts": list(EFFORT_LEVELS.get(pid, ())),
            })

        file_agents = file_data.get("agents") or {}
        registry = get_agent_registry(settings.config_dir)
        agent_list: List[Dict[str, Any]] = []
        for agent in registry.all():
            override = file_agents.get(agent.id) if isinstance(file_agents, dict) else None
            base_default = agent.default_provider_id
            base_fallback = list(agent.fallback_provider_ids)
            if isinstance(override, dict) and "default_provider_id" in override:
                default_source = "user"
                default_value = override["default_provider_id"]
            else:
                default_source = "committed"
                default_value = base_default
            if isinstance(override, dict) and "fallback_provider_ids" in override:
                fallback_source = "user"
                fallback_value = list(override["fallback_provider_ids"])
            else:
                fallback_source = "committed"
                fallback_value = base_fallback
            has_model_override = isinstance(override, dict) and "provider_models" in override
            has_effort_override = isinstance(override, dict) and "provider_efforts" in override
            agent_list.append({
                "id": agent.id,
                "display_name": agent.display_name,
                "custom": registry.is_custom(agent.id),
                "role": agent.role,
                "persona": agent.persona,
                "aliases": list(agent.aliases),
                "trigger_points": list(agent.trigger_points),
                "default_provider_id": {"value": default_value, "source": default_source},
                "fallback_provider_ids": {"value": fallback_value, "source": fallback_source},
                "provider_models": {
                    "value": override["provider_models"] if has_model_override else agent.provider_models,
                    "source": "user" if has_model_override else "committed",
                },
                "provider_efforts": {
                    "value": override["provider_efforts"] if has_effort_override else agent.provider_efforts,
                    "source": "user" if has_effort_override else "committed",
                },
                "fallback_context_approved": {"value": bool(override.get("fallback_context_approved", False)) if isinstance(override, dict) else False, "source": "user" if isinstance(override, dict) and "fallback_context_approved" in override else "committed"},
                "base_provider_models": agent.provider_models,
                "base_provider_efforts": agent.provider_efforts,
                "base_default_provider_id": base_default,
                "base_fallback_provider_ids": base_fallback,
            })

        return {
            "connections": api_connections.connection_views(settings, file_data),
            "runtime": runtime_view,
            "extensions": extensions_view,
            "providers": provider_list,
            "agents": agent_list,
        }

    @router.get("/connections")
    def get_connections():
        return {"connections": api_connections.connection_views(settings)}

    def _catalog(cid, refresh=False):
        from backend.app.adapters.api_catalog import catalog
        data = hub_settings.load(_settings_file_path())
        connection = data.get("connections", {}).get(cid)
        if connection is None:
            from backend.app.adapters.cli_catalog import cli_catalog
            provider = next((p for p in load_cli_providers(settings.config_dir) if p.id == cid), None)
            if provider is None:
                raise HTTPException(status_code=404, detail="unknown connection")
            return cli_catalog(provider, scope=str(settings.config_dir), refresh=refresh)
        return catalog(settings, cid, connection, refresh=refresh)

    @router.get("/connections/{connection_id}/models")
    def get_models(connection_id: str):
        return _catalog(connection_id)

    @router.post("/connections/{connection_id}/models/refresh")
    def refresh_models(connection_id: str):
        return _catalog(connection_id, refresh=True)

    @router.get("/settings")
    def get_hub_settings():
        return _build_view()

    def _validate_route(default: str, fallbacks: List[str], models: Dict[str, str], efforts: Dict[str, str],
                        connections: dict) -> None:
        valid_ids = _committed_provider_ids() | set(connections)
        if default not in valid_ids:
            raise _bad_request(f"未知的主用连接：{default}")
        if len(set(fallbacks)) != len(fallbacks) or default in fallbacks:
            raise _bad_request("备用连接不能重复，也不能与主用连接相同")
        for pid in fallbacks:
            if pid not in valid_ids:
                raise _bad_request(f"未知的备用连接：{pid}")
        for pid, model in models.items():
            if pid not in (default, *fallbacks) or pid not in {"codex", "claude", "antigravity"} | set(connections):
                raise _bad_request(f"该连接不支持指定模型：{pid}")
            if model != model.strip() or len(model) > 200 or any(ord(c) < 32 for c in model):
                raise _bad_request("模型 ID 无效")
        for pid, effort in efforts.items():
            if pid not in (default, *fallbacks):
                raise _bad_request(f"该连接不在角色路由中：{pid}")
            problem = _invalid_effort(pid, effort)
            if problem:
                raise _bad_request(problem)
        for pid in (default, *fallbacks):
            if pid in connections and not models.get(pid, "").strip():
                raise _bad_request("API 连接需要填写模型 ID")

    def _save_custom_agent(agent_id: Optional[str], payload: CustomAgentInput) -> dict:
        file_path = _settings_file_path()
        existing = hub_settings.load(file_path)
        custom = [entry for entry in existing.get(CUSTOM_AGENTS_KEY) or [] if isinstance(entry, dict)]
        registry = get_agent_registry(settings.config_dir)
        if agent_id is None:
            if len(custom) >= MAX_CUSTOM_AGENTS:
                raise _bad_request(f"最多添加 {MAX_CUSTOM_AGENTS} 个自定义角色")
            if payload.id is not None:
                agent_id = payload.id.strip()
                if not _CUSTOM_ID.fullmatch(agent_id):
                    raise _bad_request("角色 ID 需以小写字母开头，只含小写字母、数字、- 或 _，长度 2–32")
                if registry.get(agent_id) is not None:
                    raise _bad_request(f"角色 ID 已存在：{agent_id}")
            else:
                agent_id = "role-" + secrets.token_hex(3)
                while registry.get(agent_id) is not None:
                    agent_id = "role-" + secrets.token_hex(3)
            index = None
        else:
            if payload.id is not None and payload.id != agent_id:
                raise _bad_request("角色 ID 创建后不能修改")
            index = next((i for i, entry in enumerate(custom) if entry.get("id") == agent_id), None)
            if index is None:
                if registry.get(agent_id) is not None:
                    raise HTTPException(status_code=403, detail="内置角色不能在这里编辑")
                raise HTTPException(status_code=404, detail="unknown agent")
        profile = _clean_custom_agent(payload, registry, agent_id)
        default = payload.default_provider_id
        fallbacks = list(payload.fallback_provider_ids)
        models = {pid: model for pid, model in payload.provider_models.items() if model.strip()}
        efforts = {pid: effort for pid, effort in payload.provider_efforts.items() if effort}
        _validate_route(default, fallbacks, models, efforts, existing.get("connections") or {})
        profile.update(default_provider_id=default, fallback_provider_ids=fallbacks, provider_models=models,
                       provider_efforts=efforts)

        overrides = existing.setdefault("agents", {})
        section = overrides.get(agent_id, {}) if isinstance(overrides.get(agent_id), dict) else {}
        previous = custom[index] if index is not None else {}
        old_fallbacks = section.get("fallback_provider_ids", previous.get("fallback_provider_ids", []))
        # The dialog edits the role's own route; drop table overrides so the
        # saved values are what actually runs.
        for key in _ROUTE_KEYS:
            section.pop(key, None)
        if old_fallbacks != fallbacks or not section.get("fallback_context_approved"):
            section.pop("fallback_context_approved", None)
            section.pop("fallback_context_targets", None)
        if section:
            overrides[agent_id] = section
        else:
            overrides.pop(agent_id, None)
        if index is None:
            custom.append(profile)
        else:
            custom[index] = profile
        existing[CUSTOM_AGENTS_KEY] = custom
        existing.setdefault("schema_version", 1)
        hub_settings.save(file_path, existing)
        return _build_view()

    @router.post("/agents")
    def create_custom_agent(payload: CustomAgentInput):
        with _settings_write_lock:
            return _save_custom_agent(None, payload)

    @router.put("/agents/{agent_id}")
    def update_custom_agent(agent_id: str, payload: CustomAgentInput):
        with _settings_write_lock:
            return _save_custom_agent(agent_id, payload)

    @router.delete("/agents/{agent_id}")
    def delete_custom_agent(agent_id: str):
        with _settings_write_lock:
            file_path = _settings_file_path()
            existing = hub_settings.load(file_path)
            custom = [entry for entry in existing.get(CUSTOM_AGENTS_KEY) or [] if isinstance(entry, dict)]
            remaining = [entry for entry in custom if entry.get("id") != agent_id]
            if len(remaining) == len(custom):
                if get_agent_registry(settings.config_dir).get(agent_id) is not None:
                    raise HTTPException(status_code=403, detail="内置角色不能删除")
                raise HTTPException(status_code=404, detail="unknown agent")
            existing[CUSTOM_AGENTS_KEY] = remaining
            if isinstance(existing.get("agents"), dict):
                existing["agents"].pop(agent_id, None)
            hub_settings.save(file_path, existing)
            return _build_view()

    @router.put("/settings")
    def put_hub_settings(patch: HubSettingsPatch):
        with _settings_write_lock:
            return _put_hub_settings(patch)

    def _put_hub_settings(patch: HubSettingsPatch):
        file_path = _settings_file_path()
        existing = hub_settings.load(file_path)
        previous_connections = existing.get("connections", {})
        try:
            connections, pending_keys = api_connections.prepare_connections(previous_connections, patch.connections or {})
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="invalid connection configuration; verify endpoint, credential action and field values") from None
        valid_ids = _committed_provider_ids() | set(connections)

        if patch.providers:
            for pid in patch.providers:
                if pid not in _committed_provider_ids():
                    raise HTTPException(
                        status_code=400,
                        detail="unknown provider: {pid}".format(pid=pid),
                    )

        if patch.agents:
            registry = get_agent_registry(settings.config_dir)
            valid_agent_ids = set(registry.ids())
            for aid, ov in patch.agents.items():
                if aid not in valid_agent_ids:
                    raise HTTPException(
                        status_code=400,
                        detail="unknown agent: {aid}".format(aid=aid),
                    )
                if ov.default_provider_id is not None and ov.default_provider_id not in valid_ids:
                    raise HTTPException(
                        status_code=400,
                        detail="unknown provider in agent override: {pid}".format(
                            pid=ov.default_provider_id,
                        ),
                    )
                if ov.provider_models is not None:
                    for pid, model in ov.provider_models.items():
                        if pid not in valid_ids or pid not in {"codex", "claude", "antigravity"} | set(connections):
                            raise HTTPException(status_code=400, detail=f"unsupported model provider: {pid}")
                        if model != model.strip() or len(model) > 200 or any(ord(c) < 32 for c in model):
                            raise HTTPException(status_code=400, detail="invalid model ID")
                if ov.provider_efforts is not None:
                    for pid, effort in ov.provider_efforts.items():
                        problem = _invalid_effort(pid, effort) if pid in valid_ids else f"未知的连接：{pid}"
                        if problem:
                            raise HTTPException(status_code=400, detail=problem)
                if ov.fallback_provider_ids is not None:
                    for pid in ov.fallback_provider_ids:
                        if pid not in valid_ids:
                            raise HTTPException(
                                status_code=400,
                                detail="unknown provider in agent fallback: {pid}".format(pid=pid),
                            )

        existing.setdefault("schema_version", 1)

        if patch.providers:
            providers_section = existing.setdefault("providers", {})
            for pid, ov in patch.providers.items():
                if ov.enabled is not None:
                    providers_section.setdefault(pid, {})["enabled"] = ov.enabled

        if patch.runtime:
            runtime_section = existing.setdefault("runtime", {})
            for field_name in (
                "use_fake_cli",
                "max_turns",
                "max_a2a_invocations",
                "cli_timeout",
                "a2a_idle_timeout",
            ):
                val = getattr(patch.runtime, field_name)
                if val is not None:
                    runtime_section[field_name] = val

        if patch.extensions:
            runtime_section = existing.setdefault("runtime", {})
            for name, field_name in EXTENSION_FIELDS.items():
                val = getattr(patch.extensions, name)
                if val is not None:
                    runtime_section[field_name] = val

        if patch.agents:
            agents_section = existing.setdefault("agents", {})
            for aid, ov in patch.agents.items():
                if ov.clear:
                    agents_section.pop(aid, None)
                    continue
                section = agents_section.setdefault(aid, {})
                if ov.provider_models is not None:
                    section["provider_models"] = dict(ov.provider_models)
                if ov.provider_efforts is not None:
                    section["provider_efforts"] = dict(ov.provider_efforts)
                if ov.default_provider_id is not None:
                    section["default_provider_id"] = ov.default_provider_id
                if ov.fallback_provider_ids is not None:
                    if section.get("fallback_provider_ids") != ov.fallback_provider_ids:
                        section["fallback_context_approved"] = False
                    section["fallback_provider_ids"] = list(ov.fallback_provider_ids)
                if ov.fallback_context_approved is not None:
                    section["fallback_context_approved"] = ov.fallback_context_approved

        registry = get_agent_registry(settings.config_dir)
        for agent in registry.all():
            override = existing.get("agents", {}).get(agent.id, {})
            primary = override.get("default_provider_id", agent.default_provider_id)
            fallbacks = override.get("fallback_provider_ids", agent.fallback_provider_ids)
            models = override.get("provider_models", agent.provider_models)
            for pid in [primary, *fallbacks]:
                if pid.startswith("api-") and pid not in valid_ids:
                    raise HTTPException(status_code=400, detail="connection is still referenced by an agent")
                if pid in connections and not models.get(pid, "").strip():
                    raise HTTPException(status_code=400, detail="API connections require an explicit model ID")
            for pid in fallbacks:
                old, new = previous_connections.get(pid), connections.get(pid)
                if old and new and any(old.get(k) != new.get(k) for k in ("base_url", "protocol", "allow_private")):
                    existing.setdefault("agents", {}).setdefault(agent.id, {})["fallback_context_approved"] = False
        # Approval binds the actual API endpoint/protocol/policy, not just a
        # reusable connection ID. A new fallback always needs renewed consent.
        from backend.app.adapters.fallback_policy import endpoint_binding
        for agent in registry.all():
            section = existing.get("agents", {}).get(agent.id)
            if section is None:
                continue
            fallbacks = section.get("fallback_provider_ids", agent.fallback_provider_ids)
            if section.get("fallback_context_approved"):
                section["fallback_context_targets"] = {pid: endpoint_binding(connections[pid])
                    for pid in fallbacks if pid in connections}
            else:
                section.pop("fallback_context_targets", None)
        staged_credentials = {}
        try:
            for cid, value in pending_keys.items():
                reference = api_connections.write_key(settings, value)
                staged_credentials[cid] = {"key_ref": reference}
                connections[cid]["key_ref"] = reference
            existing["connections"] = connections
            hub_settings.save(file_path, existing)
        except OSError:
            api_connections.retire_keys(settings, staged_credentials, {})
            raise HTTPException(status_code=500, detail="connection configuration could not be saved") from None
        api_connections.retire_keys(settings, previous_connections, connections)
        return _build_view()

    return router
