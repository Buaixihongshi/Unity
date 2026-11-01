import { Fragment, useLayoutEffect, useRef, type ReactNode } from "react";
import { formatAgentName } from "../lib/missionView";
import type { A2AInvocationRecord, TurnView } from "../types";
import { ChatBubble } from "./ChatBubble";
import { StatusBadge } from "./StatusBadge";
import { ArtifactPanel } from "./ArtifactPanel";

function A2AArrow({ invocation }: { invocation: A2AInvocationRecord }) {
  const sender = formatAgentName(invocation.sender_agent_id);
  const target = formatAgentName(invocation.target_agent_id);
  const isWaitReply = invocation.mode === "wait_reply";
  const arrow = isWaitReply ? "↳⇌" : "↳";
  return (
    <div className={`a2a-arrow${invocation.reply_failed ? " a2a-reply-failed" : ""}`} title={invocation.purpose}>
      <span>{arrow}</span>
      <strong>{sender} {isWaitReply ? "⇌" : "→"} {target}</strong>
      <span>{invocation.purpose}</span>
      {isWaitReply && invocation.reply_text && (
        <div className="a2a-reply-preview" title={invocation.reply_text}>
          ↩ {invocation.reply_text.slice(0, 100)}{invocation.reply_text.length > 100 ? "…" : ""}
        </div>
      )}
    </div>
  );
}

export function TurnTimeline({
  turns,
  conversationId,
  emptyContent,
  afterContent,
  collapsedAgentMissionId,
  contentRevision,
  editableMessageId,
  onEditMessage,
}: {
  turns: TurnView[];
  conversationId?: string;
  emptyContent?: ReactNode;
  afterContent?: ReactNode;
  collapsedAgentMissionId?: string;
  contentRevision?: number;
  editableMessageId?: string;
  onEditMessage?: () => void;
}) {
  const containerRef = useRef<HTMLElement>(null);
  const lastConvRef = useRef<string | undefined>(undefined);
  const stickToBottomRef = useRef<boolean>(true);
  const collapsed = collapsedAgentMissionId ? turns.filter((t) => t.missionId === collapsedAgentMissionId && (t.role === 'agent' || t.role === 'assistant')) : [];
  const collapsedIds = new Set(collapsed.map((turn) => turn.id));

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const convChanged = lastConvRef.current !== conversationId;
    if (convChanged) {
      el.scrollTop = el.scrollHeight;
      lastConvRef.current = conversationId;
      stickToBottomRef.current = true;
      return;
    }

    if (stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [conversationId, turns.length, contentRevision]);

  useLayoutEffect(()=>{
    const el=containerRef.current;
    const locate=()=>{stickToBottomRef.current=false;};
    el?.addEventListener('artifact-locate',locate);
    return ()=>el?.removeEventListener('artifact-locate',locate);
  });

  function handleScroll() {
    const el = containerRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distance < 80;
  }

  if (turns.length === 0) {
    if (emptyContent) {
      return (
        <section className="turn-timeline onboarding-timeline" aria-label="任务轮次">
          {emptyContent}
        </section>
      );
    }
    return (
      <div className="empty-state">从对话开始，和团队一起把想法变成结果。</div>
    );
  }

  return (
    <section
      ref={containerRef}
      onScroll={handleScroll}
      className="turn-timeline"
      aria-label="任务轮次"
    >
      {turns.filter((turn) => !collapsedIds.has(turn.id)).map((turn) => (
        <Fragment key={turn.id}>
          {turn.a2aInvocation ? <A2AArrow invocation={turn.a2aInvocation} /> : null}
          {turn.artifact ? (
            <ArtifactPanel key={turn.artifact.created_at} missionId={turn.artifact.mission_id} artifacts={[turn.artifact]} variant="chat" />
          ) : turn.isStatusEvent ? <StatusBadge turn={turn} /> : <ChatBubble turn={turn} onEdit={turn.id === editableMessageId ? onEditMessage : undefined} />}
        </Fragment>
      ))}
      {collapsed.length > 0 && <details className="deliberation-original-messages">
        <summary>查看本场原始发言（{collapsed.length} 条）</summary>
        {collapsed.map((turn) => <ChatBubble key={turn.id} turn={turn} />)}
      </details>}
      {afterContent}
    </section>
  );
}
