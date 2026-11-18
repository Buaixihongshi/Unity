import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { DesktopSettingsSection } from "../components/DesktopSettingsSection";
import type { BridgeState } from "../lib/desktopBridge";

describe("DesktopSettingsSection", () => {
  const mockState: BridgeState = {
    appVersion: "1.0.2",
    autoDownload: true,
    lastCheckedAt: "2026-11-11T03:00:00Z",
    canCheck: true,
  };

  it("renders current version and auto-download toggle", () => {
    const onSetAutoDownload = vi.fn();
    const onCheckForUpdates = vi.fn();

    render(
      <DesktopSettingsSection
        bridgeState={mockState}
        onSetAutoDownload={onSetAutoDownload}
        onCheckForUpdates={onCheckForUpdates}
      />
    );

    expect(screen.getByTestId("desktop-version-badge")).toHaveTextContent("v1.0.2");

    const toggle = screen.getByTestId("desktop-auto-download-toggle");
    expect(toggle).toHaveTextContent("ON");
    fireEvent.click(toggle);
    expect(onSetAutoDownload).toHaveBeenCalledWith(false);

    const checkBtn = screen.getByTestId("desktop-check-updates-btn");
    expect(checkBtn).toHaveTextContent("检查更新");
    fireEvent.click(checkBtn);
    expect(onCheckForUpdates).toHaveBeenCalledTimes(1);
  });

  it("disables check button with honest tooltip and without spinning when canCheck is false", () => {
    const onCheckForUpdates = vi.fn();
    render(
      <DesktopSettingsSection
        bridgeState={{ ...mockState, canCheck: false }}
        onSetAutoDownload={vi.fn()}
        onCheckForUpdates={onCheckForUpdates}
      />
    );

    const checkBtn = screen.getByTestId("desktop-check-updates-btn");
    expect(checkBtn).toBeDisabled();
    expect(checkBtn).toHaveTextContent("检查更新");
    expect(checkBtn).toHaveAttribute("title", "当前构建暂不支持检查更新");

    const icon = checkBtn.querySelector("svg");
    expect(icon).not.toHaveClass("spin-icon");

    fireEvent.click(checkBtn);
    expect(onCheckForUpdates).not.toHaveBeenCalled();
  });

  it("shows checking state and spinning animation when isChecking is true", () => {
    render(
      <DesktopSettingsSection
        bridgeState={mockState}
        onSetAutoDownload={vi.fn()}
        onCheckForUpdates={vi.fn()}
        isChecking={true}
      />
    );

    const checkBtn = screen.getByTestId("desktop-check-updates-btn");
    expect(checkBtn).toBeDisabled();
    expect(checkBtn).toHaveTextContent("正在检查…");

    const icon = checkBtn.querySelector("svg");
    expect(icon).toHaveClass("spin-icon");
  });

  it("renders diagnostic information in folded details and allows copy", async () => {
    const mockRuntime = {
      started: {
        git_sha: "abc1234567",
        captured_at: "2026-11-11 11:00:00",
        pid: 91628,
        python: "3.12.12",
        environment: "desktop",
      },
    };

    const mockFrontend = {
      git_sha: "def7890123",
      captured_at: "2026-11-11 10:58:00",
      environment: "desktop",
    };

    render(
      <DesktopSettingsSection
        bridgeState={mockState}
        onSetAutoDownload={vi.fn()}
        onCheckForUpdates={vi.fn()}
        runtime={mockRuntime as any}
        frontend={mockFrontend as any}
      />
    );

    const fold = screen.getByTestId("desktop-diagnostics-details");
    expect(fold).toBeInTheDocument();

    expect(screen.getByText("系统诊断信息（排错用）")).toBeInTheDocument();
    expect(screen.getByText("PID 91628 · Python 3.12.12")).toBeInTheDocument();

    const copyBtn = screen.getByTestId("desktop-copy-diagnostics-btn");
    expect(copyBtn).toHaveTextContent("复制环境诊断信息");
  });
});
