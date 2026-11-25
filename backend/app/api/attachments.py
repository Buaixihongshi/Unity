import asyncio
import tempfile
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from backend.app.attachments.limits import MAX_IMAGE_BYTES, public_limits
from backend.app.attachments.store import AttachmentError, AttachmentStore


def build_router(repository):
    router = APIRouter(tags=['attachments'])
    store = AttachmentStore(repository)
    decode_slots = asyncio.Semaphore(2)

    @router.get('/api/config/attachments')
    def limits():
        return public_limits()

    @router.post('/api/conversations/{conversation_id}/attachments', status_code=201)
    async def upload(conversation_id: str, request: Request):
        if repository.get_conversation(conversation_id) is None:
            raise AttachmentError('ATTACHMENT_INVALID', '会话不存在', 404)
        directory = store.directory(conversation_id)
        store.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.upload-', delete=False) as stream:
                temp = Path(stream.name)
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise AttachmentError('ATTACHMENT_TOO_LARGE', '图片超过 3.75 MiB', 413, limit_bytes=MAX_IMAGE_BYTES)
                    stream.write(chunk)
            async with decode_slots:
                return await asyncio.to_thread(store.register, conversation_id, temp,
                    request.headers.get('content-type', '').split(';')[0].strip().lower(),
                    unquote(request.headers.get('x-attachment-name', '图片')))
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    @router.get('/api/attachments/{attachment_id}/content')
    def content(attachment_id: str, conversation_id: str):
        item = store.get(attachment_id)
        if not item or item['conversation_id'] != conversation_id or repository.get_conversation(conversation_id) is None:
            raise AttachmentError('ATTACHMENT_INVALID', '附件不存在', 404)
        return FileResponse(store.path(item), media_type=item['mime'],
                            headers={'X-Content-Type-Options': 'nosniff', 'Content-Disposition': 'inline',
                                     'Cache-Control': 'private, no-store'})

    return router
