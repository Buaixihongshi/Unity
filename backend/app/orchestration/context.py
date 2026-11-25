import re
from typing import Dict, Optional

from backend.app import host_rules
from backend.app.domain.agent_registry import AgentRegistry, get_agent_registry
from backend.app.domain.history import prioritize_messages
from backend.app.domain.models import MessageRole, is_provider_verification_mission
from backend.app.orchestration.revision import revised_messages
from backend.app.storage.repository import Repository

_WHITESPACE_RUN = re.compile(r"\s+")


def build_conversation_context(
    repository: Repository,
    conversation_id: Optional[str],
    *,
    mission_id: Optional[str] = None,
    limit: int = 8,
    max_chars_per_message: int = 1200,
    max_chars: int = 6000,
    reply_to_turn_id: Optional[str] = None,
    agent_registry: Optional[AgentRegistry] = None,
) -> str:
    if limit <= 0 or max_chars_per_message <= 0:
        return ""
    history = repository.read_message_history(
        conversation_id=conversation_id, mission_id=None if conversation_id else mission_id,
    )
    entries = history.project()
    messages = [entry.message for entry in entries]
    if not messages:
        return ""
    registry = agent_registry or get_agent_registry()
    revised = revised_messages(history.events)
    answered_old = set(revised.values())
    header = "最近对话：\n"
    remaining = max_chars - len(header)
    lines: dict[str, str] = {}
    # Allocate space in priority order; display in event order. The mission goal
    # remains a separate, unabridged prompt field in _build_prompt.
    for msg in prioritize_messages(entries, reply_to_turn_id=reply_to_turn_id)[:limit]:
        speaker = _speaker_label(msg.role, msg.agent_id, registry)
        if msg.id in revised:
            speaker += "（此条已被用户修改，以后面的新版为准）"
        elif msg.role == MessageRole.AGENT and msg.mission_id in answered_old:
            speaker += "（回应的是修改前的说法）"
        text = _WHITESPACE_RUN.sub(" ", msg.content).strip()
        if msg.attachments:
            text += " " + " ".join(f"[图片 {aid}，本任务附件]" if msg.mission_id == mission_id else f"[图片 {aid}，之前已提供]" for aid in msg.attachments)
        prefix, suffix = f"{speaker}：", f" [id={msg.id}]"
        budget = min(max_chars_per_message, remaining - len(prefix) - len(suffix) - 1)
        if budget < len("…[已截断]…"):
            continue
        line = prefix + _truncate(text, budget) + suffix
        lines[msg.id] = line
        remaining -= len(line) + 1
    return header + "\n".join(lines[m.id] for m in messages if m.id in lines) if lines else ""


def _truncate(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    marker = "…[已截断]…"
    available = budget - len(marker)
    head = (available * 2 + 2) // 3
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def build_artifact_index(
    repository: Repository,
    mission_id: str,
    *,
    limit: int = 12,
) -> str:
    current = repository.get_mission(mission_id)
    mission_ids = [mission_id]
    if current and current.conversation_id:
        mission_ids = [
            mission.id
            for mission in repository.list_missions_for_conversation(current.conversation_id)
            if not is_provider_verification_mission(mission)
        ]
    latest: Dict[tuple[str, str], dict] = {}
    for source_id in mission_ids:
        for ev in repository.list_events_after(source_id, 0):
            if ev.event_type != "artifact":
                continue
            path = ev.payload.get("path")
            if not isinstance(path, str) or not path:
                continue
            latest[(source_id, path)] = {
                "path": path,
                "source_mission_id": source_id,
                "agent_id": ev.payload.get("agent_id") or "?",
                "title": ev.payload.get("title") or path,
                "created_at": ev.created_at,
            }
    if not latest:
        return ""
    entries = sorted(
        latest.values(), key=lambda e: e["created_at"], reverse=True
    )[:limit]
    lines = [
        host_rules.ARTIFACT_QUOTE.text,
    ]
    for entry in entries:
        lines.append(
            "- {path}  by {agent}（{title}） source_mission_id={source}".format(
                path=entry["path"],
                source=entry["source_mission_id"],
                agent=entry["agent_id"],
                title=entry["title"],
            )
        )
    return "\n".join(lines)


def _speaker_label(
    role: MessageRole,
    agent_id: Optional[str],
    registry: AgentRegistry,
) -> str:
    if role == MessageRole.USER:
        return "用户"
    if role == MessageRole.SYSTEM:
        return "系统"
    return registry.display_name(agent_id)
