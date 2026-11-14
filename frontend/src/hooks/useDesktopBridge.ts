import { useState, useEffect, useCallback, useMemo } from "react";
import {
  hasNativeBridge,
  isMockBridgeActive,
  initMockBridgeIfEnabled,
  postBridgeCommand,
  getDismissedVersion,
  setDismissedVersion,
  type BridgeState,
  type BridgeEventDetail,
} from "../lib/desktopBridge";

export interface UpdateNotice {
  status: "idle" | "found" | "ready";
  version?: string;
  build?: string;
  willInstallOnQuit?: boolean;
  percent?: number;
}

export function useDesktopBridge() {
  const [state, setState] = useState<BridgeState | null>(null);
  const [updateNotice, setUpdateNotice] = useState<UpdateNotice>({ status: "idle" });
  const [dismissedVersion, setLocalDismissedVersion] = useState<string | null>(() => getDismissedVersion());
  const [isChecking, setIsChecking] = useState(false);

  useEffect(() => {
    initMockBridgeIfEnabled();

    const handleBridgeEvent = (event: Event) => {
      const customEvent = event as CustomEvent<BridgeEventDetail>;
      const detail = customEvent.detail;
      if (!detail) return;

      if (detail.type === "state") {
        setState({
          appVersion: detail.appVersion,
          autoDownload: detail.autoDownload,
          lastCheckedAt: detail.lastCheckedAt,
          canCheck: detail.canCheck,
          checking: detail.checking,
          updateError: detail.updateError,
          updateStatus: detail.updateStatus,
        });
        if (detail.checking !== undefined) setIsChecking(detail.checking);
        if (detail.updateStatus === "idle") setUpdateNotice({ status: "idle" });
      } else if (detail.type === "update-found") {
        setIsChecking(false);
        setUpdateNotice((prev) => ({
          ...prev,
          status: "found",
          version: detail.version,
          build: detail.build,
        }));
      } else if (detail.type === "download-progress") {
        setUpdateNotice((prev) => ({
          ...prev,
          percent: detail.percent,
        }));
      } else if (detail.type === "update-ready") {
        setIsChecking(false);
        setUpdateNotice({
          status: "ready",
          version: detail.version,
          build: detail.build,
          willInstallOnQuit: detail.willInstallOnQuit,
        });
      } else if (detail.type === "update-error") {
        setIsChecking(false);
        setState((prev) => prev ? { ...prev, updateError: detail.message } : prev);
      } else if (detail.type === "up-to-date") {
        setIsChecking(false);
        setState((prev) => (prev ? { ...prev, lastCheckedAt: detail.checkedAt, canCheck: true } : prev));
        setUpdateNotice((prev) => (prev.status === "ready" ? prev : { status: "idle" }));
      }
    };

    document.addEventListener("unity-bridge", handleBridgeEvent);
    postBridgeCommand({ type: "getState" });
    return () => {
      document.removeEventListener("unity-bridge", handleBridgeEvent);
    };
  }, []);

  // Is desktop bridge environment
  const isDesktop = useMemo(() => {
    const hasBridge = hasNativeBridge() || isMockBridgeActive();
    return hasBridge && Boolean(state);
  }, [state]);

  const isDismissed = useMemo(() => {
    const updateID = updateNotice.build ?? updateNotice.version;
    if (!updateID) return false;
    return dismissedVersion === updateID;
  }, [updateNotice.version, updateNotice.build, dismissedVersion]);

  // Notice should show only if:
  // 1) update-ready (ready to install on quit / restart now)
  // 2) or autoDownload is false and update is found
  // and NOT dismissed
  const showCapsule = useMemo(() => {
    if (!isDesktop || isDismissed) return false;
    if (updateNotice.status === "ready") return true;
    if (updateNotice.status === "found" && state?.autoDownload === false) return true;
    return false;
  }, [isDesktop, isDismissed, updateNotice.status, state?.autoDownload]);

  const dismissCurrentUpdate = useCallback(() => {
    const updateID = updateNotice.build ?? updateNotice.version;
    if (updateID) {
      setDismissedVersion(updateID);
      setLocalDismissedVersion(updateID);
    }
  }, [updateNotice.version, updateNotice.build]);

  const setAutoDownload = useCallback((value: boolean) => {
    postBridgeCommand({ type: "setAutoDownload", value });
    setState((prev) => (prev ? { ...prev, autoDownload: value } : prev));
  }, []);

  const checkForUpdates = useCallback(() => {
    if (state?.canCheck === false) return;
    setIsChecking(true);
    postBridgeCommand({ type: "checkForUpdates" });
  }, [state?.canCheck]);

  const installNow = useCallback(() => {
    postBridgeCommand({ type: "installNow" });
  }, []);

  return {
    isDesktop,
    state,
    updateNotice,
    showCapsule,
    isDismissed,
    isChecking,
    dismissCurrentUpdate,
    setAutoDownload,
    checkForUpdates,
    installNow,
  };
}
