import { useState, useCallback } from "react";
import { RefreshCw, Check, Copy, ChevronRight, Laptop } from "lucide-react";
import type { BridgeState } from "../lib/desktopBridge";
import type { RuntimeIdentity, SourceIdentity } from "../types";
import "./DesktopSettingsSection.css";

interface DesktopSettingsSectionProps {
  bridgeState: BridgeState | null;
  onSetAutoDownload: (value: boolean) => void;
  onCheckForUpdates: () => void;
  isChecking?: boolean;
  runtime?: RuntimeIdentity;
  frontend?: SourceIdentity;
}

export function DesktopSettingsSection({
  bridgeState,
  onSetAutoDownload,
  onCheckForUpdates,
  isChecking = false,
  runtime,
  frontend = typeof __FRONTEND_IDENTITY__ !== "undefined" ? __FRONTEND_IDENTITY__ : undefined,
}: DesktopSettingsSectionProps) {
  const [copied, setCopied] = useState(false);
  const started = runtime?.started;
  const canCheck = bridgeState?.canCheck !== false;
  const isDisabled = !canCheck || isChecking;

  const formatLastChecked = (iso: string | null | undefined) => {
    if (!iso) return "从未检查";
    try {
      const date = new Date(iso);
      if (isNaN(date.getTime())) return iso;
      return date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return iso;
    }
  };

  const getDiagnosticText = useCallback(() => {
    const lines = [
      `=== Unity Desktop 诊断信息 ===`,
      `应用版本: v${bridgeState?.appVersion ?? "未知"}`,
      `自动更新开关: ${bridgeState?.autoDownload ? "开启" : "关闭"}`,
      `上次检查时间: ${bridgeState?.lastCheckedAt ?? "未知"}`,
      `前端 Git SHA: ${frontend?.git_sha ?? "未知"} (${frontend?.dirty ? "包含未提交修改" : "clean"})`,
      `前端构建时间: ${frontend?.captured_at ?? "未知"}`,
      `后端 Git SHA: ${started?.git_sha ?? "未知"} (${started?.dirty ? "包含未提交修改" : "clean"})`,
      `后端启动时间: ${started?.captured_at ?? "未知"}`,
      `后端 PID: ${started?.pid ?? "未知"}`,
      `Python 版本: ${started?.python ?? "未知"}`,
      `环境: ${started?.environment ?? frontend?.environment ?? "desktop"}`,
    ];
    return lines.join("\n");
  }, [bridgeState, frontend, started]);

  const handleCopyDiagnostics = async () => {
    const text = getDiagnosticText();
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // ignore
    }
  };

  return (
    <section className="settings-section desktop-settings-section" aria-labelledby="settings-desktop-title">
      <div className="settings-section-heading">
        <h3 id="settings-desktop-title" className="desktop-heading-title">
          <Laptop size={18} aria-hidden="true" />
          桌面应用
        </h3>
        <p className="settings-section-hint">
          管理 Unity 桌面端外壳的更新偏好与系统排错信息。
        </p>
      </div>

      <div className="desktop-settings-card">
        {/* Version & Update check */}
        <div className="desktop-setting-row">
          <div className="desktop-setting-info">
            <span className="desktop-setting-label">当前版本</span>
            <span className="desktop-version-badge" data-testid="desktop-version-badge">
              v{bridgeState?.appVersion ?? "1.0.0"}
            </span>
          </div>
          <div className="desktop-check-action">
            <span className="desktop-last-checked-hint">
              上次检查：{formatLastChecked(bridgeState?.lastCheckedAt)}
            </span>
            <button
              type="button"
              className="desktop-btn desktop-btn-secondary"
              onClick={onCheckForUpdates}
              disabled={isDisabled}
              title={!canCheck ? "当前构建暂不支持检查更新" : undefined}
              data-testid="desktop-check-updates-btn"
            >
              <RefreshCw
                size={14}
                className={isChecking ? "spin-icon" : ""}
                aria-hidden="true"
              />
              {isChecking ? "正在检查…" : "检查更新"}
            </button>
          </div>
        </div>

        {/* Auto download switch */}
        <div className="desktop-setting-row">
          <div className="desktop-setting-info">
            <label htmlFor="desktop-auto-download-switch" className="desktop-setting-label">
              退出时自动安装更新
            </label>
            <p className="desktop-setting-desc">
              开启后，新版本将在后台静默下载，并在下次退出应用时自动完成替换。
            </p>
          </div>
          <button
            id="desktop-auto-download-switch"
            type="button"
            role="switch"
            aria-checked={bridgeState?.autoDownload ?? true}
            className={`settings-toggle${(bridgeState?.autoDownload ?? true) ? " on" : ""}`}
            data-testid="desktop-auto-download-toggle"
            onClick={() => onSetAutoDownload(!(bridgeState?.autoDownload ?? true))}
          >
            {(bridgeState?.autoDownload ?? true) ? "ON" : "OFF"}
          </button>
        </div>
      </div>

      {/* Diagnostics details fold (检修口折叠) */}
      <details className="desktop-diagnostics-fold" data-testid="desktop-diagnostics-details">
        <summary className="desktop-diagnostics-summary">
          <ChevronRight size={14} className="fold-arrow" aria-hidden="true" />
          <span>系统诊断信息（排错用）</span>
        </summary>
        <div className="desktop-diagnostics-body">
          <div className="desktop-diagnostics-grid">
            <div>
              <strong>前端快照：</strong>
              <span>{frontend?.git_sha ? frontend.git_sha.slice(0, 7) : "未知"}</span>
              <span className="desktop-dim-time"> · {frontend?.captured_at ?? "未知时间"}</span>
            </div>
            <div>
              <strong>后端快照：</strong>
              <span>{started?.git_sha ? started.git_sha.slice(0, 7) : "未知"}</span>
              <span className="desktop-dim-time"> · {started?.captured_at ?? "未知时间"}</span>
            </div>
            <div>
              <strong>进程与解释器：</strong>
              <span>PID {started?.pid ?? "未知"} · Python {started?.python ?? "未知"}</span>
            </div>
            <div>
              <strong>运行环境：</strong>
              <span>{started?.environment ?? frontend?.environment ?? "desktop"}</span>
            </div>
          </div>
          <div className="desktop-diagnostics-action">
            <button
              type="button"
              className="desktop-btn desktop-btn-copy"
              onClick={handleCopyDiagnostics}
              data-testid="desktop-copy-diagnostics-btn"
            >
              {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
              {copied ? "已复制诊断信息" : "复制环境诊断信息"}
            </button>
          </div>
        </div>
      </details>
    </section>
  );
}
