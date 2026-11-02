"""Global observability: every conversation in one payload.

`service.build_payload` answers "what happened in this mission". This module
answers the settings-page question: "what has the whole hub been doing", across
conversations, optionally limited to a time window.

The scan is deliberate and local: the event log is small (hundreds of turns),
so recomputing on every read keeps the numbers honest and needs no cache
invalidation. If the log grows, add a cache here rather than a second source of
truth.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.app.observability import checks as checks_module
from backend.app.observability import pricing
from backend.app.observability.turns import Turn, build_turns
from backend.app.storage.repository import Repository

WINDOWS = ("today", "7d", "30d", "all")
DEFAULT_RECENT = 10


def window_start(window: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """UTC start of the window, or None for "all"."""
    now = (now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # "Today" means the operator's today, not UTC's: this app runs on a laptop.
    now = now.astimezone()
    if window == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "7d":
        return now - timedelta(days=7)
    if window == "30d":
        return now - timedelta(days=30)
    return None


def _at(turn: Turn) -> Optional[datetime]:
    if not turn.started_at:
        return None
    try:
        value = datetime.fromisoformat(turn.started_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _in_window(turn: Turn, start: Optional[datetime]) -> bool:
    if start is None:
        return True
    at = _at(turn)
    return at is not None and at >= start


def _daily(turns: list[Turn], now: datetime, days: int = 14) -> list[dict]:
    buckets: dict[str, dict[str, float]] = {}
    for turn in turns:
        at = _at(turn)
        if at is None:
            continue
        key = at.astimezone().date().isoformat()
        bucket = buckets.setdefault(key, {"turns": 0, "cost": 0.0})
        bucket["turns"] += 1
        bucket["cost"] += turn.cost_usd or 0.0
    out = []
    for offset in range(days - 1, -1, -1):
        key = (now.date() - timedelta(days=offset)).isoformat()
        row = buckets.get(key, {"turns": 0, "cost": 0.0})
        out.append({"date": key, "turns": int(row["turns"]), "cost": round(row["cost"], 2)})
    return out


def _coverage(turns: list[Turn]) -> dict:
    tool_calls = sum(len(t.tools) for t in turns)
    outputs = sum(1 for t in turns for s in t.tools if s.output.strip())
    return {
        "turns": len(turns),
        "native_calls": sum(1 for t in turns for s in t.steps if s.kind == "native"),
        "native_rate": round(sum(1 for t in turns if t.native_visible) / len(turns), 3) if turns else None,
        "tool_calls": tool_calls,
        "output_rate": round(outputs / tool_calls, 3) if tool_calls else None,
        "cost_known_rate": round(sum(1 for t in turns if t.cost_known) / len(turns), 3) if turns else None,
    }


def _spend(turns: list[Turn]) -> dict:
    by_provider: dict[str, dict[str, float]] = {}
    total = reported = estimated = 0.0
    unknown = 0
    for turn in turns:
        provider = turn.provider_id or "unknown"
        bucket = by_provider.setdefault(provider, {"usd": 0.0, "turns": 0, "unknown": 0, "tokens": 0})
        bucket["turns"] += 1
        bucket["tokens"] += pricing.total_tokens(turn.usage)
        if turn.cost_usd is None:
            unknown += 1
            bucket["unknown"] += 1
            continue
        total += turn.cost_usd
        bucket["usd"] += turn.cost_usd
        if turn.cost_source == "provider":
            reported += turn.cost_usd
        else:
            estimated += turn.cost_usd
    return {
        "total": round(total, 2),
        "reported": round(reported, 2),
        "estimated": round(estimated, 2),
        "unknown_turns": unknown,
        "by_provider": [
            {"provider": provider, "usd": round(row["usd"], 2),
             "turns": int(row["turns"]), "unknown": int(row["unknown"]),
             "tokens": int(row["tokens"])}
            for provider, row in sorted(by_provider.items(), key=lambda kv: -kv[1]["usd"])
        ],
    }


def _by_agent(turns: list[Turn]) -> list[dict]:
    buckets: dict[str, dict[str, float]] = {}
    for turn in turns:
        agent = turn.agent_id or "unknown"
        bucket = buckets.setdefault(agent, {"turns": 0, "tokens": 0, "cost": 0.0, "tools": 0, "native": 0,
                                            "blocked": 0, "blocked_environment": 0, "blocked_user": 0})
        bucket["turns"] += 1
        # Only the agent's own failures count as "blocked"; the environment's
        # and the user's are shown beside it, never mixed in.
        owner = turn.attribution
        if owner == "agent":
            bucket["blocked"] += 1
        elif owner:
            bucket[f"blocked_{owner}"] += 1
        bucket["tokens"] += pricing.total_tokens(turn.usage)
        bucket["cost"] += turn.cost_usd or 0.0
        bucket["tools"] += len(turn.tools)
        bucket["native"] += sum(1 for s in turn.steps if s.kind == "native")
    return [
        {"agent": agent, "turns": int(row["turns"]), "tokens": int(row["tokens"]),
         "cost": round(row["cost"], 2), "tools": int(row["tools"]), "native": int(row["native"]),
         "blocked": int(row["blocked"]), "blocked_environment": int(row["blocked_environment"]),
         "blocked_user": int(row["blocked_user"])}
        for agent, row in sorted(buckets.items(), key=lambda kv: -kv[1]["turns"])
    ]


def _recent(turns: list[Turn], limit: int, agent_names: dict[str, str]) -> list[dict]:
    ordered = sorted([t for t in turns if t.started_at], key=lambda t: t.started_at or "", reverse=True)
    out = []
    for turn in ordered[: max(limit, 0)]:
        out.append({
            "turn_id": turn.turn_id,
            "mission_id": turn.mission_id,
            "mission": turn.mission_title,
            "agent": turn.agent_id,
            "agent_name": agent_names.get(turn.agent_id, ""),
            "provider": turn.provider_id,
            "model": turn.model,
            "status": turn.status,
            "duration_ms": turn.duration_ms,
            "tokens": pricing.total_tokens(turn.usage),
            "cost": round(turn.cost_usd, 2) if turn.cost_usd is not None else None,
            "checks": turn.checks,
            "at": turn.started_at,
        })
    return out


def _failures(turns: list[Turn], limit: int = 8) -> list[dict]:
    out = []
    for turn in turns:
        for check in turn.checks:
            if check.get("value") == 0:
                out.append({"check": check["name"], "layer": check.get("layer", "agent"), "note": check["note"],
                            "agent": turn.agent_id, "turn_id": turn.turn_id,
                            "mission": turn.mission_title, "at": turn.started_at})
    return sorted(out, key=lambda row: row.get("at") or "", reverse=True)[:limit]


def collect_overview(
    repository: Repository,
    *,
    window: str = "all",
    recent: int = DEFAULT_RECENT,
    now: Optional[datetime] = None,
    agent_names: Optional[dict[str, str]] = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    window = window if window in WINDOWS else "all"
    start = window_start(window, now)

    turns: list[Turn] = []
    conversation_ids: set[str] = set()
    mission_ids: set[str] = set()
    for mission in repository.list_missions():
        mission_turns = build_turns(
            repository.list_events_after(mission.id, 0),
            repository.list_messages(mission.id),
            mission,
        )
        for turn in mission_turns:
            if not _in_window(turn, start):
                continue
            turn.checks = checks_module.run_checks(turn)
            turns.append(turn)
            mission_ids.add(mission.id)
            if mission.conversation_id:
                conversation_ids.add(mission.conversation_id)

    names = agent_names or {}
    summary = checks_module.summarize(turns)
    checks = [
        {"name": row["name"], "layer": row["layer"], "health": row["health"], "what": row["what"],
         "pass": row["passed"], "fail": row["failed"], "na": row["na"], "rate": row["pass_rate"]}
        for row in summary["checks"]
    ]
    coverage = _coverage(turns)
    spend = _spend(turns)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window": window,
        "totals": {
            "conversations": len(conversation_ids),
            "missions": len(mission_ids),
            "turns": len(turns),
            "tool_calls": coverage["tool_calls"],
            "native_calls": coverage["native_calls"],
            "tokens": sum(pricing.total_tokens(t.usage) for t in turns),
            "cost": spend["total"],
        },
        "checks": checks,
        "turns_passed": summary["turns_passed"],
        "coverage": coverage,
        "spend": spend,
        "by_agent": _by_agent(turns),
        "by_provider": spend["by_provider"],
        "daily": _daily(turns, now),
        "recent_turns": _recent(turns, recent, names),
        "failures": _failures(turns),
    }
