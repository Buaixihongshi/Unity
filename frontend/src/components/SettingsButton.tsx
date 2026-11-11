import { useRef, useState, useCallback } from "react";
import { Settings } from "lucide-react";
import type { ProviderStatusState } from "../hooks/useProviderStatus";
import type { HubSettingsPatch, HubSettingsView, OnboardingView, RuntimeIdentity, SourceIdentity } from "../types";
import type { BridgeState } from "../lib/desktopBridge";
import { SettingsModal } from "./SettingsModal";

interface Props {
  view: HubSettingsView | null;
  conversationId?: string | null;
  providerStatus?: ProviderStatusState;
  onboarding?: OnboardingView | null;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  onRefreshProviders?: () => void;
  onVerifyProvider?: (providerId: string) => void;
  onSave: (patch: HubSettingsPatch) => void;
  onViewChange?: (view: HubSettingsView) => void;
  isDesktop?: boolean;
  bridgeState?: BridgeState | null;
  onSetAutoDownload?: (value: boolean) => void;
  onCheckForUpdates?: () => void;
  runtime?: RuntimeIdentity;
  frontend?: SourceIdentity;
}

export function SettingsButton({
  view,
  conversationId,
  providerStatus,
  onboarding,
  open: controlledOpen,
  onOpenChange,
  onRefreshProviders,
  onVerifyProvider,
  onSave,
  onViewChange,
  isDesktop,
  bridgeState,
  onSetAutoDownload,
  onCheckForUpdates,
  runtime,
  frontend,
}: Props) {
  const [localOpen, setLocalOpen] = useState(false);
  const open = controlledOpen ?? localOpen;
  const buttonRef = useRef<HTMLButtonElement>(null);

  const setOpen = useCallback((next: boolean) => {
    if (controlledOpen === undefined) setLocalOpen(next);
    onOpenChange?.(next);
  }, [controlledOpen, onOpenChange]);

  const handleClose = useCallback(() => {
    setOpen(false);
    setTimeout(() => buttonRef.current?.focus(), 0);
  }, [setOpen]);

  const handleSave = useCallback(
    (patch: HubSettingsPatch) => {
      onSave(patch);
      handleClose();
    },
    [onSave, handleClose],
  );

  return (
    <>
      <button
        ref={buttonRef}
        className="settings-button"
        data-testid="settings-button"
        aria-label="打开设置"
        disabled={!view}
        onClick={() => setOpen(true)}
      >
        <Settings size={18} strokeWidth={2} aria-hidden="true" />
      </button>
      {open && view && (
        <SettingsModal
          view={view}
          conversationId={conversationId}
          providerStatus={providerStatus}
          onboarding={onboarding}
          onRefreshProviders={onRefreshProviders}
          onVerifyProvider={onVerifyProvider ? (providerId) => {
            handleClose();
            onVerifyProvider(providerId);
          } : undefined}
          onSave={handleSave}
          onViewChange={onViewChange}
          onClose={handleClose}
          isDesktop={isDesktop}
          bridgeState={bridgeState}
          onSetAutoDownload={onSetAutoDownload}
          onCheckForUpdates={onCheckForUpdates}
          runtime={runtime}
          frontend={frontend}
        />
      )}
    </>
  );
}
