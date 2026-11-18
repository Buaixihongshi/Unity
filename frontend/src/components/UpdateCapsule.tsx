import { Sparkles, X, ArrowUpCircle } from "lucide-react";
import type { UpdateNotice } from "../hooks/useDesktopBridge";
import "./UpdateCapsule.css";

interface UpdateCapsuleProps {
  notice: UpdateNotice;
  autoDownload?: boolean;
  onInstallNow: () => void;
  onDismiss: () => void;
}

export function UpdateCapsule({
  notice,
  autoDownload = true,
  onInstallNow,
  onDismiss,
}: UpdateCapsuleProps) {
  if (notice.status === "idle") return null;

  const isReady = notice.status === "ready";
  const isFound = notice.status === "found" && !autoDownload;

  if (!isReady && !isFound) return null;

  return (
    <aside
      className="update-capsule"
      role="status"
      aria-live="polite"
      aria-label="应用更新通知"
      data-testid="update-capsule"
    >
      <div className="update-capsule-content">
        {isReady ? (
          <>
            <Sparkles size={14} className="update-capsule-icon" aria-hidden="true" />
            <span className="update-capsule-text">
              新版本已就绪，退出时自动安装
            </span>
            <button
              type="button"
              className="update-capsule-action-btn"
              onClick={onInstallNow}
              title="立即退出并安装更新"
              data-testid="update-install-btn"
            >
              立即重启
            </button>
          </>
        ) : (
          <>
            <ArrowUpCircle size={14} className="update-capsule-icon" aria-hidden="true" />
            <span className="update-capsule-text">
              发现新版本 v{notice.version ?? ""}
            </span>
            <button
              type="button"
              className="update-capsule-action-btn"
              onClick={onInstallNow}
              title="下载并安装新版本"
              data-testid="update-install-btn"
            >
              更新
            </button>
          </>
        )}
      </div>
      <button
        type="button"
        className="update-capsule-close-btn"
        onClick={onDismiss}
        aria-label="忽略此版本更新提醒"
        title="稍后提醒（本版本不再弹出）"
        data-testid="update-dismiss-btn"
      >
        <X size={14} aria-hidden="true" />
      </button>
    </aside>
  );
}
