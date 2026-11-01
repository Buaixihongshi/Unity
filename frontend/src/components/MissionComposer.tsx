import { PersonaPortrait } from "./PersonaPortrait";
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import {
  findActiveMention,
  getMentionSuggestions,
  replaceActiveMention,
  type MentionOption,
} from "../lib/mentions";
import { avatarInitial, getPersonaColor } from "../lib/personaColors";
import {
  findActiveSkillCommand,
  getSkillSuggestions,
  replaceActiveSkillCommand,
  type SkillCommandOption,
} from "../lib/skillCommands";
import {
  applyEdit,
  confirmMention,
  type ConfirmedMention,
} from "../lib/inputConfirmations";

export interface ComposerInterruptTarget {
  agentId: string;
  label: string;
}

type SuggestionMenu =
  | { kind: "member"; query: string; options: MentionOption[] }
  | { kind: "skill"; query: string; options: SkillCommandOption[] };

/** 高亮检索命中片段（大小写不敏感）；query 为空或未命中时原样返回文本。 */
function highlightMatch(text: string, query: string): ReactNode {
  if (!query) return text;
  const index = text.toLowerCase().indexOf(query.toLowerCase());
  if (index < 0) return text;
  return (
    <>
      {text.slice(0, index)}
      <mark className="mention-hl">{text.slice(index, index + query.length)}</mark>
      {text.slice(index + query.length)}
    </>
  );
}

export function MissionComposer(props: {
  value: string;
  busy: boolean;
  hasActiveMission: boolean;
  disabled?: boolean;
  onChange: (value: string) => void;
  /** 已确认 @提及实体范围（受控）。父级持有并在发送成功后清空，组件据此复位，避免残留旧绑定。 */
  confirmations?: ConfirmedMention[];
  /** 浮层确认的 @提及实体范围（含编辑失效），供发送链路消费，避免全文反解析路由。 */
  onConfirmationsChange?: (mentions: ConfirmedMention[]) => void;
  onSubmit: () => void;
  submitLabel?: string;
  submitDisabled?: boolean;
  placeholder?: string;
  /** Roundtable mode routes replies through target_agent_id, so @ / skill autocomplete is off. */
  mentionsEnabled?: boolean;
  /** `/discuss` belongs to the optional discussion extension. */
  skillsEnabled?: boolean;
  interruptTargets?: ComposerInterruptTarget[];
  interruptTargetId?: string | null;
  onInterruptTargetChange?: (agentId: string | null) => void;
  /** Stop the current run; editing an earlier message is a separate action. */
  onStop?: () => void;
  stopping?: boolean;
  /** Set while the input rewrites the last user message. */
  revising?: boolean;
  onCancelRevise?: () => void;
}) {
  const [menu, setMenu] = useState<SuggestionMenu | null>(null);
  const [highlighted, setHighlighted] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const confirmations = props.confirmations ?? [];

  function reportConfirmations(next: ConfirmedMention[]) {
    props.onConfirmationsChange?.(next);
  }
  const mentionsEnabled = props.mentionsEnabled !== false;
  const interruptTargets = props.interruptTargets ?? [];
  const showInterruptTargets = interruptTargets.length > 0 && Boolean(props.onInterruptTargetChange);

  useEffect(() => {
    if (!menu) setHighlighted(0);
  }, [menu]);

  useEffect(() => {
    if (props.revising) textareaRef.current?.focus();
  }, [props.revising]);

  function handleChange(newValue: string) {
    props.onChange(newValue);
    // 编辑正文时同步平移/失效已确认提及范围。
    reportConfirmations(applyEdit(props.value, newValue, confirmations));
    if (!mentionsEnabled) {
      setMenu(null);
      return;
    }
    const cursor = textareaRef.current?.selectionStart ?? newValue.length;

    const mention = findActiveMention(newValue, cursor);
    if (mention) {
      const results = getMentionSuggestions(mention.query);
      setMenu(results.length > 0 ? { kind: "member", query: mention.query, options: results } : null);
      setHighlighted(0);
      return;
    }

    const skill = props.skillsEnabled === false ? null : findActiveSkillCommand(newValue, cursor);
    if (skill) {
      const results = getSkillSuggestions(skill.query);
      setMenu(results.length > 0 ? { kind: "skill", query: skill.query, options: results } : null);
      setHighlighted(0);
      return;
    }

    setMenu(null);
  }

  function selectAt(index: number) {
    if (!menu || index < 0 || index >= menu.options.length) return;
    const cursor = textareaRef.current?.selectionStart ?? props.value.length;

    if (menu.kind === "member") {
      const option = menu.options[index] as MentionOption;
      const { newValue, newCursor, start } = replaceActiveMention(props.value, cursor, option);
      props.onChange(newValue);
      reportConfirmations(
        confirmMention(props.value, newValue, start, option, confirmations),
      );
      setMenu(null);
      requestAnimationFrame(() => {
        textareaRef.current?.focus();
        textareaRef.current?.setSelectionRange(newCursor, newCursor);
      });
    } else {
      const option = menu.options[index] as SkillCommandOption;
      const { newValue, newCursor } = replaceActiveSkillCommand(props.value, cursor, option);
      props.onChange(newValue);
      setMenu(null);
      requestAnimationFrame(() => {
        textareaRef.current?.focus();
        textareaRef.current?.setSelectionRange(newCursor, newCursor);
      });
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // 中文输入法 IME 组词期间不拦截任何按键，避免打断组词。
    if (event.nativeEvent.isComposing) return;

    if (menu) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setHighlighted((i) => (i + 1) % menu.options.length);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setHighlighted((i) => (i - 1 + menu.options.length) % menu.options.length);
      } else if (event.key === "Enter" || event.key === "Tab") {
        // 浮层打开期间，Enter/Tab 仅确认补全当前候选，绝不发送消息。
        event.preventDefault();
        selectAt(highlighted);
      } else if (event.key === "Escape") {
        event.preventDefault();
        setMenu(null);
      }
      return;
    }

    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      props.value.trim() &&
      !props.busy &&
      !props.disabled &&
      !props.submitDisabled
    ) {
      event.preventDefault();
      props.onSubmit();
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!props.value.trim() || props.busy || props.disabled || props.submitDisabled) return;
    props.onSubmit();
  }

  const submitLabel = props.revising
    ? "重新发送"
    : props.submitLabel ?? (props.hasActiveMission ? "发送插话" : "发送");

  return (
    <form className="mission-composer" onSubmit={submit}>
      {showInterruptTargets ? (
        <div className="composer-interrupt-target">
          <label htmlFor="interrupt-target">点名先回应（可选）</label>
          <select
            id="interrupt-target"
            value={props.interruptTargetId ?? ""}
            disabled={props.disabled || props.busy}
            onChange={(event) =>
              props.onInterruptTargetChange?.(event.target.value || null)
            }
          >
            <option value="">不指定，按顺序自然回应</option>
            {interruptTargets.map((target) => (
              <option key={target.agentId} value={target.agentId}>
                {target.label}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      {props.onStop ? (
        <div className="composer-notice">
          <span>{props.stopping ? "正在停止当前运行…" : "补充消息将在当前回合结束后处理。"}</span>
          <button type="button" className="composer-notice-action" onClick={props.onStop}
            disabled={props.disabled || props.stopping || props.busy}>
            {props.stopping ? "停止中…" : "停止"}
          </button>
        </div>
      ) : props.revising ? (
        <div className="composer-notice composer-notice-revising">
          <span>正在修改上一条发言：发送后原话标为「已修改」，按新内容重新开始。</span>
          <button type="button" className="composer-notice-action" onClick={props.onCancelRevise}
            disabled={props.busy}>
            取消修改
          </button>
        </div>
      ) : null}
      <label htmlFor="mission-input">消息输入</label>
      <div className="composer-input-wrapper">
        <textarea
          ref={textareaRef}
          id="mission-input"
          value={props.value}
          disabled={props.disabled}
          onChange={(e) => handleChange(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={props.disabled ? "归档会话只读；恢复后可继续对话" : props.placeholder ?? (mentionsEnabled ? "说说你的想法，@ 选择伙伴 · Enter 发送" : "聊点什么，Enter 发送 · Shift + Enter 换行")}
          rows={2}
          role="combobox"
          aria-expanded={menu !== null}
          aria-controls="mention-listbox"
          aria-activedescendant={menu ? `mention-opt-${highlighted}` : undefined}
        />
        {menu && (
          <div className={`mention-menu${menu.kind === "skill" ? " mention-menu-skill" : ""}`}>
            <div className="mention-menu-header">
              <span>{menu.kind === "member" ? "选择提及成员" : "技能指令"}</span>
              <span className="mention-menu-hint">
                {menu.query ? `匹配 “${menu.query}”` : "全部"}
              </span>
            </div>
            <ul
              id="mention-listbox"
              role="listbox"
              aria-label={menu.kind === "member" ? "Agent 候选" : "技能命令"}
            >
              {menu.options.map((opt, idx) => (
                <li
                  key={menu.kind === "member" ? (opt as MentionOption).id : (opt as SkillCommandOption).cmd}
                  id={`mention-opt-${idx}`}
                  role="option"
                  aria-selected={idx === highlighted}
                  className={`mention-option ${idx === highlighted ? "mention-option-active" : ""}`}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    selectAt(idx);
                  }}
                >
                  {menu.kind === "member" ? (
                    <>
                      <span className="mention-member">
                        <span
                          className="mention-avatar"
                          style={{ background: (opt as MentionOption).color ?? getPersonaColor((opt as MentionOption).id).accent }}
                        >
                          <PersonaPortrait agentId={(opt as MentionOption).id}>{avatarInitial((opt as MentionOption).label)}</PersonaPortrait>
                        </span>
                        <span className="mention-name-box">
                          <strong className="mention-name">
                            {highlightMatch((opt as MentionOption).label, menu.query)}
                          </strong>
                          {(opt as MentionOption).pinyin ? (
                            <span className="mention-pinyin">
                              {(opt as MentionOption).pinyin} · {(opt as MentionOption).abbr}
                            </span>
                          ) : null}
                        </span>
                      </span>
                      <span className="mention-role">{(opt as MentionOption).role}</span>
                    </>
                  ) : (
                    <>
                      <span className="mention-member mention-cmd-box">
                        <strong className="mention-cmd">
                          {highlightMatch((opt as SkillCommandOption).cmd, menu.query)}
                        </strong>
                        <span className="mention-desc">{(opt as SkillCommandOption).desc}</span>
                      </span>
                      <span className="mention-key">↵</span>
                    </>
                  )}
                </li>
              ))}
            </ul>
            <div className="mention-menu-footer">
              <span>↑↓ 导航 · Tab / ↵ 确认 · Esc 关闭</span>
              <span className="mention-menu-footnote">确认补全，不发送</span>
            </div>
          </div>
        )}
        <button
          className="composer-send"
          type="submit"
          disabled={props.disabled || props.busy || props.submitDisabled || !props.value.trim()}
        >
          {submitLabel}
        </button>
      </div>
    </form>
  );
}
