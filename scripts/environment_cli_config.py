"""Copy effective CLI settings, never daily conversations or credential stores."""
from __future__ import annotations

import hashlib
import json

from scripts.environment_common import EnvironmentError, home, write_json


RUNTIME_DEFAULTS = {"use_fake_cli": False, "max_turns": 6, "max_a2a_invocations": 8,
                    "cli_timeout": 300, "a2a_idle_timeout": 90}
PROVIDER_FIELDS = {"id", "label", "command", "args", "enabled", "mcp_enabled"}


def runtime_cli_snapshot(checkout, *, real=False):
    source = home(checkout) / "runtime-state"
    paths = [source / "config/cli-providers.json", source / "config/agents.json",
             source / "data/hub_settings.json"]
    try:
        raw = [p.read_bytes() if p.exists() else None for p in paths]
        if raw[0] is None or raw[1] is None:
            raise EnvironmentError("Runtime CLI configuration is missing; configure the daily instance first")
        providers, agents, overrides = [json.loads(value) if value is not None else {} for value in raw]
        entries = agents["agents"] if isinstance(agents, dict) else agents
        if not isinstance(providers, list) or not providers or not isinstance(entries, list) or not entries:
            raise ValueError("Invalid configuration shape")
        exported = []
        for provider in providers:
            item = {k: v for k, v in provider.items() if k in PROVIDER_FIELDS}
            item["enabled"] = overrides.get("providers", {}).get(item["id"], {}).get("enabled", item.get("enabled", False))
            if not isinstance(item["enabled"], bool) or not isinstance(item.get("command"), str):
                raise ValueError("Invalid provider")
            if real and item["id"] == "fake":
                item["enabled"] = False
            exported.append(item)
        ids = {p["id"] for p in exported}
        if len(ids) != len(exported): raise ValueError("Duplicate provider")
        route_ids = ids - {"fake"} if real else ids
        routes = {}
        for agent in entries:
            override = overrides.get("agents", {}).get(agent["id"], {})
            primary = override.get("default_provider_id", agent["default_provider_id"])
            fallbacks = override.get("fallback_provider_ids", agent.get("fallback_provider_ids", []))
            if primary not in route_ids or not isinstance(fallbacks, list) or any(p not in route_ids for p in fallbacks):
                raise EnvironmentError("Daily role uses a demo or non-CLI connection; CLI-only inheritance cannot silently change its route")
            routes[agent["id"]] = {"default_provider_id": primary, "fallback_provider_ids": fallbacks,
                "provider_models": {k: v for k, v in override.get("provider_models", agent.get("provider_models", {})).items() if k in ids},
                "provider_efforts": {k: v for k, v in override.get("provider_efforts", agent.get("provider_efforts", {})).items() if k in ids},
                "fallback_context_approved": bool(override.get("fallback_context_approved", False))}
        runtime = {k: overrides.get("runtime", {}).get(k, v) for k, v in RUNTIME_DEFAULTS.items()}
        if not isinstance(runtime["use_fake_cli"], bool): raise ValueError("Invalid runtime mode")
        if real:
            runtime["use_fake_cli"] = False
        if any((p.read_bytes() if p.exists() else None) != value for p, value in zip(paths, raw)):
            raise EnvironmentError("Daily CLI configuration changed while reading; retry without using a mixed snapshot")
        result = {"providers": exported, "agents": routes, "runtime": runtime}
        result["digest"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        return result
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        raise EnvironmentError("Cannot read valid daily CLI settings; no fallback or credential copy was performed") from exc


def inherit_runtime_cli(checkout, state, *, real=False):
    if state.resolve() == (home(checkout) / "runtime-state").resolve():
        raise EnvironmentError("CLI inheritance must not write to the daily state")
    snapshot = runtime_cli_snapshot(checkout, real=real)
    agents = json.loads((state / "config/agents.json").read_text())
    target_ids = {a["id"] for a in (agents["agents"] if isinstance(agents, dict) else agents)}
    if target_ids != set(snapshot["agents"]):
        raise EnvironmentError("Daily and candidate role IDs differ; resolve role mapping explicitly")
    path = state / "data/hub_settings.json"
    target = json.loads(path.read_text()) if path.exists() else {"schema_version": 1}
    target.setdefault("runtime", {}).update(snapshot["runtime"], maintenance_enabled=False)
    for aid, route in snapshot["agents"].items():
        target.setdefault("agents", {}).setdefault(aid, {}).update(route)
        target["agents"][aid].pop("fallback_context_targets", None)
    for provider in snapshot["providers"]:
        target.setdefault("providers", {}).setdefault(provider["id"], {})["enabled"] = provider["enabled"]
    # Source API connections, key references, login files and database are absent
    # from this allowlisted snapshot. Existing target-local connections remain local.
    write_json(state / "config/cli-providers.json", snapshot["providers"])
    write_json(path, target)
    write_json(state / "runtime/cli-source.json", {"source": "runtime", "digest": snapshot["digest"]})
    return snapshot
