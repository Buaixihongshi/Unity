import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { UpdateCapsule } from "../components/UpdateCapsule";

describe("UpdateCapsule", () => {
  it("renders nothing when idle", () => {
    const { container } = render(
      <UpdateCapsule
        notice={{ status: "idle" }}
        onInstallNow={vi.fn()}
        onDismiss={vi.fn()}
      />
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders ready state with restart button and dismiss button", () => {
    const onInstallNow = vi.fn();
    const onDismiss = vi.fn();

    render(
      <UpdateCapsule
        notice={{ status: "ready", version: "1.0.3", willInstallOnQuit: true }}
        autoDownload={true}
        onInstallNow={onInstallNow}
        onDismiss={onDismiss}
      />
    );

    expect(screen.getByTestId("update-capsule")).toBeInTheDocument();
    expect(screen.getByText("新版本已就绪，退出时自动安装")).toBeInTheDocument();

    const restartBtn = screen.getByTestId("update-install-btn");
    expect(restartBtn).toHaveTextContent("立即重启");
    fireEvent.click(restartBtn);
    expect(onInstallNow).toHaveBeenCalledTimes(1);

    const dismissBtn = screen.getByTestId("update-dismiss-btn");
    fireEvent.click(dismissBtn);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("renders found state when autoDownload is false", () => {
    const onInstallNow = vi.fn();

    render(
      <UpdateCapsule
        notice={{ status: "found", version: "1.0.4" }}
        autoDownload={false}
        onInstallNow={onInstallNow}
        onDismiss={vi.fn()}
      />
    );

    expect(screen.getByText("发现新版本 v1.0.4")).toBeInTheDocument();
    const updateBtn = screen.getByTestId("update-install-btn");
    expect(updateBtn).toHaveTextContent("更新");
    fireEvent.click(updateBtn);
    expect(onInstallNow).toHaveBeenCalledTimes(1);
  });

  it("does not render when found but autoDownload is true (silent background downloading)", () => {
    const { container } = render(
      <UpdateCapsule
        notice={{ status: "found", version: "1.0.4" }}
        autoDownload={true}
        onInstallNow={vi.fn()}
        onDismiss={vi.fn()}
      />
    );
    expect(container.firstChild).toBeNull();
  });
});
