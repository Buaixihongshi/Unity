import { useEffect, useRef, useState } from 'react';
import type { Attachment } from '../types';
import type { ImageAttachmentsController } from '../hooks/useImageAttachments';
import './ImageAttachments.css';

export function ImageDrafts({ images, disabled, budgetFull }: { images: ImageAttachmentsController; disabled?: boolean; budgetFull?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const full = Boolean(images.limits && images.drafts.length >= images.limits.max_message_images);
  return <div className="image-drafts">
    <div className="image-draft-list">
      {images.drafts.map(item => <div key={item.localId} className={`image-draft image-draft-${item.status}`}>
        <img src={item.previewUrl} alt={item.file?.name ?? item.attachment?.display_name ?? '图片预览'} />
        {item.status === 'uploading' && <span className="image-draft-status" role="status">上传中…</span>}
        {item.error && <span className="image-draft-status" role="alert">{item.error}
          {item.retryable && <button type="button" disabled={disabled} onClick={() => images.retry(item.localId)}>重试</button>}
        </span>}
        <button type="button" className="image-remove" aria-label={`移除图片 ${item.file?.name ?? item.attachment?.display_name ?? ''}`}
          disabled={disabled} onClick={() => images.remove(item.localId)}>×</button>
      </div>)}
    </div>
    <input ref={input} type="file" multiple accept={images.limits?.mime_types.join(',') ?? 'image/png,image/jpeg,image/webp,image/gif'}
      aria-label="选择图片文件" hidden disabled={disabled || full || budgetFull || !images.limits}
      onChange={event => { images.add(Array.from(event.target.files ?? [])); event.target.value = ''; }} />
    <button type="button" className="image-add" disabled={disabled || full || budgetFull || !images.limits}
      title={budgetFull ? '本任务的图片已达上限' : full ? '每条消息的图片已达上限' : '选择图片，也可以粘贴或拖入'}
      onClick={() => input.current?.click()}>＋ 图片</button>
    {budgetFull && <span className="image-hint">本任务的图片已达上限</span>}
    {images.notice && <span className="image-hint" role="status">{images.notice}</span>}
  </div>;
}

export function MessageImages({ images }: { images: Attachment[] }) {
  const [active, setActive] = useState<number | null>(null);
  const close = useRef<HTMLButtonElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (active === null) return;
    close.current?.focus();
    function key(event: KeyboardEvent) {
      if (event.key === 'Escape') setActive(null);
      if (event.key === 'ArrowLeft') setActive(index => index === null ? null : (index + images.length - 1) % images.length);
      if (event.key === 'ArrowRight') setActive(index => index === null ? null : (index + 1) % images.length);
      if (event.key === 'Tab') {
        const buttons = close.current?.parentElement?.querySelectorAll('button');
        const targets = Array.from(buttons ?? []);
        const index = targets.indexOf(document.activeElement as HTMLButtonElement);
        event.preventDefault();
        targets[(index + (event.shiftKey ? targets.length - 1 : 1)) % targets.length]?.focus();
      }
    }
    document.addEventListener('keydown', key);
    return () => { document.removeEventListener('keydown', key); opener.current?.focus(); };
  }, [active, images.length]);
  if (!images.length) return null;
  return <>
    <div className="message-images">{images.map((item, index) => <button type="button" key={item.id}
      onClick={event => { opener.current = event.currentTarget; setActive(index); }} aria-label={`查看图片 ${item.display_name}`}>
      <img src={item.url} alt={item.display_name} width={item.width} height={item.height} loading="lazy" />
    </button>)}</div>
    {active !== null && <div className="image-lightbox" role="dialog" aria-modal="true" aria-label="查看大图"
      onClick={event => { if (event.target === event.currentTarget) setActive(null); }}>
      <button ref={close} type="button" className="image-lightbox-close" aria-label="关闭大图" onClick={() => setActive(null)}>×</button>
      {images.length > 1 && <button type="button" aria-label="上一张图片" onClick={() => setActive((active + images.length - 1) % images.length)}>‹</button>}
      <img src={images[active].url} alt={images[active].display_name} />
      {images.length > 1 && <button type="button" aria-label="下一张图片" onClick={() => setActive((active + 1) % images.length)}>›</button>}
    </div>}
  </>;
}
