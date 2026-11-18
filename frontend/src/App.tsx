import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MissionRoomPage } from "./components/MissionRoomPage";
import { TasksWorkspace } from "./components/TasksWorkspace";
import { useMissionStream } from "./hooks/useMissionStream";
import { useProviderStatus } from "./hooks/useProviderStatus";
import {
  completeOnboarding,
  activateOnboardingProvider,
  createConversation,
  createConversationMission,
  deleteConversation,
  fetchConversation,
  fetchMission,
  getHealth,
  getHubSettings,
  getOnboarding,
  interruptMission,
  listAgents,
  listConversations,
  newRequestId,
  pauseMission,
  sendDiscussionControl,
  updateConversation,
  updateHubSettings,
  startOnboardingFirstChat,
  verifyOnboardingProvider,
  type MissionCreatePayload,
} from "./lib/api";
import { formatDiscussionError } from "./lib/roundtableView";
import { detectUnsupportedTargetedSkill, parseDiscussCommand } from "./lib/skillCommands";
import { buildConversationTimeline } from "./lib/conversationTimeline";
import { buildAgentParticipants, buildMissionRuntimeSummary, buildTurnViews } from "./lib/missionView";
import { MENTION_OPTIONS, findTypedMentions, parseMentionTarget, setMentionOptions, stripTypedMentions } from "./lib/mentions";
import {
  confirmedMentionIds,
  stripConfirmedMentions,
  type ConfirmedMention,
} from "./lib/inputConfirmations";
import {
  isTerminalMissionStatus,
  type ConversationDetail,
  type ConversationSummary,
  type DiscussionControlAction,
  type HealthView,
  type HubSettingsPatch,
  type HubSettingsView,
  type MissionDetail,
  type OnboardingView,
} from "./types";
import "./styles.css";

export default function App() {
  const [section, setSection] = useState<"conversations" | "tasks">(() => readWorkspaceRoute().section);
  const [selectedTaskId, setSelectedTaskId] = useState<string | undefined>(() => readWorkspaceRoute().taskId);
  const [tasksVisited, setTasksVisited] = useState(() => readWorkspaceRoute().section === "tasks");
  const conversationRequest = useRef(0);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationScope, setConversationScope] = useState<"active" | "archived">("active");
  const [selectedConvId, setSelectedConvId] = useState<string | undefined>();
  const [convDetail, setConvDetail] = useState<ConversationDetail | undefined>();
  const [latestMissionDetail, setLatestMissionDetail] = useState<MissionDetail | undefined>();
  const [input, setInput] = useState("");
  const [confirmations, setConfirmations] = useState<ConfirmedMention[]>([]);
  /** The last user message explicitly selected for editing, if any. */
  const [revisingMessageId, setRevisingMessageId] = useState<string | null>(null);
  const draftBeforeRevision = useRef<{ input: string; confirmations: ConfirmedMention[] } | null>(null);
  /** The mission on screen, so a slow stop can tell the user has moved elsewhere. */
  const shownMissionId = useRef<string | undefined>(undefined);
  const [stopping, setStopping] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [discussionBusy, setDiscussionBusy] = useState(false);
  const [hubSettings, setHubSettings] = useState<HubSettingsView | null>(null);
  const [health, setHealth] = useState<HealthView | null>(null);
  const [healthLoading, setHealthLoading] = useState(true);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [onboarding, setOnboarding] = useState<OnboardingView | null>(null);
  const [onboardingBusy, setOnboardingBusy] = useState(false);
  const [onboardingError, setOnboardingError] = useState<string | null>(null);
  const providerStatus = useProviderStatus();

  useEffect(() => {
    const sync = () => {
      const route = readWorkspaceRoute();
      setSection(route.section);
      if (route.section === "tasks") {
        setTasksVisited(true);
        if (route.taskId) setSelectedTaskId(route.taskId);
      }
    };
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  function navigateWorkspace(next: "conversations" | "tasks", taskId = selectedTaskId) {
    setSection(next);
    if (next === "tasks") {
      setTasksVisited(true);
      setSelectedTaskId(taskId);
    }
    const hash = next === "tasks" ? `#tasks${taskId ? `/${encodeURIComponent(taskId)}` : ""}` : "#conversations";
    if (window.location.hash !== hash) window.location.hash = hash;
  }

  async function openTaskConversation(id: string) {
    navigateWorkspace("conversations");
    await handleSelectConversation(id);
  }

  const [, setAgentDirectoryVersion] = useState(0);
  const refreshAgentDirectory = useCallback(() => {
    listAgents()
      .then((agents) => {
        setMentionOptions(
          agents.map((a) => ({
            id: a.id,
            label: a.display_name,
            aliases: a.aliases,
            role: a.role,
            custom: a.custom,
          })),
        );
        // Names, colours and @ candidates read the directory at render time.
        setAgentDirectoryVersion((version) => version + 1);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    listConversations().then(setConversations).catch(() => {});
    getHubSettings().then(setHubSettings).catch(() => {});
    refreshAgentDirectory();
  }, [refreshAgentDirectory]);

  const handleSettingsViewChange = useCallback((view: HubSettingsView) => {
    setHubSettings(view);
    refreshAgentDirectory();
  }, [refreshAgentDirectory]);

  const loadHealth = useCallback(async () => {
    setHealthLoading(true);
    setHealthError(null);
    try {
      setHealth(await getHealth());
    } catch (err) {
      setHealth(null);
      setHealthError(err instanceof Error ? err.message : "后端尚未就绪");
    } finally {
      setHealthLoading(false);
    }
  }, []);

  const loadOnboarding = useCallback(async () => {
    try {
      const state = await getOnboarding();
      setOnboarding(state);
      if (state.state !== "completed" && state.conversation_id) {
        const detail = await fetchConversation(state.conversation_id);
        setSelectedConvId(state.conversation_id);
        setConvDetail(detail);
        setLatestMissionDetail(detail.latest_mission_detail ?? undefined);
      }
    } catch (err) {
      setOnboardingError(err instanceof Error ? err.message : "加载启动引导失败");
    }
  }, []);

  useEffect(() => {
    void loadHealth();
    void loadOnboarding();
  }, [loadHealth, loadOnboarding]);

  useEffect(() => {
    // Background identity refresh must not reset onboarding or its loading UI.
    const refresh = () => {
      if (document.visibilityState !== "hidden") {
        void getHealth().then((next) => {
          setHealth(next);
          setHealthError(null);
        }).catch(() => {
          setHealth(null);
          setHealthError("后端连接中断，运行身份暂不可核对");
        });
      }
    };
    const timer = window.setInterval(refresh, 30000);
    window.addEventListener("focus", refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
    };
  }, []);

  const handleSaveSettings = useCallback(async (patch: HubSettingsPatch) => {
    try {
      const updated = await updateHubSettings(patch);
      setHubSettings(updated);
      providerStatus.refresh();
    } catch {
      setError("保存设置失败");
    }
  }, [providerStatus.refresh]);

  const hasRunningMission = Boolean(
    latestMissionDetail && (!isTerminalMissionStatus(latestMissionDetail.mission.status) ||
      (latestMissionDetail.discussion?.protocol === "deliberation_v2" && latestMissionDetail.discussion.phase !== "ended")),
  );

  useEffect(() => {
    shownMissionId.current = latestMissionDetail?.mission.id;
  }, [latestMissionDetail?.mission.id]);

  const handleSnapshot = useCallback((snapshot: MissionDetail) => {
    setLatestMissionDetail((prev) => {
      if (prev && snapshot.mission.id === prev.mission.id && isOlderSnapshot(snapshot, prev)) {
        return prev;
      }
      return snapshot;
    });
    setError(null);
  }, []);

  const streamState = useMissionStream(
    hasRunningMission ? latestMissionDetail?.mission.id : undefined,
    handleSnapshot,
  );

  useEffect(() => {
    if (
      latestMissionDetail &&
      isTerminalMissionStatus(latestMissionDetail.mission.status) &&
      selectedConvId
    ) {
      fetchConversation(selectedConvId).then(setConvDetail).catch(() => {});
      listConversations().then(setConversations).catch(() => {});
      if (
        onboarding?.mission_id === latestMissionDetail.mission.id ||
        onboarding?.verification_mission_id === latestMissionDetail.mission.id ||
        onboarding?.team_intro_mission_id === latestMissionDetail.mission.id
      ) {
        getOnboarding().then(setOnboarding).catch(() => {});
      }
    }
  }, [latestMissionDetail?.mission.status, latestMissionDetail?.mission.id, selectedConvId, onboarding?.mission_id, onboarding?.verification_mission_id, onboarding?.team_intro_mission_id]);

  const displayMessages = useMemo(
    () => buildConversationTimeline(convDetail, latestMissionDetail),
    [convDetail, latestMissionDetail],
  );
  const lastUserMessage = [...displayMessages].reverse().find((message) => message.role === "user");
  const editableMessage = !hasRunningMission && !latestMissionDetail?.discussion
    && !convDetail?.has_autonomous_work && conversationScope !== "archived"
    && lastUserMessage && !lastUserMessage.metadata?.revised ? lastUserMessage : undefined;

  const quietDiscussion = latestMissionDetail?.discussion?.protocol === 'deliberation_v2';

  const turns = useMemo(
    () => buildTurnViews(
      displayMessages,
      latestMissionDetail?.mission.kind === "provider_verification"
        ? []
        : latestMissionDetail?.a2a_invocations ?? [],
      {
        livePartial: quietDiscussion || latestMissionDetail?.mission.kind === "provider_verification" ? undefined : latestMissionDetail?.live_partial,
        turns: quietDiscussion || latestMissionDetail?.mission.kind === "provider_verification" ? undefined : latestMissionDetail?.turns,
        usageByTurn: latestMissionDetail?.mission.kind === "provider_verification" ? undefined : latestMissionDetail?.usage_summary?.by_turn,
        thoughts: quietDiscussion ? undefined : latestMissionDetail?.mission.kind === "provider_verification"
          ? convDetail?.thoughts
          : [
              ...(convDetail?.thoughts ?? []).filter((record) => record.mission_id !== latestMissionDetail?.mission.id),
              ...(latestMissionDetail?.thoughts ?? []),
            ],
        toolEvents: quietDiscussion ? undefined : latestMissionDetail?.mission.kind === "provider_verification"
          ? convDetail?.tool_events
          : [
              ...(convDetail?.tool_events ?? []).filter((record) => record.mission_id !== latestMissionDetail?.mission.id),
              ...(latestMissionDetail?.tool_events ?? []),
            ],
      },
    ),
    [quietDiscussion, displayMessages, convDetail?.thoughts, convDetail?.tool_events, latestMissionDetail?.mission.kind, latestMissionDetail?.mission.id, latestMissionDetail?.a2a_invocations, latestMissionDetail?.live_partial, latestMissionDetail?.turns, latestMissionDetail?.usage_summary, latestMissionDetail?.thoughts, latestMissionDetail?.tool_events],
  );
  const agentParticipants = useMemo(
    () => buildAgentParticipants(turns),
    [turns],
  );
  const runtimeSummary = useMemo(
    () => ({ ...buildMissionRuntimeSummary(latestMissionDetail, turns),
      ...(convDetail?.work_status_label ? { statusLabel: convDetail.work_status_label } : {}) }),
    [latestMissionDetail, turns, convDetail?.work_status_label],
  );

  async function handleSelectConversation(convId: string) {
    const request = ++conversationRequest.current;
    setRevisingMessageId(null);
    setSelectedConvId(convId);
    setConvDetail(undefined);
    setLatestMissionDetail(undefined);
    try {
      const detail = await fetchConversation(convId);
      if (request !== conversationRequest.current) return;
      setConvDetail(detail);
      setConversationScope(detail.conversation.status);
      if (detail.latest_mission_detail) {
        setLatestMissionDetail(detail.latest_mission_detail);
      } else {
        setLatestMissionDetail(undefined);
      }
    } catch {
      if (request === conversationRequest.current) setError("加载会话失败");
    }
  }

  async function handleRenameConversation(convId: string, title: string) {
    try {
      const detail = await updateConversation(convId, { title });
      if (convId === selectedConvId) {
        setConvDetail(detail);
      }
      listConversations().then(setConversations).catch(() => {});
    } catch {
      setError("重命名失败");
    }
  }

  async function handleConversationStatus(convId: string, status: "active" | "archived") {
    try {
      await updateConversation(convId, { status });
      if (status === "archived" && convId === selectedConvId) {
        conversationRequest.current++;
        setSelectedConvId(undefined);
        setConvDetail(undefined);
        setLatestMissionDetail(undefined);
      }
      setConversations(await listConversations(conversationScope));
    } catch (err) {
      setError(err instanceof Error ? err.message : status === "archived" ? "归档失败" : "恢复失败");
    }
  }

  async function handleDeleteConversation(convId: string) {
    try {
      await deleteConversation(convId);
      if (convId === selectedConvId) {
        conversationRequest.current++;
        setSelectedConvId(undefined);
        setConvDetail(undefined);
        setLatestMissionDetail(undefined);
      }
      setConversations(await listConversations(conversationScope));
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    }
  }

  async function handleConversationScopeChange(scope: "active" | "archived") {
    conversationRequest.current++;
    setConversationScope(scope);
    setError(null);
    try {
      setConversations(await listConversations(scope));
      setSelectedConvId(undefined);
      setConvDetail(undefined);
      setLatestMissionDetail(undefined);
    } catch {
      setError("加载会话列表失败");
    }
  }

  async function handleNewConversation() {
    const request = ++conversationRequest.current;
    setRevisingMessageId(null);
    try {
      const conv = await createConversation();
      if (request !== conversationRequest.current) return;
      setSelectedConvId(conv.id);
      setConvDetail({
        conversation: conv,
        missions: [],
        messages: [],
        latest_mission_detail: null,
      });
      setLatestMissionDetail(undefined);
      const updated = await listConversations();
      setConversations(updated);
    } catch {
      setError("创建会话失败");
    }
  }

  const refreshConversationState = useCallback(async () => {
    if (!selectedConvId) return;
    try {
      const detail = await fetchConversation(selectedConvId);
      setConvDetail(detail);
      setLatestMissionDetail(detail.latest_mission_detail ?? undefined);
    } catch {
      // Keep the last known snapshot; the stream may still recover.
    }
  }, [selectedConvId]);

  function handleContinueDiscussion() {
    const previousGoal = latestMissionDetail?.mission.goal ?? "";
    setError(null);
    if (previousGoal) {
      setConfirmations([]);
      setInput((prev) => (prev.trim() ? prev : `/discuss ${previousGoal}`));
    }
  }

  async function sendDiscussionControlRequest(action: DiscussionControlAction, fields: import("./types").DiscussionControlFields = {}) {
    const mission = latestMissionDetail;
    if (!mission) return;
    setDiscussionBusy(true);
    setError(null);
    try {
      const updated = await sendDiscussionControl(
        mission.mission.id,
        { action, ...fields, ...(mission.discussion?.protocol === "deliberation_v2" ? { expected_revision: mission.discussion.revision } : {}) },
        newRequestId(action),
      );
      setLatestMissionDetail(updated);
    } catch (err) {
      setError(formatDiscussionError(err instanceof Error ? err.message : "发送讨论控制失败"));
      await refreshConversationState();
    } finally {
      setDiscussionBusy(false);
    }
  }

  async function submit() {
    const rawInput = input.trim();
    // 组合收紧：讨论技能不支持指定单个成员，明确拦截而不是退化成普通 goal。
    if (detectUnsupportedTargetedSkill(rawInput)) {
      setError("/discuss 讨论技能不支持指定成员：请去掉 @成员，或将 /discuss 放在正文开头。");
      return;
    }
    const skillCommand = parseDiscussCommand(rawInput);
    if (skillCommand && !skillCommand.goal) {
      setError("请在 /discuss 后输入要讨论的问题。");
      return;
    }

    // 浮层确认过的 @提及优先，按确认顺序路由；手动打全的 `@别名`（未点选、未回车）
    // 同样点名，避免用户以为指定了人物却由别人回答。
    const missionTargetAgentIds = [...new Set([
      ...confirmedMentionIds(confirmations),
      ...findTypedMentions(input).map((mention) => mention.agentId),
    ])];
    // 用未 trim 的原文剥离确认实体，保证实体坐标与提交文本一致：
    // 尾随/前导空格都不会让坐标错位，纯 @提及 无正文也会被正确拦截。
    const bodyless = skillCommand
      ? skillCommand.goal
      : stripTypedMentions(stripConfirmedMentions(input, confirmations));
    if (!bodyless.trim()) {
      setError("仅 @成员 不能作为任务内容，请补充具体任务。");
      return;
    }

    const activeMission = hasRunningMission ? latestMissionDetail : undefined;
    // Ordinary chats take no input mid-run: the draft waits until the run ends or is stopped.
    if (activeMission && !activeMission.discussion) return;
    const roundtableActive = activeMission?.mission.room_mode === "roundtable";
    if (activeMission && skillCommand) {
      setError("当前任务尚未结束。请先完成或停止当前任务，再调用 /discuss。");
      return;
    }
    const revising = revisingMessageId && !activeMission ? revisingMessageId : null;
    if (revising && skillCommand) {
      setError("修改上一条发言时不能调用 /discuss；请取消修改后再发起讨论。");
      return;
    }
    const payload: MissionCreatePayload =
      skillCommand
        ? {
            goal: skillCommand.goal,
            skill: skillCommand.skill,
            request_id: newRequestId("skill"),
          }
        : {
            goal: rawInput,
            target_agent_ids: missionTargetAgentIds,
            ...(revising ? { revises_message_id: revising } : {}),
          };

    setBusy(true);
    setError(null);
    try {
      if (activeMission) {
        const updated = await interruptMission(activeMission.mission.id, rawInput, {
          targetAgentId: roundtableActive
            ? (activeMission.discussion?.protocol === "deliberation_v2"
                ? undefined
                : parseMentionTarget(rawInput).targetAgentId)
            : missionTargetAgentIds[0],
          requestId: roundtableActive ? newRequestId("input") : undefined,
        });
        setLatestMissionDetail(updated);
      } else if (selectedConvId) {
        if (payload.skill) await ensureSkillRuntime();
        const detail = await createConversationMission(selectedConvId, payload);
        setConvDetail(detail);
        if (detail.latest_mission_detail) {
          setLatestMissionDetail(detail.latest_mission_detail);
        }
        listConversations().then(setConversations).catch(() => {});
      } else {
        if (payload.skill) await ensureSkillRuntime();
        const conv = await createConversation();
        setSelectedConvId(conv.id);
        const detail = await createConversationMission(conv.id, payload);
        setConvDetail(detail);
        if (detail.latest_mission_detail) {
          setLatestMissionDetail(detail.latest_mission_detail);
        }
        listConversations().then(setConversations).catch(() => {});
      }
      setInput("");
      setConfirmations([]);
      setRevisingMessageId(null);
    } catch (err) {
      // Requests that lost the race keep the user's text and refresh the truth.
      setError(formatDiscussionError(err instanceof Error ? err.message : "请求失败"));
      await refreshConversationState();
    } finally {
      setBusy(false);
    }
  }

  async function handleStop() {
    const mission = latestMissionDetail;
    if (!mission) return;
    setStopping(true);
    setError(null);
    try {
      let detail = await pauseMission(mission.mission.id);
      // The running turn stops within about a second; wait for the run to close.
      const stillShown = () => shownMissionId.current === mission.mission.id;
      for (let attempt = 0; attempt < 60 && stillShown() && !isTerminalMissionStatus(detail.mission.status); attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 250));
        detail = await fetchMission(mission.mission.id);
      }
      // The user switched conversations meanwhile; the stop still applies, the screen is theirs.
      if (!stillShown()) return;
      setLatestMissionDetail(detail);
      if (!isTerminalMissionStatus(detail.mission.status)) {
        setError("已请求停止，当前运行还在结束中，请稍候。");
        return;
      }
      await refreshConversationState();
    } catch (err) {
      setError(err instanceof Error ? err.message : "停止失败");
    } finally {
      setStopping(false);
    }
  }

  async function ensureSkillRuntime() {
    const runtime = await getHealth();
    if (!runtime.project_skills?.includes("discuss") || !runtime.discussion_protocols?.includes("deliberation_v2")) {
      throw new Error("后端尚未加载 discuss 技能，请重启后端后重试；当前输入已保留。");
    }
  }

  async function handleStartOnboarding(providerId: string) {
    setOnboardingBusy(true);
    setOnboardingError(null);
    try {
      const response = await startOnboardingFirstChat(providerId);
      setOnboarding(response.onboarding);
      setSelectedConvId(response.conversation.conversation.id);
      setConvDetail(response.conversation);
      setLatestMissionDetail(response.conversation.latest_mission_detail ?? undefined);
      listConversations().then(setConversations).catch(() => {});
    } catch (err) {
      setOnboardingError(err instanceof Error ? err.message : "启动首次对话失败");
    } finally {
      setOnboardingBusy(false);
    }
  }

  async function handleCompleteOnboarding() {
    setOnboardingBusy(true);
    setOnboardingError(null);
    try {
      setOnboarding(await completeOnboarding(false));
    } catch (err) {
      setOnboardingError(err instanceof Error ? err.message : "完成启动引导失败");
    } finally {
      setOnboardingBusy(false);
    }
  }

  async function handleSkipRealCliSetup() {
    setOnboardingBusy(true);
    setOnboardingError(null);
    try {
      setOnboarding(await completeOnboarding(true));
    } catch (err) {
      setOnboardingError(err instanceof Error ? err.message : "完成启动引导失败");
    } finally {
      setOnboardingBusy(false);
    }
  }

  async function handleUseProviderForTeam(providerId: string) {
    setOnboardingBusy(true);
    setOnboardingError(null);
    try {
      const teamIds = hubSettings?.agents.map((agent) => agent.id) ?? MENTION_OPTIONS.map((option) => option.id);
      const agents = Object.fromEntries(
        teamIds.map((agentId) => [
          agentId,
          { default_provider_id: providerId, fallback_provider_ids: [] },
        ]),
      );
      const updated = await updateHubSettings({
        runtime: { use_fake_cli: false },
        providers: { [providerId]: { enabled: true } },
        agents,
      });
      setHubSettings(updated);
      setOnboarding(await completeOnboarding(false));
      providerStatus.refresh();
    } catch (err) {
      setOnboardingError(err instanceof Error ? err.message : "团队配置保存失败");
    } finally {
      setOnboardingBusy(false);
    }
  }

  async function handleVerifyProvider(providerId: string) {
    setOnboardingBusy(true);
    setOnboardingError(null);
    try {
      const response = await verifyOnboardingProvider(providerId);
      setOnboarding(response.onboarding);
      setSelectedConvId(response.conversation.conversation.id);
      setConvDetail(response.conversation);
      setLatestMissionDetail(response.conversation.latest_mission_detail ?? undefined);
      listConversations().then(setConversations).catch(() => {});
    } catch (err) {
      setOnboardingError(err instanceof Error ? err.message : "真实 CLI 验证启动失败");
    } finally {
      setOnboardingBusy(false);
    }
  }

  async function handleActivateProvider(providerId: string) {
    setOnboardingBusy(true);
    setOnboardingError(null);
    try {
      const response = await activateOnboardingProvider(providerId);
      setOnboarding(response.onboarding);
      setSelectedConvId(response.conversation.conversation.id);
      setConvDetail(response.conversation);
      setLatestMissionDetail(response.conversation.latest_mission_detail ?? undefined);
      listConversations().then(setConversations).catch(() => {});
      getHubSettings().then(setHubSettings).catch(() => {});
      providerStatus.refresh();
    } catch (err) {
      setOnboardingError(err instanceof Error ? err.message : "真实 CLI 接管失败");
    } finally {
      setOnboardingBusy(false);
    }
  }

  const tasksEnabled = hubSettings?.extensions?.tasks?.value ?? false;
  const discussionEnabled = hubSettings?.extensions?.discussion?.value ?? false;

  return (
    <MissionRoomPage
      section={section}
      onSectionChange={navigateWorkspace}
      onOpenTask={id => navigateWorkspace("tasks", id)}
      tasksEnabled={tasksEnabled}
      discussionEnabled={discussionEnabled}
      taskContent={tasksEnabled && tasksVisited ? <TasksWorkspace selectedTaskId={selectedTaskId}
        active={section === "tasks"} onSelectTask={id => navigateWorkspace("tasks", id)}
        onOpenConversation={id => void openTaskConversation(id)} /> : undefined}
      hasAutonomousWork={convDetail?.has_autonomous_work}
      onWorkUpdate={refreshConversationState}
      detail={latestMissionDetail}
      turns={turns}
      agentParticipants={agentParticipants}
      runtimeSummary={runtimeSummary}
      streamState={streamState}
      providerStatus={providerStatus}
      input={input}
      busy={busy}
      error={error}
      onInputChange={setInput}
      confirmations={confirmations}
      onConfirmationsChange={setConfirmations}
      onSubmit={submit}
      conversations={conversations}
      selectedConversationId={selectedConvId}
      onSelectConversation={handleSelectConversation}
      onNewConversation={handleNewConversation}
      onRenameConversation={handleRenameConversation}
      conversationScope={conversationScope}
      onConversationScopeChange={handleConversationScopeChange}
      onConversationStatusChange={handleConversationStatus}
      onDeleteConversation={handleDeleteConversation}
      loadedConversationId={convDetail?.conversation.id}
      displayMessages={displayMessages}
      hubSettings={hubSettings}
      onSaveSettings={handleSaveSettings}
      onSettingsViewChange={handleSettingsViewChange}
      discussionBusy={discussionBusy}
      onRequestFinish={() => {
        void sendDiscussionControlRequest("finish");
      }}
      onDiscussionControl={(action, fields) => { void sendDiscussionControlRequest(action, fields); }}
      onRequestStop={() => {
        void sendDiscussionControlRequest("stop");
      }}
      onContinueDiscussion={handleContinueDiscussion}
      onEditMessage={editableMessage ? () => {
        if (!revisingMessageId) draftBeforeRevision.current = { input, confirmations };
        setRevisingMessageId(String(editableMessage.id));
        setInput(editableMessage.content);
        setConfirmations([]);
        setError(null);
      } : undefined}
      editableMessageId={editableMessage ? String(editableMessage.id) : undefined}
      onStop={() => { void handleStop(); }}
      stopping={stopping}
      revising={Boolean(revisingMessageId) && !hasRunningMission}
      onCancelRevise={() => {
        setRevisingMessageId(null);
        setInput(draftBeforeRevision.current?.input ?? "");
        setConfirmations(draftBeforeRevision.current?.confirmations ?? []);
        draftBeforeRevision.current = null;
      }}
      onboarding={{
        health,
        healthLoading,
        healthError,
        onboarding,
        actionBusy: onboardingBusy,
        actionError: onboardingError,
        onStart: handleStartOnboarding,
        onRetryHealth: loadHealth,
        onComplete: handleCompleteOnboarding,
        onSkipRealCli: handleSkipRealCliSetup,
        onUseProviderForTeam: handleUseProviderForTeam,
        onRefreshProviders: loadOnboarding,
        onVerifyProvider: handleVerifyProvider,
        onActivateProvider: handleActivateProvider,
      }}
    />
  );
}

function readWorkspaceRoute(): { section: "conversations" | "tasks"; taskId?: string } {
  const match = /^#tasks(?:\/([^/]+))?$/.exec(window.location.hash);
  if (!match) return { section: "conversations" };
  try { return { section: "tasks", taskId: match[1] ? decodeURIComponent(match[1]) : undefined }; }
  catch { return { section: "tasks" }; }
}

function isOlderSnapshot(nextDetail: MissionDetail, currentDetail?: MissionDetail): boolean {
  if (!currentDetail || nextDetail.mission.id !== currentDetail.mission.id) return false;
  const nextUpdatedAt = Date.parse(nextDetail.mission.updated_at);
  const currentUpdatedAt = Date.parse(currentDetail.mission.updated_at);
  return (
    Number.isFinite(nextUpdatedAt) &&
    Number.isFinite(currentUpdatedAt) &&
    nextUpdatedAt < currentUpdatedAt
  );
}
