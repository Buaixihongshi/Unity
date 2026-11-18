import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SettingsButton } from "../components/SettingsButton";
import type { HubSettingsView } from "../types";

const mockView: HubSettingsView = {
  runtime: {
    use_fake_cli: { value: true, source: "default" },
    max_turns: { value: 6, source: "default" },
    max_a2a_invocations: { value: 8, source: "default" },
    cli_timeout: { value: 120, source: "default" },
    a2a_idle_timeout: { value: 90, source: "default" },
  },
  providers: [
    { id: "fake", label: "Fake", enabled: { value: true, source: "committed" } },
  ],
  agents: [],
};

describe("SettingsButton", () => {
  it("clicking the button opens modal", () => {
    render(<SettingsButton view={mockView} onSave={vi.fn()} />);
    expect(screen.queryByText("设置")).toBeNull();
    fireEvent.click(screen.getByTestId("settings-button"));
    expect(screen.getByRole("heading", { name: "设置" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "团队配置" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "团队配置" }));
    expect(screen.getByRole("heading", { name: "团队配置" })).toBeTruthy();
  });

  it("closing modal returns focus to button", () => {
    vi.useFakeTimers();
    render(<SettingsButton view={mockView} onSave={vi.fn()} />);
    const button = screen.getByTestId("settings-button");
    fireEvent.click(button);
    expect(screen.getByRole("heading", { name: "设置" })).toBeTruthy();
    fireEvent.click(screen.getByTestId("settings-close"));
    expect(screen.queryByRole("heading", { name: "设置" })).toBeNull();
    vi.runAllTimers();
    expect(document.activeElement).toBe(button);
    vi.useRealTimers();
  });

  it("shows desktop settings nav button when isDesktop is true", () => {
    const onSetAutoDownload = vi.fn();
    const onCheckForUpdates = vi.fn();

    render(
      <SettingsButton
        view={mockView}
        onSave={vi.fn()}
        isDesktop={true}
        bridgeState={{
          appVersion: "1.0.2",
          autoDownload: true,
          lastCheckedAt: null,
          canCheck: true,
        }}
        onSetAutoDownload={onSetAutoDownload}
        onCheckForUpdates={onCheckForUpdates}
      />
    );

    fireEvent.click(screen.getByTestId("settings-button"));
    const desktopNavBtn = screen.getByTestId("settings-nav-desktop");
    expect(desktopNavBtn).toBeInTheDocument();
    expect(desktopNavBtn).toHaveTextContent("桌面应用");

    // Click desktop nav
    fireEvent.click(desktopNavBtn);
    expect(screen.getByRole("heading", { name: "桌面应用" })).toBeInTheDocument();
    expect(screen.getByTestId("desktop-version-badge")).toHaveTextContent("v1.0.2");
  });
});
