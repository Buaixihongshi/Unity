import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import io
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from backend.app.api.mission_requests import MissionRequest, create_requested_mission
from backend.app.attachments.delivery import cli_input, prepare
from backend.app.attachments.limits import MAX_IMAGE_BYTES, MAX_MISSION_ENCODED_BYTES, encoded_size
from backend.app.attachments.store import AttachmentError, AttachmentStore
from backend.app.domain.models import Conversation, Message, MessageRole, Mission, MissionStatus
from backend.app.main import create_app


def image_bytes(size=(2, 2), fmt='PNG', *, animated=False):
    output = io.BytesIO()
    image = Image.new('RGB', size, 'red')
    if animated:
        image.save(output, fmt, save_all=True, append_images=[Image.new('RGB', size, 'blue')], duration=100)
    else:
        image.save(output, fmt)
    return output.getvalue()


@pytest.fixture
def api(temp_settings, repository, monkeypatch):
    launches = []
    monkeypatch.setattr('backend.app.api.conversations._run_mission_in_background', lambda *args: launches.append(args[2]))
    monkeypatch.setattr('backend.app.api.missions._run_mission_in_background', lambda *args: launches.append(args[2]))
    repository.create_conversation(Conversation(id='c_images', title='images'))
    client = TestClient(create_app(temp_settings))
    return client, repository, launches


def upload(api, data=None, mime='image/png', cid='c_images'):
    return api[0].post(f'/api/conversations/{cid}/attachments', content=data if data is not None else image_bytes(),
                       headers={'Content-Type': mime, 'X-Attachment-Name': '..%2F%E5%9B%BE.png'})


def send(api, ids, **fields):
    return api[0].post('/api/conversations/c_images/missions', json={'goal': '', 'attachment_ids': ids, **fields})


def test_upload_exact_boundary_and_overflow_leave_no_partial_files(api):
    raw = image_bytes()
    data = raw + b'\0' * (MAX_IMAGE_BYTES - len(raw))
    response = upload(api, data)
    assert response.status_code == 201
    item = response.json()
    assert item['size'] == MAX_IMAGE_BYTES
    assert item['display_name'] == '.._图.png'
    content = api[0].get(item['url'])
    assert content.status_code == 200 and content.content == data
    assert content.headers['x-content-type-options'] == 'nosniff'
    assert content.headers['content-disposition'] == 'inline'
    rejected = upload(api, data + b'x')
    assert rejected.status_code == 413
    assert rejected.json()['code'] == 'ATTACHMENT_TOO_LARGE'
    store = AttachmentStore(api[1])
    assert not list(store.root.rglob('.upload-*'))
    with api[1]._transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM attachments').fetchone()[0] == 1
    assert api[0].get(f"/api/attachments/{item['id']}/content?conversation_id=c_other").status_code == 404


@pytest.mark.parametrize('data,mime,code,status', [
    (b'<svg></svg>', 'image/svg+xml', 'ATTACHMENT_UNSUPPORTED_TYPE', 415),
    (image_bytes(), 'image/jpeg', 'ATTACHMENT_UNSUPPORTED_TYPE', 415),
    (image_bytes()[:40], 'image/png', 'ATTACHMENT_DECODE_FAILED', 422),
    (image_bytes(fmt='JPEG')[:100], 'image/jpeg', 'ATTACHMENT_DECODE_FAILED', 422),
    (image_bytes(fmt='GIF', animated=True), 'image/gif', 'ATTACHMENT_ANIMATED', 415),
    (image_bytes(fmt='WEBP', animated=True), 'image/webp', 'ATTACHMENT_ANIMATED', 415),
    (image_bytes((8001, 1)), 'image/png', 'ATTACHMENT_DIMENSIONS_EXCEEDED', 422),
    (image_bytes((8000, 5001)), 'image/png', 'ATTACHMENT_DIMENSIONS_EXCEEDED', 422),
])
def test_invalid_image_contract_and_cleanup(api, data, mime, code, status):
    response = upload(api, data, mime)
    assert response.status_code == status and response.json()['code'] == code
    assert not list(AttachmentStore(api[1]).root.rglob('*.*'))
    with api[1]._transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM attachments').fetchone()[0] == 0


@pytest.mark.parametrize('fmt,mime', [('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('GIF', 'image/gif'), ('WEBP', 'image/webp')])
def test_static_formats(api, fmt, mime):
    assert upload(api, image_bytes(fmt=fmt), mime).status_code == 201


def test_pixel_boundary(api):
    assert upload(api, image_bytes((8000, 5000))).status_code == 201


def test_five_images_atomic_rejection_and_four_image_only_message(api):
    ids = [upload(api).json()['id'] for _ in range(5)]
    response = send(api, ids)
    assert response.status_code == 422 and response.json()['code'] == 'ATTACHMENT_COUNT_EXCEEDED'
    assert api[1].list_missions() == [] and api[2] == []
    assert api[1].list_messages_for_conversation('c_images') == []
    response = send(api, ids[:4])
    assert response.status_code == 201
    message = response.json()['messages'][0]
    assert [i['id'] for i in message['attachments']] == ids[:4]
    assert message['content'] == ''
    assert len(list(AttachmentStore(api[1]).directory('c_images').glob('*.png'))) == 1


def test_cross_conversation_bound_and_duplicate_ids_rejected(api):
    aid = upload(api).json()['id']
    api[1].create_conversation(Conversation(id='c_other', title='other'))
    assert api[0].post('/api/conversations/c_other/missions', json={'goal': 'x', 'attachment_ids': [aid]}).json()['code'] == 'ATTACHMENT_INVALID'
    assert send(api, [aid, aid]).json()['code'] == 'ATTACHMENT_INVALID'
    assert send(api, [aid]).status_code == 201
    mission = api[1].list_missions()[0]
    api[1].update_mission(mission.model_copy(update={'status': MissionStatus.COMPLETED}))
    assert send(api, [aid]).json()['code'] == 'ATTACHMENT_INVALID'
    assert len(api[1].list_missions()) == 1


def test_mission_eighth_passes_ninth_rejected_without_message(api):
    ids = [upload(api).json()['id'] for _ in range(9)]
    assert send(api, ids[:4]).status_code == 201
    mission = api[1].list_missions()[0]
    url = f'/api/missions/{mission.id}/interruptions'
    assert api[0].post(url, json={'attachment_ids': ids[4:8]}).status_code == 202
    before = len(api[1].list_messages(mission.id))
    response = api[0].post(url, json={'content': 'keep', 'attachment_ids': ids[8:]})
    assert response.status_code == 422 and response.json()['code'] == 'MISSION_ATTACHMENT_BUDGET_EXCEEDED'
    assert len(api[1].list_messages(mission.id)) == before


def test_encoded_byte_budget_exact_boundary_and_above(api):
    raw = image_bytes()
    sizes = [MAX_IMAGE_BYTES] * 3 + [786_432]
    assert sum(encoded_size(size) for size in sizes) == MAX_MISSION_ENCODED_BYTES
    ids = [upload(api, raw + b'\0' * (size - len(raw))).json()['id'] for size in sizes]
    assert send(api, ids).status_code == 201
    mission = api[1].list_missions()[0]
    extra = upload(api).json()['id']
    with pytest.raises(AttachmentError, match='base64'):
        api[1].add_interruption(mission.id, 'do not commit', attachment_ids=[extra])
    assert len(api[1].list_messages(mission.id)) == 1


def test_revision_retains_or_explicitly_replaces_and_request_replay(api, temp_settings):
    aid = upload(api).json()['id']
    first, _, _ = create_requested_mission(api[1], temp_settings, MissionRequest(goal='old', attachment_ids=[aid]), 'c_images')
    old = api[1].list_messages(first.id)[0]
    api[1].update_mission(first.model_copy(update={'status': MissionStatus.COMPLETED}))
    request = MissionRequest(goal='new', revises_message_id=old.id, request_id='revision')
    revised, _, created = create_requested_mission(api[1], temp_settings, request, 'c_images')
    assert created and api[1].list_messages(revised.id)[0].attachments == [aid]
    assert not create_requested_mission(api[1], temp_settings, request, 'c_images')[2]
    api[1].update_mission(revised.model_copy(update={'status': MissionStatus.COMPLETED}))
    newer = api[1].list_messages(revised.id)[0]
    again, _, _ = create_requested_mission(api[1], temp_settings, MissionRequest(goal='again', revises_message_id=newer.id), 'c_images')
    assert api[1].list_messages(again.id)[0].attachments == [aid]
    api[1].update_mission(again.model_copy(update={'status': MissionStatus.COMPLETED}))
    newer = api[1].list_messages(again.id)[0]
    replaced, _, _ = create_requested_mission(api[1], temp_settings, MissionRequest(goal='text only', revises_message_id=newer.id, attachment_ids=[]), 'c_images')
    assert api[1].list_messages(replaced.id)[0].attachments == []


def test_concurrent_binding_cannot_reuse_same_attachment(api):
    aid = upload(api).json()['id']
    mission = Mission(id='m_race', goal='race', title='race', conversation_id='c_images')
    api[1].create_mission(mission)
    def bind(_):
        try:
            api[1].add_interruption(mission.id, 'race', attachment_ids=[aid])
            return 'ok'
        except AttachmentError as error:
            return error.body['code']
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(bind, range(2))) == ['ATTACHMENT_INVALID', 'ok']
    assert len(api[1].list_messages(mission.id)) == 1


def test_cleanup_keeps_references_and_delete_conversation_removes_blobs(api):
    ids = [upload(api).json()['id'] for _ in range(2)]
    store = AttachmentStore(api[1])
    with api[1]._transaction() as db:
        for aid in ids:
            item = store.get(aid, db)
            item['created_at'] = '2000-01-01T00:00:00+00:00'
            db.execute('UPDATE attachments SET data = ? WHERE id = ?', (json.dumps(item), aid))
    assert send(api, ids[:1]).status_code == 201
    store.cleanup()
    assert store.get(ids[0]) and store.get(ids[1]) is None
    assert store.path(store.get(ids[0])).is_file()
    api[1].delete_conversation('c_images')
    assert not store.directory('c_images').exists() and store.get(ids[0]) is None


def test_native_path_degraded_and_history_delivery(api, tmp_path):
    aid = upload(api).json()['id']
    send(api, [aid])
    mission = api[1].list_missions()[0]
    tools = SimpleNamespace(repository=api[1])
    prompt, images = prepare(tools, mission.id, 'chulan', 't1', 'claude', 'hello', 'native', tmp_path)
    command, stdin = cli_input(['claude', '-p', prompt], 'claude', images, tmp_path, prompt)
    assert command == ['claude', '-p', '--input-format', 'stream-json']
    data = json.loads(stdin.read_text())
    assert base64.b64decode(data['message']['content'][0]['source']['data']) == image_bytes()
    assert 'data' not in api[1].list_events_after(mission.id, 0)[-1].payload['images'][0]
    command, stdin = cli_input(['codex', 'exec', prompt], 'codex', images, tmp_path, prompt)
    assert command[-2:] == ['-i', str(images[0]['path'])] and stdin is None
    path_prompt, _ = prepare(tools, mission.id, 'chulan', 't2', 'agy', 'hello', 'path', tmp_path)
    assert str(images[0]['path']) in path_prompt
    none_prompt, none_images = prepare(tools, mission.id, 'chulan', 't3', 'other', 'hello', 'none')
    assert not none_images and '看不到图片' in none_prompt
    from backend.app.orchestration.context import build_conversation_context
    assert f'[图片 {aid}，之前已提供]' in build_conversation_context(api[1], 'c_images', mission_id='m_next')


@pytest.mark.parametrize('protocol', ['anthropic', 'openai_chat', 'openai_responses'])
def test_api_image_payload_shapes(api, monkeypatch, protocol):
    from backend.app.adapters import api_transport
    aid = upload(api).json()['id']
    store = AttachmentStore(api[1])
    image = store.get(aid)
    image['path'] = store.path(image)
    captured = []
    @contextmanager
    def request(connection, key, method, path, payload, cancelled):
        captured.append((path, payload))
        event = {'type': 'message_stop'} if protocol == 'anthropic' else {'type': 'response.completed'}
        yield io.BytesIO(('data: ' + ('[DONE]' if protocol == 'openai_chat' else json.dumps(event)) + '\n\n').encode())
    monkeypatch.setattr(api_transport, 'request', request)
    api_transport.stream_text({'protocol': protocol}, 'test', 'model', 'hello', lambda _: None, images=[image])
    payload = captured[0][1]
    content = payload['input'][0]['content'] if protocol == 'openai_responses' else payload['messages'][0]['content']
    block = next(item for item in content if item['type'] not in {'text', 'input_text'})
    assert block['type'] == {'anthropic': 'image', 'openai_chat': 'image_url', 'openai_responses': 'input_image'}[protocol]
    assert base64.b64encode(image_bytes()).decode() in json.dumps(block)


def test_discussion_delivery_obeys_frozen_snapshot(api, temp_settings, tmp_path):
    from backend.app.api.mission_requests import DiscussionRequest
    from backend.app.extensions.discussion.deliberation import Deliberation
    aid = upload(api).json()['id']
    mission, _, _ = create_requested_mission(api[1], temp_settings, MissionRequest(
        goal='snapshot', attachment_ids=[aid], room_mode='roundtable',
        discussion=DiscussionRequest(protocol='deliberation_v2', participant_ids=['chulan', 'baobao'])), 'c_images')
    policy = Deliberation(api[1])
    reserved = policy.claim_batch(mission.id, temp_settings.artifact_root)[0]
    lease = policy.authorize(mission.id, reserved['turn_id'], 'fake')
    pending = upload(api).json()['id']
    policy.interrupt(mission.id, 'next snapshot', None, 'next', [pending])
    tools = SimpleNamespace(repository=api[1], lease_token=lease['token'])
    _, images = prepare(tools, mission.id, lease['agent_id'], lease['turn_id'], 'fake', 'frozen', 'native', tmp_path)
    assert [image['id'] for image in images] == [aid]
    assert len(AttachmentStore(api[1]).for_mission(mission.id)) == 2


@pytest.mark.parametrize('basename', ['claude', 'codex'])
def test_mcp_process_receives_pixels_and_removes_temporary_files(api, temp_settings, monkeypatch, basename):
    from backend.app.adapters.mcp_subprocess import MCPSubprocessAdapter
    from backend.app.mcp.tools import MCPTools
    from backend.tests.test_mcp_subprocess_adapter import _make_streaming_proc
    aid = upload(api).json()['id']
    send(api, [aid])
    mission = api[1].list_missions()[0]
    captured = {}
    def popen(argv, **kwargs):
        captured['argv'] = argv
        if basename == 'claude':
            captured['input'] = json.loads(kwargs['stdin'].read())
            captured['temporary'] = Path(kwargs['stdin'].name)
        else:
            path = Path(argv[-1])
            captured['image'] = path.read_bytes()
            captured['temporary'] = path
        return _make_streaming_proc([])
    monkeypatch.setattr('backend.app.adapters.mcp_subprocess.subprocess.Popen', popen)
    adapter = MCPSubprocessAdapter(basename, ['-p'] if basename == 'claude' else ['exec', '--json'],
        db_path=temp_settings.database_path, artifact_root=temp_settings.artifact_root, timeout=2)
    adapter.run_turn(mission.id, 'baobao', 'turn_pixels', basename, 'inspect', MCPTools(api[1], temp_settings.artifact_root))
    if basename == 'claude':
        assert '--input-format' in captured['argv']
        assert base64.b64decode(captured['input']['message']['content'][0]['source']['data']) == image_bytes()
    else:
        assert captured['argv'][-2] == '-i' and captured['image'] == image_bytes()
    assert not captured['temporary'].exists()
    records = '\n'.join(path.read_text() for path in (temp_settings.database_path.parent / 'turn-records').rglob('*.jsonl'))
    assert base64.b64encode(image_bytes()).decode() not in records


def test_private_records_redact_upstream_image_echoes(temp_settings):
    from backend.app.adapters.turn_record import TurnRecord
    record = TurnRecord(temp_settings.database_path, mission_id='m_private', turn_id='t', agent_id='baobao', provider_id='codex', route='chat')
    pixels = base64.b64encode(image_bytes()).decode()
    record.stream('stdout', json.dumps({'source': {'type': 'base64', 'media_type': 'image/png', 'data': pixels}}))
    record.stream('stderr', 'data:image/png;base64,' + pixels)
    record.finish(exit_code=1)
    assert pixels not in record.path.read_text()


def test_orphan_cleanup_only_removes_old_unreferenced_files(api):
    import os
    store = AttachmentStore(api[1])
    directory = store.directory('c_images')
    directory.mkdir(parents=True)
    old = directory / '.upload-crashed'
    recent = directory / '.upload-active'
    old.write_bytes(b'old')
    recent.write_bytes(b'active')
    os.utime(old, (1, 1))
    store.cleanup()
    assert not old.exists() and recent.exists()


def test_debug_echoes_and_mutated_blob_do_not_leak_pixels(api, tmp_path, monkeypatch):
    from backend.app.adapters.mcp_subprocess import _write_debug_files
    pixels = base64.b64encode(image_bytes()).decode()
    config = tmp_path / 'mcp.json'
    config.write_text('{}')
    directory = tmp_path / 'debug'
    monkeypatch.setenv('YIREN_MCP_DEBUG_DIR', str(directory))
    _write_debug_files('t', 'claude', json.dumps({'data': pixels}), 'data:image/png;base64,' + pixels, config)
    assert all(pixels not in path.read_text() for path in directory.iterdir())
    aid = upload(api).json()['id']
    send(api, [aid])
    store = AttachmentStore(api[1])
    path = store.path(store.get(aid))
    path.write_bytes(b'changed')
    mission = api[1].list_missions()[0]
    with pytest.raises(AttachmentError, match='变化'):
        prepare(SimpleNamespace(repository=api[1]), mission.id, 'baobao', 't', 'codex', 'hi', 'native', tmp_path)


@pytest.mark.parametrize('protocol', ['roundtable_v1', 'deliberation_v2'])
def test_discussion_image_interruptions_are_idempotent_and_atomic(api, temp_settings, protocol):
    from backend.app.api.mission_requests import DiscussionRequest
    from backend.app.extensions.discussion.deliberation import Deliberation
    from backend.app.extensions.discussion.roundtable import Roundtable
    mission, _, _ = create_requested_mission(api[1], temp_settings, MissionRequest(
        goal='images', room_mode='roundtable', discussion=DiscussionRequest(protocol=protocol,
            participant_ids=['chulan', 'baobao'])), 'c_images')
    policy = Deliberation(api[1]) if protocol == 'deliberation_v2' else Roundtable(api[1])
    ids = [upload(api).json()['id'] for _ in range(5)]
    policy.interrupt(mission.id, '', None, 'image-input', ids[:4])
    policy.interrupt(mission.id, '', None, 'image-input', ids[:4])
    assert len(api[1].list_messages(mission.id)) == 2
    before = len(api[1].list_events_after(mission.id, 0))
    with pytest.raises(AttachmentError):
        policy.interrupt(mission.id, 'reject entire message', None, 'bad-input', ids)
    assert len(api[1].list_events_after(mission.id, 0)) == before


def test_concurrent_budget_claims_cannot_exceed_eight(api):
    ids = [upload(api).json()['id'] for _ in range(9)]
    send(api, ids[:4])
    mission = api[1].list_missions()[0]
    api[1].add_interruption(mission.id, 'seven', attachment_ids=ids[4:7])
    def claim(aid):
        try:
            api[1].add_interruption(mission.id, 'eighth', attachment_ids=[aid])
            return 'ok'
        except AttachmentError as error:
            return error.body['code']
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(claim, ids[7:])) == ['MISSION_ATTACHMENT_BUDGET_EXCEEDED', 'ok']
    assert len(AttachmentStore(api[1]).for_mission(mission.id)) == 8


def test_degraded_reply_is_marked_in_mission_and_conversation(api, temp_settings):
    from backend.app.api.mission_detail import build_mission_detail
    aid = upload(api).json()['id']
    send(api, [aid])
    mission = api[1].list_missions()[0]
    prepare(SimpleNamespace(repository=api[1]), mission.id, 'baobao', 't_none', 'api-text', 'hello', 'none')
    api[1].add_message(Message(id='msg_reply', mission_id=mission.id, conversation_id='c_images',
        role=MessageRole.AGENT, content='请用文字描述图片', agent_id='baobao', turn_id='t_none', provider_id='api-text'))
    for detail in [build_mission_detail(api[1], temp_settings, mission.id), api[0].get('/api/conversations/c_images').json()]:
        reply = next(message for message in detail['messages'] if message['id'] == 'msg_reply')
        assert reply['metadata']['image_degraded'] == '该连接不支持图片输入'


def test_api_image_capability_defaults_and_validation():
    from backend.app.storage.api_connections import prepare_connections
    native, _ = prepare_connections({}, {'api-a': {'base_url': 'https://example.com/v1'}})
    assert native['api-a']['image_input'] == 'native'
    text, _ = prepare_connections(native, {'api-a': {'image_input': 'none'}})
    assert text['api-a']['image_input'] == 'none'
    with pytest.raises(ValueError, match='image input'):
        prepare_connections(text, {'api-a': {'image_input': 'path'}})
