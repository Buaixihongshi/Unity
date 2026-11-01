"""The user paused and rewrote their last message.

The old wording stays in history, marked superseded, so nothing disappears from
the record. The run started by the rewrite carries a handover: what changed,
who had already answered the old wording, how far the stopped turn got, and who
never spoke, so the next turn neither repeats work nor builds on a dropped premise.
"""
from typing import Dict, Optional

from backend.app.domain.errors import DiscussionError
from backend.app.domain.models import Message, MessageRole, Mission
from backend.app.orchestration.interruption import INTERRUPTED_EVENT
from backend.app.storage.repository import Repository

REVISED_EVENT = "message.revised"
MAX_QUOTE_CHARS = 1200


def original_message(repository: Repository, conversation_id: str, message_id: str) -> Message:
    """Only the latest user message of the conversation can be rewritten."""
    messages = repository.list_messages_for_conversation(conversation_id)
    users = [m for m in messages if m.role == MessageRole.USER]
    if not users or users[-1].id != message_id:
        raise DiscussionError("只能修改会话里最后一条用户发言", 409)
    return users[-1]


def record(repository: Repository, mission: Mission, original: Message) -> None:
    repository.add_event(mission.id, REVISED_EVENT, {
        "message_id": original.id,
        "previous_mission_id": original.mission_id,
        "original_content": original.content,
    })


def revised_messages(events) -> Dict[str, str]:
    """Superseded user message id -> the mission that answered the old wording."""
    return {e.payload["message_id"]: e.payload.get("previous_mission_id", "")
            for e in events if e.event_type == REVISED_EVENT}


def _quote(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_QUOTE_CHARS else text[:MAX_QUOTE_CHARS] + "…"


def handover_note(repository: Repository, mission: Mission, registry) -> str:
    revisions = [e.payload for e in repository.list_events_after(mission.id, 0) if e.event_type == REVISED_EVENT]
    if not revisions:
        return ""
    revision = revisions[-1]
    previous_id = revision.get("previous_mission_id") or ""
    replied = list(dict.fromkeys(
        registry.display_name(m.agent_id) for m in repository.list_messages(previous_id)
        if m.role == MessageRole.AGENT and m.agent_id))
    interrupted: Optional[dict] = next(
        (e.payload for e in reversed(repository.list_events_after(previous_id, 0))
         if e.event_type == INTERRUPTED_EVENT), None)
    lines = [
        "【交接：用户修改了上一条发言】",
        "原话：" + _quote(revision.get("original_content", "")),
        "改为：" + _quote(mission.goal),
        "修改前已回应：" + "、".join(replied) if replied else "修改前还没有人回应。",
    ]
    if interrupted and interrupted.get("agent_id"):
        partial = _quote(interrupted.get("partial_text") or "")
        stopped = "被中止的回合：" + registry.display_name(interrupted["agent_id"])
        lines.append(stopped + ("，中止时已输出：「{}」".format(partial) if partial else "，中止前没有输出。"))
    remaining = [registry.display_name(a) for a in (interrupted or {}).get("remaining_agent_ids") or []]
    if remaining:
        lines.append("原计划尚未发言：" + "、".join(remaining))
    lines.append("以修改后的说法为准。修改前的回应只针对旧说法：已核实的事实可以引用，"
                 "不要沿用已作废的前提，也不要重复已经完成的内容。")
    return "\n".join(lines)
