"""One creation contract for the standalone and conversation endpoints."""
import json
import re
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, StrictInt, model_validator

from backend.app import extension_points
from backend.app.domain.agent_registry import get_agent_registry
from backend.app.domain.errors import DiscussionError
from backend.app.domain.models import Conversation, Message, MessageRole, Mission, MissionStatus, utc_now
from backend.app.orchestration import interruption, revision


class DiscussionRequest(BaseModel):
    protocol: Literal['roundtable_v1', 'deliberation_v2'] = 'roundtable_v1'
    participant_ids: Optional[list[str]] = Field(default=None, min_length=2, max_length=3)
    max_turns: Optional[StrictInt] = Field(default=None, ge=3, le=12)
    # V2 enforces 2–3 in the validator; V1 keeps the legacy 1–3 range.
    max_exchange_rounds: StrictInt = Field(default=2, ge=1, le=3)

    @model_validator(mode='after')
    def validate_participants(self):
        if self.participant_ids is not None and len(set(self.participant_ids)) != len(self.participant_ids):
            raise ValueError('Discussion participants must be unique')
        if self.protocol == 'deliberation_v2' and self.max_turns is not None:
            raise ValueError('V2 does not accept max_turns; use max_exchange_rounds')
        if self.protocol == 'deliberation_v2' and self.max_exchange_rounds < 2:
            raise ValueError('V2 requires 2–3 exchange rounds')
        if self.max_turns is not None and self.participant_ids is not None and self.max_turns < len(self.participant_ids) + 1:
            raise ValueError('Reserve an opening turn for everyone and a summary turn')
        return self


class MissionRequest(BaseModel):
    goal: str
    attachment_ids: Optional[list[str]] = None
    skill: Optional[Literal['discuss']] = None
    title: Optional[str] = None
    target_agent_id: Optional[str] = None
    target_agent_ids: Optional[list[str]] = None
    room_mode: Literal['task', 'roundtable'] = 'task'
    discussion: Optional[DiscussionRequest] = None
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    work_mode: Optional[Literal['document', 'development', 'online']] = None
    # Rewrites the conversation's last user message after a pause.
    revises_message_id: Optional[str] = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode='before')
    @classmethod
    def normalize_skill(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        goal = data.get('goal')
        if isinstance(goal, str):
            command = re.match(r'^/discuss(?:\s|$)', goal.lstrip())
            if command:
                if data.get('skill') not in (None, 'discuss'):
                    raise ValueError('Unknown project skill')
                data['skill'] = 'discuss'
                data['goal'] = goal.lstrip()[command.end():].strip()
        if data.get('skill') == 'discuss':
            if data.get('target_agent_id') is not None or data.get('target_agent_ids') is not None:
                raise ValueError('Discuss skill uses discussion.participant_ids, not target agents')
            if data.get('room_mode') not in (None, 'task', 'roundtable'):
                raise ValueError('Discuss skill requires roundtable mode')
            discussion = data.get('discussion')
            if isinstance(discussion, DiscussionRequest):
                discussion = discussion.model_dump()
            if discussion is None:
                discussion = {}
            if isinstance(discussion, dict):
                discussion = dict(discussion)
                if discussion.get('protocol', 'deliberation_v2') != 'deliberation_v2':
                    raise ValueError('Discuss skill requires deliberation_v2, not roundtable_v1')
                discussion['protocol'] = 'deliberation_v2'
            data.update(room_mode='roundtable', discussion=discussion)
        return data

    @model_validator(mode='after')
    def validate_mode(self):
        if self.room_mode == 'roundtable':
            if self.work_mode is not None:
                raise ValueError('Discussion does not authorize autonomous execution')
            if self.discussion is None:
                raise ValueError('Roundtable requires discussion configuration')
            if self.discussion.participant_ids is None and self.skill != 'discuss':
                raise ValueError('Roundtable requires discussion.participant_ids')
            if self.target_agent_id is not None or self.target_agent_ids is not None:
                raise ValueError('Use discussion.participant_ids for roundtables')
            if not self.goal.strip() and not self.attachment_ids:
                raise ValueError('Discussion goal must not be empty')
            if self.discussion.protocol == 'deliberation_v2' and len(self.goal) > 8000:
                raise ValueError('V2 goal exceeds 8000 characters; provide supporting material separately')
        elif self.discussion is not None:
            raise ValueError('Task mode does not accept discussion configuration')
        return self


class CreateMissionRequest(MissionRequest):
    conversation_id: Optional[str] = None


def occupies_conversation(mission):
    return mission.status in {MissionStatus.PENDING, MissionStatus.RUNNING, MissionStatus.AWAITING_INPUT} or (
        extension_points.any('occupies_conversation', mission))


def ensure_available(repo, conversation):
    if conversation.status == 'archived':
        raise DiscussionError('Conversation is archived; restore it before creating a mission')
    interruption.recover(repo, repo.list_missions_for_conversation(conversation.id))
    if any(occupies_conversation(m) for m in repo.list_missions_for_conversation(conversation.id)):
        raise DiscussionError('Conversation already has an active mission')


def _normalize_legacy_fingerprint(fingerprint):
    """Only V1 records predate these two discussion default fields."""
    fingerprint = dict(fingerprint)
    for optional in ('work_mode', 'revises_message_id', 'attachment_ids'):
        if fingerprint.get(optional) is None:
            fingerprint.pop(optional, None)
    discussion = fingerprint.get('discussion')
    if (fingerprint.get('room_mode') != 'roundtable' or fingerprint.get('skill') is not None
            or not isinstance(discussion, dict) or discussion.get('protocol', 'roundtable_v1') != 'roundtable_v1'):
        return fingerprint
    normalized = {**fingerprint, 'discussion': dict(discussion)}
    normalized['discussion'].setdefault('protocol', 'roundtable_v1')
    normalized['discussion'].setdefault('max_exchange_rounds', 2)
    return normalized


def create_requested_mission(repo, settings, payload, conversation_id=None):
    scope = f'conversation:{conversation_id}' if conversation_id else 'new_conversation'
    # Existing idempotency records predate the optional skill field.
    excluded = {'conversation_id', 'skill'} if payload.skill is None else {'conversation_id'}
    if payload.attachment_ids is None:
        excluded.add('attachment_ids')
    if payload.work_mode is None:
        excluded.add('work_mode')
    if payload.revises_message_id is None:
        excluded.add('revises_message_id')
    fingerprint = payload.model_dump(exclude=excluded)
    with repo._transaction(immediate=True) as db:
        if payload.request_id:
            row = db.execute("SELECT mission_id, payload FROM events WHERE event_type = 'mission.request_accepted' AND json_extract(payload, '$.scope') = ? AND json_extract(payload, '$.request_id') = ?", (scope, payload.request_id)).fetchone()
            if row:
                original = json.loads(row['payload'])['request']
                if _normalize_legacy_fingerprint(original) != _normalize_legacy_fingerprint(fingerprint):
                    raise DiscussionError('request_id was already used with different input')
                mission = repo.get_mission(row['mission_id'])
                return mission, repo.get_conversation(mission.conversation_id), False
        from backend.app.settings import EXTENSION_DISABLED
        if payload.discussion and not settings.discussion_enabled:
            raise DiscussionError(EXTENSION_DISABLED['discussion'], 409)
        if payload.work_mode and not settings.tasks_enabled:
            raise DiscussionError(EXTENSION_DISABLED['tasks'], 409)
        registry = get_agent_registry(settings.config_dir)
        valid_ids = set(registry.ids())
        targets = payload.target_agent_ids or ([payload.target_agent_id] if payload.target_agent_id else [])
        if any(t not in valid_ids for t in targets):
            raise DiscussionError('Invalid target_agent_id(s): ' + ', '.join(t for t in targets if t not in valid_ids), 400)
        config = None
        if payload.discussion:
            config = extension_points.first('mission_prepare', repo, settings, payload, registry, valid_ids)
            if config is None:
                raise DiscussionError(EXTENSION_DISABLED['discussion'], 409)
        if conversation_id:
            conversation = repo.get_conversation(conversation_id)
            if conversation is None:
                raise DiscussionError('Conversation not found', 404)
            ensure_available(repo, conversation)
        else:
            if payload.revises_message_id:
                raise DiscussionError('只能在已有会话里修改发言', 400)
            conversation = Conversation(id=f'c_{uuid4().hex[:12]}', title=payload.title or payload.goal[:40])
            repo.create_conversation(conversation)
        if payload.room_mode == 'task' and not payload.work_mode:
            if any(t.get('active_mission_id') or t.get('release', {}).get('status') == 'running'
                   for row in db.execute('SELECT data FROM work_boards') for t in json.loads(row['data'])['tasks']):
                raise DiscussionError('工作区正在执行共享工作，请等待当前动作结束或在工作卡中暂停后续动作')
        original = None
        if payload.revises_message_id:
            if payload.room_mode != 'task' or payload.discussion or payload.work_mode or payload.skill:
                raise DiscussionError('讨论和任务不支持修改发言', 400)
            original = revision.original_message(repo, conversation.id, payload.revises_message_id)
        mission = Mission(id=f'm_{uuid4().hex}', title=payload.title or payload.goal, goal=payload.goal,
                          conversation_id=conversation.id, room_mode=payload.room_mode, discussion=config,
                          skill=payload.skill,
                          target_agent_id=targets[0] if targets else None, target_agent_ids=targets)
        repo.create_mission(mission)
        mission = extension_points.first('mission_created', repo, settings, payload, mission, conversation,
                                         targets, registry) or mission
        attachment_ids = payload.attachment_ids if payload.attachment_ids is not None else (original.attachments if original else [])
        repo.add_message(Message(id=f'msg_{uuid4().hex}', mission_id=mission.id, conversation_id=conversation.id,
                                 role=MessageRole.USER, content=payload.goal, attachments=attachment_ids),
                         **({"revises_message_id": original.id} if original else {}))
        if original is not None:
            revision.record(repo, mission, original)
        conversation = conversation.model_copy(update={'updated_at': utc_now()})
        repo.update_conversation(conversation)
        if payload.request_id:
            repo.add_event(mission.id, 'mission.request_accepted', {'scope': scope, 'request_id': payload.request_id, 'request': fingerprint})
        return mission, conversation, True
