import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArtifactViewerModal, useArtifactContent } from "./ArtifactPanel";
import { artifactDownloadUrl, fetchArtifactIndex } from "../lib/api";
import {
  artifactKindGroups,
  filterArtifacts,
  type SidebarArtifact,
} from "../lib/artifactSidebar";
import { formatAgentName } from "../lib/missionView";
import "./ArtifactSidebar.css";

export function ArtifactSidebar({
  items,
  onLocate,
  onPinnedChange,
}: {
  items: SidebarArtifact[];
  onLocate: (item: SidebarArtifact) => string | undefined;
  onPinnedChange: (pinned: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [wide, setWide] = useState(
    () => window.matchMedia?.("(min-width: 1440px)").matches ?? false,
  );
  const [pin, setPin] = useState(() => {
    try {
      return localStorage.getItem("unity.artifactSidebar.pinned") === "true";
    } catch {
      return false;
    }
  });
  const [scope, setScope] = useState<"conversation" | "all">("conversation");
  const [allItems, setAllItems] = useState<SidebarArtifact[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestVersion = useRef(0);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("all");
  const [active, setActive] = useState<SidebarArtifact>();
  const { content, load } = useArtifactContent();
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const pinned = open && pin && wide;
  useEffect(() => {
    if (!window.matchMedia) return;
    const media = window.matchMedia("(min-width: 1440px)");
    const change = () => setWide(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  useEffect(() => {
    onPinnedChange(pinned);
  }, [pinned, onPinnedChange]);
  useEffect(() => {
    if (pin && wide) setOpen(true);
  }, [pin, wide]);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (!open && wasOpen.current) trigger.current?.focus();
    wasOpen.current = open;
  }, [open]);
  const close = () => setOpen(false);
  useEffect(() => {
    if (!open || pinned) return;
    panel.current?.focus();
    const key = (event: KeyboardEvent) => {
      if (active) return;
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
      if (event.key === "Tab") {
        const nodes = panel.current?.querySelectorAll<HTMLElement>(
          "button:not(:disabled),input,a[href]",
        );
        if (!nodes?.length) return;
        const first = nodes[0],
          last = nodes[nodes.length - 1];
        if (
          event.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === panel.current)
        ) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [open, pinned, active]);
  useEffect(() => {
    const version = ++requestVersion.current;
    if (scope !== "all" || !open) return;
    setLoading(true);
    setError("");
    setAllItems([]);
    setCursor(null);
    const timer = window.setTimeout(() => {
      fetchArtifactIndex(query)
        .then((page) => {
          if (version !== requestVersion.current) return;
          setAllItems(
            page.items.map((item) => ({
              ...item,
              identity: item.id,
              eventId: item.event_id,
            })),
          );
          setCursor(page.next_cursor);
        })
        .catch((e: unknown) => {
          if (version === requestVersion.current)
            setError(e instanceof Error ? e.message : "加载失败");
        })
        .finally(() => {
          if (version === requestVersion.current) setLoading(false);
        });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      requestVersion.current++;
    };
  }, [scope, query, open]);
  const loadMore = () => {
    if (!cursor || loading) return;
    const version = requestVersion.current;
    setLoading(true);
    setError("");
    fetchArtifactIndex(query, cursor)
      .then((page) => {
        if (version !== requestVersion.current) return;
        setAllItems((prev) => [
          ...new Map(
            [
              ...prev,
              ...page.items.map((item) => ({
                ...item,
                identity: item.id,
                eventId: item.event_id,
              })),
            ].map((item) => [item.identity, item]),
          ).values(),
        ]);
        setCursor(page.next_cursor);
      })
      .catch((e: unknown) => {
        if (version === requestVersion.current)
          setError(e instanceof Error ? e.message : "加载失败");
      })
      .finally(() => {
        if (version === requestVersion.current) setLoading(false);
      });
  };
  const source = scope === "all" ? allItems : items;
  const visible = filterArtifacts(source, query, kind);
  return (
    <>
      <button
        ref={trigger}
        type="button"
        className={`artifact-tray-trigger ${open ? "is-open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span aria-hidden="true" className="artifact-trigger-glow" />
        产物 <span>{items.length}</span>
      </button>
      {open && !pinned && (
        <button
          type="button"
          className="artifact-tray-backdrop"
          aria-label="关闭产物栏遮罩"
          onClick={close}
        />
      )}
      {open && (
        <aside
          ref={panel}
          tabIndex={-1}
          aria-label="交付产物"
          className={`artifact-sidebar ${pinned ? "is-pinned" : "is-drawer"}`}
        >
          <header>
            <strong>交付产物</strong>
            <button
              type="button"
              disabled={!wide}
              aria-pressed={pin}
              onClick={() => {
                const next = !pin;
                setPin(next);
                try {
                  localStorage.setItem(
                    "unity.artifactSidebar.pinned",
                    String(next),
                  );
                } catch {
                  /* private browsing */
                }
              }}
            >
              固定
            </button>
            <button type="button" aria-label="收起产物栏" onClick={close}>
              ×
            </button>
          </header>
          <div
            role="tablist"
            aria-label="产物范围"
            onKeyDown={(event) => {
              if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                event.preventDefault();
                setScope(scope === "all" ? "conversation" : "all");
                setKind("all");
                const next =
                  event.currentTarget.querySelector<HTMLButtonElement>(
                    scope === "all"
                      ? "button:first-child"
                      : "button:last-child",
                  );
                next?.focus();
              }
            }}
          >
            <button
              role="tab"
              tabIndex={scope === "conversation" ? 0 : -1}
              aria-selected={scope === "conversation"}
              onClick={() => {
                setScope("conversation");
                setKind("all");
              }}
            >
              当前会话 {items.length}
            </button>
            <button
              role="tab"
              tabIndex={scope === "all" ? 0 : -1}
              aria-selected={scope === "all"}
              onClick={() => {
                setScope("all");
                setKind("all");
              }}
            >
              全部仓库
            </button>
          </div>
          <p className="artifact-scope-note">
            {scope === "all" ? "活跃普通会话 · 按保存记录排列" : "当前会话"} ·
            文件已保存不代表已验证
          </p>
          <input
            aria-label="搜索产物"
            placeholder="搜索标题或路径"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="artifact-kind-filters">
            <button
              type="button"
              aria-pressed={kind === "all"}
              onClick={() => setKind("all")}
            >
              {scope === "all" ? "已加载" : "全部"} {source.length}
            </button>
            {artifactKindGroups(source).map((group) => (
              <button
                key={group.kind}
                type="button"
                aria-pressed={kind === group.kind}
                onClick={() => setKind(group.kind)}
              >
                {group.kind} {group.count}
              </button>
            ))}
          </div>
          <p role="status">{loading ? "加载中…" : ""}</p>
          {error && (
            <p role="alert">
              {error}
              <button
                onClick={() => {
                  setScope("conversation");
                }}
              >
                返回当前会话
              </button>
            </p>
          )}
          <ul className="artifact-sidebar-list">
            {visible.map((item) => (
              <li key={item.identity}>
                <button
                  type="button"
                  className="artifact-sidebar-open"
                  onClick={() => {
                    setActive(item);
                    load(item.mission_id, item.path ?? "");
                  }}
                >
                  <div className="artifact-card-title-row">
                    <strong>{item.title}</strong>
                  </div>
                  <div className="artifact-card-meta-row">
                    <span className="artifact-kind-badge">{item.kind ?? "file"}</span>
                    <span className="artifact-meta-dot">·</span>
                    <span className="artifact-agent-tag">
                      {item.agent_id ? formatAgentName(item.agent_id) : "文件"}
                    </span>
                  </div>
                  {item.path !== item.title && (
                    <small className="artifact-card-path">{item.path}</small>
                  )}
                  <time dateTime={item.created_at}>
                    {new Date(item.created_at).toLocaleString()}
                  </time>
                </button>
                {scope === "all" && (
                  <button
                    type="button"
                    className="artifact-conversation-link"
                    onClick={() => {
                      onLocate(item);
                      if (!pinned) close();
                    }}
                  >
                    <span aria-hidden="true" className="artifact-conv-arrow">↳</span>
                    <span>{item.conversation_title || "未命名会话"}</span>
                  </button>
                )}
                <div className="artifact-card-actions">
                  <button
                    type="button"
                    className="artifact-btn-locate"
                    onClick={() => {
                      onLocate(item);
                      if (!pinned) close();
                    }}
                  >
                    定位
                  </button>
                  <a
                    href={artifactDownloadUrl(item.mission_id, item.path ?? "")}
                    download={item.title}
                    className="artifact-btn-download"
                  >
                    下载
                  </a>
                </div>
              </li>
            ))}
          </ul>
          {!visible.length && !loading && !error && (
            <p>
              {scope === "all" && cursor
                ? "已扫描较近记录，继续查找"
                : "没有匹配的产物"}
            </p>
          )}
          {scope === "conversation" && query.trim() && !visible.length && (
            <button
              type="button"
              onClick={() => {
                setScope("all");
                setKind("all");
              }}
            >
              去全部仓库搜「{query}」
            </button>
          )}
          {scope === "all" && cursor && (
            <button type="button" disabled={loading} onClick={loadMore}>
              加载更多
            </button>
          )}
        </aside>
      )}
      {active &&
        createPortal(
          <ArtifactViewerModal
            artifact={active}
            path={active.path ?? ""}
            state={content[`${active.mission_id}:${active.path}`]}
            onClose={() => setActive(undefined)}
          />,
          document.body,
        )}
    </>
  );
}
