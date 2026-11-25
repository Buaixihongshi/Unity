"""Build image input without putting pixels in prompts, argv or turn records."""
import base64
import hashlib
import json
import shutil

from backend.app.attachments.limits import MIME_EXTENSIONS
from backend.app.attachments.store import AttachmentError, AttachmentStore


def prepare(tools, mission_id, agent_id, turn_id, provider_id, prompt, mode, directory=None):
    store = AttachmentStore(tools.repository)
    from backend.app import extension_points
    mission = tools.repository.get_mission(mission_id)
    message_ids = extension_points.first('attachment_message_ids', tools.repository, mission,
        agent_id, turn_id, provider_id, getattr(tools, 'lease_token', None))
    images = store.for_mission(mission_id, message_ids=message_ids)
    if not images:
        return prompt, []
    payload = dict(turn_id=turn_id, agent_id=agent_id, provider_id=provider_id, mode=mode,
                   images=[{k: item[k] for k in ('id', 'sha256', 'mime', 'size')} for item in images])
    if mode == 'none':
        tools.repository.add_event(mission_id, 'attachment.degraded', {**payload, 'reason': '该连接不支持图片输入'})
        return prompt + f'\n用户附了 {len(images)} 张图片，你当前的连接看不到图片内容，请据此回答或请用户用文字描述。', []
    delivered = []
    for item in images:
        source = store.path(item)
        if directory is not None:
            target = directory / (item['id'] + '.' + MIME_EXTENSIONS[item['mime']])
            shutil.copyfile(source, target)
        else:
            target = source
        if target.stat().st_size != item['size'] or hashlib.sha256(target.read_bytes()).hexdigest() != item['sha256']:
            raise AttachmentError('ATTACHMENT_INVALID', '图片文件已发生变化，请重新上传')
        delivered.append({**item, 'path': target})
    tools.repository.add_event(mission_id, 'attachment.delivery', payload)
    if mode == 'path':
        prompt += '\n本轮附件（图片，按下列顺序读取）：\n' + '\n'.join(
            f"{item['id']}: {item['path']}" for item in delivered)
    else:
        prompt += '\n本轮附图顺序：' + '、'.join(item['id'] for item in delivered)
    return prompt, delivered


def encoded(image):
    data = image['path'].read_bytes()
    if len(data) != image['size'] or hashlib.sha256(data).hexdigest() != image['sha256']:
        raise AttachmentError('ATTACHMENT_INVALID', '图片文件已发生变化，请重新上传')
    return base64.b64encode(data).decode('ascii')


def claude_input(prompt, images):
    content = [{'type': 'image', 'source': {'type': 'base64', 'media_type': item['mime'], 'data': encoded(item)}}
               for item in images]
    content.append({'type': 'text', 'text': prompt})
    return json.dumps({'type': 'user', 'message': {'role': 'user', 'content': content}}) + '\n'


def cli_input(command, basename, images, directory, prompt):
    """command already ends in the prompt. Return argv and optional stdin file."""
    if not images:
        return command, None
    if basename == 'codex':
        return [*command, *[arg for item in images for arg in ('-i', str(item['path']))]], None
    if basename == 'claude':
        path = directory / 'image-input.jsonl'
        path.write_text(claude_input(prompt, images), encoding='utf-8')
        return [*command[:-1], '--input-format', 'stream-json'], path
    return command, None


def annotate(messages, repository, events):
    degraded = {(e.payload.get('turn_id'), e.payload.get('provider_id')): e.payload.get('reason')
                for e in events if e.event_type == 'attachment.degraded'}
    store = AttachmentStore(repository)
    for message in messages:
        ids = message.pop('attachment_ids', [])
        message['attachments'] = [store.view(item) for aid in ids if (item := store.get(aid))]
        reason = degraded.get((message.get('turn_id'), message.get('provider_id')))
        if reason:
            message.setdefault('metadata', {})['image_degraded'] = reason
