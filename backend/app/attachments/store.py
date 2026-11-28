import hashlib
import json
import re
import shutil
import warnings
from datetime import timedelta
import time
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 40_000_000

from backend.app.attachments.limits import (
    MAX_EDGE, MAX_IMAGE_BYTES, MAX_MESSAGE_IMAGES, MAX_MISSION_ENCODED_BYTES,
    MAX_MISSION_IMAGES, MAX_PIXELS, MIME_EXTENSIONS, encoded_size,
)
from backend.app.domain.models import utc_now


class AttachmentError(ValueError):
    def __init__(self, code, message, status=422, **details):
        super().__init__(message)
        self.status = status
        self.body = dict(code=code, message=message, **details)


def decode_image(path, declared):
    with path.open('rb') as stream:
        head = stream.read(16)
    mime = ('image/png' if head.startswith(b'\x89PNG\r\n\x1a\n') else
            'image/jpeg' if head.startswith(b'\xff\xd8\xff') else
            'image/gif' if head.startswith((b'GIF87a', b'GIF89a')) else
            'image/webp' if head[:4] == b'RIFF' and head[8:12] == b'WEBP' else None)
    if mime is None or mime != declared:
        raise AttachmentError('ATTACHMENT_UNSUPPORTED_TYPE', '仅支持 PNG、JPEG、WebP 和静态 GIF，文件类型须与内容一致', 415)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                if width > MAX_EDGE or height > MAX_EDGE or width * height > MAX_PIXELS:
                    raise AttachmentError('ATTACHMENT_DIMENSIONS_EXCEEDED', '图片尺寸超限',
                                          width=width, height=height, max_edge=MAX_EDGE, max_pixels=MAX_PIXELS)
                if getattr(image, 'n_frames', 1) > 1:
                    raise AttachmentError('ATTACHMENT_ANIMATED', '暂不支持动图', 415)
                if Image.MIME.get(image.format) != mime:
                    raise AttachmentError('ATTACHMENT_UNSUPPORTED_TYPE', '图片类型不一致', 415)
                image.verify()
            with Image.open(path) as image:
                image.load()
        return mime, width, height
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AttachmentError('ATTACHMENT_DIMENSIONS_EXCEEDED', '图片像素超限', max_pixels=MAX_PIXELS) from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        if isinstance(exc, AttachmentError):
            raise
        raise AttachmentError('ATTACHMENT_DECODE_FAILED', '图片已损坏，无法解码') from None


class AttachmentStore:
    def __init__(self, repository):
        self.repo = repository
        self.root = repository.database_path.parent / 'attachments'

    def directory(self, conversation_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', conversation_id):
            raise AttachmentError('ATTACHMENT_INVALID', '附件会话无效')
        directory = self.root / conversation_id
        if self.root.is_symlink() or directory.is_symlink():
            raise AttachmentError('ATTACHMENT_INVALID', '附件存储无效')
        return directory

    def get(self, attachment_id, db=None):
        if db is None:
            with self.repo._transaction() as connection:
                return self.get(attachment_id, connection)
        row = db.execute('SELECT data FROM attachments WHERE id = ?', (attachment_id,)).fetchone()
        return json.loads(row['data']) if row else None

    def path(self, item):
        if not re.fullmatch(r'[0-9a-f]{64}', item['sha256']) or item['mime'] not in MIME_EXTENSIONS:
            raise AttachmentError('ATTACHMENT_INVALID', '附件记录无效')
        path = self.directory(item['conversation_id']) / (item['sha256'] + '.' + MIME_EXTENSIONS[item['mime']])
        if path.is_symlink() or not path.is_file():
            raise AttachmentError('ATTACHMENT_INVALID', '图片文件已不可用')
        return path

    def view(self, item):
        return {**{k: item[k] for k in ('id', 'mime', 'size', 'width', 'height', 'display_name')},
                'url': f"/api/attachments/{item['id']}/content?conversation_id={item['conversation_id']}"}

    def register(self, conversation_id, temp, declared, display_name):
        size = temp.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise AttachmentError('ATTACHMENT_TOO_LARGE', '图片超过 3.75 MiB', 413, limit_bytes=MAX_IMAGE_BYTES)
        mime, width, height = decode_image(temp, declared)
        digest = hashlib.sha256(temp.read_bytes()).hexdigest()
        name = re.sub(r'[\x00-\x1f\x7f/\\]', '_', display_name or '图片')[:120]
        item = dict(id='att_' + uuid4().hex, conversation_id=conversation_id, sha256=digest,
                    size=size, mime=mime, width=width, height=height, display_name=name,
                    message_id=None, created_at=utc_now().isoformat())
        with self.repo._transaction(immediate=True) as db:
            if self.repo.get_conversation(conversation_id) is None:
                raise AttachmentError('ATTACHMENT_INVALID', '会话不存在', 404)
            directory = self.directory(conversation_id)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = directory / (digest + '.' + MIME_EXTENSIONS[mime])
            created = not target.exists()
            if target.is_symlink():
                raise AttachmentError('ATTACHMENT_INVALID', '附件存储无效')
            try:
                if created:
                    temp.replace(target)
                db.execute('INSERT INTO attachments VALUES (?, ?, ?)',
                           (item['id'], conversation_id, json.dumps(item)))
            except Exception:
                if created:
                    target.unlink(missing_ok=True)
                raise
        return self.view(item)

    def bind(self, message, db, *, revises_message_id=None):
        ids = message.attachments
        if len(ids) > MAX_MESSAGE_IMAGES:
            raise AttachmentError('ATTACHMENT_COUNT_EXCEEDED', '每条消息最多 4 张图片')
        if len(set(ids)) != len(ids):
            raise AttachmentError('ATTACHMENT_INVALID', '附件不能重复')
        source = db.execute('SELECT data FROM messages WHERE id = ?', (revises_message_id,)).fetchone() if revises_message_id else None
        original = json.loads(source['data']) if source else {}
        reusable = set(original.get('attachments', [])) if original.get('conversation_id') == message.conversation_id else set()
        images = []
        for aid in ids:
            item = self.get(aid, db)
            if (not item or item['conversation_id'] != message.conversation_id or
                    (item['message_id'] is not None and aid not in reusable)):
                raise AttachmentError('ATTACHMENT_INVALID', '附件不存在、不属于本会话或已被使用')
            self.path(item)
            images.append(item)
        used = self.for_mission(message.mission_id)
        count = len(used) + len(images)
        total = sum(encoded_size(i['size']) for i in [*used, *images])
        if count > MAX_MISSION_IMAGES or total > MAX_MISSION_ENCODED_BYTES:
            raise AttachmentError('MISSION_ATTACHMENT_BUDGET_EXCEEDED', '本任务最多 8 张图片，base64 后合计最多 16 MiB',
                                  used_count=len(used), used_encoded_bytes=sum(encoded_size(i['size']) for i in used),
                                  max_count=MAX_MISSION_IMAGES, max_encoded_bytes=MAX_MISSION_ENCODED_BYTES)
        for item in images:
            if item['message_id'] is None:
                item['message_id'] = message.id
                db.execute('UPDATE attachments SET data = ? WHERE id = ?', (json.dumps(item), item['id']))

    def for_mission(self, mission_id, *, message_ids=None):
        images = []
        for message in self.repo.list_messages(mission_id):
            if message_ids is not None and message.id not in message_ids:
                continue
            for aid in message.attachments:
                item = self.get(aid)
                if not item or item['conversation_id'] != message.conversation_id:
                    raise AttachmentError('ATTACHMENT_INVALID', '消息附件不可用')
                images.append(item)
        return images

    def cleanup(self):
        cutoff = (utc_now() - timedelta(hours=24)).isoformat()
        with self.repo._transaction(immediate=True) as db:
            referenced = {aid for row in db.execute('SELECT data FROM messages')
                          for aid in json.loads(row['data']).get('attachments', [])}
            items = [json.loads(row['data']) for row in db.execute('SELECT data FROM attachments')]
            for item in items:
                if item['message_id'] is None and item['created_at'] < cutoff and item['id'] not in referenced:
                    db.execute('DELETE FROM attachments WHERE id = ?', (item['id'],))
                    if not db.execute("SELECT 1 FROM attachments WHERE conversation_id = ? AND json_extract(data, '$.sha256') = ?",
                                      (item['conversation_id'], item['sha256'])).fetchone():
                        try:
                            self.path(item).unlink()
                        except AttachmentError:
                            pass

            # Recover files left by a process crash before the row commit. Never
            # follow symlinks, and retain every blob still named by a DB record.
            if self.root.is_dir() and not self.root.is_symlink():
                live = {(item['conversation_id'], item['sha256'] + '.' + MIME_EXTENSIONS[item['mime']])
                        for row in db.execute('SELECT data FROM attachments') if (item := json.loads(row['data']))}
                for directory in self.root.iterdir():
                    if not directory.is_dir() or directory.is_symlink():
                        continue
                    for path in directory.iterdir():
                        if path.is_symlink() or not path.is_file() or (directory.name, path.name) in live:
                            continue
                        if (path.name.startswith('.upload-') or re.fullmatch(r'[0-9a-f]{64}\.(png|jpg|gif|webp)', path.name)) and path.stat().st_mtime < time.time() - 86400:
                            path.unlink(missing_ok=True)

    def delete_conversation(self, conversation_id, db):
        db.execute('DELETE FROM attachments WHERE conversation_id = ?', (conversation_id,))
        shutil.rmtree(self.directory(conversation_id), ignore_errors=True)
