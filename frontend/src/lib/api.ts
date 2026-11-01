import type { ArtifactContent, ConnectionCatalogView, Conversation, ConversationDetail, ConversationSummary, DiscussionControlAction, HealthView, CustomAgentInput, HubConnectionView, HubSettingsPatch, HubSettingsView, MaintenanceRun, MaintenanceRunDetail, Mission, MissionDetail, ObservabilityOverviewPayload, ObservabilityTurnDetailPayload, OnboardingFirstChatResponse, OnboardingView, RegisteredRepository, RoomMode } from "../types";

const API_ROOT = "";

async function parseJson<T>(response: Response, fallbackMessage: string): Promise<T> {
  if (!response.ok) {
    let detail: string | null = null;
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === "string" && body.detail.trim()) {
        detail = body.detail;
      }
    } catch {}
    throw new Error(detail || fallbackMessage);
  }
  return response.json() as Promise<T>;
}

export async function listMissions(): Promise<Mission[]> {
  const response = await fetch(`${API_ROOT}/api/missions`);
  return parseJson<Mission[]>(response, "加载任务列表失败");
}

export async function fetchMission(missionId: string): Promise<MissionDetail> {
  const response = await fetch(`${API_ROOT}/api/missions/${missionId}`);
  return parseJson<MissionDetail>(response, "加载任务详情失败");
}

/** One turn with steps/checks, for the drill-down from global observability. */
export async function fetchObservabilityTurn(turnId: string): Promise<ObservabilityTurnDetailPayload> {
  const response = await fetch(`${API_ROOT}/api/observability/turns/${encodeURIComponent(turnId)}`);
  return parseJson<ObservabilityTurnDetailPayload>(response, "加载回合详情失败");
}

/** Global observability across every conversation (settings → 观测). */
export async function fetchObservabilityOverview(
  window: "today" | "7d" | "30d" | "all" = "all",
): Promise<ObservabilityOverviewPayload> {
  const params = new URLSearchParams({ window, limit: "12" });
  const response = await fetch(`${API_ROOT}/api/observability/overview?${params.toString()}`);
  return parseJson<ObservabilityOverviewPayload>(response, "加载观测数据失败");
}

export interface MissionCreatePayload {
  goal: string;
  skill?: "discuss";
  title?: string;
  target_agent_ids?: string[];
  room_mode?: RoomMode;
  discussion?: { participant_ids: string[]; max_turns?: number; protocol?: "roundtable_v1" | "deliberation_v2"; max_exchange_rounds?: number };
  request_id?: string;
  work_mode?: 'document' | 'development' | 'online';
  /** Explicitly rewrites the conversation's last user message. */
  revises_message_id?: string;
}

/** Stable client-side idempotency key; the backend replays same-id requests. */
export function newRequestId(scope: string): string {
  const cryptoApi = globalThis.crypto as Crypto | undefined;
  const unique =
    cryptoApi && typeof cryptoApi.randomUUID === "function"
      ? cryptoApi.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  return `${scope}-${unique}`;
}

/** Task mode keeps the original body shape so older API consumers stay valid. */
function missionCreateBody(payload: MissionCreatePayload): string {
  const body: Record<string, unknown> = { goal: payload.goal };
  if (payload.title) body.title = payload.title;
  if (payload.skill) body.skill = payload.skill;
  if (payload.work_mode) body.work_mode = payload.work_mode;
  if (payload.room_mode) body.room_mode = payload.room_mode;
  if (payload.discussion) body.discussion = payload.discussion;
  if (payload.target_agent_ids && payload.target_agent_ids.length > 0) {
    body.target_agent_ids = payload.target_agent_ids;
  }
  if (payload.request_id) body.request_id = payload.request_id;
  if (payload.revises_message_id) body.revises_message_id = payload.revises_message_id;
  return JSON.stringify(body);
}

export async function createMission(payload: MissionCreatePayload): Promise<MissionDetail> {
  const response = await fetch(`${API_ROOT}/api/missions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: missionCreateBody(payload),
  });
  return parseJson<MissionDetail>(response, "创建任务失败");
}

export async function interruptMission(
  missionId: string,
  content: string,
  options?: { targetAgentId?: string; requestId?: string },
): Promise<MissionDetail> {
  const response = await fetch(`${API_ROOT}/api/missions/${missionId}/interruptions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(
      options?.requestId
        ? { content, target_agent_id: options?.targetAgentId, request_id: options.requestId }
        : { content, target_agent_id: options?.targetAgentId },
    ),
  });
  return parseJson<MissionDetail>(response, "发送插话失败");
}

/** Stop the running chat now: the current turn ends and nobody else is scheduled. */
export async function pauseMission(missionId: string): Promise<MissionDetail> {
  const response = await fetch(`${API_ROOT}/api/missions/${missionId}/pause`, { method: "POST" });
  return parseJson<MissionDetail>(response, "停止失败");
}

export async function sendDiscussionControl(
  missionId: string,
  payload: { action: DiscussionControlAction } & import("../types").DiscussionControlFields,
  requestId: string,
): Promise<MissionDetail> {
  const response = await fetch(`${API_ROOT}/api/missions/${missionId}/discussion-controls`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...payload,
      request_id: requestId,
    }),
  });
  return parseJson<MissionDetail>(response, "发送讨论控制失败");
}

export async function fetchArtifactContent(
  missionId: string,
  path: string,
): Promise<ArtifactContent> {
  const response = await fetch(
    `${API_ROOT}/api/missions/${missionId}/artifacts/content?path=${encodeURIComponent(path)}`,
  );
  return parseJson<ArtifactContent>(response, "加载产物内容失败");
}

export function artifactDownloadUrl(missionId: string, path: string): string {
  return `${API_ROOT}/api/missions/${encodeURIComponent(missionId)}/artifacts/file?path=${encodeURIComponent(path)}`;
}

export function missionStreamUrl(missionId: string): string {
  return `${API_ROOT}/api/missions/${missionId}/stream`;
}

export async function listConversations(status: "active" | "archived" = "active"): Promise<ConversationSummary[]> {
  const response = await fetch(`${API_ROOT}/api/conversations?status=${status}`);
  return parseJson<ConversationSummary[]>(response, "加载会话列表失败");
}

export async function createConversation(title?: string): Promise<Conversation> {
  const response = await fetch(`${API_ROOT}/api/conversations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: title || undefined }),
  });
  return parseJson<Conversation>(response, "创建会话失败");
}

export async function updateConversation(
  conversationId: string,
  patch: { title?: string; status?: "active" | "archived" },
): Promise<ConversationDetail> {
  const response = await fetch(`${API_ROOT}/api/conversations/${conversationId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return parseJson<ConversationDetail>(response, "更新会话详情失败");
}

export async function deleteConversation(conversationId: string): Promise<void> {
  const response = await fetch(`${API_ROOT}/api/conversations/${conversationId}`, { method: "DELETE" });
  if (!response.ok) await parseJson<never>(response, "删除会话失败");
}

export async function fetchConversation(conversationId: string): Promise<ConversationDetail> {
  const response = await fetch(`${API_ROOT}/api/conversations/${conversationId}`);
  return parseJson<ConversationDetail>(response, "加载会话详情失败");
}

export async function createConversationMission(
  conversationId: string,
  payload: MissionCreatePayload,
): Promise<ConversationDetail> {
  const response = await fetch(`${API_ROOT}/api/conversations/${conversationId}/missions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: missionCreateBody(payload),
  });
  return parseJson<ConversationDetail>(response, "在会话中创建任务失败");
}

export async function getHubSettings(): Promise<HubSettingsView> {
  const response = await fetch(`${API_ROOT}/api/hub/settings`);
  return parseJson<HubSettingsView>(response, "加载 Hub 设置失败");
}

export async function updateHubSettings(patch: HubSettingsPatch): Promise<HubSettingsView> {
  const response = await fetch(`${API_ROOT}/api/hub/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return parseJson<HubSettingsView>(response, "更新 Hub 设置失败");
}

export async function createCustomAgent(input: CustomAgentInput): Promise<HubSettingsView> {
  const response = await fetch(`${API_ROOT}/api/hub/agents`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return parseJson<HubSettingsView>(response, "新增角色失败");
}

export async function updateCustomAgent(agentId: string, input: CustomAgentInput): Promise<HubSettingsView> {
  const response = await fetch(`${API_ROOT}/api/hub/agents/${encodeURIComponent(agentId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return parseJson<HubSettingsView>(response, "保存角色失败");
}

export async function deleteCustomAgent(agentId: string): Promise<HubSettingsView> {
  const response = await fetch(`${API_ROOT}/api/hub/agents/${encodeURIComponent(agentId)}`, {
    method: "DELETE",
  });
  return parseJson<HubSettingsView>(response, "删除角色失败");
}

export async function listHubConnections(): Promise<{ connections: HubConnectionView[] }> {
  const response = await fetch(`${API_ROOT}/api/hub/connections`);
  return parseJson<{ connections: HubConnectionView[] }>(response, "加载连接列表失败");
}

export async function fetchConnectionModels(connectionId: string): Promise<ConnectionCatalogView> {
  const response = await fetch(`${API_ROOT}/api/hub/connections/${encodeURIComponent(connectionId)}/models`);
  return parseJson<ConnectionCatalogView>(response, "加载模型目录失败");
}

export async function refreshConnectionModels(connectionId: string): Promise<ConnectionCatalogView> {
  const response = await fetch(`${API_ROOT}/api/hub/connections/${encodeURIComponent(connectionId)}/models/refresh`, {
    method: "POST",
  });
  return parseJson<ConnectionCatalogView>(response, "刷新模型目录失败");
}

export interface AgentSummary {
  id: string;
  display_name: string;
  role: string;
  aliases: string[];
  capability_hints: string[];
  custom?: boolean;
}

export async function listAgents(): Promise<AgentSummary[]> {
  const response = await fetch(`${API_ROOT}/api/agents`);
  const body = await parseJson<{ agents: AgentSummary[] }>(response, "加载 agent 列表失败");
  return body.agents;
}

export async function getHealth(): Promise<HealthView> {
  const response = await fetch(`${API_ROOT}/api/health`);
  return parseJson<HealthView>(response, "Unity 后端尚未就绪");
}

export async function getOnboarding(): Promise<OnboardingView> {
  const response = await fetch(`${API_ROOT}/api/onboarding`);
  return parseJson<OnboardingView>(response, "加载启动引导失败");
}

export async function startOnboardingFirstChat(providerId: string): Promise<OnboardingFirstChatResponse> {
  const response = await fetch(`${API_ROOT}/api/onboarding/first-chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider_id: providerId, confirmed_real_call: true }),
  });
  return parseJson<OnboardingFirstChatResponse>(response, "启动首次对话失败");
}

export async function completeOnboarding(dismissedRealCliSetup = false): Promise<OnboardingView> {
  const response = await fetch(`${API_ROOT}/api/onboarding`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ completed: true, dismissed_real_cli_setup: dismissedRealCliSetup }),
  });
  return parseJson<OnboardingView>(response, "完成启动引导失败");
}

export async function verifyOnboardingProvider(providerId: string): Promise<OnboardingFirstChatResponse> {
  const response = await fetch(`${API_ROOT}/api/onboarding/providers/${providerId}/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirmed_real_call: true }),
  });
  return parseJson<OnboardingFirstChatResponse>(response, "真实 CLI 验证启动失败");
}

export async function activateOnboardingProvider(providerId: string): Promise<OnboardingFirstChatResponse> {
  const response = await fetch(`${API_ROOT}/api/onboarding/providers/${providerId}/activate`, {
    method: "POST",
  });
  return parseJson<OnboardingFirstChatResponse>(response, "真实 CLI 接管失败");
}

export async function listRegisteredRepositories(): Promise<RegisteredRepository[]> {
  const response = await fetch(`${API_ROOT}/api/maintenance/repositories`);
  return parseJson<RegisteredRepository[]>(response, "加载代码仓库失败");
}

export async function registerRepository(payload: {
  name: string;
  local_path: string;
  github_repo: string;
  validation_commands: string[][];
}): Promise<RegisteredRepository> {
  const response = await fetch(`${API_ROOT}/api/maintenance/repositories`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return parseJson<RegisteredRepository>(response, "登记代码仓库失败");
}

export async function listMaintenanceRuns(): Promise<MaintenanceRun[]> {
  const response = await fetch(`${API_ROOT}/api/maintenance/runs`);
  return parseJson<MaintenanceRun[]>(response, "加载维护任务失败");
}

export async function createMaintenanceRun(payload: {
  repository_id: string;
  goal: string;
  issue_url?: string;
}): Promise<MaintenanceRunDetail> {
  const response = await fetch(`${API_ROOT}/api/maintenance/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return parseJson<MaintenanceRunDetail>(response, "创建维护任务失败");
}

export async function getMaintenanceRun(runId: string): Promise<MaintenanceRunDetail> {
  const response = await fetch(`${API_ROOT}/api/maintenance/runs/${runId}`);
  return parseJson<MaintenanceRunDetail>(response, "加载维护任务详情失败");
}

export async function validateMaintenanceRun(runId: string): Promise<MaintenanceRunDetail> {
  const response = await fetch(`${API_ROOT}/api/maintenance/runs/${runId}/validate`, {
    method: "POST",
  });
  return parseJson<MaintenanceRunDetail>(response, "运行仓库验证失败");
}

export async function resumeMaintenanceRun(runId: string): Promise<MaintenanceRunDetail> {
  const response = await fetch(`${API_ROOT}/api/maintenance/runs/${runId}/resume`, {
    method: "POST",
  });
  return parseJson<MaintenanceRunDetail>(response, "恢复维护任务失败");
}

export async function fetchSharedWork(conversationId: string): Promise<import('../types').SharedWorkBoardView> {
  return parseJson(await fetch(`/api/conversations/${encodeURIComponent(conversationId)}/work`), '加载共享任务失败');
}

export async function updateSharedWork(conversationId: string, action: string, expectedRevision: number, data: Record<string, unknown> = {}): Promise<import('../types').SharedWorkBoardView> {
  return parseJson(await fetch(`/api/conversations/${encodeURIComponent(conversationId)}/work`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, expected_revision: expectedRevision, data, operation_id: newRequestId('work') }),
  }), '更新共享任务失败');
}
export async function fetchMemoryOverview(): Promise<import('../types').MemoryOverviewView> {
  return parseJson(await fetch(`/api/memory/overview`), '加载记忆状态失败');
}

export async function fetchAgentMemory(agentId: string, includeForgotten = false): Promise<{ entries: import('../types').MemoryEntryView[] }> {
  const query = includeForgotten ? '?include_forgotten=true' : '';
  return parseJson(await fetch(`/api/memory/agents/${encodeURIComponent(agentId)}/entries${query}`), '加载记忆条目失败');
}

export async function updateMemoryEntry(entryId: string, patch: { title?: string; body?: string; tags?: string[] }): Promise<{ id: string; revision: number; updated_at: string }> {
  return parseJson(await fetch(`/api/memory/entries/${encodeURIComponent(entryId)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
  }), '更新记忆条目失败');
}

export async function setMemoryEntryStatus(entryId: string, status: 'active' | 'forgotten'): Promise<{ id: string; status: string }> {
  return parseJson(await fetch(`/api/memory/entries/${encodeURIComponent(entryId)}/${status === 'forgotten' ? 'forget' : 'restore'}`, {
    method: 'POST',
  }), '更新记忆条目状态失败');
}

export async function purgeMemoryEntry(entryId: string): Promise<{ id: string; purged: boolean }> {
  return parseJson(await fetch(`/api/memory/entries/${encodeURIComponent(entryId)}`, { method: 'DELETE' }), '清除记忆条目失败');
}

export async function rebuildMemoryIndex(): Promise<import('../types').MemoryConsistency> {
  return parseJson(await fetch(`/api/memory/rebuild`, { method: 'POST' }), '重建记忆索引失败');
}

export async function fetchMemoryCheckpoints(conversationId: string): Promise<{ checkpoints: import('../types').MemoryCheckpointView[] }> {
  return parseJson(await fetch(`/api/memory/conversations/${encodeURIComponent(conversationId)}/checkpoints`), '加载会话检查点失败');
}

export async function clearMemoryCheckpoints(conversationId: string, agentId?: string): Promise<{ removed: number }> {
  const query = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
  return parseJson(await fetch(`/api/memory/conversations/${encodeURIComponent(conversationId)}/checkpoints${query}`, {
    method: 'DELETE',
  }), '清除会话检查点失败');
}

export async function fetchMemoryHandovers(agentId?: string): Promise<{ handovers: import('../types').MemoryHandoverView[] }> {
  const query = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
  return parseJson(await fetch(`/api/memory/handovers${query}`), '加载模型交接记录失败');
}

export interface ArtifactIndexPage {
  items: Array<import('./artifactSidebar').SidebarArtifact & { event_id: string }>;
  next_cursor: string | null;
}
export async function fetchArtifactIndex(query: string, cursor?: string): Promise<ArtifactIndexPage> {
  const params = new URLSearchParams({q:query,limit:'50'});
  if(cursor)params.set('cursor',cursor);
  return parseJson<ArtifactIndexPage>(await fetch(`/api/artifacts?${params}`), '加载全部仓库失败');
}
