from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional, Literal

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover - compatibility for Python < 3.11
    class StrEnum(str, Enum):
        __str__ = str.__str__

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RoomMode(StrEnum):
    TASK = "task"
    ROUNDTABLE = "roundtable"
    GAME = "game"


class TaskComplexity(StrEnum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


class OrchestrationMode(StrEnum):
    """How a task mission is staffed.

    ``AUTO`` is the shipped product behaviour.  The other modes are explicit
    experiment arms: they let one task be run with a single agent, a
    self-reflection loop, a fixed multi-agent workflow, or dynamic A2A routing
    without changing personas or providers.
    """

    AUTO = "auto"
    SINGLE = "single"
    REFLECTION = "reflection"
    FIXED = "fixed"
    DYNAMIC = "dynamic"


class OrchestrationConfig(BaseModel):
    """Optional per-mission orchestration override used by evaluation runs."""

    mode: OrchestrationMode = OrchestrationMode.AUTO
    participant_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_participants(self):
        if len(set(self.participant_ids)) != len(self.participant_ids):
            raise ValueError("orchestration participant_ids must be unique")
        if self.mode in (OrchestrationMode.SINGLE, OrchestrationMode.REFLECTION):
            if len(self.participant_ids) > 1:
                raise ValueError(
                    "single/reflection orchestration accepts at most one participant"
                )
        if self.mode in (OrchestrationMode.FIXED, OrchestrationMode.DYNAMIC):
            if self.participant_ids and len(self.participant_ids) < 2:
                raise ValueError(
                    "fixed/dynamic orchestration needs at least two participants"
                )
        return self


class MissionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"


class MissionKind(StrEnum):
    TASK = "task"
    PROVIDER_VERIFICATION = "provider_verification"


class MessageRole(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class TurnStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class AgentProfile(BaseModel):
    id: str
    display_name: str
    role: str
    persona: str
    default_provider_id: str
    fallback_provider_ids: list[str] = Field(default_factory=list)
    provider_models: dict[str, str] = Field(default_factory=dict)
    provider_efforts: dict[str, str] = Field(default_factory=dict)
    capability_hints: list[str] = Field(default_factory=list)
    trigger_points: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


class CLIProvider(BaseModel):
    id: str
    label: str
    command: str
    args: list[str] = Field(default_factory=list)
    enabled: bool = False
    mcp_enabled: bool = False


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Conversation(BaseModel):
    # Task execution contexts share storage, but are not user chat threads.
    kind: Literal["chat", "task"] = "chat"
    id: str
    title: str
    status: ConversationStatus = ConversationStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DiscussionConfig(BaseModel):
    participant_ids: List[str]
    max_turns: int
    protocol: str = "roundtable_v1"
    max_exchange_rounds: int = 2
    provider_routes: dict[str, list[str]] = Field(default_factory=dict)
    profiles: list[dict[str, Any]] = Field(default_factory=list)
    skill_instructions: Optional[str] = None
    skill_hash: Optional[str] = None


class Mission(BaseModel):
    id: str
    title: str
    goal: str
    skill: Optional[str] = None
    kind: MissionKind = MissionKind.TASK
    room_mode: RoomMode = RoomMode.TASK
    discussion: Optional[DiscussionConfig] = None
    status: MissionStatus = MissionStatus.PENDING
    complexity: TaskComplexity = TaskComplexity.SIMPLE
    conversation_id: Optional[str] = None
    target_agent_id: Optional[str] = None
    target_agent_ids: List[str] = Field(default_factory=list)
    orchestration: Optional[OrchestrationConfig] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: Optional[datetime] = None


def is_provider_verification_mission(mission: Mission) -> bool:
    """Recognise new verification missions and pre-migration local records."""
    if mission.kind == MissionKind.PROVIDER_VERIFICATION:
        return True
    return (
        mission.title.startswith("验证 ")
        and "接入" in mission.title
        and "接入验证" in mission.goal
    )


class Message(BaseModel):
    id: str
    mission_id: str
    role: MessageRole
    content: str
    agent_id: Optional[str] = None
    turn_id: Optional[str] = None
    provider_id: Optional[str] = None
    conversation_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class Turn(BaseModel):
    id: str
    mission_id: str
    agent_id: str
    provider_id: str
    status: TurnStatus = TurnStatus.PENDING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


class Artifact(BaseModel):
    id: str
    mission_id: str
    turn_id: str
    agent_id: str
    path: Path
    title: str
    mime_type: str = "text/markdown"
    created_at: datetime = Field(default_factory=utc_now)


class Interrupt(BaseModel):
    id: str
    mission_id: str
    content: str
    target_agent_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    handled_at: Optional[datetime] = None


class MissionEvent(BaseModel):
    id: str
    mission_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)
