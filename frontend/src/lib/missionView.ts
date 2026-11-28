import type {
  A2AInvocationRecord,
  AgentParticipant,
  Capability,
  MissionDetail,
  MissionRuntimeSummary,
  ProviderLikeStatus,
  ThoughtEntry,
  ThreadEvent,
  ToolEventRecord,
  TurnRecord,
  TurnToolCall,
  TurnView,
} from "../types";
import { MENTION_OPTIONS, mentionOptionById } from "./mentions";
import { compactPersonaProviderLabel } from "./providerLabels";

export type { ThoughtEntry } from "../types";

const CAPABILITY_LABEL: Record<Capability, string> = {
  planning: "Planning",
  implementation: "Implementation",
  review: "Review",
  coordination: "Coordination",
  unknown: "Agent",
};

/** Team directory in config order; synced from `/api/agents`, so user roles appear too. */
function teamAgents(): Array<{ agentId: string; personaLabel: string; capability: Capability }> {
  return MENTION_OPTIONS.map((option) => ({
    agentId: option.id,
    personaLabel: option.label,
    capability: "unknown" as Capability,
  }));
}

const STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  pending: "待启动",
  running: "运行中",
  awaiting_input: "等待你的选择",
  blocked: "已阻塞",
  completed: "已完成",
  completed_with_warnings: "已完成（有警告）",
  failed: "失败",
};

export function formatMissionStatus(status?: string | null): string {
  if (!status) return "未启动";
  return STATUS_LABEL[status] ?? status;
}

export function buildMissionRuntimeSummary(
  detail: MissionDetail | undefined,
  turns: TurnView[],
): MissionRuntimeSummary {
  const mission = detail?.mission;
  const agentTurnCount = turns.filter(
    (turn) => !turn.isStatusEvent && turn.role === "agent",
  ).length;
  return {
    title: "Unity",
    statusLabel: formatMissionStatus(mission?.status),
    complexityLabel: mission?.complexity ?? "unknown",
    agentTurnCount,
    artifactCount: detail?.artifacts?.length ?? 0,
    updatedAt: mission?.updated_at,
  };
}

export function buildProviderIssueSummary(providers: ProviderLikeStatus[]): string[] {
  return providers
    .filter((provider) => provider.enabled && !provider.available)
    .map((provider) =>
      provider.error ? `${provider.label}：${provider.error}` : `${provider.label}：不可用`,
    );
}

export function formatTurnIdentity(input: {
  capability: Capability;
  providerLabel: string;
  personaLabel?: string;
}): string {
  if (input.personaLabel) {
    return compactPersonaProviderLabel(input.personaLabel, input.providerLabel);
  }
  return `${CAPABILITY_LABEL[input.capability]} turn · ${input.providerLabel}`;
}

function isMessageEvent(event: ThreadEvent): boolean {
  if (event.role === "agent" || event.role === "user") {
    return (
      event.event_type.includes("message") ||
      event.event_type === "interrupt"
    );
  }
  return false;
}

export interface BuildTurnViewsOptions {
  livePartial?: Record<string, string>;
  turns?: TurnRecord[];
  usageByTurn?: { turn_id: string; input_tokens: number; cached_input_tokens: number; output_tokens: number; reasoning_output_tokens: number; total_cost_usd: number | null; duration_ms: number; cache_creation_input_tokens: number }[];
  thoughts?: { turn_id: string; content: string; kind?: string; tags?: string[]; created_at: string }[];
  toolEvents?: ToolEventRecord[];
}

export function buildTurnViews(
  events: ThreadEvent[] = [],
  a2aInvocations: A2AInvocationRecord[] = [],
  options: BuildTurnViewsOptions = {},
): TurnView[] {
  let agentTurnCount = 0;
  const pendingInvocations = [...a2aInvocations];
  const thoughtsByTurn = buildThoughtsByTurn(events, options.thoughts);
  const toolCallsByTurn = buildToolCallsByTurn(options.toolEvents);
  const displayEvents = events.filter((event) => !isThoughtEvent(event));
  const baseViews = displayEvents.map((event) => {
    const metadata = event.metadata ?? {};
    const capability = metadata.capability ?? inferCapability(event);
    const providerLabel = metadata.provider_label ?? event.provider_id ?? "Provider pending";
    // Streaming partials arrive before backend metadata; use the live directory name meanwhile.
    const personaLabel = metadata.persona_label ?? event.character_name
      ?? (event.agent_id ? mentionOptionById(event.agent_id)?.label : undefined);
    const isStatus = !isMessageEvent(event);
    if (event.role === "agent" && !isStatus) {
      agentTurnCount++;
    }
    let a2aInvocation: A2AInvocationRecord | undefined;
    if (event.role === "agent" && !isStatus && event.agent_id) {
      // Prefer precise turn_id match; fall back to first target_agent_id match.
      let matchIndex = event.turn_id
        ? pendingInvocations.findIndex(
            (item) => item.target_turn_id != null && item.target_turn_id === event.turn_id,
          )
        : -1;
      if (matchIndex < 0) {
        matchIndex = pendingInvocations.findIndex(
          (item) => item.target_turn_id == null && item.target_agent_id === event.agent_id,
        );
      }
      if (matchIndex >= 0) {
        a2aInvocation = pendingInvocations.splice(matchIndex, 1)[0];
      }
    }
    const usageByTurn = options.usageByTurn ?? [];
    const turnUsage = event.turn_id
      ? usageByTurn.find((u) => u.turn_id === event.turn_id)
      : undefined;
    return {
      id: String(event.id),
      turnIndex: isStatus ? 0 : event.role === "agent" ? agentTurnCount : 0,
      role: event.role,
      eventType: event.event_type,
      missionId: event.mission_id,
      artifact: event.artifact,
      content: event.content,
      attachments: event.attachments,
      capability,
      providerLabel,
      personaLabel,
      agentId: event.agent_id,
      createdAt: event.created_at,
      metadata,
      isStatusEvent: isStatus,
      a2aInvocation,
      usage: turnUsage,
      thoughts: event.turn_id ? thoughtsByTurn.get(event.turn_id) ?? [] : [],
      toolCalls: event.turn_id ? toolCallsByTurn.get(event.turn_id) ?? [] : [],
    };
  });

  const livePartial = options.livePartial ?? {};
  const turnRecords = options.turns ?? [];

  // Build existingTurnIds directly from events (turn_id lives at top-level, not in metadata).
  const existingTurnIds = new Set(
    events
      .filter((e) => e.role === "agent" && e.event_type === "message" && e.turn_id)
      .map((e) => e.turn_id as string),
  );

  // Append in-flight synthetic bubbles for turn_ids that the backend has
  // marked active (key present in livePartial). The partial string may be
  // empty — Gemini/Codex emit no token deltas, so the bubble must surface
  // before any text arrives, otherwise users stare at a blank screen for
  // 10–30s.
  const synthetic: TurnView[] = [];
  for (const [turnId, partial] of Object.entries(livePartial)) {
    if (partial === undefined) continue;
    if (existingTurnIds.has(turnId)) continue;
    const turnRec = turnRecords.find((t) => t.turn_id === turnId);
    if (!turnRec || turnRec.status !== "started") continue; // only show for in-flight turns
    let invocationIndex = pendingInvocations.findIndex(
      (item) => item.target_turn_id === turnId,
    );
    if (invocationIndex < 0) {
      invocationIndex = pendingInvocations.findIndex(
        (item) => item.target_turn_id == null && item.target_agent_id === turnRec.agent_id,
      );
    }
    const a2aInvocation = invocationIndex >= 0
      ? pendingInvocations.splice(invocationIndex, 1)[0]
      : undefined;
    agentTurnCount += 1;
    synthetic.push({
      id: `live:${turnId}`,
      turnIndex: agentTurnCount,
      role: "agent",
      eventType: "message.partial",
      content: "",
      capability: "unknown",
      providerLabel: turnRec.provider_id || "Provider pending",
      // 等待回复时也显示角色中文名，而不是 agent id（如 chulan）。
      personaLabel: mentionOptionById(turnRec.agent_id)?.label,
      agentId: turnRec.agent_id,
      createdAt: turnRec.started_at ?? new Date().toISOString(),
      metadata: { turn_id: turnId, provider_id: turnRec.provider_id },
      isStatusEvent: false,
      a2aInvocation,
      partialText: partial,
      thoughts: thoughtsByTurn.get(turnId) ?? [],
      toolCalls: toolCallsByTurn.get(turnId) ?? [],
    });
  }

  const unmatchedA2A = pendingInvocations.map((invocation): TurnView => {
    const targetTurn = invocation.target_turn_id
      ? turnRecords.find((turn) => turn.turn_id === invocation.target_turn_id)
      : undefined;
    const targetName = formatAgentName(invocation.target_agent_id);
    return {
      id: `a2a:${invocation.event_id}`,
      turnIndex: 0,
      role: "system",
      eventType: `a2a.${targetTurn?.status ?? "pending"}`,
      content: describeA2ATargetTurn(targetName, targetTurn),
      capability: "coordination",
      providerLabel: targetTurn?.provider_id || "A2A",
      personaLabel: formatAgentName(invocation.sender_agent_id),
      agentId: invocation.target_agent_id,
      createdAt: targetTurn?.completed_at ?? targetTurn?.started_at ?? invocation.created_at,
      metadata: targetTurn
        ? { turn_id: targetTurn.turn_id, provider_id: targetTurn.provider_id }
        : {},
      isStatusEvent: true,
      a2aInvocation: invocation,
    };
  });

  return [...baseViews, ...unmatchedA2A, ...synthetic];
}

function describeA2ATargetTurn(targetName: string, turn?: TurnRecord): string {
  if (!turn) return `等待${targetName}接力`;
  if (turn.status === "started") return `${targetName}正在接力`;
  if (turn.status === "completed") return `${targetName}接力已结束，但未发布消息`;
  const latestAttempt = [...turn.attempts].reverse().find(
    (attempt) => attempt.error || attempt.summary,
  );
  const detail = latestAttempt?.error ?? latestAttempt?.summary;
  const label = turn.status === "failed" ? "接力失败" : "接力已阻塞";
  return detail ? `${targetName}${label}：${detail}` : `${targetName}${label}`;
}

export function formatAgentName(agentId: string): string {
  return mentionOptionById(agentId)?.label ?? agentId;
}

function isThoughtEvent(event: ThreadEvent): boolean {
  return event.event_type === "thought" || event.event_type === "agent.thought";
}

function getStringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const strings = value.filter((item): item is string => typeof item === "string" && item.length > 0);
  return strings.length > 0 ? strings : undefined;
}

function buildThoughtsByTurn(
  events: ThreadEvent[],
  records: BuildTurnViewsOptions["thoughts"] = [],
): Map<string, ThoughtEntry[]> {
  const thoughts = new Map<string, ThoughtEntry[]>();

  function add(turnId: string | null | undefined, thought: ThoughtEntry) {
    if (!turnId) return;
    const bucket = thoughts.get(turnId) ?? [];
    bucket.push(thought);
    thoughts.set(turnId, bucket);
  }

  for (const event of events) {
    if (!isThoughtEvent(event)) continue;
    const metadata = event.metadata ?? {};
    add(event.turn_id ?? metadata.turn_id, {
      content: event.content,
      kind: metadata.kind,
      tags: getStringArray(metadata.tags),
      createdAt: event.created_at,
    });
  }

  for (const record of records) {
    add(record.turn_id, {
      content: record.content,
      kind: record.kind,
      tags: record.tags,
      createdAt: record.created_at,
    });
  }

  for (const bucket of thoughts.values()) {
    bucket.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  }

  return thoughts;
}

function nativeActivityPhase(event: ToolEventRecord): TurnToolCall["phase"] {
  if (event.phase === "failed" || event.ok === false) return "failed";
  if (event.phase === "result") return event.ok === true ? "completed" : "started";
  // Claude's content_block_stop only finishes generating the tool arguments.
  // Execution is confirmed by the subsequent tool_result, including in history.
  if (event.provider_id === "claude" && event.phase === "completed" && event.ok !== true) {
    return "started";
  }
  return event.phase;
}

function buildToolCallsByTurn(
  events: ToolEventRecord[] = [],
): Map<string, TurnToolCall[]> {
  const byTurn = new Map<string, TurnToolCall[]>();
  for (const event of events) {
    if (event.event_type !== "tool.call" && event.event_type !== "provider.activity") continue;
    if (!event.turn_id) continue;
    const bucket = byTurn.get(event.turn_id) ?? [];
    const existing = event.activity_id
      ? bucket.findIndex((call) => call.activityId === event.activity_id)
      : -1;
    const previous = existing >= 0 ? bucket[existing] : undefined;
    const toolCall: TurnToolCall = {
      toolName: event.tool_name === "tool" && previous ? previous.toolName : event.tool_name,
      summary: event.summary?.trim() || previous?.summary,
      toolInput: event.tool_input ?? event.args ?? previous?.toolInput,
      createdAt: previous?.createdAt ?? event.created_at,
      eventType: event.event_type,
      activityId: event.activity_id,
      phase: nativeActivityPhase(event),
    };
    if (existing >= 0) bucket[existing] = toolCall;
    else bucket.push(toolCall);
    byTurn.set(event.turn_id, bucket);
  }
  for (const bucket of byTurn.values()) {
    bucket.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  }
  return byTurn;
}

export function findLatestAgentTurn(turns: TurnView[]): TurnView | undefined {
  for (let i = turns.length - 1; i >= 0; i--) {
    if (!turns[i].isStatusEvent && turns[i].role === "agent") {
      return turns[i];
    }
  }
  return undefined;
}

export function buildAgentParticipants(turns: TurnView[]): AgentParticipant[] {
  const seen = new Map<string, TurnView>();
  for (const turn of turns) {
    if (!turn.isStatusEvent && turn.role === "agent" && turn.agentId) {
      seen.set(turn.agentId, turn);
    }
  }
  return teamAgents().map((agent) => {
    const turn = seen.get(agent.agentId);
    if (turn) {
      return {
        agentId: agent.agentId,
        personaLabel: turn.personaLabel ?? agent.personaLabel,
        capability: turn.capability,
        providerLabel: turn.providerLabel,
        turnIndex: turn.turnIndex,
        participated: true,
      };
    }
    return {
      agentId: agent.agentId,
      personaLabel: agent.personaLabel,
      capability: agent.capability,
      providerLabel: "",
      turnIndex: 0,
      participated: false,
    };
  });
}

/**
 * Derive the transport path used for a turn from its attempt summaries.
 *
 * - "via MCP" in any attempt summary → "mcp"
 * - "stdout" in any attempt summary (but no MCP) → "stdout"
 * - anything else (fake adapter, unknown) → "fake"
 */
export type TurnPath = "mcp" | "stdout" | "fake";

export function deriveTurnPath(turn: TurnRecord): TurnPath {
  const summaries = (turn.attempts ?? []).map((a) => a.summary ?? "");
  if (summaries.some((s) => s.includes("via MCP"))) return "mcp";
  if (summaries.some((s) => s.includes("stdout"))) return "stdout";
  return "fake";
}

function inferCapability(event: ThreadEvent): Capability {
  const text = `${event.event_type} ${event.content}`.toLowerCase();
  if (text.includes("review") || text.includes("risk") || text.includes("风险")) return "review";
  if (text.includes("implement") || text.includes("artifact") || text.includes("执行")) return "implementation";
  if (text.includes("plan") || text.includes("规划")) return "planning";
  return "unknown";
}
