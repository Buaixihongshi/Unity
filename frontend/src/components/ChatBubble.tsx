import { PersonaPortrait } from "./PersonaPortrait";
import { Pencil } from "lucide-react";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import { mentionOptionById } from "../lib/mentions";
import { renderMermaid } from "../lib/mermaid";
import { avatarInitial, getPersonaColor } from "../lib/personaColors";
import { compactPersonaProviderLabel } from "../lib/providerLabels";
import type { TurnView } from "../types";
import { ThoughtBlock } from "./ThoughtBlock";
import { TurnMetadataDisclosure } from "./TurnMetadataDisclosure";
import { liveActivityLabel } from "./NativeActivity";

type MarkdownNode = { tagName?: string; value?: string; properties?: { className?: unknown }; children?: MarkdownNode[] };

/** ```mermaid 代码块的源码；其他代码块返回 null。 */
function mermaidSource(pre?: MarkdownNode): string | null {
  const code = pre?.children?.[0];
  const classes = code?.tagName === "code" ? code.properties?.className : undefined;
  if (!Array.isArray(classes) || !classes.includes("language-mermaid")) return null;
  return (code?.children ?? []).map((child) => child.value ?? "").join("").trim();
}

function MermaidDiagram({ source, streaming }: { source: string; streaming: boolean }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    // 流式输出时图表源码还不完整，等整条消息到齐再渲染。
    if (streaming) return;
    let cancelled = false;
    setFailed(false);
    renderMermaid(source)
      .then((result) => { if (!cancelled) setSvg(result); })
      .catch(() => { if (!cancelled) { setSvg(null); setFailed(true); } });
    return () => { cancelled = true; };
  }, [source, streaming]);
  if (svg) {
    return <div className="bubble-mermaid" role="img" aria-label="Mermaid 图表" dangerouslySetInnerHTML={{ __html: svg }} />;
  }
  return (
    <figure className="bubble-mermaid-source">
      <pre><code>{source}</code></pre>
      <figcaption>{failed ? "图表语法无法解析，显示源码" : "图表渲染中…"}</figcaption>
    </figure>
  );
}

function MessageMarkdown({ content, streaming = false }: { content: string; streaming?: boolean }) {
  return (
    <div className="bubble-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noopener noreferrer" />
          ),
          pre: ({ node, ...props }) => {
            const source = mermaidSource(node as MarkdownNode | undefined);
            return source === null ? <pre {...props} /> : <MermaidDiagram source={source} streaming={streaming} />;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

export function ChatBubble({ turn, onEdit }: { turn: TurnView; onEdit?: () => void }) {
  if (turn.role === "user") {
    return (
      <div className="chat-row chat-row-user">
        <span className="chat-avatar chat-avatar-user" aria-hidden="true">我</span>
        <article className="chat-bubble chat-bubble-user">
          <div className="bubble-sender">
            老板{turn.metadata.revised ? <span className="bubble-revised">已修改</span> : null}
          </div>
          <MessageMarkdown content={turn.content} />
          {onEdit && <button type="button" className="bubble-edit" onClick={onEdit} aria-label="编辑上一条消息" title="编辑消息"><Pencil size={16} aria-hidden="true" /></button>}
        </article>
      </div>
    );
  }

  const color = getPersonaColor(turn.agentId);
  const personaName = turn.personaLabel ?? (turn.agentId ? mentionOptionById(turn.agentId)?.label : undefined) ?? turn.agentId;
  const identity = compactPersonaProviderLabel(personaName, turn.providerLabel);
  const isStreaming =
    !turn.content && (turn.eventType === "message.partial" || !!turn.partialText);
  const displayContent = turn.content || turn.partialText || "";
  const showStreamingStatus = isStreaming && !displayContent;
  const audit = turn.metadata.discussion_audit;
  const discussionResult = turn.metadata.discussion_visibility === 'final';
  // Native activity is a live progress hint; once the turn finishes it lives in the thought block's tool list.
  const activityLine = isStreaming && !discussionResult ? liveActivityLabel(turn.toolCalls) : null;
  const actions = { accepted: '已采纳', partially_accepted: '部分采纳', not_accepted: '未采纳' };

  return (
    <div className="chat-row chat-row-agent">
      <span className="chat-avatar" style={{ background: color.accent }} title={personaName ?? undefined} aria-hidden="true">
        <PersonaPortrait agentId={turn.agentId}>{avatarInitial(personaName ?? "") || "?"}</PersonaPortrait>
      </span>
      <article
        className="chat-bubble chat-bubble-agent"
        style={{ borderLeftColor: color.border }}
      >
        <div className="bubble-turn-seq">{turn.metadata.discussion_visibility === 'final' ? '团队讨论结果' : `第 ${turn.turnIndex} 轮`}</div>
        <div className="bubble-header">
          <span className="bubble-persona" style={{ color: color.name }}>
            {identity}
          </span>
        </div>
        {!discussionResult && <ThoughtBlock thoughts={turn.thoughts} toolCalls={turn.toolCalls} />}
        {displayContent && <MessageMarkdown content={displayContent} streaming={!turn.content} />}
        {turn.metadata.discussion_pending_decision && <p className="discussion-result-note">其中的取舍仍待你决定。</p>}
        {turn.metadata.discussion_revision_reviewed === false && <p className="discussion-result-note">此稿已按复核意见修订，修订部分未再次独立复核。</p>}
        {audit && (audit.sources.length > 0 || audit.reviews.length > 0) && <details className="discussion-result-audit">
          <summary>查看依据与复核记录</summary>
          {audit.sources.length > 0 && <ul aria-label="讨论依据">{audit.sources.map((source) => <li key={source.id}>
            <strong>{source.label}</strong>{source.summary && <p>{source.summary}</p>}
          </li>)}</ul>}
          {audit.reviews.length > 0 && <ul aria-label="复核记录">{audit.reviews.map((review, index) => <li key={`${review.reviewer}:${index}`}>
            <strong>{review.reviewer}</strong><p>{review.problem}</p>
            {review.action && <p>{actions[review.action]}{review.reason ? `：${review.reason}` : ''}</p>}
          </li>)}</ul>}
        </details>}
        {(showStreamingStatus || activityLine) && (
          <div className="streaming-status" aria-live="polite">
            <span className={activityLine ? "streaming-activity" : undefined}>{activityLine ?? "等待完整回复"}</span>
            <span className="typing-dots" aria-hidden="true">
              <span /><span /><span />
            </span>
          </div>
        )}
        {isStreaming && !showStreamingStatus && !activityLine && (
          <span className="typing-dots" aria-hidden="true">
            <span /><span /><span />
          </span>
        )}
        <TurnMetadataDisclosure metadata={turn.metadata} usage={turn.usage} />
      </article>
    </div>
  );
}
