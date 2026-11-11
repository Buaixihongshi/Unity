export interface BridgeState {
  appVersion: string;
  autoDownload: boolean;
  lastCheckedAt: string | null;
  canCheck: boolean;
}

export type BridgeEventDetail =
  | ({ type: "state" } & BridgeState)
  | { type: "update-found"; version: string }
  | { type: "download-progress"; percent: number }
  | { type: "update-ready"; version: string; willInstallOnQuit: boolean }
  | { type: "up-to-date"; checkedAt: string };

export type BridgeCommand =
  | { type: "setAutoDownload"; value: boolean }
  | { type: "checkForUpdates" }
  | { type: "installNow" }
  | { type: "getState" };

const DISMISSED_VERSION_KEY = "unity_dismissed_update_version";
const MOCK_STORAGE_KEY = "unity_mock_bridge";

export function hasNativeBridge(): boolean {
  if (typeof window === "undefined") return false;
  const webkit = (window as unknown as { webkit?: { messageHandlers?: { unityBridge?: { postMessage?: (msg: unknown) => void } } } }).webkit;
  return typeof webkit?.messageHandlers?.unityBridge?.postMessage === "function";
}

export function isMockBridgeActive(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const params = new URLSearchParams(window.location.search);
    if (params.get("mockBridge") === "1" || params.get("mockBridge") === "true") {
      return true;
    }
    return window.localStorage.getItem(MOCK_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function setMockBridgeActive(active: boolean): void {
  if (typeof window === "undefined") return;
  try {
    if (active) {
      window.localStorage.setItem(MOCK_STORAGE_KEY, "1");
    } else {
      window.localStorage.removeItem(MOCK_STORAGE_KEY);
    }
  } catch {
    // ignore
  }
}

export function postBridgeCommand(command: BridgeCommand): boolean {
  if (typeof window === "undefined") return false;
  const webkit = (window as unknown as { webkit?: { messageHandlers?: { unityBridge?: { postMessage?: (msg: unknown) => void } } } }).webkit;
  if (typeof webkit?.messageHandlers?.unityBridge?.postMessage === "function") {
    webkit.messageHandlers.unityBridge.postMessage(command);
    return true;
  }
  if (isMockBridgeActive()) {
    handleMockCommand(command);
    return true;
  }
  return false;
}

export function getDismissedVersion(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(DISMISSED_VERSION_KEY);
  } catch {
    return null;
  }
}

export function setDismissedVersion(version: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(DISMISSED_VERSION_KEY, version);
  } catch {
    // ignore
  }
}

export function isVersionDismissed(version: string | undefined): boolean {
  if (!version) return false;
  return getDismissedVersion() === version;
}

export function clearDismissedVersion(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(DISMISSED_VERSION_KEY);
  } catch {
    // ignore
  }
}

// ── Mock Bridge Implementation for Dev and Demonstration ──

let mockState: BridgeState = {
  appVersion: "1.0.2",
  autoDownload: true,
  lastCheckedAt: new Date().toISOString(),
  canCheck: true,
};

export function dispatchMockBridgeEvent(detail: BridgeEventDetail): void {
  if (typeof document === "undefined") return;
  document.dispatchEvent(new CustomEvent("unity-bridge", { detail }));
}

function handleMockCommand(command: BridgeCommand): void {
  if (command.type === "getState") {
    dispatchMockBridgeEvent({ type: "state", ...mockState });
  } else if (command.type === "setAutoDownload") {
    mockState = { ...mockState, autoDownload: command.value };
    dispatchMockBridgeEvent({ type: "state", ...mockState });
  } else if (command.type === "checkForUpdates") {
    mockState = { ...mockState, canCheck: false };
    dispatchMockBridgeEvent({ type: "state", ...mockState });
    window.setTimeout(() => {
      const now = new Date().toISOString();
      mockState = { ...mockState, canCheck: true, lastCheckedAt: now };
      dispatchMockBridgeEvent({ type: "state", ...mockState });
      if (!mockState.autoDownload) {
        dispatchMockBridgeEvent({ type: "update-found", version: "1.0.3" });
      } else {
        dispatchMockBridgeEvent({ type: "update-ready", version: "1.0.3", willInstallOnQuit: true });
      }
    }, 600);
  } else if (command.type === "installNow") {
    // Simulate restart/install trigger
    // In real app, the shell will restart the app and install update.
  }
}

export function initMockBridgeIfEnabled(): void {
  if (isMockBridgeActive() && !hasNativeBridge()) {
    window.setTimeout(() => {
      dispatchMockBridgeEvent({ type: "state", ...mockState });
    }, 50);
  }
}
