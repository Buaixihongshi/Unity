import { useState } from 'react';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MissionComposer } from '../components/MissionComposer';
import { MessageImages } from '../components/ImageAttachments';
import { useImageAttachments } from '../hooks/useImageAttachments';
import { fetchAttachmentLimits, uploadAttachment } from '../lib/api';
import type { Attachment } from '../types';

vi.mock('../lib/api', () => ({ fetchAttachmentLimits: vi.fn(), uploadAttachment: vi.fn() }));
const limits = { limit_bytes: 3_932_160, max_message_images: 4, max_mission_images: 8,
  max_mission_encoded_bytes: 16_777_216, mime_types: ['image/png', 'image/jpeg', 'image/webp', 'image/gif'] };
const attachment = (id = 'att_1'): Attachment => ({ id, mime: 'image/png', size: 42, width: 100, height: 50,
  display_name: `${id}.png`, url: `/api/attachments/${id}/content?conversation_id=c_1` });

function Harness({ onSend = vi.fn(), clearAfter = false }: { onSend?: (ids?: string[]) => unknown; clearAfter?: boolean }) {
  const [value, setValue] = useState('这段文字要保留');
  const images = useImageAttachments(async () => 'c_1');
  return <><MissionComposer value={value} busy={false} hasActiveMission={false} onChange={setValue} images={images}
    onSubmit={async ids => { await onSend(ids); if (clearAfter) { images.clear(); setValue(''); } }} />
    <button onClick={images.clear}>切换会话</button></>;
}

function select(files: File[]) { fireEvent.change(screen.getByLabelText('选择图片文件'), { target: { files } }); }
function png(name = 'a.png') { return new File(['image'], name, { type: 'image/png' }); }
function deferred<T>() { let resolve!: (value: T) => void, reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:preview'), revokeObjectURL: vi.fn() }));
  vi.mocked(fetchAttachmentLimits).mockResolvedValue(limits);
  vi.mocked(uploadAttachment).mockImplementation(async (_cid, file) => attachment(`att_${file.name}`));
});

describe('图片草稿和发送', () => {
  it('超限和不支持类型立即标红，不上传，保留文字并禁止发送', async () => {
    render(<Harness />);
    await waitFor(() => expect(screen.getByText('＋ 图片')).toBeEnabled());
    select([new File([new Uint8Array(limits.limit_bytes + 1)], 'large.png', { type: 'image/png' }),
      new File(['<svg/>'], 'vector.svg', { type: 'image/svg+xml' })]);
    expect(uploadAttachment).not.toHaveBeenCalled();
    expect(screen.getByText(/超过 3.75 MiB/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    expect(screen.getByLabelText('消息输入')).toHaveValue('这段文字要保留');
    fireEvent.click(screen.getByRole('button', { name: '移除图片 large.png' }));
    fireEvent.click(screen.getByRole('button', { name: '移除图片 vector.svg' }));
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled();
  });

  it('一次六张只收前四张，明确显示未添加的数量', async () => {
    render(<Harness />);
    await waitFor(() => expect(screen.getByText('＋ 图片')).toBeEnabled());
    select(Array.from({ length: 6 }, (_, i) => png(`${i}.png`)));
    expect(screen.getByText(/已添加前 4 张，另有 2 张未添加/)).toBeInTheDocument();
    await waitFor(() => expect(uploadAttachment).toHaveBeenCalledTimes(4));
    expect(screen.getByText('＋ 图片')).toBeDisabled();
    expect(screen.getAllByRole('button', { name: /移除图片/ })).toHaveLength(4);
  });

  it('上传中点击发送，成功后自动携带附件提交并清空已发送草稿', async () => {
    const upload = deferred<Attachment>(); const onSend = vi.fn();
    vi.mocked(uploadAttachment).mockReturnValue(upload.promise);
    render(<Harness onSend={onSend} clearAfter />);
    await waitFor(() => expect(screen.getByText('＋ 图片')).toBeEnabled());
    select([png()]);
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(screen.getByRole('button', { name: '图片上传中…' })).toBeDisabled();
    expect(onSend).not.toHaveBeenCalled();
    await act(async () => upload.resolve(attachment()));
    await waitFor(() => expect(onSend).toHaveBeenCalledWith(['att_1']));
    expect(screen.getByLabelText('消息输入')).toHaveValue('');
    expect(screen.queryByRole('button', { name: /移除图片/ })).not.toBeInTheDocument();
  });

  it.each(['reject', 'resolve'] as const)('取消修订后，旧上传延迟 %s 不影响恢复草稿的上传等待', async settle => {
    const oldUpload = deferred<Attachment>(), freshUpload = deferred<Attachment>();
    vi.mocked(uploadAttachment).mockReturnValueOnce(oldUpload.promise).mockReturnValueOnce(freshUpload.promise);
    const { result } = renderHook(() => useImageAttachments(async () => 'c_1'));
    await waitFor(() => expect(result.current.limits).toEqual(limits));
    act(() => result.current.add([png()]));
    await waitFor(() => expect(uploadAttachment).toHaveBeenCalledTimes(1));
    const saved = result.current.snapshot();
    act(() => result.current.restore([]));
    expect(vi.mocked(uploadAttachment).mock.calls[0][2]?.aborted).toBe(true);
    act(() => result.current.restoreDraft(saved));
    await waitFor(() => expect(uploadAttachment).toHaveBeenCalledTimes(2));
    await act(async () => {
      if (settle === 'reject') oldUpload.reject(new Error('延迟取消'));
      else oldUpload.resolve(attachment('att_stale'));
    });

    const onReady = vi.fn();
    let ready!: Promise<string[] | null>;
    await act(async () => { ready = result.current.ready().then(ids => { onReady(ids); return ids; }); });
    expect(onReady).not.toHaveBeenCalled();
    expect(result.current.drafts[0]).toMatchObject({ localId: saved[0].localId, status: 'uploading' });
    await act(async () => { freshUpload.resolve(attachment('att_fresh')); await ready; });
    expect(onReady).toHaveBeenCalledExactlyOnceWith(['att_fresh']);
  });

  it('等待期间上传失败取消发送，草稿保留，网络失败可以重试', async () => {
    const upload = deferred<Attachment>(); const onSend = vi.fn();
    vi.mocked(uploadAttachment).mockReturnValueOnce(upload.promise);
    render(<Harness onSend={onSend} />);
    await waitFor(() => expect(screen.getByText('＋ 图片')).toBeEnabled());
    select([png()]);
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await act(async () => upload.reject(new Error('网络断开')));
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByLabelText('消息输入')).toHaveValue('这段文字要保留');
    expect(screen.getByText('网络断开')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '发送' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(onSend).toHaveBeenCalledWith(['att_a.png']));
  });

  it('发送被拒绝时不自行清空文字或已上传图片', async () => {
    const onSend = vi.fn(async () => false);
    render(<Harness onSend={onSend} />);
    await waitFor(() => expect(screen.getByText('＋ 图片')).toBeEnabled());
    select([png()]);
    await waitFor(() => expect(screen.queryByText('上传中…')).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(screen.getByLabelText('消息输入')).toHaveValue('这段文字要保留');
    expect(screen.getByRole('button', { name: '移除图片 a.png' })).toBeInTheDocument();
  });

  it('等待上传时切换会话不会向新会话自动发送', async () => {
    const upload = deferred<Attachment>(); const onSend = vi.fn();
    vi.mocked(uploadAttachment).mockReturnValue(upload.promise);
    render(<Harness onSend={onSend} />);
    await waitFor(() => expect(screen.getByText('＋ 图片')).toBeEnabled());
    select([png()]);
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    fireEvent.click(screen.getByText('切换会话'));
    await act(async () => upload.resolve(attachment()));
    expect(onSend).not.toHaveBeenCalled();
  });

  it('图片可以单独发送，粘贴和拖拽复用同一上传流程', async () => {
    const onSend = vi.fn();
    render(<Harness onSend={onSend} />);
    await waitFor(() => expect(screen.getByText('＋ 图片')).toBeEnabled());
    fireEvent.change(screen.getByLabelText('消息输入'), { target: { value: '' } });
    fireEvent.paste(screen.getByLabelText('消息输入'), { clipboardData: { files: [png()] } });
    fireEvent.drop(screen.getByLabelText('消息输入'), { dataTransfer: { files: [png('b.png')] } });
    await waitFor(() => expect(uploadAttachment).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => expect(onSend).toHaveBeenCalledWith(['att_a.png', 'att_b.png']));
  });
});

it('点击图片在当前页查看，可切换并用 Esc 关闭', () => {
  render(<MessageImages images={[attachment(), attachment('att_2')]} />);
  fireEvent.click(screen.getByRole('button', { name: '查看图片 att_1.png' }));
  expect(screen.getByRole('dialog', { name: '查看大图' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '下一张图片' }));
  expect(screen.getByRole('dialog').querySelector('img')).toHaveAttribute('src', attachment('att_2').url);
  fireEvent.keyDown(document, { key: 'Escape' });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
