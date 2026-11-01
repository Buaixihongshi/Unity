"""Read-only index of saved artifacts in ordinary conversations."""
from pathlib import PurePosixPath, PureWindowsPath

from fastapi import APIRouter, Query
from backend.app.api.mission_detail import _derive_artifact_events
from backend.app.domain.models import ConversationStatus, is_provider_verification_mission
from backend.app.storage.repository import Repository


def build_router(repository: Repository) -> APIRouter:
    router = APIRouter(prefix='/api/artifacts', tags=['artifacts'])

    @router.get('')
    def index(q: str = Query('', max_length=200), limit: int = Query(50, ge=1, le=100),
              cursor: int | None = Query(None, ge=1, le=9223372036854775807), include_archived: bool = False):
        conversations = {c.id: c for c in repository.list_conversations()
                         if c.kind == 'chat' and (include_archived or c.status == ConversationStatus.ACTIVE)}
        missions = {m.id: m for m in repository.list_missions()
                    if m.conversation_id in conversations and not is_provider_verification_mission(m)}
        items = []
        before = cursor
        # One latest-identity query per request; keep bounded decoding work.
        batch = repository.list_artifact_events(2001, before)
        exhausted = len(batch) <= 2000
        for event in batch[:2000]:
            before = int(event.id)
            if event.mission_id not in missions:
                continue
            raw_path = event.payload.get('path')
            if not isinstance(raw_path, str) or not raw_path or '\\' in raw_path:
                continue
            path = PurePosixPath(raw_path)
            if path.is_absolute() or PureWindowsPath(raw_path).drive or '..' in path.parts:
                continue
            derived = _derive_artifact_events([event])
            if not derived:
                continue
            saved = derived[0]
            artifact = saved['artifact']
            if q.strip().casefold() not in f"{artifact['title']}\n{artifact['path']}".casefold():
                continue
            conversation = conversations[missions[event.mission_id].conversation_id]
            items.append({**artifact, 'event_id': saved['id'],
                          'conversation_id': conversation.id, 'conversation_title': conversation.title})
            if len(items) == limit:
                exhausted = event is batch[-1]
                break
        return {'items': items, 'next_cursor': None if exhausted else str(before)}

    return router
