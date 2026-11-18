import { ArtifactSidebar } from "./ArtifactSidebar";
import { collectConversationArtifacts } from "../lib/artifactSidebar";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { MessagesSquare, Workflow } from "lucide-react";
import type { StreamState } from "../hooks/useMissionStream";
import type { ProviderStatusState } from "../hooks/useProviderStatus";
import {
  isTerminalMissionStatus,
  type AgentParticipant,
  type ConversationSummary,
  type HubSettingsPatch,
  type HubSettingsView,
  type MissionDetail,
  type MissionRuntimeSummary,
  type ThreadEvent,
  type TurnView,
} from "../types";
import { ConversationList } from "./ConversationList";
import { DiscussionSkillStatus } from "./DiscussionSkillStatus";
import { DiscussionHighlights } from "./DiscussionHighlights";
import { MissionComposer } from "./MissionComposer";
import type { ConfirmedMention } from "../lib/inputConfirmations";
import { MissionHeader } from "./MissionHeader";
import { OnboardingCard, type OnboardingCardProps } from "./OnboardingCard";
import { SettingsButton } from "./SettingsButton";
import { TurnTimeline } from "./TurnTimeline";
import { EnvironmentBadge } from "./EnvironmentBadge";
import { UpdateCapsule } from "./UpdateCapsule";
import { useDesktopBridge } from "../hooks/useDesktopBridge";
import { SharedWorkWorkspace } from "./SharedWorkWorkspace";
import "./WorkspaceShell.css";

export function MissionRoomPage(props: {
  section?: "conversations" | "tasks";
  onSectionChange?: (section: "conversations" | "tasks") => void;
  onOpenTask?: (id: string) => void;
  taskContent?: ReactNode;
  /** Optional extensions; the core chat works without either. */
  tasksEnabled?: boolean;
  discussionEnabled?: boolean;
  detail?: MissionDetail;
  turns: TurnView[];
  agentParticipants: AgentParticipant[];
  runtimeSummary: MissionRuntimeSummary;
  streamState: StreamState;
  providerStatus: ProviderStatusState;
  input: string;
  busy: boolean;
  error?: string | null;
  onInputChange: (value: string) => void;
  confirmations?: ConfirmedMention[];
  onConfirmationsChange?: (mentions: ConfirmedMention[]) => void;
  onSubmit: () => void;
  hasAutonomousWork?: boolean;
  conversations: ConversationSummary[];
  selectedConversationId?: string;
  loadedConversationId?: string;
  onSelectConversation: (id: string) => void;
  onNewConversation: () => void;
  onRenameConversation: (id: string, title: string) => void;
  conversationScope: "active" | "archived";
  onConversationScopeChange: (scope: "active" | "archived") => void;
  onConversationStatusChange: (id: string, status: "active" | "archived") => void;
  onDeleteConversation?: (id: string) => void;
  displayMessages: ThreadEvent[];
  hubSettings?: HubSettingsView | null;
  onSaveSettings?: (patch: HubSettingsPatch) => void;
  onSettingsViewChange?: (view: HubSettingsView) => void;
  onboarding?: OnboardingCardProps;
  discussionBusy: boolean;
  onRequestFinish: () => void;
  onDiscussionControl?: (action: import("../types").DiscussionControlAction, fields?: import("../types").DiscussionControlFields) => void;
  onRequestStop: () => void;
  onContinueDiscussion: () => void;
  onWorkUpdate?: () => void;
  /** Stop an ordinary running chat without changing the input. */
  onStop?: () => void;
  editableMessageId?: string;
  onEditMessage?: () => void;
  stopping?: boolean;
  revising?: boolean;
  onCancelRevise?: () => void;
}) {
  const [pendingArtifact,setPendingArtifact] = useState<{conversationId:string;identity:string}>();
  const [locateNotice,setLocateNotice] = useState('');
  const locate = useCallback((identity:string)=>{
    const card=Array.from(layoutRef.current?.querySelectorAll<HTMLElement>('[data-artifact-identity]') ?? []).find(el=>el.dataset.artifactIdentity===identity);
    if(!card)return '记录未加载，未找到文件卡片';
    if(card.closest('details:not([open])'))return '文件卡片在折叠的讨论记录里';
    card.dispatchEvent(new CustomEvent('artifact-locate',{bubbles:true}));
    card.scrollIntoView({block:'center',behavior:'smooth'});
    card.classList.add('artifact-card--located');
    window.setTimeout(()=>card.classList.remove('artifact-card--located'),1500);
    return undefined;
  },[]);
  useEffect(()=>{
    if(!pendingArtifact || pendingArtifact.conversationId!==props.selectedConversationId)return;
    // Wait for selected conversation's persisted timeline, not a fixed delay.
    if(props.loadedConversationId!==pendingArtifact.conversationId)return;
    setLocateNotice(locate(pendingArtifact.identity) ?? '已定位');setPendingArtifact(undefined);
  },[pendingArtifact,props.selectedConversationId,props.loadedConversationId,props.displayMessages,locate]);
  const layoutRef = useRef<HTMLDivElement>(null);
  const [artifactPinned,setArtifactPinned] = useState(false);
  const onPinnedChange = useCallback((pinned: boolean)=>setArtifactPinned(pinned),[]);
  const artifacts = collectConversationArtifacts(props.displayMessages);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const desktopBridge = useDesktopBridge();
  const tasksEnabled = props.tasksEnabled ?? false;
  const section = tasksEnabled ? props.section : "conversations";
  // Ordinary conversations hide the guide once they have turns; the welcome
  // conversation keeps it below its first real replies until setup finishes.
  const inOnboardingConversation = Boolean(
    props.selectedConversationId
    && props.selectedConversationId === props.onboarding?.onboarding?.conversation_id,
  );
  const showOnboarding = (props.turns.length === 0 || inOnboardingConversation)
    && props.onboarding?.onboarding?.state !== "completed" && !props.hasAutonomousWork;
  const hasActiveMission = Boolean(
    props.detail && (!isTerminalMissionStatus(props.detail.mission.status) ||
      (props.detail.discussion?.protocol === 'deliberation_v2' && props.detail.discussion.phase !== 'ended')),
  );
  const discussion = props.detail?.discussion ?? null;
  // Only discussions take input mid-run; an ordinary chat keeps the draft until it stops.
  const chatRunning = hasActiveMission && !discussion;
  const submitLabel = hasActiveMission
    ? (discussion ? "发送插话" : "运行中…")
    : "发送";
  const onboardingCard = props.onboarding && showOnboarding && !(discussion?.protocol === 'deliberation_v2' && props.turns.length > 0) ? (
    <OnboardingCard
      {...props.onboarding}
      onOpenTeamSettings={() => {
        props.onboarding?.onComplete();
        setSettingsOpen(true);
      }}
    />
  ) : null;
  const timeline = <TurnTimeline turns={props.turns} conversationId={props.selectedConversationId}
    contentRevision={discussion?.revision}
    editableMessageId={props.editableMessageId} onEditMessage={props.onEditMessage}
    emptyContent={discussion?.protocol === "deliberation_v2" ? <DiscussionHighlights discussion={discussion} goal={props.detail?.mission.goal ?? ""} /> : onboardingCard}
    afterContent={<>
      {props.turns.length > 0 && onboardingCard}
      {discussion && <DiscussionHighlights discussion={discussion} goal={props.detail?.mission.goal ?? ""} />}
    </>} />;
  const discussionPanel = discussion ? <DiscussionSkillStatus key={props.detail?.mission.id} discussion={discussion}
    missionStatus={props.detail?.mission.status ?? 'pending'} busy={props.discussionBusy}
    onRequestFinish={props.onRequestFinish} onControl={props.onDiscussionControl}
    onRequestStop={props.onRequestStop} onContinue={props.onContinueDiscussion} /> : null;
  const composer = <div className="discussion-panels composer-layout-compact">
    {discussionPanel && <div className="composer-discussion-status">{discussionPanel}</div>}
    <MissionComposer value={props.input} busy={props.busy} hasActiveMission={hasActiveMission}
      disabled={props.conversationScope === "archived"} submitLabel={submitLabel} submitDisabled={props.stopping || chatRunning}
      mentionsEnabled={!(hasActiveMission && discussion)} skillsEnabled={props.discussionEnabled ?? false}
      onChange={props.onInputChange}
      confirmations={props.confirmations} onConfirmationsChange={props.onConfirmationsChange} onSubmit={props.onSubmit}
      onStop={chatRunning ? props.onStop : undefined} stopping={props.stopping}
      revising={props.revising} onCancelRevise={props.onCancelRevise}
      />
  </div>;
  return (
    <main className="mission-room task-room">
      <div className="mission-header-bar">
        <MissionHeader runtimeSummary={props.runtimeSummary} streamState={props.streamState} showRuntime={section !== "tasks"} />
        <div className="mission-header-actions">
          {desktopBridge.showCapsule ? (
            <UpdateCapsule
              notice={desktopBridge.updateNotice}
              autoDownload={desktopBridge.state?.autoDownload ?? true}
              onInstallNow={desktopBridge.installNow}
              onDismiss={desktopBridge.dismissCurrentUpdate}
            />
          ) : null}
          <SettingsButton
            view={props.hubSettings ?? null}
            conversationId={props.selectedConversationId}
            providerStatus={props.providerStatus}
            onboarding={props.onboarding?.onboarding}
            open={settingsOpen}
            onOpenChange={setSettingsOpen}
            onRefreshProviders={() => {
              props.providerStatus.refresh();
              props.onboarding?.onRefreshProviders();
            }}
            onVerifyProvider={props.onboarding?.onVerifyProvider}
            onSave={props.onSaveSettings ?? (() => {})}
            onViewChange={props.onSettingsViewChange}
            isDesktop={desktopBridge.isDesktop}
            bridgeState={desktopBridge.state}
            onSetAutoDownload={desktopBridge.setAutoDownload}
            onCheckForUpdates={desktopBridge.checkForUpdates}
            isCheckingUpdates={desktopBridge.isChecking}
            runtime={props.onboarding?.health?.runtime}
          />
        </div>
      </div>
      {!desktopBridge.isDesktop ? (
        <EnvironmentBadge runtime={props.onboarding?.health?.runtime} loading={props.onboarding?.healthLoading} />
      ) : null}
      {props.error && section !== "tasks" ? (
        <div role="alert" className="error-banner">{props.error}</div>
      ) : null}
      <div className="unity-shell">
        {tasksEnabled ? <nav className="unity-navigation" aria-label="工作区导航">
          <button type="button" aria-current={props.section !== "tasks" ? "page" : undefined} onClick={() => props.onSectionChange?.("conversations")}><MessagesSquare size={22} /><span>会话</span></button>
          <button type="button" aria-current={props.section === "tasks" ? "page" : undefined} onClick={() => props.onSectionChange?.("tasks")}><Workflow size={22} /><span>任务</span></button>
        </nav> : null}
        <div className="unity-workspace-pane" hidden={section === "tasks"}>
        <div className="workspace-layout">
        <ConversationList
          conversations={props.conversations}
          selectedId={props.selectedConversationId}
          onSelect={props.onSelectConversation}
          onNew={props.onNewConversation}
          onRename={props.onRenameConversation}
          scope={props.conversationScope}
          onScopeChange={props.onConversationScopeChange}
          onStatusChange={props.onConversationStatusChange}
          onDelete={props.onDeleteConversation}
        />
        <div ref={layoutRef} className={`conversation-layout${artifactPinned ? " has-artifact-sidebar" : ""}`}>
          <section className="conversation-main" aria-label="任务对话">
            {tasksEnabled && props.selectedConversationId ? <SharedWorkWorkspace key={props.selectedConversationId}
              conversationId={props.selectedConversationId} goal={props.detail?.mission.goal ?? ''}
              archived={props.conversationScope === 'archived'} onUpdate={props.onWorkUpdate}
              onOpenTask={props.onOpenTask}
              chat={timeline} composer={composer} /> : <>{timeline}{composer}</>}
          </section>
          {locateNotice && <p role="status" className="artifact-locate-notice">{locateNotice}</p>}
          <ArtifactSidebar items={artifacts} onPinnedChange={onPinnedChange} onLocate={item=>{
            if(item.conversation_id && item.conversation_id!==props.selectedConversationId){
              setPendingArtifact({conversationId:item.conversation_id,identity:item.identity});
              setLocateNotice('正在加载会话…');props.onSelectConversation(item.conversation_id);return '正在加载会话';
            }
            const notice=locate(item.identity);setLocateNotice(notice ?? '已定位');return notice;
          }} />
        </div>
      </div>
        </div>
        {tasksEnabled ? <div className="unity-workspace-pane unity-tasks-pane" hidden={props.section !== "tasks"}>{props.taskContent}</div> : null}
      </div>
    </main>
  );
}
