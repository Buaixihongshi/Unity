import { PersonaPortrait } from "./PersonaPortrait";
import { Fragment, useState, useMemo, useEffect } from "react";
import {
  Activity,
  AlertTriangle,
  Brain,
  ChevronDown,
  KeyRound,
  Pencil,
  Puzzle,
  Plus,
  RefreshCw,
  RotateCcw,
  SlidersHorizontal,
  Trash2,
  UsersRound,
  Laptop,
  X,
} from "lucide-react";
import type { ProviderStatusState } from "../hooks/useProviderStatus";
import { roleToneIndex } from "../lib/personaColors";
import { ObservabilityOverview } from "./ObservabilityOverview";
import { MemoryPanel } from "./MemoryPanel";
import { DesktopSettingsSection } from "./DesktopSettingsSection";
import type { BridgeState } from "../lib/desktopBridge";
import { RoleEditor, type RoleRouteOption } from "./RoleEditor";
import { EffortSelect, effortAfterModelChange, effortChoices, hasFixedEffort } from "./EffortSelect";
import {
  createCustomAgent,
  deleteCustomAgent,
  fetchConnectionModels,
  refreshConnectionModels,
  updateCustomAgent,
} from "../lib/api";
import type {
  AgentOverridePatch,
  ConnectionCatalogView,
  CustomAgentInput,
  HubConnectionPatch,
  HubConnectionProtocol,
  HubConnectionView,
  HubSettingsAgentView,
  HubSettingsPatch,
  HubSettingsProviderView,
  HubSettingsExtensionsView,
  ExtensionName,
  HubSettingsView,
  OnboardingProviderStatus,
  OnboardingView,
} from "../types";

const INTERNAL_PROVIDERS = new Set(["fake"]);
const RETIRED_PROVIDERS = new Set(["gemini"]);

const RUNTIME_LABELS: Record<string, string> = {
  use_fake_cli: "演示模式（离线，不调用模型）",
  max_turns: "最大 Turn 数",
  max_a2a_invocations: "最大 A2A 调用数",
  cli_timeout: "执行窗口（秒，有工具返回可续时）",
  a2a_idle_timeout: "A2A 无进展超时（秒，0=关闭）",
};

const EXTENSION_OPTIONS: { name: ExtensionName; label: string; desc: string }[] = [
  { name: "discussion", label: "多人讨论", desc: "/discuss 与圆桌：几位伙伴先独立作答，再交流分歧、汇总结论。" },
  { name: "tasks", label: "任务", desc: "把对话整理成任务提议，确认后在独立工作区持续推进，并提供任务页。" },
  { name: "maintenance", label: "仓库维护", desc: "让伙伴在登记的仓库里执行维护并留下验证记录。" },
];

const MANUAL_MODEL = "__manual__";

/** Show the id only when the display name does not already say it. */
function modelLabel(model: { id: string; label: string }, open: string, close = ""): string {
  return model.label.toLowerCase() === model.id.toLowerCase()
    ? model.label
    : `${model.label}${open}${model.id}${close}`;
}

const CATALOG_STATUS_LABELS: Record<string, string> = {
  ready: "已获取",
  stale: "上次结果",
  error: "获取失败",
  unsupported: "不提供列表",
  loading: "获取中",
};

interface AgentDraft {
  providerModels?: Record<string, string>;
  providerEfforts?: Record<string, string>;
  defaultProvider?: string;
  fallbacks?: string[];
  fallbackContextApproved?: boolean;
  clear?: boolean;
}

type SettingsSectionId = "team" | "runtime" | "extensions" | "desktop" | "observability" | "memory";

interface Props {
  view: {
    runtime: import("../types").HubSettingsRuntimeView;
    extensions?: HubSettingsExtensionsView;
    providers: HubSettingsProviderView[];
    agents: HubSettingsAgentView[];
    connections?: HubConnectionView[];
  };
  providerStatus?: ProviderStatusState;
  onboarding?: OnboardingView | null;
  onRefreshProviders?: () => void;
  onVerifyProvider?: (providerId: string) => void;
  onSave: (patch: HubSettingsPatch) => void;
  /** Role add/edit/delete save immediately and hand back the fresh view. */
  onViewChange?: (view: HubSettingsView) => void;
  onClose: () => void;
  conversationId?: string | null;
  isDesktop?: boolean;
  bridgeState?: BridgeState | null;
  onSetAutoDownload?: (value: boolean) => void;
  onCheckForUpdates?: () => void;
  isCheckingUpdates?: boolean;
  runtime?: import("../types").RuntimeIdentity;
  frontend?: import("../types").SourceIdentity;
}

export function SettingsModal({
  view,
  providerStatus,
  onboarding,
  onRefreshProviders,
  onVerifyProvider,
  onSave,
  onViewChange,
  onClose,
  conversationId,
  isDesktop,
  bridgeState,
  onSetAutoDownload,
  onCheckForUpdates,
  isCheckingUpdates,
  runtime,
  frontend,
}: Props) {
  const [providerDraft, setProviderDraft] = useState<Record<string, boolean>>({});
  const [runtimeDraft, setRuntimeDraft] = useState<Record<string, string>>({});
  const [extensionDraft, setExtensionDraft] = useState<Partial<Record<ExtensionName, boolean>>>({});
  const [agentDraft, setAgentDraft] = useState<Record<string, AgentDraft>>({});
  const [connectionDrafts, setConnectionDrafts] = useState<Record<string, HubConnectionPatch>>({});
  const [catalogs, setCatalogs] = useState<Record<string, ConnectionCatalogView>>({});
  const [catalogLoading, setCatalogLoading] = useState<Record<string, boolean>>({});
  const [expandedAgentId, setExpandedAgentId] = useState<string | null>(null);
  const [manualModelInputMode, setManualModelInputMode] = useState<Record<string, boolean>>({});
  const [copiedCommand, setCopiedCommand] = useState<string | null>(null);
  const [activeSection, setActiveSection] = useState<SettingsSectionId>("team");
  /** "new" while adding a role, or the id of the custom role being edited. */
  const [roleEditor, setRoleEditor] = useState<string | null>(null);
  const [roleActionError, setRoleActionError] = useState<string | null>(null);

  const agentRows = view.agents ?? [];

  // Base connections: from view.connections if present, or synthesized from view.providers.
  // The offline demo provider is a test fixture, not a connection the user manages.
  const baseConnections: HubConnectionView[] = useMemo(() => {
    if (view.connections && view.connections.length > 0) {
      return view.connections.filter(
        (c) => !INTERNAL_PROVIDERS.has(c.id) && !RETIRED_PROVIDERS.has(c.id),
      );
    }
    return view.providers
      .filter((p) => !INTERNAL_PROVIDERS.has(p.id) && !RETIRED_PROVIDERS.has(p.id))
      .map((p) => ({
        id: p.id,
        name: p.label,
        kind: "cli" as const,
        protocol: null,
        base_url: null,
        enabled: p.enabled.value,
        has_key: false,
        timeout: 120,
        allow_private: true,
      }));
  }, [view.connections, view.providers]);

  // Unified list of active connections
  const allConnections = useMemo(() => {
    const list: Array<
      HubConnectionView & { isNew?: boolean; isDeleted?: boolean; patch?: HubConnectionPatch }
    > = [];
    const seenIds = new Set<string>();

    for (const base of baseConnections) {
      seenIds.add(base.id);
      const patch = connectionDrafts[base.id];
      if (patch?.delete) {
        list.push({ ...base, isDeleted: true, patch });
        continue;
      }
      const enabled =
        patch?.enabled !== undefined
          ? patch.enabled
          : base.id in providerDraft
          ? providerDraft[base.id]
          : base.enabled;
      list.push({
        ...base,
        name: patch?.name ?? base.name,
        protocol: patch?.protocol !== undefined ? patch.protocol : base.protocol,
        base_url: patch?.base_url !== undefined ? patch.base_url : base.base_url,
        enabled,
        timeout: patch?.timeout ?? base.timeout,
        allow_private: patch?.allow_private ?? base.allow_private,
        patch,
      });
    }

    for (const [id, patch] of Object.entries(connectionDrafts)) {
      if (!seenIds.has(id) && !patch.delete) {
        list.push({
          id,
          name: patch.name || "自定义 API 连接",
          kind: "api",
          protocol: patch.protocol || "openai_responses",
          base_url: patch.base_url || "https://api.openai.com/v1",
          enabled: patch.enabled ?? true,
          has_key: Boolean(patch.api_key),
          timeout: patch.timeout ?? 60,
          allow_private: patch.allow_private ?? false,
          isNew: true,
          patch,
        });
      }
    }

    return list;
  }, [baseConnections, connectionDrafts, providerDraft]);

  const [selectedConnectionId, setSelectedConnectionId] = useState<string>(
    () => baseConnections[0]?.id ?? "",
  );

  // Load model lists so each role can pick from them: CLIs are listed by the local binary
  // (no prompt, no usage); API connections only return their cached catalog.
  useEffect(() => {
    let cancelled = false;
    for (const connection of baseConnections) {
      setCatalogLoading((prev) => ({ ...prev, [connection.id]: true }));
      fetchConnectionModels(connection.id)
        .then((res) => {
          if (cancelled) return;
          setCatalogs((prev) => (prev[connection.id] ? prev : { ...prev, [connection.id]: res }));
        })
        .finally(() => {
          if (!cancelled) setCatalogLoading((prev) => ({ ...prev, [connection.id]: false }));
        })
        .catch(() => {});
    }
    return () => {
      cancelled = true;
    };
  }, [baseConnections]);

  const enabledProviders = useMemo(() => {
    const map: Record<string, boolean> = {};
    for (const p of view.providers) {
      map[p.id] = p.id in providerDraft ? providerDraft[p.id] : p.enabled.value;
    }
    for (const c of allConnections) {
      if (!c.isDeleted) {
        map[c.id] = c.enabled;
      }
    }
    return map;
  }, [view.providers, providerDraft, allConnections]);

  const visibleProviders = view.providers.filter(
    (provider) => !INTERNAL_PROVIDERS.has(provider.id) && !RETIRED_PROVIDERS.has(provider.id),
  );
  const routeProviders = view.providers.filter(
    (provider) => !RETIRED_PROVIDERS.has(provider.id),
  );

  const diagnosticsById = useMemo(
    () => new Map((providerStatus?.providers ?? []).map((provider) => [provider.id, provider])),
    [providerStatus?.providers],
  );
  const onboardingProvidersById = useMemo(
    () => new Map((onboarding?.providers ?? []).map((provider) => [provider.id, provider])),
    [onboarding?.providers],
  );
  const verifiedProviderIds = useMemo(
    () => new Set(onboarding?.verified_provider_ids ?? []),
    [onboarding?.verified_provider_ids],
  );

  const providerDisplayName = (provider: { id: string; label?: string; name?: string }) => {
    if (provider.id === "fake") return "演示模式（离线）";
    return provider.name || provider.label || provider.id;
  };

  const canRouteToProvider = (providerId: string) => {
    if (providerId === "fake") return true;
    if (verifiedProviderIds.has(providerId)) return true;
    return Boolean(enabledProviders[providerId]);
  };

  function copyCommand(key: string, command?: string | null) {
    if (!command) return;
    void navigator.clipboard?.writeText(command);
    setCopiedCommand(key);
  }

  function lifecycleFor(provider: { id: string; label?: string }) {
    const diagnostic = diagnosticsById.get(provider.id);
    const setup = onboardingProvidersById.get(provider.id);
    const verified = verifiedProviderIds.has(provider.id) || Boolean(setup?.verified);
    if (verified) return { label: "已验证", tone: "ready", diagnostic, setup };
    if (!providerStatus) {
      return { label: "状态未检测", tone: "checking", diagnostic, setup };
    }
    if (providerStatus?.loading && !diagnostic) {
      return { label: "检测中", tone: "checking", diagnostic, setup };
    }
    if (diagnostic?.available) {
      return { label: "已安装 · 待验证", tone: "installed", diagnostic, setup };
    }
    return { label: "未安装", tone: "missing", diagnostic, setup };
  }

  function effectiveAgent(agent: HubSettingsAgentView): {
    defaultProvider: string;
    fallbacks: string[];
    fallbackContextApproved: boolean;
    isUserOverride: boolean;
  } {
    const draft = agentDraft[agent.id];
    if (draft?.clear) {
      return {
        defaultProvider: agent.base_default_provider_id,
        fallbacks: agent.base_fallback_provider_ids,
        fallbackContextApproved: false,
        isUserOverride: false,
      };
    }
    const defaultProvider = draft?.defaultProvider ?? agent.default_provider_id.value;
    const fallbacks = draft?.fallbacks ?? agent.fallback_provider_ids.value;
    const fallbackContextApproved =
      draft?.fallbackContextApproved ?? agent.fallback_context_approved?.value ?? false;
    const isUserOverride =
      draft !== undefined ||
      agent.default_provider_id.source === "user" ||
      agent.fallback_provider_ids.source === "user" ||
      agent.provider_models?.source === "user" ||
      agent.provider_efforts?.source === "user" ||
      agent.fallback_context_approved?.source === "user";
    return { defaultProvider, fallbacks, fallbackContextApproved, isUserOverride };
  }

  function agentHasDiff(agent: HubSettingsAgentView): boolean {
    const draft = agentDraft[agent.id];
    if (!draft) return false;
    if (draft.clear) {
      return (
        agent.default_provider_id.source === "user" ||
        agent.fallback_provider_ids.source === "user" ||
        agent.provider_models?.source === "user" ||
        agent.provider_efforts?.source === "user" ||
        agent.fallback_context_approved?.source === "user"
      );
    }
    if (
      draft.providerModels !== undefined &&
      JSON.stringify(draft.providerModels) !== JSON.stringify(agent.provider_models?.value ?? {})
    ) {
      return true;
    }
    if (
      draft.providerEfforts !== undefined &&
      JSON.stringify(draft.providerEfforts) !== JSON.stringify(agent.provider_efforts?.value ?? {})
    ) {
      return true;
    }
    if (
      draft.defaultProvider !== undefined &&
      draft.defaultProvider !== agent.default_provider_id.value
    ) {
      return true;
    }
    if (
      draft.fallbacks !== undefined &&
      !sameStringList(draft.fallbacks, agent.fallback_provider_ids.value)
    ) {
      return true;
    }
    if (
      draft.fallbackContextApproved !== undefined &&
      draft.fallbackContextApproved !== (agent.fallback_context_approved?.value ?? false)
    ) {
      return true;
    }
    return false;
  }

  const hasConnectionDiff = useMemo(() => {
    return Object.keys(connectionDrafts).length > 0;
  }, [connectionDrafts]);

  const hasDiff = useMemo(() => {
    for (const p of view.providers) {
      if (p.id in providerDraft && providerDraft[p.id] !== p.enabled.value) return true;
    }
    for (const [key, raw] of Object.entries(runtimeDraft)) {
      const rtMap = view.runtime as unknown as Record<string, { value: unknown }>;
      const original = rtMap[key];
      if (!original) continue;
      if (typeof original.value === "boolean") {
        const newVal = raw === "true";
        if (newVal !== original.value) return true;
      } else {
        const numVal = Number(raw);
        if (!isNaN(numVal) && numVal !== original.value) return true;
      }
    }
    for (const agent of agentRows) {
      if (agentHasDiff(agent)) return true;
    }
    if (hasConnectionDiff) return true;
    if (Object.keys(extensionPatch()).length > 0) return true;
    return false;
  }, [view, providerDraft, runtimeDraft, extensionDraft, agentDraft, agentRows, hasConnectionDiff]);

  function extensionPatch(): Partial<Record<ExtensionName, boolean>> {
    const patch: Partial<Record<ExtensionName, boolean>> = {};
    for (const { name } of EXTENSION_OPTIONS) {
      const draft = extensionDraft[name];
      if (draft !== undefined && draft !== (view.extensions?.[name]?.value ?? false)) patch[name] = draft;
    }
    return patch;
  }

  function buildPatch(): HubSettingsPatch {
    const patch: HubSettingsPatch = {};

    // Providers
    const providerPatches: Record<string, { enabled: boolean }> = {};
    for (const p of view.providers) {
      if (p.id in providerDraft && providerDraft[p.id] !== p.enabled.value) {
        providerPatches[p.id] = { enabled: providerDraft[p.id] };
      }
    }
    if (Object.keys(providerPatches).length > 0) patch.providers = providerPatches;

    // Runtime
    const rt: Record<string, unknown> = {};
    for (const [key, raw] of Object.entries(runtimeDraft)) {
      const rtMap = view.runtime as unknown as Record<string, { value: unknown }>;
      const original = rtMap[key];
      if (!original) continue;
      if (typeof original.value === "boolean") {
        const newVal = raw === "true";
        if (newVal !== original.value) rt[key] = newVal;
      } else {
        const numVal = Number(raw);
        if (!isNaN(numVal) && numVal !== original.value) rt[key] = numVal;
      }
    }
    if (Object.keys(rt).length > 0) patch.runtime = rt;

    const extensions = extensionPatch();
    if (Object.keys(extensions).length > 0) patch.extensions = extensions;

    // Agents
    const agentPatches: Record<string, AgentOverridePatch> = {};
    for (const agent of agentRows) {
      const draft = agentDraft[agent.id];
      if (!draft) continue;
      if (draft.clear) {
        agentPatches[agent.id] = { clear: true };
        continue;
      }
      const entry: AgentOverridePatch = {};
      if (
        draft.providerModels !== undefined &&
        JSON.stringify(draft.providerModels) !== JSON.stringify(agent.provider_models?.value ?? {})
      ) {
        entry.provider_models = draft.providerModels;
      }
      if (
        draft.providerEfforts !== undefined &&
        JSON.stringify(draft.providerEfforts) !== JSON.stringify(agent.provider_efforts?.value ?? {})
      ) {
        entry.provider_efforts = draft.providerEfforts;
      }
      if (
        draft.defaultProvider !== undefined &&
        draft.defaultProvider !== agent.default_provider_id.value
      ) {
        entry.default_provider_id = draft.defaultProvider;
      }
      if (
        draft.fallbacks !== undefined &&
        !sameStringList(draft.fallbacks, agent.fallback_provider_ids.value)
      ) {
        entry.fallback_provider_ids = draft.fallbacks;
      }
      if (
        draft.fallbackContextApproved !== undefined &&
        draft.fallbackContextApproved !== (agent.fallback_context_approved?.value ?? false)
      ) {
        entry.fallback_context_approved = draft.fallbackContextApproved;
      }
      if (Object.keys(entry).length > 0) agentPatches[agent.id] = entry;
    }
    if (Object.keys(agentPatches).length > 0) patch.agents = agentPatches;

    // Connections
    if (Object.keys(connectionDrafts).length > 0) {
      patch.connections = connectionDrafts;
    }

    return patch;
  }

  function handleSave() {
    onSave(buildPatch());
  }

  function handleAttemptClose() {
    if (hasDiff) {
      const confirmed = window.confirm("当前有尚未保存的配置更改，关闭将放弃所有修改。确定要离开吗？");
      if (!confirmed) return;
    }
    onClose();
  }

  function toggleProvider(id: string) {
    if (id.startsWith("api-")) {
      const current = allConnections.find((c) => c.id === id);
      const isEnabled = current?.enabled ?? true;
      updateConnectionDraft(id, { enabled: !isEnabled });
    } else {
      setProviderDraft((prev) => ({ ...prev, [id]: !enabledProviders[id] }));
    }
  }

  function setAgentDefault(agentId: string, providerId: string) {
    setAgentDraft((prev) => {
      const current = prev[agentId] ?? {};
      return {
        ...prev,
        [agentId]: {
          ...current,
          defaultProvider: providerId,
          clear: false,
        },
      };
    });
  }

  function toggleAgentFallback(agent: HubSettingsAgentView, providerId: string) {
    setAgentDraft((prev) => {
      const eff = prev[agent.id]?.fallbacks ?? agent.fallback_provider_ids.value;
      const next = eff.includes(providerId)
        ? eff.filter((p) => p !== providerId)
        : [...eff, providerId];
      return {
        ...prev,
        [agent.id]: {
          ...(prev[agent.id] ?? {}),
          fallbacks: next,
          fallbackContextApproved: false, // 改备用须重确认
          clear: false,
        },
      };
    });
  }

  function toggleAgentFallbackContextApproval(agentId: string) {
    setAgentDraft((prev) => {
      const agent = agentRows.find((a) => a.id === agentId);
      const currentVal =
        prev[agentId]?.fallbackContextApproved ??
        agent?.fallback_context_approved?.value ??
        false;
      return {
        ...prev,
        [agentId]: {
          ...(prev[agentId] ?? {}),
          fallbackContextApproved: !currentVal,
          clear: false,
        },
      };
    });
  }

  function resetAgent(agentId: string) {
    setAgentDraft((prev) => ({ ...prev, [agentId]: { clear: true } }));
  }

  function routeAllAgentsTo(providerId: string) {
    const next: Record<string, AgentDraft> = {};
    for (const agent of agentRows) {
      const { fallbacks, fallbackContextApproved } = effectiveAgent(agent);
      // Keep each role's real fallbacks; never add the offline demo provider as a silent fallback.
      const kept = fallbacks.filter((id) => id !== providerId && !INTERNAL_PROVIDERS.has(id));
      next[agent.id] = {
        defaultProvider: providerId,
        fallbacks: kept,
        fallbackContextApproved: sameStringList(kept, fallbacks) && fallbackContextApproved,
        clear: false,
      };
    }
    setAgentDraft((prev) =>
      Object.fromEntries(
        Object.entries(next).map(([id, draft]) => {
          const agent = agentRows.find((a) => a.id === id);
          return [
            id,
            {
              ...draft,
              providerModels: prev[id]?.clear ? agent?.base_provider_models ?? {} : prev[id]?.providerModels,
              providerEfforts: prev[id]?.clear ? agent?.base_provider_efforts ?? {} : prev[id]?.providerEfforts,
            },
          ];
        }),
      ),
    );
  }

  function updateConnectionDraft(id: string, patch: Partial<HubConnectionPatch>) {
    setConnectionDrafts((prev) => ({
      ...prev,
      [id]: { ...(prev[id] ?? {}), ...patch },
    }));
  }

  function handleCreateApiConnection() {
    const newId = `api-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
    setConnectionDrafts((prev) => ({
      ...prev,
      [newId]: {
        name: "自定义 API 连接",
        protocol: "openai_responses",
        base_url: "https://api.openai.com/v1",
        enabled: true,
        timeout: 60,
        allow_private: false,
        key_action: "replace",
        api_key: "",
      },
    }));
    setSelectedConnectionId(newId);
  }

  function handleDeleteConnection(id: string) {
    if (window.confirm("确定要删除该连接配置吗？保存后生效。")) {
      setConnectionDrafts((prev) => ({
        ...prev,
        [id]: { ...(prev[id] ?? {}), delete: true },
      }));
      setSelectedConnectionId(baseConnections.find((c) => c.id !== id)?.id ?? "");
    }
  }

  async function handleRefreshCatalog(connId: string) {
    const isNew = connectionDrafts[connId] && !baseConnections.some((c) => c.id === connId);
    if (isNew) return; // 新建未保存连接先保存再刷新

    setCatalogLoading((prev) => ({ ...prev, [connId]: true }));
    try {
      const res = await refreshConnectionModels(connId);
      setCatalogs((prev) => ({ ...prev, [connId]: res }));
    } catch (err) {
      setCatalogs((prev) => ({
        ...prev,
        [connId]: {
          connection_id: connId,
          status: "error",
          models: [],
          fetched_at: null,
          error: err instanceof Error ? err.message : "获取模型目录失败",
        },
      }));
    } finally {
      setCatalogLoading((prev) => ({ ...prev, [connId]: false }));
    }
  }

  const sourceLabel = (s: string) => {
    switch (s) {
      case "user":
        return "用户";
      case "env":
        return "env";
      case "default":
        return "默认";
      case "committed":
        return "committed";
      default:
        return s;
    }
  };

  const warningProviders = visibleProviders.filter(
    (p) => !INTERNAL_PROVIDERS.has(p.id) && enabledProviders[p.id],
  );

  const selectedConnection =
    allConnections.find((c) => c.id === selectedConnectionId && !c.isDeleted) ?? null;

  function routeName(id: string): string {
    const connection = allConnections.find((c) => c.id === id);
    if (connection) return connection.name;
    const provider = view.providers.find((p) => p.id === id);
    return provider ? providerDisplayName(provider) : id;
  }

  /** Connections a role can route to; the demo provider is listed only while a role still uses it. */
  function routeOptionsFor(inUse: string[]): Array<{ id: string; label: string }> {
    const options: Array<{ id: string; label: string }> = [];
    const seen = new Set<string>();
    for (const c of allConnections) {
      if (c.isDeleted) continue;
      options.push({ id: c.id, label: `${c.name}（${c.kind === "api" ? "API" : "CLI"}）` });
      seen.add(c.id);
    }
    for (const p of routeProviders) {
      if (seen.has(p.id) || (INTERNAL_PROVIDERS.has(p.id) && !inUse.includes(p.id))) continue;
      options.push({ id: p.id, label: providerDisplayName(p) });
      seen.add(p.id);
    }
    for (const id of inUse) {
      if (!seen.has(id)) {
        options.push({ id, label: id });
        seen.add(id);
      }
    }
    return options;
  }

  /** Saved connections only: a role is stored right away, unsaved drafts do not exist yet. */
  function roleRouteOptions(inUse: string[]): RoleRouteOption[] {
    const options: RoleRouteOption[] = [];
    const seen = new Set<string>();
    for (const c of baseConnections) {
      options.push({
        id: c.id,
        label: `${c.name}（${c.kind === "api" ? "API" : "CLI"}）`,
        requiresModel: c.kind === "api",
        disabled: !canRouteToProvider(c.id),
      });
      seen.add(c.id);
    }
    for (const p of routeProviders) {
      if (seen.has(p.id) || (INTERNAL_PROVIDERS.has(p.id) && !inUse.includes(p.id))) continue;
      options.push({ id: p.id, label: providerDisplayName(p), requiresModel: false, disabled: !canRouteToProvider(p.id) });
      seen.add(p.id);
    }
    return options;
  }

  async function submitRole(input: CustomAgentInput) {
    const editingId = roleEditor && roleEditor !== "new" ? roleEditor : null;
    const updated = editingId ? await updateCustomAgent(editingId, input) : await createCustomAgent(input);
    if (editingId) {
      // The dialog saved this role's route; a pending table edit would overwrite it.
      setAgentDraft((prev) => {
        const next = { ...prev };
        delete next[editingId];
        return next;
      });
    }
    setRoleEditor(null);
    setRoleActionError(null);
    onViewChange?.(updated);
  }

  async function removeRole(agent: HubSettingsAgentView) {
    if (!window.confirm(`删除角色「${agent.display_name}」？历史消息会保留，之后不能再 @ 点名或接力给它。`)) return;
    try {
      const updated = await deleteCustomAgent(agent.id);
      setAgentDraft((prev) => {
        const next = { ...prev };
        delete next[agent.id];
        return next;
      });
      if (roleEditor === agent.id) setRoleEditor(null);
      setRoleActionError(null);
      onViewChange?.(updated);
    } catch (err) {
      setRoleActionError(err instanceof Error ? err.message : "删除角色失败");
    }
  }

  function agentModels(agent: HubSettingsAgentView): Record<string, string> {
    const draft = agentDraft[agent.id];
    return draft?.clear
      ? agent.base_provider_models ?? {}
      : draft?.providerModels ?? agent.provider_models?.value ?? {};
  }

  function agentEfforts(agent: HubSettingsAgentView): Record<string, string> {
    const draft = agentDraft[agent.id];
    return draft?.clear
      ? agent.base_provider_efforts ?? {}
      : draft?.providerEfforts ?? agent.provider_efforts?.value ?? {};
  }

  function withEffort(efforts: Record<string, string>, providerId: string, effort: string) {
    const next = { ...efforts };
    if (effort) next[providerId] = effort;
    else delete next[providerId];
    return next;
  }

  function effortChoicesFor(providerId: string, model: string): string[] {
    if (hasFixedEffort(providerId, model)) return [];
    const levels = view.providers.find((provider) => provider.id === providerId)?.efforts ?? [];
    return effortChoices(levels, catalogs[providerId], model);
  }

  function setAgentModel(agent: HubSettingsAgentView, providerId: string, raw: string) {
    const next = { ...agentModels(agent) };
    const model = raw.trim();
    if (model) next[providerId] = model;
    else delete next[providerId];
    const efforts = agentEfforts(agent);
    const effort = efforts[providerId] ?? "";
    const kept = effortAfterModelChange(effort, effortChoicesFor(providerId, model));
    setAgentDraft((prev) => ({
      ...prev,
      [agent.id]: {
        ...(prev[agent.id] ?? {}),
        providerModels: next,
        ...(kept !== effort ? { providerEfforts: withEffort(efforts, providerId, kept) } : {}),
        clear: false,
      },
    }));
  }

  function setAgentEffort(agent: HubSettingsAgentView, providerId: string, effort: string) {
    const next = withEffort(agentEfforts(agent), providerId, effort);
    setAgentDraft((prev) => ({
      ...prev,
      [agent.id]: { ...(prev[agent.id] ?? {}), providerEfforts: next, clear: false },
    }));
  }

  function renderModelField(
    agent: HubSettingsAgentView,
    providerId: string,
    models: Record<string, string>,
  ) {
    const effort = agentEfforts(agent)[providerId] ?? "";
    return (
      <div className="model-effort-group">
        {renderModelInput(agent, providerId, models)}
        <EffortSelect
          label={`${agent.display_name} ${providerId} 推理强度`}
          value={effort}
          choices={effortChoicesFor(providerId, models[providerId] ?? "")}
          onChange={(value) => setAgentEffort(agent, providerId, value)}
        />
      </div>
    );
  }

  function renderModelInput(
    agent: HubSettingsAgentView,
    providerId: string,
    models: Record<string, string>,
  ) {
    const connection = allConnections.find((c) => c.id === providerId);
    const isCli = (connection?.kind ?? "cli") === "cli";
    const value = models[providerId] ?? "";
    const label = `${agent.display_name} ${providerId} 模型`;
    const missing = !isCli && !value.trim();
    const manualKey = `${agent.id}:${providerId}`;
    const catalogModels = catalogs[providerId]?.models ?? [];

    const modelOption = (model: (typeof catalogModels)[number]) => (
      <option key={model.id} value={model.id}>
        {modelLabel(model, "（", "）")}
      </option>
    );

    if (catalogModels.length > 0 && !manualModelInputMode[manualKey]) {
      const groups = new Map<string, typeof catalogModels>();
      for (const model of catalogModels) {
        const vendor = model.vendor || "其他";
        groups.set(vendor, [...(groups.get(vendor) ?? []), model]);
      }
      const known = catalogModels.some((model) => model.id === value);
      return (
        <select
          className={`custom-select${missing ? " is-invalid" : ""}`}
          aria-label={label}
          aria-invalid={missing || undefined}
          value={value}
          onChange={(e) => {
            if (e.target.value === MANUAL_MODEL) {
              setManualModelInputMode((prev) => ({ ...prev, [manualKey]: true }));
              return;
            }
            setAgentModel(agent, providerId, e.target.value);
          }}
        >
          <option value="">{isCli ? "沿用 CLI 默认" : "请选择模型"}</option>
          {value && !known && <option value={value}>{value}</option>}
          {groups.size > 1
            ? Array.from(groups, ([vendor, items]) => (
                <optgroup key={vendor} label={vendor}>
                  {items.map(modelOption)}
                </optgroup>
              ))
            : catalogModels.map(modelOption)}
          <option value={MANUAL_MODEL}>手动输入…</option>
        </select>
      );
    }

    const input = (
      <input
        type="text"
        className={`custom-input${missing ? " is-invalid" : ""}`}
        aria-label={label}
        aria-invalid={missing || undefined}
        value={value}
        placeholder={
          catalogLoading[providerId]
            ? "正在获取模型列表…"
            : isCli
            ? "沿用 CLI 默认"
            : "必填：Model ID"
        }
        onChange={(e) => setAgentModel(agent, providerId, e.target.value)}
      />
    );
    if (catalogModels.length === 0) return input;
    return (
      <div className="model-manual-field">
        {input}
        <button
          type="button"
          className="btn-tiny"
          onClick={() => setManualModelInputMode((prev) => ({ ...prev, [manualKey]: false }))}
        >
          从列表选择
        </button>
      </div>
    );
  }

  return (
    <div className="settings-modal-backdrop" onClick={handleAttemptClose}>
      <div className="settings-modal settings-modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="settings-modal-header">
          <div>
            <h2>设置</h2>
            <p>管理 CLI 与自定义 API、角色调度、运行参数、扩展、全局观测与记忆</p>
          </div>
          <button
            type="button"
            className="settings-close"
            data-testid="settings-close"
            aria-label="关闭设置"
            onClick={handleAttemptClose}
          >
            <X size={19} aria-hidden="true" />
          </button>
        </div>

        <div className="settings-modal-content">
          <nav className="settings-nav" aria-label="设置分类">
            <button
              type="button"
              className={activeSection === "team" ? "is-active" : ""}
              aria-pressed={activeSection === "team"}
              onClick={() => setActiveSection("team")}
            >
              <UsersRound size={17} aria-hidden="true" />
              <span>团队配置</span>
            </button>
            <button
              type="button"
              className={activeSection === "runtime" ? "is-active" : ""}
              aria-pressed={activeSection === "runtime"}
              onClick={() => setActiveSection("runtime")}
            >
              <SlidersHorizontal size={17} aria-hidden="true" />
              <span>运行参数</span>
            </button>
            <button
              type="button"
              className={activeSection === "extensions" ? "is-active" : ""}
              aria-pressed={activeSection === "extensions"}
              onClick={() => setActiveSection("extensions")}
            >
              <Puzzle size={17} aria-hidden="true" />
              <span>扩展</span>
            </button>
            {isDesktop ? (
              <button
                type="button"
                className={activeSection === "desktop" ? "is-active" : ""}
                aria-pressed={activeSection === "desktop"}
                onClick={() => setActiveSection("desktop")}
                data-testid="settings-nav-desktop"
              >
                <Laptop size={17} aria-hidden="true" />
                <span>桌面应用</span>
              </button>
            ) : null}
            <button
              type="button"
              className={activeSection === "observability" ? "is-active" : ""}
              aria-pressed={activeSection === "observability"}
              onClick={() => setActiveSection("observability")}
            >
              <Activity size={17} aria-hidden="true" />
              <span>观测</span>
            </button>
            <button
              type="button"
              className={activeSection === "memory" ? "is-active" : ""}
              aria-pressed={activeSection === "memory"}
              onClick={() => setActiveSection("memory")}
            >
              <Brain size={17} aria-hidden="true" />
              <span>记忆</span>
            </button>
          </nav>

          <div className="settings-panel">
            <div className="settings-modal-body">
              {activeSection === "team" ? (
                <div className="team-settings">
                  <section className="settings-section team-roles" aria-labelledby="settings-team-title">
                    <div className="settings-section-heading">
                      <div>
                        <h3 id="settings-team-title">团队配置</h3>
                        <p className="settings-section-hint">
                          为每位角色指定主用连接和模型；主用不可用时按备用连接顺序回退。
                        </p>
                      </div>
                      <div className="team-role-actions">
                      <button
                        type="button"
                        className="btn-text-action"
                        data-testid="add-role-btn"
                        disabled={!onViewChange || roleEditor === "new"}
                        onClick={() => {
                          setRoleActionError(null);
                          setRoleEditor("new");
                        }}
                      >
                        <Plus size={12} aria-hidden="true" />
                        <span>新增角色</span>
                      </button>
                      <button
                        type="button"
                        className="button-secondary team-apply-all"
                        data-testid="team-use-codex"
                        disabled={
                          !enabledProviders.codex ||
                          !canRouteToProvider("codex") ||
                          agentRows.length === 0
                        }
                        onClick={() => routeAllAgentsTo("codex")}
                      >
                        {enabledProviders.codex ? "全部使用 Codex" : "Codex 尚未启用"}
                      </button>
                      </div>
                    </div>

                    <table className="role-table">
                      <thead>
                        <tr>
                          <th scope="col">角色</th>
                          <th scope="col">主用连接</th>
                          <th scope="col">模型</th>
                          <th scope="col">备用连接</th>
                          <th scope="col">
                            <span className="sr-only">操作</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {agentRows.map((agent) => {
                          const {
                            defaultProvider,
                            fallbacks,
                            fallbackContextApproved,
                            isUserOverride,
                          } = effectiveAgent(agent);
                          const models = agentModels(agent);
                          const options = routeOptionsFor([defaultProvider, ...fallbacks]);
                          const expanded = expandedAgentId === agent.id;
                          const detailId = `agent-fallback-detail-${agent.id}`;

                          return (
                            <Fragment key={agent.id}>
                              <tr
                                className={expanded ? "is-expanded" : undefined}
                                data-testid={"agent-row-" + agent.id}
                              >
                                <th scope="row">
                                  <div className="role-identity">
                                    <span
                                      className={`role-avatar ${agent.custom ? `avatar-custom avatar-tone-${roleToneIndex(agent.id)}` : `avatar-${agent.id}`}`}
                                      aria-hidden="true"
                                    >
                                      <PersonaPortrait agentId={agent.id}>{agent.display_name.slice(0, 1)}</PersonaPortrait>
                                    </span>
                                    <div className="role-labels">
                                      <span className="role-name" title={agent.display_name}>{agent.display_name}</span>
                                      {agent.custom && (
                                        <span className="settings-source-badge">自定义</span>
                                      )}
                                      {isUserOverride && (
                                        <span className="settings-source-badge">已修改</span>
                                      )}
                                    </div>
                                  </div>
                                </th>
                                <td>
                                  <select
                                    className="custom-select"
                                    aria-label={`${agent.display_name} 主用连接`}
                                    data-testid={"agent-default-" + agent.id}
                                    value={defaultProvider}
                                    onChange={(e) => setAgentDefault(agent.id, e.target.value)}
                                  >
                                    {options.map((option) => (
                                      <option
                                        key={option.id}
                                        value={option.id}
                                        disabled={!canRouteToProvider(option.id)}
                                      >
                                        {option.label}
                                      </option>
                                    ))}
                                  </select>
                                </td>
                                <td>{renderModelField(agent, defaultProvider, models)}</td>
                                <td>
                                  <button
                                    type="button"
                                    className="role-fallback-toggle"
                                    aria-expanded={expanded}
                                    aria-controls={detailId}
                                    aria-label={`${agent.display_name} 备用连接：${
                                      fallbacks.map(routeName).join("、") || "无"
                                    }`}
                                    onClick={() =>
                                      setExpandedAgentId(expanded ? null : agent.id)
                                    }
                                  >
                                    <span>{fallbacks.map(routeName).join("、") || "无"}</span>
                                    <ChevronDown size={14} aria-hidden="true" />
                                  </button>
                                </td>
                                <td className="role-actions">
                                  {agent.custom && onViewChange && (
                                    <>
                                      <button
                                        type="button"
                                        className="role-icon-btn"
                                        data-testid={"agent-edit-" + agent.id}
                                        aria-label={`编辑角色 ${agent.display_name}`}
                                        title="编辑角色"
                                        onClick={() => {
                                          setRoleActionError(null);
                                          setRoleEditor(agent.id);
                                        }}
                                      >
                                        <Pencil size={13} aria-hidden="true" />
                                      </button>
                                      <button
                                        type="button"
                                        className="role-icon-btn is-danger"
                                        data-testid={"agent-delete-" + agent.id}
                                        aria-label={`删除角色 ${agent.display_name}`}
                                        title="删除角色"
                                        onClick={() => void removeRole(agent)}
                                      >
                                        <Trash2 size={13} aria-hidden="true" />
                                      </button>
                                    </>
                                  )}
                                  {isUserOverride && (
                                    <button
                                      type="button"
                                      className="settings-agent-reset"
                                      data-testid={"agent-reset-" + agent.id}
                                      onClick={() => resetAgent(agent.id)}
                                    >
                                      重置
                                    </button>
                                  )}
                                </td>
                              </tr>
                              {expanded && (
                                <tr className="role-detail-row" id={detailId}>
                                  <td colSpan={5}>
                                    <fieldset className="role-fallbacks">
                                      <legend>{agent.display_name} 的备用连接</legend>
                                      <div className="fallback-options-grid">
                                        {options
                                          .filter((option) => option.id !== defaultProvider)
                                          .map((option) => (
                                            <label
                                              key={option.id}
                                              className="fallback-checkbox-label"
                                            >
                                              <input
                                                type="checkbox"
                                                data-testid={`agent-fallback-${agent.id}-${option.id}`}
                                                checked={fallbacks.includes(option.id)}
                                                disabled={!canRouteToProvider(option.id)}
                                                onChange={() => toggleAgentFallback(agent, option.id)}
                                              />
                                              <span>{option.label}</span>
                                            </label>
                                          ))}
                                      </div>
                                    </fieldset>

                                    {fallbacks.length > 0 && (
                                      <div className="role-fallback-models">
                                        {fallbacks.map((fbId) => (
                                          <div className="form-group" key={fbId}>
                                            <span className="form-label">{routeName(fbId)} 模型</span>
                                            {renderModelField(agent, fbId, models)}
                                          </div>
                                        ))}
                                      </div>
                                    )}

                                    <label className="fallback-approve-label">
                                      <input
                                        type="checkbox"
                                        data-testid={`agent-fallback-approve-${agent.id}`}
                                        checked={fallbackContextApproved}
                                        onChange={() => toggleAgentFallbackContextApproval(agent.id)}
                                      />
                                      <span>允许备用连接接收当前任务上下文</span>
                                    </label>
                                    <p className="outbound-tip">
                                      切换到备用连接时，任务上下文会发给备用方；修改备用连接后需要重新勾选。鉴权失败（401）不会切换到备用，已输出的内容不会被静默重放。
                                    </p>
                                  </td>
                                </tr>
                              )}
                            </Fragment>
                          );
                        })}
                      </tbody>
                    </table>

                    {roleActionError && (
                      <p className="role-editor-error" role="alert">
                        {roleActionError}
                      </p>
                    )}
                    {roleEditor && (
                      <RoleEditor
                        key={roleEditor}
                        agent={roleEditor === "new" ? undefined : agentRows.find((a) => a.id === roleEditor)}
                        routeOptions={roleRouteOptions(
                          roleEditor === "new"
                            ? []
                            : (() => {
                                const editing = agentRows.find((a) => a.id === roleEditor);
                                return editing
                                  ? [editing.default_provider_id.value, ...editing.fallback_provider_ids.value]
                                  : [];
                              })(),
                        )}
                        modelSuggestions={(id) => (catalogs[id]?.models ?? []).map((model) => model.id)}
                        effortChoices={effortChoicesFor}
                        onSubmit={submitRole}
                        onCancel={() => setRoleEditor(null)}
                      />
                    )}
                  </section>

                  <section className="settings-section team-connections" aria-labelledby="settings-providers-title">
                    <div className="settings-section-heading">
                      <div>
                        <h3 id="settings-providers-title">连接</h3>
                        <p className="settings-section-hint">
                          本机 CLI 与自定义 API。启用并验证后，才能分配给角色。
                        </p>
                      </div>
                      <div className="workbench-sidebar-actions">
                        <button
                          type="button"
                          className="settings-refresh-btn-sm"
                          onClick={onRefreshProviders ?? providerStatus?.refresh}
                          disabled={providerStatus?.loading}
                          title="重新检测 CLI"
                        >
                          <RefreshCw
                            size={12}
                            className={providerStatus?.loading ? "animate-spin" : ""}
                          />
                          <span>{providerStatus?.loading ? "检测中" : "检测"}</span>
                        </button>
                        <button
                          type="button"
                          className="btn-text-action"
                          data-testid="add-connection-btn"
                          onClick={handleCreateApiConnection}
                        >
                          <Plus size={12} />
                          <span>新增 API</span>
                        </button>
                      </div>
                    </div>

                    <div className="workbench-split">
                      <aside className="workbench-sidebar">
                        <div className="workbench-conn-list">
                          {allConnections
                            .filter((c) => !c.isDeleted)
                            .map((c) => {
                              const isCli = c.kind === "cli";
                              const lifecycle = lifecycleFor({ id: c.id, label: c.name });
                              const isSelected = selectedConnectionId === c.id;
                              const isEnabled = enabledProviders[c.id] ?? c.enabled;
                              const diagnostic = lifecycle.diagnostic;
                              const setup = lifecycle.setup as OnboardingProviderStatus | undefined;
                              const verified = lifecycle.tone === "ready";
                              const available = Boolean(diagnostic?.available);

                              return (
                                <article
                                  key={c.id}
                                  className={`conn-list-item ${isSelected ? "is-active" : ""} ${
                                    !isEnabled ? "is-disabled" : ""
                                  }`}
                                  data-testid={`provider-card-${c.id}`}
                                  onClick={() => setSelectedConnectionId(c.id)}
                                >
                                  <div className="conn-item-main">
                                    <div className="conn-item-header">
                                      <span
                                        className={`status-dot dot-${
                                          isCli ? lifecycle.tone : c.has_key ? "ready" : "warning"
                                        }`}
                                      />
                                      <strong>{c.name}</strong>
                                      <span className={`kind-tag ${c.kind}`}>
                                        {c.kind.toUpperCase()}
                                      </span>
                                    </div>
                                    <div className="conn-item-sub">
                                      {isCli ? (
                                        <span
                                          className={`settings-provider-lifecycle is-${lifecycle.tone}`}
                                        >
                                          {lifecycle.label}
                                        </span>
                                      ) : (
                                        <span className="conn-sub-url">
                                          {c.base_url || "未设置 Base URL"} ·{" "}
                                          {c.has_key ? "已配凭据" : "未配凭据"}
                                        </span>
                                      )}
                                    </div>
                                  </div>

                                  <div
                                    className="conn-item-controls"
                                    onClick={(e) => e.stopPropagation()}
                                  >
                                    {isCli && !available && !verified ? (
                                      setup?.install_command ? (
                                        <button
                                          type="button"
                                          className="btn-tiny"
                                          onClick={() =>
                                            copyCommand(`${c.id}:install`, setup.install_command)
                                          }
                                        >
                                          {copiedCommand === `${c.id}:install`
                                            ? "已复制"
                                            : "安装命令"}
                                        </button>
                                      ) : null
                                    ) : isCli && !verified && onVerifyProvider ? (
                                      <button
                                        type="button"
                                        className="settings-provider-verify btn-tiny"
                                        onClick={() => onVerifyProvider(c.id)}
                                      >
                                        验证真实对话
                                      </button>
                                    ) : null}

                                    <button
                                      type="button"
                                      role="switch"
                                      aria-label={`启用 ${c.name}`}
                                      aria-checked={isEnabled}
                                      className={"settings-toggle" + (isEnabled ? " on" : "")}
                                      data-testid={"toggle-" + c.id}
                                      disabled={
                                        isCli &&
                                        Boolean(providerStatus) &&
                                        !available &&
                                        !verified &&
                                        !isEnabled
                                      }
                                      onClick={() => toggleProvider(c.id)}
                                    >
                                      {isEnabled ? "ON" : "OFF"}
                                    </button>
                                  </div>
                                </article>
                              );
                            })}
                        </div>
                        {warningProviders.length > 0 && (
                          <p className="settings-cost-warning" role="note">
                            <AlertTriangle size={14} aria-hidden="true" />
                            <span>
                              已启用的 {warningProviders.map((p) => p.label).join("、")}{" "}
                              会用你的账号发起真实请求，可能计入额度或产生费用。
                            </span>
                          </p>
                        )}
                      </aside>

                      <div className="workbench-detail">
                        {selectedConnection ? (
                          <div className="conn-workbench-panel">
                            {(() => {
                              const c = selectedConnection;
                              const isCli = c.kind === "cli";
                              const lifecycle = lifecycleFor({ id: c.id, label: c.name });
                              const catalog = catalogs[c.id];
                              const isLoadingCat = catalogLoading[c.id];
                              const isNew = Boolean(c.isNew);

                              return (
                                <>
                                  <div className="detail-header">
                                    <div className="detail-header-title">
                                      <div>
                                        <div className="detail-title-row">
                                          <h3>{c.name} · 连接与凭据配置</h3>
                                          <span className={`kind-tag ${c.kind}`}>
                                            {c.kind.toUpperCase()}
                                          </span>
                                          {c.kind === "api" && (
                                            <button
                                              type="button"
                                              className="btn-danger-outline btn-sm"
                                              onClick={() => handleDeleteConnection(c.id)}
                                            >
                                              <Trash2 size={13} />
                                              <span>删除连接</span>
                                            </button>
                                          )}
                                        </div>
                                        <p className="detail-sub">
                                          {isCli
                                            ? "本地 CLI 接入服务，具备本地工具执行权限"
                                            : "自定义远程 API 端点，支持 OpenAI 及 Anthropic 协议"}
                                        </p>
                                      </div>
                                    </div>

                                    <div className="detail-header-badges">
                                      {isCli ? (
                                        <span className="ability-badge cli">
                                          具备工具执行环境
                                        </span>
                                      ) : (
                                        <span className="ability-badge api">
                                          仅纯文本对话与讨论
                                        </span>
                                      )}
                                    </div>
                                  </div>

                                  {/* CLI 属性详情 */}
                                  {isCli ? (
                                    <div className="workbench-card">
                                      <div className="card-heading">
                                        <span>CLI 本地运行环境诊断</span>
                                        <span
                                          className={`settings-provider-lifecycle is-${lifecycle.tone}`}
                                        >
                                          {lifecycle.label}
                                        </span>
                                      </div>
                                      <p className="settings-provider-detail">
                                        {lifecycle.diagnostic?.version ||
                                          lifecycle.diagnostic?.error ||
                                          (lifecycle.tone === "ready"
                                            ? "已通过 MCP 真实对话验证"
                                            : "等待本机检测")}
                                      </p>

                                      <div className="settings-provider-actions">
                                        {lifecycle.setup?.install_command ? (
                                          <button
                                            type="button"
                                            onClick={() =>
                                              copyCommand(
                                                `${c.id}:install`,
                                                lifecycle.setup?.install_command,
                                              )
                                            }
                                          >
                                            {copiedCommand === `${c.id}:install`
                                              ? "安装命令已复制"
                                              : "复制安装命令"}
                                          </button>
                                        ) : null}
                                        {lifecycle.setup?.install_url ? (
                                          <a
                                            href={lifecycle.setup.install_url}
                                            target="_blank"
                                            rel="noreferrer"
                                          >
                                            官方说明
                                          </a>
                                        ) : null}
                                        {lifecycle.setup?.auth_command ? (
                                          <button
                                            type="button"
                                            onClick={() =>
                                              copyCommand(
                                                `${c.id}:auth`,
                                                lifecycle.setup?.auth_command,
                                              )
                                            }
                                          >
                                            {copiedCommand === `${c.id}:auth`
                                              ? "登录命令已复制"
                                              : `登录：${lifecycle.setup.auth_command}`}
                                          </button>
                                        ) : null}
                                        {onVerifyProvider && lifecycle.tone !== "ready" ? (
                                          <button
                                            type="button"
                                            className="settings-provider-verify"
                                            onClick={() => onVerifyProvider(c.id)}
                                          >
                                            验证真实对话
                                          </button>
                                        ) : null}
                                      </div>
                                    </div>
                                  ) : (
                                    /* API 属性表单 */
                                    <div className="workbench-card">
                                      <div className="card-heading">
                                        <span>API 端点与网络出站设置</span>
                                      </div>

                                      <div className="form-group">
                                        <label className="form-label">连接别名</label>
                                        <input
                                          type="text"
                                          className="custom-input"
                                          value={c.name}
                                          onChange={(e) =>
                                            updateConnectionDraft(c.id, { name: e.target.value })
                                          }
                                        />
                                      </div>

                                      <div className="form-group">
                                        <label className="form-label">协议类型</label>
                                        <select
                                          className="custom-select"
                                          value={c.protocol || "openai_responses"}
                                          onChange={(e) =>
                                            updateConnectionDraft(c.id, {
                                              protocol: e.target.value as HubConnectionProtocol,
                                            })
                                          }
                                        >
                                          <option value="openai_responses">
                                            OpenAI - Responses 协议 (官方推荐)
                                          </option>
                                          <option value="openai_chat">
                                            OpenAI - Chat Completions 协议 (兼容网关)
                                          </option>
                                          <option value="anthropic">
                                            Anthropic - Messages 协议 (Claude)
                                          </option>
                                        </select>
                                      </div>

                                      <div className="form-group">
                                        <label className="form-label">
                                          <span>Base URL</span>
                                          <span className="desc-tag">
                                            默认强制 HTTPS，支持内网白名单
                                          </span>
                                        </label>
                                        <input
                                          type="text"
                                          className="custom-input"
                                          value={c.base_url || ""}
                                          placeholder="https://api.openai.com/v1"
                                          onChange={(e) =>
                                            updateConnectionDraft(c.id, {
                                              base_url: e.target.value,
                                            })
                                          }
                                        />
                                      </div>

                                      <div className="form-row">
                                        <label className="checkbox-row">
                                          <input
                                            type="checkbox"
                                            checked={c.allow_private}
                                            onChange={(e) =>
                                              updateConnectionDraft(c.id, {
                                                allow_private: e.target.checked,
                                              })
                                            }
                                          />
                                          <span>允许内网/本地端点 (如 http://localhost 或私有网关)</span>
                                        </label>

                                        <div className="timeout-input-group">
                                          <label>超时时间 (秒):</label>
                                          <input
                                            type="number"
                                            className="custom-input input-narrow"
                                            min="1"
                                            max="600"
                                            value={c.timeout}
                                            onChange={(e) =>
                                              updateConnectionDraft(c.id, {
                                                timeout: Number(e.target.value) || 60,
                                              })
                                            }
                                          />
                                        </div>
                                      </div>

                                      {/* 密钥安全三态交互 */}
                                      <div className="form-group key-form-group">
                                        <label className="form-label">
                                          <span>API 凭据密钥 (API Key)</span>
                                          <span className="desc-tag">
                                            前端无明文或掩码回传，独立加密存储
                                          </span>
                                        </label>

                                        <div className="key-state-box">
                                          {c.has_key ? (
                                            <div className="key-state-row">
                                              <div className="key-status-display">
                                                {c.patch?.key_action === "delete" ? (
                                                  <span className="badge-danger">
                                                    凭据已标记删除 (保存后生效)
                                                  </span>
                                                ) : c.patch?.key_action === "replace" ? (
                                                  <span className="badge-warning">
                                                    凭据将被新输入的密钥替换
                                                  </span>
                                                ) : (
                                                  <span className="key-status-badge">
                                                    <KeyRound size={13} />
                                                    <span>已配置凭据 (后端安全存储，无明文回传)</span>
                                                  </span>
                                                )}
                                              </div>

                                              <div className="key-action-buttons">
                                                {c.patch?.key_action === "delete" ? (
                                                  <button
                                                    type="button"
                                                    className="btn-tiny"
                                                    onClick={() =>
                                                      updateConnectionDraft(c.id, {
                                                        key_action: "keep",
                                                      })
                                                    }
                                                  >
                                                    撤销删除
                                                  </button>
                                                ) : c.patch?.key_action === "replace" ? (
                                                  <button
                                                    type="button"
                                                    className="btn-tiny"
                                                    onClick={() =>
                                                      updateConnectionDraft(c.id, {
                                                        key_action: "keep",
                                                        api_key: undefined,
                                                      })
                                                    }
                                                  >
                                                    取消替换
                                                  </button>
                                                ) : (
                                                  <>
                                                    <button
                                                      type="button"
                                                      className="btn-tiny"
                                                      onClick={() =>
                                                        updateConnectionDraft(c.id, {
                                                          key_action: "replace",
                                                        })
                                                      }
                                                    >
                                                      替换密钥
                                                    </button>
                                                    <button
                                                      type="button"
                                                      className="btn-tiny btn-danger"
                                                      onClick={() =>
                                                        updateConnectionDraft(c.id, {
                                                          key_action: "delete",
                                                        })
                                                      }
                                                    >
                                                      彻底删除
                                                    </button>
                                                  </>
                                                )}
                                              </div>
                                            </div>
                                          ) : null}

                                          {(!c.has_key || c.patch?.key_action === "replace") && (
                                            <div className="key-input-row">
                                              <input
                                                type="password"
                                                className="custom-input"
                                                placeholder="在此输入 API Key (保存后加密提交，前端不留存)"
                                                value={c.patch?.api_key || ""}
                                                onChange={(e) =>
                                                  updateConnectionDraft(c.id, {
                                                    key_action: "replace",
                                                    api_key: e.target.value,
                                                  })
                                                }
                                              />
                                            </div>
                                          )}
                                        </div>
                                      </div>
                                    </div>
                                  )}

                                  {/* 模型目录：角色表的模型下拉来自这里 */}
                                  <div className="workbench-card catalog-card">
                                    <div className="card-heading">
                                      <span>模型目录</span>
                                      <div className="catalog-status-actions">
                                        {catalog?.status && !isLoadingCat && (
                                          <span
                                            className={`catalog-status-badge status-${catalog.status}`}
                                          >
                                            {CATALOG_STATUS_LABELS[catalog.status] ?? catalog.status}
                                          </span>
                                        )}
                                        {catalog?.status !== "unsupported" && (
                                          <button
                                            type="button"
                                            className="settings-refresh-btn-sm"
                                            disabled={isNew || isLoadingCat}
                                            onClick={() => handleRefreshCatalog(c.id)}
                                            title={isNew ? "保存连接后才能获取模型" : "重新获取模型列表"}
                                          >
                                            <RefreshCw
                                              size={12}
                                              className={isLoadingCat ? "animate-spin" : ""}
                                            />
                                            <span>{isLoadingCat ? "获取中…" : "重新获取"}</span>
                                          </button>
                                        )}
                                      </div>
                                    </div>

                                    {isNew ? (
                                      <p className="settings-section-hint">
                                        先点击底部「保存更改」保存连接，再获取模型列表。
                                      </p>
                                    ) : isLoadingCat && !catalog ? (
                                      <p className="settings-section-hint">正在获取模型列表…</p>
                                    ) : catalog?.status === "unsupported" ? (
                                      <p className="settings-section-hint">
                                        {isCli
                                          ? "这个 CLI 不提供模型列表，可以在角色表里手动输入模型 ID。"
                                          : "这个连接不提供模型列表，可以在角色表里手动输入模型 ID。"}
                                      </p>
                                    ) : (
                                      <>
                                        {catalog?.error && (
                                          <div className="catalog-notice" role="status">
                                            <AlertTriangle size={14} aria-hidden="true" />
                                            <span>
                                              {catalog.status === "stale"
                                                ? `这次没能更新，仍显示上次的列表：${catalog.error}`
                                                : `没能获取模型列表：${catalog.error}`}
                                            </span>
                                          </div>
                                        )}
                                        {catalog?.models && catalog.models.length > 0 ? (
                                          <div className="catalog-models-summary">
                                            <p className="catalog-count-text">
                                              共 {catalog.models.length} 个模型，可在上方角色表中为每位角色选择。
                                            </p>
                                            <div className="catalog-chips-preview">
                                              {catalog.models.map((m) => (
                                                <span key={m.id} className="model-chip-preview" title={m.id}>
                                                  {modelLabel(m, " · ")}
                                                </span>
                                              ))}
                                            </div>
                                          </div>
                                        ) : !catalog?.error ? (
                                          <p className="settings-section-hint">
                                            还没有模型列表，点击「重新获取」。
                                          </p>
                                        ) : null}
                                      </>
                                    )}
                                  </div>
                                </>
                              );
                            })()}
                          </div>
                        ) : (
                          <p className="settings-section-hint">选择左侧的连接查看详情。</p>
                        )}
                      </div>
                    </div>
                  </section>
                </div>
              ) : null}

              {activeSection === "runtime" ? (
                <section
                  className="settings-section"
                  aria-labelledby="settings-runtime-title"
                >
                  <h3 id="settings-runtime-title">运行参数</h3>
                  <p className="settings-section-hint">
                    控制任务轮次、Agent 接力预算与执行窗口。每 5
                    分钟检查一次工具返回，有新结果就续一个执行窗口。保存后对新任务生效。
                  </p>
                  {(
                    Object.keys(RUNTIME_LABELS) as (keyof typeof RUNTIME_LABELS)[]
                  ).map((key) => {
                    const rtMap = view.runtime as unknown as Record<
                      string,
                      { value: unknown; source: string }
                    >;
                    const field = rtMap[key];
                    if (!field) return null;
                    // Demo mode is a test fixture: only offer the switch to turn it off.
                    if (key === "use_fake_cli" && field.value !== true) return null;
                    const isBool = typeof field.value === "boolean";
                    const currentValue =
                      key in runtimeDraft ? runtimeDraft[key] : String(field.value);
                    const currentSource =
                      key in runtimeDraft &&
                      (isBool
                        ? (runtimeDraft[key] === "true") !== field.value
                        : Number(runtimeDraft[key]) !== field.value)
                        ? "user"
                        : field.source;

                    return (
                      <div className="settings-runtime-row" key={key}>
                        <label htmlFor={"rt-" + key}>{RUNTIME_LABELS[key]}</label>
                        {isBool ? (
                          <button
                            type="button"
                            role="switch"
                            aria-checked={currentValue === "true"}
                            className={
                              "settings-toggle" +
                              (currentValue === "true" ? " on" : "")
                            }
                            data-testid={"input-" + key}
                            onClick={() =>
                              setRuntimeDraft((prev) => ({
                                ...prev,
                                [key]:
                                  prev[key] !== undefined
                                    ? prev[key] === "true"
                                      ? "false"
                                      : "true"
                                    : field.value
                                    ? "false"
                                    : "true",
                              }))
                            }
                          >
                            {currentValue === "true" ? "ON" : "OFF"}
                          </button>
                        ) : (
                          <input
                            id={"rt-" + key}
                            type="number"
                            min={key === "a2a_idle_timeout" ? 0 : undefined}
                            data-testid={"input-" + key}
                            value={currentValue}
                            onChange={(e) =>
                              setRuntimeDraft((prev) => ({
                                ...prev,
                                [key]: e.target.value,
                              }))
                            }
                          />
                        )}
                        <span className="settings-source-badge">
                          {sourceLabel(currentSource)}
                        </span>
                      </div>
                    );
                  })}
                </section>
              ) : null}

              {activeSection === "extensions" ? (
                <section className="settings-section" aria-labelledby="settings-extensions-title">
                  <h3 id="settings-extensions-title">扩展</h3>
                  <p className="settings-section-hint">
                    Unity 的核心是让几位伙伴共享上下文、彼此接力。下面这些能力默认关闭，按需开启；关闭只停止新的创建和调度，已有记录保留，重新开启后可继续。
                  </p>
                  {EXTENSION_OPTIONS.map(({ name, label, desc }) => {
                    const field = view.extensions?.[name];
                    const on = extensionDraft[name] ?? field?.value ?? false;
                    const changed = extensionDraft[name] !== undefined && on !== (field?.value ?? false);
                    return (
                      <div className="settings-runtime-row" key={name}>
                        <label htmlFor={"ext-" + name}>
                          {label}
                          <span className="settings-extension-desc">{desc}</span>
                        </label>
                        <button
                          id={"ext-" + name}
                          type="button"
                          role="switch"
                          aria-checked={on}
                          className={"settings-toggle" + (on ? " on" : "")}
                          data-testid={"extension-" + name}
                          onClick={() => setExtensionDraft((prev) => ({ ...prev, [name]: !on }))}
                        >
                          {on ? "ON" : "OFF"}
                        </button>
                        <span className="settings-source-badge">
                          {sourceLabel(changed ? "user" : field?.source ?? "default")}
                        </span>
                      </div>
                    );
                  })}
                </section>
              ) : null}

              {activeSection === "desktop" ? (
                <DesktopSettingsSection
                  bridgeState={bridgeState ?? null}
                  onSetAutoDownload={onSetAutoDownload ?? (() => {})}
                  onCheckForUpdates={onCheckForUpdates ?? (() => {})}
                  isChecking={isCheckingUpdates}
                  runtime={runtime}
                  frontend={frontend}
                />
              ) : null}

              {activeSection === "observability" ? <ObservabilityOverview /> : null}
              {activeSection === "memory" ? <MemoryPanel conversationId={conversationId} /> : null}
            </div>

            {activeSection === "observability" || activeSection === "memory" || activeSection === "desktop" ? null : (
              <div className="settings-modal-footer">
                <div className="footer-status">
                  <span className={`status-dot dot-${hasDiff ? "warning" : "ready"}`} />
                  <span className="footer-status-text">
                    {hasDiff
                      ? "有尚未保存的配置更改 (离开前请保存)"
                      : "所有更改已保存"}
                  </span>
                </div>
                <div className="footer-buttons">
                  <button type="button" onClick={handleAttemptClose}>
                    取消
                  </button>
                  <button
                    type="button"
                    className="settings-save-btn"
                    data-testid="settings-save"
                    disabled={!hasDiff}
                    onClick={handleSave}
                  >
                    保存更改
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function sameStringList(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}
