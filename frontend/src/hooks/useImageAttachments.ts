import { useEffect, useRef, useState } from 'react';
import { fetchAttachmentLimits, uploadAttachment } from '../lib/api';
import type { Attachment, AttachmentLimits } from '../types';

export interface ImageDraft {
  localId: string;
  file?: File;
  previewUrl: string;
  status: 'uploading' | 'ready' | 'error';
  attachment?: Attachment;
  error?: string;
  retryable?: boolean;
}

export function useImageAttachments(ensureConversation: () => Promise<string>) {
  const [drafts, setDrafts] = useState<ImageDraft[]>([]);
  const current = useRef<ImageDraft[]>([]);
  const pending = useRef(new Map<string, { promise: Promise<void>; abort: AbortController }>());
  const [limits, setLimits] = useState<AttachmentLimits>();
  const [notice, setNotice] = useState('');
  const generation = useRef(0);

  function update(next: ImageDraft[]) { current.current = next; setDrafts(next); }
  function clear() {
    generation.current++;
    for (const upload of pending.current.values()) upload.abort.abort();
    pending.current.clear();
    for (const draft of current.current) if (draft.previewUrl.startsWith('blob:')) URL.revokeObjectURL(draft.previewUrl);
    update([]);
    setNotice('');
  }
  useEffect(() => {
    let mounted = true;
    fetchAttachmentLimits().then(value => { if (mounted) setLimits(value); })
      .catch(() => { if (mounted) setNotice('图片配置加载失败，请刷新后重试'); });
    return () => {
      mounted = false;
      generation.current++;
      for (const upload of pending.current.values()) upload.abort.abort();
      for (const draft of current.current) if (draft.previewUrl.startsWith('blob:')) URL.revokeObjectURL(draft.previewUrl);
    };
  }, []);

  function start(draft: ImageDraft) {
    const epoch = generation.current;
    const abort = new AbortController();
    const promise = (async () => {
      try {
        const conversationId = await ensureConversation();
        if (abort.signal.aborted) return;
        const attachment = await uploadAttachment(conversationId, draft.file!, abort.signal);
        if (epoch === generation.current) update(current.current.map(item => item.localId === draft.localId
          ? { ...item, status: 'ready', attachment, error: undefined } : item));
      } catch (error) {
        if (!abort.signal.aborted && epoch === generation.current) update(current.current.map(item => item.localId === draft.localId
          ? { ...item, status: 'error', error: error instanceof Error ? error.message : '图片上传失败',
              retryable: !(error instanceof Error && 'status' in error && Number(error.status) < 500) } : item));
      } finally {
        pending.current.delete(draft.localId);
      }
    })();
    pending.current.set(draft.localId, { promise, abort });
  }

  function add(files: File[]) {
    if (!limits) { setNotice('图片配置尚未就绪，请稍后重试'); return; }
    const selected = files.slice(0, Math.max(0, limits.max_message_images - current.current.length));
    setNotice(selected.length < files.length
      ? `每条消息最多 ${limits.max_message_images} 张图片，已添加前 ${selected.length} 张，另有 ${files.length - selected.length} 张未添加` : '');
    const next = selected.map((file): ImageDraft => {
      const error = file.size > limits.limit_bytes
        ? `超过 ${(limits.limit_bytes / 1024 / 1024).toFixed(2)} MiB（实际 ${file.size.toLocaleString()} 字节）`
        : !limits.mime_types.includes(file.type) ? '不支持此图片类型' : undefined;
      return { localId: crypto.randomUUID(), file, previewUrl: URL.createObjectURL(file),
        status: error ? 'error' : 'uploading', error };
    });
    update([...current.current, ...next]);
    for (const draft of next) if (draft.status === 'uploading') start(draft);
  }

  function remove(localId: string) {
    pending.current.get(localId)?.abort.abort();
    const draft = current.current.find(item => item.localId === localId);
    if (draft?.previewUrl.startsWith('blob:')) URL.revokeObjectURL(draft.previewUrl);
    update(current.current.filter(item => item.localId !== localId));
  }

  function retry(localId: string) {
    const draft = current.current.find(item => item.localId === localId);
    if (!draft?.file || !draft.retryable) return;
    const next: ImageDraft = { ...draft, status: 'uploading', error: undefined };
    update(current.current.map(item => item.localId === localId ? next : item));
    start(next);
  }

  async function ready(): Promise<string[] | null> {
    const epoch = generation.current;
    await Promise.allSettled([...pending.current.values()].map(item => item.promise));
    if (epoch !== generation.current) return null;
    if (current.current.some(item => item.status !== 'ready')) return null;
    return current.current.map(item => item.attachment!.id);
  }

  function restore(attachments: Attachment[]) {
    clear();
    update(attachments.map(item => ({ localId: item.id, previewUrl: item.url, status: 'ready', attachment: item })));
  }
  function snapshot() { return current.current.map(item => ({ ...item })); }
  function restoreDraft(saved: ImageDraft[]) {
    clear();
    const next = saved.map(item => ({ ...item, previewUrl: item.file ? URL.createObjectURL(item.file) : item.previewUrl }));
    update(next);
    for (const draft of next) if (draft.status === 'uploading') start(draft);
  }
  return { drafts, limits, notice, add, remove, retry, ready, clear, restore, snapshot, restoreDraft };
}

export type ImageAttachmentsController = ReturnType<typeof useImageAttachments>;
