export type MissionStatus =
  | "queued"
  | "pending"
  | "running"
  | "awaiting_input"
  | "blocked"
  | "completed"
  | "completed_with_warnings"
  | "failed";

export const TERMINAL_MISSION_STATUSES = [
  "blocked",
  "completed",
  "completed_with_warnings",
  "failed",
] as const;

export function isTerminalMissionStatus(status: string): boolean {
  return TERMINAL_MISSION_STATUSES.includes(
    status as (typeof TERMINAL_MISSION_STATUSES)[number],
  );
}

export type RoomMode = "task" | "roundtable";

export type DiscussionPhase = "opening" | "exchange" | "closing" | "ended" | "independent" | "awaiting_synthesizer" | "synthesis" | "review" | "finalizing";

export type DiscussionControlAction = "finish" | "stop" | "select_synthesizer" | "retry" | "record_decision" | "restart_with_input" | "defer_input" | "save_knowledge_candidate";

export interface DiscussionControlFields {
  agent_id?: string;
  turn_id?: string;
  decision_id?: string;
  option_id?: string;
  input_ids?: string[];
  goal?: string;
  expected_revision?: number;
}

export interface DiscussionResult {
  id: string;
  kind: "opinion" | "contribution" | "draft" | "review";
  agent_id: string;
  phase: DiscussionPhase;
  data: {
    position_summary?: string;
    assumptions?: string[];
    unknowns?: string[];
    actions?: string[];
    claims?: Array<{ id: string; statement: string; evidence_status: string }>;
    sections?: Array<{ id: string; content: string; source_ids: string[] }>;
    findings?: Array<{ id: string; section_id: string; problem: string; desired_change: string; blocking: boolean }>;
    dispositions?: Array<{ finding_id: string; action: string; reason: string }>;
    issue_decisions?: Array<{ issue_id: string; status: string; decision: string }>;
    no_findings?: boolean;
  };
}

export interface DiscussionAgreement {
  skip: boolean;
  reason: "decisions_agree" | "conflicting_decisions" | "assumption_mismatch" | "no_shared_decision" | string;
  shared_decision_keys: string[];
  unshared_decision_keys?: string[];
  conflicts?: Array<{ decision_key: string; claim_ids: string[]; recommendations: string[]; conditions: string[][] }>;
  assumptions_equal?: boolean;
  relevant_agent_ids?: string[];
}

export type DiscussionEndReason =
  | "responses_complete"
  | "budget_exhausted"
  | "user_finish"
  | "user_stop"
  | "provider_failed"
  | "summary_failed"
  | "service_restart"
  | "runner_failed"
  | "no_successful_turn";

export interface DiscussionConfigView {
  participant_ids: string[];
  max_turns: number;
  protocol?: "roundtable_v1" | "deliberation_v2";
  max_exchange_rounds?: number;
}

export interface DiscussionView {
  protocol?: "roundtable_v1" | "deliberation_v2";
  participant_ids: string[];
  phase: DiscussionPhase;
  turns_used: number;
  turns_limit: number;
  current_speaker?: string | null;
  current_turn_id?: string | null;
  pending_control?: DiscussionControlAction | null;
  end_reason?: DiscussionEndReason | "completed" | "completed_with_reservations" | "input_revised" | null;
  summary_message_ids?: Array<number | string>;
  unanswered_interrupt_ids?: Array<number | string>;
  revision?: number;
  awaiting_reason?: "select_synthesizer" | "resolve_value_choice" | "revise_input" | null;
  current_speakers?: string[];
  progress?: Array<{ turn_id: string; agent_id: string; provider_id?: string | null; phase: DiscussionPhase; status: string; attempt: number; error?: string | null; failure_code?: string | null; exit_code?: number | null }>;
  attempts_used?: number;
  round_index?: number;
  max_exchange_rounds?: number;
  exchange_end_reason?: "exchange_skipped" | "no_new_information" | "exchange_budget_exhausted" | "issues_classified" | null;
  exchange_agents?: string[];
  agreement?: DiscussionAgreement | null;
  synthesizer_id?: string | null;
  draft_id?: string | null;
  final_id?: string | null;
  retryable?: boolean;
  error?: string | null;
  results?: DiscussionResult[];
  issues?: Array<{ id: string; problem: string; impact: string; category: string; status: string;
    technical_decision?: string; decision_status?: string; deferred?: boolean; resolution?: string;
    positions?: Record<string, { proposal: string }> }>;
  value_decisions?: Array<{ id: string; question: string; options: Array<{ id: string; label: string }> }>;
  decisions?: Record<string, string>;
  pending_inputs?: string[];
  rules_version?: string;
  rules_hash?: string;
  knowledge_candidate_id?: string | null;
  build_id?: string;
}

export type EventRole = "user" | "agent" | "assistant" | "system";
export type Capability = "planning" | "implementation" | "review" | "coordination" | "unknown";

export interface Mission {
  id: string;
  title?: string;
  goal: string;
  skill?: "discuss" | null;
  kind?: "task" | "provider_verification";
  room_mode?: RoomMode;
  discussion?: DiscussionConfigView | null;
  status: MissionStatus;
  complexity?: string;
  current_capability?: Capability;
  current_speaker_id?: string | null;
  summary?: string;
  blocked_reason?: string;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface ThreadEventMetadata {
  discussion_visibility?: "internal" | "final";
  discussion_pending_decision?: boolean;
  discussion_revision_reviewed?: boolean;
  discussion_audit?: {
    sources: Array<{ id: string; label: string; summary?: string }>;
    reviews: Array<{ reviewer: string; problem: string; action?: "accepted" | "partially_accepted" | "not_accepted"; reason?: string }>;
  };
  capability?: Capability;
  provider_id?: string;
  provider_label?: string;
  persona_id?: string;
  persona_label?: string;
  turn_id?: string;
  kind?: string;
  tags?: string[];
  duration_ms?: number;
  tool_calls?: Array<{ tool_name: string; tool_input?: Record<string, unknown> }>;
  handoff_to?: string;
  trace_blocks?: Array<{ type: string; title: string; content: string }>;
  artifact_id?: string;
  host_progress?: HostProgress;
  /** The user paused and rewrote this message; the later version supersedes it. */
  revised?: boolean;
}

/** Host-recorded snapshot of a timed-out turn; never an agent message. */
export interface HostProgress {
  source: "host";
  reason: string;
  error?: string;
  tool_calls: number;
  tools: Array<{ name: string; count: number }>;
  artifacts: string[];
  published: boolean;
  last_activity?: { kind: string; label: string; at?: string | null; seconds_before_stop?: number } | null;
  stopped_at?: string;
}

export interface ThreadEvent {
  artifact?: Artifact;
  id: number | string;
  mission_id: string;
  event_type: string;
  role: EventRole;
  content: string;
  agent_id?: string | null;
  character_id?: string | null;
  character_name?: string | null;
  provider_id?: string | null;
  turn_id?: string | null;
  metadata?: ThreadEventMetadata;
  created_at: string;
}

export interface Artifact {
  id: string;
  mission_id: string;
  title: string;
  kind?: string;
  path?: string;
  file_path?: string;
  preview?: string;
  created_at: string;
  agent_id?: string | null;
  turn_id?: string | null;
}

export interface ArtifactContent {
  path: string;
  content: string;
  kind: string;
  total_chars: number;
  truncated: boolean;
}

export interface RegisteredRepository {
  id: string;
  name: string;
  local_path: string;
  github_repo: string;
  remote_name: string;
  default_branch: string;
  validation_commands: string[][];
  forbidden_paths: string[];
  max_changed_files: number;
  max_repair_rounds: number;
  enabled: boolean;
}

export type MaintenancePhase =
  | "queued"
  | "planning"
  | "implementing"
  | "local_verifying"
  | "reviewing"
  | "draft_pr"
  | "ci_waiting"
  | "ci_repairing"
  | "ready_for_approval"
  | "merging"
  | "completed"
  | "waiting_approval"
  | "blocked"
  | "failed"
  | "cancelled";

export interface MaintenanceRun {
  id: string;
  repository_id: string;
  goal: string;
  issue_url?: string | null;
  phase: MaintenancePhase;
  branch_name?: string | null;
  workspace_path?: string | null;
  repair_round: number;
  pull_request_url?: string | null;
  last_error?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ValidationResult {
  id: string;
  run_id: string;
  command: string[];
  exit_code: number;
  duration_ms: number;
  stdout: string;
  stderr: string;
  created_at: string;
}

export interface MaintenanceRunDetail {
  run: MaintenanceRun;
  repository: RegisteredRepository;
  validation_results: ValidationResult[];
  approvals: Array<Record<string, unknown>>;
  feedback: Array<Record<string, unknown>>;
  events: Array<{
    id: string;
    event_type: string;
    payload: Record<string, unknown>;
    created_at: string;
  }>;
}

export interface TurnAttempt {
  attempt: number;
  provider_id: string;
  event_type?: string;
  status?: "started" | "completed" | "blocked" | "failed";
  duration_ms?: number;
  error?: string;
  summary?: string;
}

export interface TurnRecord {
  turn_id: string;
  agent_id: string;
  provider_id: string;
  status: "started" | "completed" | "blocked" | "failed";
  attempts: TurnAttempt[];
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  message_ids: string[];
}

export interface ToolEventRecord {
  id: number | string;
  mission_id: string;
  event_type: "tool.call" | "tool.result" | "tool.error" | "provider.activity";
  activity_id?: string;
  phase?: "started" | "completed" | "result" | "failed";
  tool_name: string;
  agent_id: string;
  turn_id: string;
  provider_id?: string | null;
  tool_input?: Record<string, unknown>;
  artifact_id?: string | null;
  summary?: string;
  error?: string;
  args?: Record<string, unknown>;
  output?: string;
  call_id?: string;
  duration_ms?: number;
  ok?: boolean;
  created_at: string;
}

export interface InterruptRecord {
  event_id: number | string;
  content: string;
  target_agent_id?: string | null;
  status: "pending" | "handled" | "cancelled" | "deferred";
  created_at: string;
  handled_at?: string | null;
  handled_by_agent_id?: string | null;
  turn_id?: string | null;
  handled_in_summary?: boolean;
  reason?: string | null;
}

export interface A2AInvocationRecord {
  event_id: number | string;
  sender_agent_id: string;
  sender_turn_id: string;
  target_agent_id: string;
  /** The turn_id assigned to the invoked agent's turn. Set by the runner. */
  target_turn_id?: string | null;
  purpose: string;
  mode: "fire_and_forget" | "wait_reply";
  queued_position?: number | null;
  created_at: string;
  reply_text?: string | null;
  reply_failed?: boolean;
}

export interface UsageRecord {
  input_tokens: number;
  cached_input_tokens: number;
  cache_creation_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_cost_usd: number | null;
  duration_ms: number;
}

export interface UsageSummary {
  mission_total: UsageRecord | null;
  by_agent: (UsageRecord & { agent_id: string })[];
  by_provider: (UsageRecord & { provider_id: string })[];
  by_turn: (UsageRecord & { turn_id: string; agent_id: string; provider_id: string })[];
}

export interface ThoughtEntry {
  content: string;
  kind?: string;
  tags?: string[];
  createdAt: string;
}

export interface TurnToolCall {
  toolName: string;
  summary?: string;
  toolInput?: Record<string, unknown>;
  createdAt: string;
  eventType: "tool.call" | "tool.result" | "tool.error" | "provider.activity";
  activityId?: string;
  phase?: "started" | "completed" | "failed";
}

export interface MissionThoughtRecord {
  id: number | string;
  mission_id: string;
  agent_id?: string | null;
  turn_id: string;
  provider_id?: string | null;
  content: string;
  kind?: string;
  tags?: string[];
  created_at: string;
}

export interface MissionDetail {
  artifact_events?: ThreadEvent[];
  mission: Mission;
  discussion?: DiscussionView | null;
  events?: ThreadEvent[];
  messages?: ThreadEvent[];
  failure_events?: ThreadEvent[];
  artifacts?: Artifact[];
  turns?: TurnRecord[];
  tool_events?: ToolEventRecord[];
  interrupts?: InterruptRecord[];
  a2a_invocations?: A2AInvocationRecord[];
  usage_summary?: UsageSummary;
  live_partial?: Record<string, string>;
  thoughts?: MissionThoughtRecord[];
}

export interface TurnView {
  artifact?: Artifact;
  id: string;
  missionId?: string;
  turnIndex: number;
  role: EventRole;
  eventType: string;
  content: string;
  capability: Capability;
  providerLabel: string;
  personaLabel?: string;
  agentId?: string | null;
  createdAt: string;
  metadata: ThreadEventMetadata;
  isStatusEvent: boolean;
  a2aInvocation?: A2AInvocationRecord;
  partialText?: string;
  usage?: UsageRecord;
  thoughts?: ThoughtEntry[];
  toolCalls?: TurnToolCall[];
}

export interface AgentParticipant {
  agentId: string;
  personaLabel: string;
  capability: Capability;
  providerLabel: string;
  turnIndex: number;
  participated: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  status: "active" | "archived";
  created_at: string;
  updated_at: string;
}

export interface ConversationSummary extends Conversation {
  latest_mission_id?: string | null;
  latest_message_preview?: string | null;
  mission_count: number;
}

export interface ConversationDetail {
  has_autonomous_work?: boolean;
  work_status_label?: string | null;
  artifact_events?: ThreadEvent[];
  conversation: Conversation;
  missions: Mission[];
  messages: ThreadEvent[];
  failure_events?: ThreadEvent[];
  latest_mission_detail?: MissionDetail | null;
  thoughts?: MissionThoughtRecord[];
  tool_events?: ToolEventRecord[];
}

export interface MissionRuntimeSummary {
  title: string;
  statusLabel: string;
  complexityLabel: string;
  agentTurnCount: number;
  artifactCount: number;
  updatedAt?: string;
}

export interface ProviderLikeStatus {
  id: string;
  label: string;
  enabled: boolean;
  available: boolean;
  version?: string | null;
  error?: string | null;
}

export type SettingSource = "user" | "env" | "default" | "committed";

export interface SettingField<T> {
  value: T;
  source: SettingSource;
}

export interface HubSettingsProviderView {
  id: string;
  label: string;
  enabled: SettingField<boolean>;
  /** Effort levels this CLI accepts; empty when it has no effort setting. */
  efforts?: string[];
}

export interface HubSettingsRuntimeView {
  use_fake_cli: SettingField<boolean>;
  max_turns: SettingField<number>;
  max_a2a_invocations: SettingField<number>;
  cli_timeout: SettingField<number>;
  a2a_idle_timeout: SettingField<number>;
}

export type HubConnectionProtocol = "openai_responses" | "openai_chat" | "anthropic";
export type HubConnectionKind = "cli" | "api";

export interface HubConnectionView {
  id: string;
  name: string;
  kind: HubConnectionKind;
  protocol: HubConnectionProtocol | null;
  base_url: string | null;
  enabled: boolean;
  has_key: boolean;
  timeout: number;
  allow_private: boolean;
}

export interface HubConnectionPatch {
  name?: string;
  protocol?: HubConnectionProtocol | null;
  base_url?: string | null;
  enabled?: boolean;
  timeout?: number;
  allow_private?: boolean;
  key_action?: "keep" | "replace" | "delete";
  api_key?: string;
  delete?: boolean;
}

export type ConnectionCatalogStatus = "ready" | "stale" | "error" | "unsupported" | "loading";

export interface ConnectionModelItem {
  id: string;
  label: string;
  vendor: string | null;
  /** Effort levels this model supports, when the CLI reports them per model. */
  efforts?: string[];
}

export interface ConnectionCatalogView {
  connection_id: string;
  status: ConnectionCatalogStatus;
  models: ConnectionModelItem[];
  fetched_at: string | null;
  error: string | null;
}

export interface HubSettingsAgentView {
  id: string;
  display_name: string;
  /** 用户在设置中新增的角色；内置角色不可删除。 */
  custom?: boolean;
  role?: string;
  persona?: string;
  aliases?: string[];
  trigger_points?: string[];
  default_provider_id: SettingField<string>;
  fallback_provider_ids: SettingField<string[]>;
  provider_models?: SettingField<Record<string, string>>;
  base_provider_models?: Record<string, string>;
  provider_efforts?: SettingField<Record<string, string>>;
  base_provider_efforts?: Record<string, string>;
  base_default_provider_id: string;
  base_fallback_provider_ids: string[];
  fallback_context_approved?: SettingField<boolean>;
}

export type ExtensionName = "discussion" | "tasks" | "maintenance";

/** Optional features on top of the shared-context core; all default off. */
export type HubSettingsExtensionsView = Record<ExtensionName, SettingField<boolean>>;

export interface HubSettingsView {
  runtime: HubSettingsRuntimeView;
  extensions?: HubSettingsExtensionsView;
  providers: HubSettingsProviderView[];
  agents: HubSettingsAgentView[];
  connections?: HubConnectionView[];
}

export interface CustomAgentInput {
  id?: string;
  display_name: string;
  role: string;
  persona: string;
  aliases: string[];
  trigger_points: string[];
  default_provider_id: string;
  fallback_provider_ids: string[];
  provider_models: Record<string, string>;
  provider_efforts?: Record<string, string>;
}

export interface ProviderOverridePatch {
  enabled?: boolean;
}

export interface RuntimePatch {
  use_fake_cli?: boolean;
  max_turns?: number;
  max_a2a_invocations?: number;
  cli_timeout?: number;
  a2a_idle_timeout?: number;
}

export interface AgentOverridePatch {
  provider_models?: Record<string, string>;
  provider_efforts?: Record<string, string>;
  default_provider_id?: string;
  fallback_provider_ids?: string[];
  fallback_context_approved?: boolean;
  clear?: boolean;
}

export interface HubSettingsPatch {
  providers?: Record<string, ProviderOverridePatch>;
  runtime?: RuntimePatch;
  extensions?: Partial<Record<ExtensionName, boolean>>;
  agents?: Record<string, AgentOverridePatch>;
  connections?: Record<string, HubConnectionPatch>;
}

export interface SourceIdentity {
  environment?: string;
  git_sha: string | null;
  branch?: string | null;
  dirty: boolean | null;
  source_digest: string | null;
  checkout_id: string | null;
  captured_at?: string | null;
}

export interface RuntimeIdentity {
  started: SourceIdentity & {
    pid: number;
    python: string;
    checkout: string;
    database: string;
    config: string;
    artifacts: string;
    use_fake_cli: boolean;
  };
  current: SourceIdentity;
  source_changed: boolean | null;
}

export interface HealthView {
  status: "ok";
  version: string;
  database: "ready";
  config: "ready";
  fake_provider: "ready";
  discussion_protocols?: string[];
  project_skills?: string[];
  build_id?: string;
  runtime?: RuntimeIdentity;
}

export type OnboardingState =
  | "ready_for_first_chat"
  | "first_chat_running"
  | "first_chat_failed"
  | "first_chat_succeeded"
  | "provider_verifying"
  | "provider_verification_failed"
  | "provider_verified"
  | "team_intro_running"
  | "team_intro_failed"
  | "team_intro_completed"
  | "completed";

export interface OnboardingProviderStatus {
  id: string;
  label: string;
  available: boolean;
  version?: string | null;
  path?: string | null;
  error?: string | null;
  verified: boolean;
  verification_error?: string | null;
  activated: boolean;
  install_command?: string | null;
  install_url?: string | null;
  auth_command?: string | null;
}

export interface OnboardingView {
  state: OnboardingState;
  guide_agent_id: string;
  conversation_id?: string | null;
  mission_id?: string | null;
  first_chat_provider_id?: string | null;
  first_chat_error?: string | null;
  verification_provider_id?: string | null;
  verification_mission_id?: string | null;
  activated_provider_id?: string | null;
  team_intro_mission_id?: string | null;
  first_reply_completed_at?: string | null;
  team_intro_completed_at?: string | null;
  completed_at?: string | null;
  verified_provider_ids: string[];
  providers: OnboardingProviderStatus[];
  recommended_provider_id?: string | null;
  verification_error?: string | null;
  dismissed_real_cli_setup: boolean;
  /** Offline demo environments block every real onboarding call. */
  real_cli_blocked?: boolean;
}

export interface OnboardingFirstChatResponse {
  onboarding: OnboardingView;
  conversation: ConversationDetail;
}

/** Turn-level observability (GET /api/observability). */
export type CheckValue = 1 | 0 | null;

export type ObservabilityLayer = "trace" | "agent";

export interface ObservabilityCheck {
  name: string;
  source: "code" | "judge";
  layer?: ObservabilityLayer;
  value: CheckValue;
  note: string;
}

export interface ObservabilityStep {
  id: string;
  kind: "tool" | "native" | "a2a" | "artifact";
  tool: string;
  ts: string;
  call_id?: string;
  ok: boolean;
  error: string;
  args: Record<string, unknown> | null;
  output: string;
  summary: string;
  duration_ms: number | null;
  phase: string | null;
  agent_id: string;
  provider_id: string;
  source: string;
}

export interface ObservabilityCoverage {
  has_reply: boolean;
  native_visible: boolean;
  tool_calls: number;
  tool_outputs: number;
  cost_known: boolean;
}

export interface ObservabilityTurn {
  turn_id: string;
  mission_id: string;
  mission_title: string;
  index: number;
  agent_id: string;
  agent_name: string;
  provider_id: string;
  model?: string;
  session_id?: string;
  status: string;
  purpose: string;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number;
  reply: string;
  error: string;
  failure_class?: string | null;
  attribution?: "environment" | "user" | "agent" | null;
  steps: ObservabilityStep[];
  usage: UsageRecord | null;
  tokens: number;
  cost_usd: number | null;
  cost_source: "provider" | "estimate" | null;
  checks: ObservabilityCheck[];
  coverage: ObservabilityCoverage;
}

export interface ObservabilityCheckRow {
  name: string;
  what: string;
  passed: number;
  failed: number;
  na: number;
  pass_rate: number | null;
  newest_failure: { turn_id: string; agent_id: string; note: string; started_at: string | null } | null;
}

export interface ObservabilityPayload {
  generated_at: string;
  scope: {
    mission_id: string | null;
    mission_title: string | null;
    missions: number;
    turns: number;
    shown: number;
  };
  summary: {
    checks: { checks: ObservabilityCheckRow[]; turns: number; turns_passed: number };
    coverage: {
      turns: number;
      turns_with_reply: number;
      turns_native_visible: number;
      native_visible_rate: number | null;
      tool_calls: number;
      tool_outputs: number;
      tool_output_rate: number | null;
      turns_cost_known: number;
      cost_known_rate: number | null;
    };
    spend: {
      total_usd: number;
      reported_usd: number;
      estimated_usd: number;
      unknown_turns: number;
      by_provider: { provider_id: string; usd: number; turns: number; unknown: number }[];
    };
  };
  turns: ObservabilityTurn[];
}

/** Global observability (settings → 观测). */
export interface ObservabilityOverviewCheck {
  name: string;
  layer?: ObservabilityLayer;
  /** Why the numbers may not mean much: never fails, or almost always n/a. */
  health?: string | null;
  what: string;
  pass: number;
  fail: number;
  na: number;
  rate: number | null;
}

export interface ObservabilityOverviewTurn {
  turn_id: string;
  mission_id: string;
  mission: string;
  agent: string;
  agent_name: string;
  provider: string;
  model: string;
  status: string;
  duration_ms: number;
  tokens: number;
  cost: number | null;
  checks: ObservabilityCheck[];
  at: string | null;
}

export interface ObservabilityFailure {
  check: string;
  layer?: ObservabilityLayer;
  note: string;
  agent: string;
  turn_id: string;
  mission: string;
  at: string | null;
}

export interface ObservabilityOverviewPayload {
  generated_at: string;
  window: "today" | "7d" | "30d" | "all";
  totals: {
    conversations: number;
    missions: number;
    turns: number;
    tool_calls: number;
    native_calls: number;
    tokens: number;
    cost: number;
  };
  checks: ObservabilityOverviewCheck[];
  turns_passed: number;
  coverage: {
    turns: number;
    native_calls: number;
    native_rate: number | null;
    tool_calls: number;
    output_rate: number | null;
    cost_known_rate: number | null;
  };
  spend: {
    total: number;
    reported: number;
    estimated: number;
    unknown_turns: number;
    by_provider: { provider: string; usd: number; turns: number; unknown: number; tokens: number }[];
  };
  by_agent: {
    agent: string;
    turns: number;
    tokens: number;
    cost: number;
    tools: number;
    native: number;
    /** Blocked turns the agent owns; environment and user failures are counted apart. */
    blocked?: number;
    blocked_environment?: number;
    blocked_user?: number;
  }[];
  by_provider: { provider: string; usd: number; turns: number; unknown: number; tokens: number }[];
  daily: { date: string; turns: number; cost: number }[];
  recent_turns: ObservabilityOverviewTurn[];
  failures: ObservabilityFailure[];
}

export interface ObservabilityTurnDetailPayload {
  mission: { id: string; title: string };
  turn: ObservabilityTurn;
}

export interface WorkCriterion { id: string; text: string }
export interface WorkContract {
  id: string; version: number; source: string; goal: string; scope: string; non_goals: string;
  criteria: WorkCriterion[]; validation_commands: string[][]; digest: string; activated_at: string | null;
  delivery?: 'document' | 'development' | 'online'; risks?: string[];
}
export interface WorkEvidence {
  id: string; contract_id: string; contract_digest: string; commit_sha: string; passed: boolean;
  criterion_ids: string[]; mission_id: string; agent_id: string; created_at: string;
  checks: { command: string[]; exit_code: number; output: string; output_sha256: string; truncated: boolean; log_id: string }[];
  candidate_id?: string; observations?: WorkCriterion[]; method?: string; artifact_paths?: string[];
}
export interface WorkReview {
  id: string; contract_id: string; criterion_ids: string[]; commit_sha: string; evidence_id: string;
  reviewer_id: string; mission_id: string; decision: 'approved' | 'changes_requested'; note: string;
  candidate_id?: string; created_at?: string;
}
export interface WorkEnvironmentPreview {
  status: 'stopped' | 'starting' | 'ready' | 'failed';
  url?: string | null; ports?: { backend: number; frontend: number } | null;
  instance_id?: string | null; candidate_id?: string | null;
  failure_reason?: string | null; log_id?: string | null;
}
export interface WorkEnvironment {
  short_id?: string; status: 'pending' | 'preparing' | 'ready' | 'failed';
  branch?: string | null; workspace_dir?: string | null; base_sha?: string | null;
  failure_reason?: string | null; preview?: WorkEnvironmentPreview | null;
}
export interface SharedWorkTask {
  workflow_step_id?: string;
  environment?: WorkEnvironment;
  id: string; title: string; contract_id: string; owner_id: string; reviewer_id: string;
  criterion_ids: string[]; depends_on: string[]; status: 'ready' | 'running' | 'validating' | 'review' | 'done' | 'blocked' | 'waiting' | 'cancelled';
  progress: string; blocker: string | null; active_mission_id: string | null;
  candidate_sha: string | null; evidence: WorkEvidence[]; reviews: WorkReview[];
  attempts: { mission_id: string; agent_id: string; stage: string; outcome?: string }[];
  parent_id?: string | null; root_id?: string; next_action?: string; waiting_reason?: string | null;
  waiting_for?: string | null; paused?: boolean; attempts_used?: number; max_attempts?: number;
  risks?: string[]; authors?: string[]; review_required?: boolean;
  candidate?: { id: string; base_sha: string; artifact_paths?: string[] } | null;
  submission?: { kind?: string; summary: string; artifact_paths: string[]; limitations: string } | null;
  release?: { status: string; environment: string; candidate_id: string; log_id?: string };
  transfer?: { from: string; to: string; reason: string; status: string };
  deliveries?: { contract_id: string; candidate_id: string; summary: string; created_at: string }[];
}
export interface WorkDecision {
  id: string; task_id: string; kind: string; question: string; options: WorkCriterion[];
  recommendation?: string; artifact_paths: string[]; contract_id: string; candidate_id?: string;
  status: 'pending' | 'answered' | 'stale'; option_id?: string; note?: string; environment?: string;
  created_at?: string; answered_at?: string; actor?: string;
}
export interface WorkProposal {
  id: string; title: string; owner_id: string; agreement: WorkContract;
  status: 'pending' | 'accepted' | 'rejected' | 'superseded';
  created_by: string; mission_id: string; turn_id: string; created_at: string; digest: string;
  task_id?: string;
}
export interface SharedWorkBoardView {
  execution?: { active: { conversation_id: string; task_id: string; title: string }[]; queued?: boolean };
  proposals?: WorkProposal[];
  protocol?: 'adaptive_v1'; decisions?: WorkDecision[]; legacy_board?: SharedWorkBoardView;
  conversation_id: string; revision: number; enabled: boolean; active_contract_id: string | null;
  contracts: WorkContract[]; tasks: SharedWorkTask[]; workspace_root: string; current_head?: string;
  events: { id: string; action: string; actor: string; created_at: string; task_id?: string; mission_id?: string; reason?: string }[];
}
export interface MemoryEntryView {
  id: string; agent_id: string; title: string; tags: string[];
  status: 'active' | 'forgotten' | string; revision: number;
  created_at: string; updated_at: string; source_refs: string[]; body: string;
}
export interface MemoryAgentSummary {
  id: string; display_name: string; role: string; active: number; forgotten: number;
}
export interface MemoryConsistency {
  files: number; indexed: number; stale: number; missing_files: number;
  applied: boolean; active_by_agent: Record<string, number>;
}
export interface MemoryOverviewView {
  memory_root: string; consistency: MemoryConsistency; agents: MemoryAgentSummary[];
}
export interface MemoryCheckpointView {
  conversation_id: string; agent_id: string; text: string;
  source_message_ids: string[]; cursor_event_id: number; updated_at: string;
}
export interface MemoryHandoverView {
  id: string; agent_id: string; reason: string; previous: string; current: string;
  mission_id: string | null; conversation_id: string | null;
  acknowledged: boolean; created_at: string; acknowledged_at: string | null;
}

// Independent, user-facing work items; execution conversations remain an implementation carrier.
export interface TaskWorkflowStep {
  id: string; title: string; owner_id: string; goal: string; depends_on: string[];
  criterion_ids?: string[]; task_id?: string;
}
export interface TaskOrchestration {
  mode: 'adaptive' | 'workflow'; steps: TaskWorkflowStep[];
}
export interface TaskSummary {
  protocol?: 'legacy' | 'adaptive_v1';
  id: string; title: string; owner_id: string; status: SharedWorkTask['status']; paused: boolean;
  delivery: 'document' | 'development' | 'online'; created_at: string; updated_at: string;
  conversation_id: string; source_conversation_id: string | null; orchestration: TaskOrchestration;
  current_step?: string | null; pending_decisions: number;
}
export interface TaskDetail {
  protocol?: 'legacy' | 'adaptive_v1'; archived?: boolean;
  task: SharedWorkTask; board: SharedWorkBoardView; source_conversation_id: string | null;
  execution_conversation_id: string; orchestration: TaskOrchestration;
  /** Running attempts and the newest execution note, so the page moves while chat does. */
  live?: TaskLive;
}
export interface TaskLive {
  running: { task_id: string; mission_id: string; agent_id: string; started_at?: string | null; attempt: number }[];
  latest_message: { task_id: string; mission_id: string; agent_id?: string | null; created_at?: string | null; content: string } | null;
}
