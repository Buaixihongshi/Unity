import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  hasNativeBridge,
  isMockBridgeActive,
  setMockBridgeActive,
  postBridgeCommand,
  getDismissedVersion,
  setDismissedVersion,
  clearDismissedVersion,
  dispatchMockBridgeEvent,
  type BridgeState,
} from "../lib/desktopBridge";
import { useDesktopBridge } from "../hooks/useDesktopBridge";

describe("desktopBridge lib contracts", () => {
  beforeEach(() => {
    localStorage.clear();
    delete (window as unknown as { webkit?: unknown }).webkit;
  });

  afterEach(() => {
    localStorage.clear();
    delete (window as unknown as { webkit?: unknown }).webkit;
  });

  it("detects native bridge correctly", () => {
    expect(hasNativeBridge()).toBe(false);

    (window as unknown as { webkit: unknown }).webkit = {
      messageHandlers: {
        unityBridge: {
          postMessage: vi.fn(),
        },
      },
    };
    expect(hasNativeBridge()).toBe(true);
  });

  it("manages mock bridge toggle via localStorage", () => {
    expect(isMockBridgeActive()).toBe(false);
    setMockBridgeActive(true);
    expect(isMockBridgeActive()).toBe(true);
    setMockBridgeActive(false);
    expect(isMockBridgeActive()).toBe(false);
  });

  it("posts bridge command to native webkit handler when available", () => {
    const postMessage = vi.fn();
    (window as unknown as { webkit: unknown }).webkit = {
      messageHandlers: {
        unityBridge: {
          postMessage,
        },
      },
    };

    const ok = postBridgeCommand({ type: "checkForUpdates" });
    expect(ok).toBe(true);
    expect(postMessage).toHaveBeenCalledWith({ type: "checkForUpdates" });

    postBridgeCommand({ type: "setAutoDownload", value: false });
    expect(postMessage).toHaveBeenCalledWith({ type: "setAutoDownload", value: false });

    postBridgeCommand({ type: "installNow" });
    expect(postMessage).toHaveBeenCalledWith({ type: "installNow" });

    postBridgeCommand({ type: "getState" });
    expect(postMessage).toHaveBeenCalledWith({ type: "getState" });
  });

  it("mock bridge responds to getState by dispatching state event", () => {
    setMockBridgeActive(true);
    const listener = vi.fn();
    document.addEventListener("unity-bridge", listener);

    postBridgeCommand({ type: "getState" });
    expect(listener).toHaveBeenCalled();
    const event = listener.mock.calls[0][0] as CustomEvent;
    expect(event.detail.type).toBe("state");
    expect(event.detail.appVersion).toBe("1.0.2");

    document.removeEventListener("unity-bridge", listener);
    setMockBridgeActive(false);
  });

  it("tracks dismissed update versions in localStorage", () => {
    expect(getDismissedVersion()).toBeNull();
    setDismissedVersion("1.0.3");
    expect(getDismissedVersion()).toBe("1.0.3");
    clearDismissedVersion();
    expect(getDismissedVersion()).toBeNull();
  });
});

describe("useDesktopBridge hook", () => {
  let mockPostMessage: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    localStorage.clear();
    mockPostMessage = vi.fn();
    (window as unknown as { webkit: unknown }).webkit = {
      messageHandlers: {
        unityBridge: {
          postMessage: mockPostMessage,
        },
      },
    };
  });

  afterEach(() => {
    localStorage.clear();
    delete (window as unknown as { webkit?: unknown }).webkit;
  });

  it("initializes as non-ready until state event arrives", () => {
    const { result } = renderHook(() => useDesktopBridge());
    expect(result.current.isDesktop).toBe(false);
    expect(result.current.state).toBeNull();
    expect(result.current.showCapsule).toBe(false);

    // Shell sends state
    const initialState: BridgeState = {
      appVersion: "1.0.2",
      autoDownload: true,
      lastCheckedAt: "2026-11-11T03:00:00Z",
      canCheck: true,
    };

    act(() => {
      dispatchMockBridgeEvent({ type: "state", ...initialState });
    });

    expect(result.current.isDesktop).toBe(true);
    expect(result.current.state?.appVersion).toBe("1.0.2");
    expect(result.current.state?.autoDownload).toBe(true);
  });

  it("shows update capsule on update-ready and supports dismiss", () => {
    const { result } = renderHook(() => useDesktopBridge());

    act(() => {
      dispatchMockBridgeEvent({
        type: "state",
        appVersion: "1.0.2",
        autoDownload: true,
        lastCheckedAt: null,
        canCheck: true,
      });
    });

    // Background download progress happens quietly (capsule not shown)
    act(() => {
      dispatchMockBridgeEvent({ type: "download-progress", percent: 50 });
    });
    expect(result.current.showCapsule).toBe(false);

    // Update ready!
    act(() => {
      dispatchMockBridgeEvent({ type: "update-ready", version: "1.0.3", willInstallOnQuit: true });
    });
    expect(result.current.showCapsule).toBe(true);
    expect(result.current.updateNotice.status).toBe("ready");
    expect(result.current.updateNotice.version).toBe("1.0.3");

    // Dismiss update for this version
    act(() => {
      result.current.dismissCurrentUpdate();
    });
    expect(result.current.showCapsule).toBe(false);
    expect(getDismissedVersion()).toBe("1.0.3");

    // Later, a newer version appears -> capsule shows again
    act(() => {
      dispatchMockBridgeEvent({ type: "update-ready", version: "1.0.4", willInstallOnQuit: true });
    });
    expect(result.current.showCapsule).toBe(true);
  });

  it("shows update capsule when autoDownload is false and update is found", () => {
    const { result } = renderHook(() => useDesktopBridge());

    act(() => {
      dispatchMockBridgeEvent({
        type: "state",
        appVersion: "1.0.2",
        autoDownload: false,
        lastCheckedAt: null,
        canCheck: true,
      });
    });

    act(() => {
      dispatchMockBridgeEvent({ type: "update-found", version: "1.0.3" });
    });

    expect(result.current.showCapsule).toBe(true);
    expect(result.current.updateNotice.status).toBe("found");
  });

  it("requests state snapshot on mount via getState handshake", () => {
    renderHook(() => useDesktopBridge());
    expect(mockPostMessage).toHaveBeenCalledWith({ type: "getState" });
  });

  it("dispatches postMessage commands when helper methods called", () => {
    const { result } = renderHook(() => useDesktopBridge());
    expect(mockPostMessage).toHaveBeenCalledWith({ type: "getState" });
    mockPostMessage.mockClear();

    act(() => {
      result.current.setAutoDownload(false);
    });
    expect(mockPostMessage).toHaveBeenCalledWith({ type: "setAutoDownload", value: false });

    act(() => {
      result.current.checkForUpdates();
    });
    expect(mockPostMessage).toHaveBeenCalledWith({ type: "checkForUpdates" });

    act(() => {
      result.current.installNow();
    });
    expect(mockPostMessage).toHaveBeenCalledWith({ type: "installNow" });
  });
});
