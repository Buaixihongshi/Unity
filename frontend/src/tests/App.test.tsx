import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import type { ConversationDetail, MissionDetail, MissionStatus } from "../types";

const BACKEND_MISSION_STATUSES: MissionStatus[] = [
  "queued",
  "pending",
  "running",
  "blocked",
  "completed",
  "failed",
  "completed_with_warnings",
];

function makeConvDetail(overrides?: Partial<ConversationDetail>): ConversationDetail {
  return {
    conversation: { id: "c_1", title: "测试", status: "active", created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:00:00Z" },
    missions: [{ id: "m1", goal: "设计一个多 Agent 任务室", status: "queued" as MissionStatus, created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:00:00Z" }],
    messages: [],
    latest_mission_detail: {
      mission: { id: "m1", goal: "设计一个多 Agent 任务室", status: "queued" as MissionStatus, created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:00:00Z" },
      events: [],
      artifacts: [],
    },
    ...overrides,
  };
}

function hubSettingsWith(enabled: Partial<Record<"discussion" | "tasks" | "maintenance", boolean>>) {
  const field = (value: boolean) => ({ value, source: value ? "user" : "default" });
  return new Response(JSON.stringify({
    runtime: {}, providers: [], agents: [],
    extensions: {
      discussion: field(enabled.discussion ?? false),
      tasks: field(enabled.tasks ?? false),
      maintenance: field(enabled.maintenance ?? false),
    },
  }), { status: 200 });
}

function stubFetch(extraHandlers?: (url: string, init?: RequestInit) => Response | null) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (extraHandlers) {
      const result = extraHandlers(url, init);
      if (result) return result;
    }
    if (url === "/api/providers") {
      return new Response(JSON.stringify([]), { status: 200 });
    }
    if (url === "/api/health") {
      return new Response(JSON.stringify({
        status: "ok",
        version: "0.1.0",
        database: "ready",
        config: "ready",
        fake_provider: "ready",
      }), { status: 200 });
    }
    if (url === "/api/onboarding" && (!init || init.method === "GET")) {
      return new Response(JSON.stringify({
        state: "completed",
        guide_agent_id: "chulan",
        conversation_id: null,
        mission_id: null,
        first_reply_completed_at: null,
        completed_at: "2026-05-05T00:00:00Z",
        verified_provider_ids: [],
        providers: [],
        dismissed_real_cli_setup: true,
      }), { status: 200 });
    }
    if (url.startsWith("/api/conversations?") && (!init || init.method !== "POST")) {
      return new Response(JSON.stringify([]), { status: 200 });
    }
    if (url === "/api/conversations" && init?.method === "POST") {
      return new Response(JSON.stringify({ id: "c_1", title: "新对话", status: "active", created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:00:00Z" }), { status: 200 });
    }
    if (url.match(/\/api\/conversations\/[^/]+\/missions/) && init?.method === "POST") {
      return new Response(JSON.stringify(makeConvDetail()), { status: 200 });
    }
    if (url.match(/\/api\/conversations\/[^/]+$/) && (!init || init.method === "GET")) {
      return new Response(JSON.stringify(makeConvDetail()), { status: 200 });
    }
    if (url === "/api/missions" && init?.method === "POST") {
      return new Response(JSON.stringify({
        mission: { id: "m1", goal: "设计一个多 Agent 任务室", status: "queued", created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:00:00Z" },
        events: [],
        artifacts: [],
      }), { status: 200 });
    }
    if (url === "/api/missions") {
      return new Response(JSON.stringify([]), { status: 200 });
    }
    return new Response("{}", { status: 404 });
  });
}

/** 等待 selectAt 里 requestAnimationFrame 设置的光标落位，避免后续打字被移动光标打乱顺序。 */
async function flushRaf() {
  await new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  });
}

describe("Unity app", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
    vi.unstubAllGlobals();
    vi.stubGlobal("fetch", stubFetch());
  });

  afterEach(() => { window.history.replaceState(null, "", "/"); });

  it("任务有独立入口；未选择会话也能打开，切回仍保留聊天草稿", async () => {
    const request = stubFetch(url => url === "/api/tasks" ? new Response(JSON.stringify({ tasks: [] }), { status: 200 })
      : url === "/api/hub/settings" ? hubSettingsWith({ tasks: true }) : null);
    vi.stubGlobal("fetch", request);
    render(<App />);
    const input = screen.getByLabelText("消息输入");
    await userEvent.type(input, "切换后继续讨论的草稿");
    const navigation = await screen.findByRole("navigation", { name: "工作区导航" });
    await userEvent.click(within(navigation).getByRole("button", { name: "任务" }));
    expect(await screen.findByRole("heading", { name: "把一件事交给团队" })).toBeVisible();
    expect(within(navigation).getByRole("button", { name: "任务" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("navigation", { name: "任务列表" })).toBeVisible();
    expect(input).not.toBeVisible();
    expect(request.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
    await userEvent.click(within(navigation).getByRole("button", { name: "会话" }));
    expect(input).toBeVisible();
    expect(input).toHaveValue("切换后继续讨论的草稿");
    expect(screen.queryByRole("navigation", { name: "任务列表" })).not.toBeInTheDocument();
    expect(window.location.hash).toBe("#conversations");
  });

  it("默认只保留核心对话：不显示任务入口，也不请求任务", async () => {
    window.history.replaceState(null, "", "/#tasks");
    const request = stubFetch(url => url === "/api/hub/settings" ? hubSettingsWith({}) : null);
    vi.stubGlobal("fetch", request);
    render(<App />);
    expect(await screen.findByLabelText("消息输入")).toBeVisible();
    await waitFor(() => expect(request.mock.calls.some(([url]) => String(url) === "/api/hub/settings")).toBe(true));
    expect(screen.queryByRole("navigation", { name: "工作区导航" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "把一件事交给团队" })).toBeNull();
    expect(request.mock.calls.some(([url]) => String(url).startsWith("/api/tasks"))).toBe(false);
  });

  it("任务入口可直接从URL打开，不需要先选择一个会话", async () => {
    window.history.replaceState(null, "", "/#tasks");
    vi.stubGlobal("fetch", stubFetch(url => url === "/api/tasks" ? new Response(JSON.stringify({ tasks: [] }), { status: 200 })
      : url === "/api/hub/settings" ? hubSettingsWith({ tasks: true }) : null));
    render(<App />);
    expect(await screen.findByRole("heading", { name: "把一件事交给团队" })).toBeVisible();
    const navigation = screen.getByRole("navigation", { name: "工作区导航" });
    expect(within(navigation).getByRole("button", { name: "任务" })).toHaveAttribute("aria-current", "page");
  });

  it("晚到的来源会话请求不能覆盖用户后来选择的会话", async () => {
    const conversations = ['a', 'b'].map(id => ({ id: `c_${id}`, title: `会话${id.toUpperCase()}`, status: 'active', created_at: '', updated_at: '', mission_count: 0 }));
    let finishOld!: (response: Response) => void;
    const old = new Promise<Response>(resolve => { finishOld = resolve; });
    const base = stubFetch();
    const detail = (id: string) => ({ ...makeConvDetail(), conversation: conversations.find(c => c.id === `c_${id}`), missions: [], latest_mission_detail: null,
      messages: [{ id: id === 'b' ? 22 : 11, mission_id: `m_${id}`, event_type: 'message', role: 'user', content: `当前会话${id.toUpperCase()}`, created_at: '2026-10-27T00:00:00Z' }] });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.startsWith('/api/conversations?')) return new Response(JSON.stringify(conversations), { status: 200 });
      if (url === '/api/conversations/c_a') return old;
      if (url === '/api/conversations/c_b') return new Response(JSON.stringify(detail('b')), { status: 200 });
      return base(input, init);
    }));
    render(<App />);
    const list = await screen.findByRole('navigation', { name: '会话列表' });
    await userEvent.click(await within(list).findByRole('button', { name: /会话A/ }));
    await userEvent.click(within(list).getByRole('button', { name: /会话B/ }));
    expect(await screen.findByText('当前会话B')).toBeVisible();
    await act(async () => { finishOld(new Response(JSON.stringify(detail('a')), { status: 200 })); });
    expect(screen.getByText('当前会话B')).toBeVisible();
    expect(screen.queryByText('当前会话A')).not.toBeInTheDocument();
  });

  it("clears a previously matching identity when the backend becomes unreachable", async () => {
    let offline = false;
    const source = __FRONTEND_IDENTITY__;
    vi.stubGlobal("fetch", stubFetch((url) => {
      if (url !== "/api/health") return null;
      if (offline) throw new Error("offline");
      return new Response(JSON.stringify({
        status: "ok", version: "0.1.0", database: "ready", config: "ready", fake_provider: "ready",
        runtime: { started: { ...source, pid: 123, python: "3.12.12", use_fake_cli: true },
          current: source, source_changed: false },
      }), { status: 200 });
    }));
    render(<App />);
    expect(await screen.findByText("启动版本一致")).toBeInTheDocument();
    offline = true;
    window.dispatchEvent(new Event("focus"));
    expect(await screen.findByText("版本信息不完整")).toBeInTheDocument();
    expect(screen.queryByText("启动版本一致")).not.toBeInTheDocument();
  });
  it("creates a mission from the composer via conversation API", async () => {
    render(<App />);
    await userEvent.type(screen.getByLabelText("消息输入"), "设计一个多 Agent 任务室");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(screen.queryByRole("region", { name: "Agent 参与状态" })).not.toBeInTheDocument();
    expect(await screen.findByText("从对话开始，和团队一起把想法变成结果。")).toBeInTheDocument();
  });

  it("keeps historical thoughts and tools after a follow-up without duplicating the live mission", async () => {
    const stamp = "2026-05-05T00:00:00Z";
    const details: MissionDetail[] = [1, 2].map((n) => ({
      mission: { id: `m${n}`, goal: "讨论", status: "completed", created_at: stamp, updated_at: stamp },
      messages: [{ id: `msg${n}`, mission_id: `m${n}`, turn_id: `turn${n}`, agent_id: "chulan", role: "agent", event_type: "message", content: `回复${n}`, created_at: stamp }],
      thoughts: [{ id: `thought${n}`, mission_id: `m${n}`, turn_id: `turn${n}`, content: `摘要${n}`, created_at: stamp }],
      tool_events: [{ id: `tool${n}`, mission_id: `m${n}`, turn_id: `turn${n}`, agent_id: "chulan", event_type: "tool.call", tool_name: "publish_message", created_at: stamp }],
    }));
    let count = 0;
    const conversation = () => makeConvDetail({
      messages: details.slice(0, count).flatMap((detail) => detail.messages ?? []),
      thoughts: details.slice(0, count).flatMap((detail) => detail.thoughts ?? []),
      tool_events: details.slice(0, count).flatMap((detail) => detail.tool_events ?? []),
      latest_mission_detail: details[count - 1],
    });
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      if (url === "/api/conversations/c_1/missions" && init?.method === "POST") {
        count += 1;
        return new Response(JSON.stringify(conversation()));
      }
      if (url === "/api/conversations/c_1" && (!init || init.method === "GET")) {
        return new Response(JSON.stringify(conversation()));
      }
      return null;
    }));
    render(<App />);
    for (const n of [1, 2]) {
      await userEvent.type(screen.getByLabelText("消息输入"), `问题${n}`);
      await userEvent.click(screen.getByRole("button", { name: "发送" }));
      expect(await screen.findByText(`回复${n}`)).toBeInTheDocument();
    }
    const toggles = screen.getAllByRole("button", { name: "展开思考过程" });
    expect(toggles).toHaveLength(2);
    for (const toggle of toggles) expect(toggle).toHaveTextContent("· 1 条");
    expect(screen.getByText("摘要1")).toBeInTheDocument();
    expect(screen.getByText("摘要2")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "含工具 · 1" })).toHaveLength(2);
  });

  it("starts the first real CLI conversation from the onboarding card", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const onboardingReady = {
      state: "ready_for_first_chat",
      guide_agent_id: "chulan",
      conversation_id: null,
      mission_id: null,
      first_reply_completed_at: null,
      completed_at: null,
      verified_provider_ids: [],
      recommended_provider_id: "codex",
      providers: [{
        id: "codex",
        label: "Codex CLI",
        available: true,
        version: "codex 9.9.9",
        verified: false,
        activated: false,
      }],
      dismissed_real_cli_setup: false,
    };
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      calls.push({ url, init });
      if (url === "/api/onboarding" && (!init || init.method === "GET")) {
        return new Response(JSON.stringify(onboardingReady), { status: 200 });
      }
      if (url === "/api/onboarding/first-chat" && init?.method === "POST") {
        return new Response(JSON.stringify({
          onboarding: {
            ...onboardingReady,
            state: "first_chat_running",
            conversation_id: "c_welcome",
            mission_id: "m_welcome",
            first_chat_provider_id: "codex",
          },
          conversation: makeConvDetail({
            conversation: {
              id: "c_welcome",
              title: "欢迎使用 Unity",
              status: "active",
              created_at: "2026-05-05T00:00:00Z",
              updated_at: "2026-05-05T00:00:00Z",
            },
            // The welcome conversation already has a turn; the guide must stay.
            messages: [{ id: "msg_welcome", mission_id: "m_welcome", event_type: "message", role: "user", content: "@张楚岚 你好，介绍一下自己", created_at: "2026-05-05T00:00:00Z" }],
            latest_mission_detail: {
              mission: {
                id: "m_welcome",
                goal: "欢迎",
                status: "running",
                created_at: "2026-05-05T00:00:00Z",
                updated_at: "2026-05-05T00:00:00Z",
              },
              messages: [],
              artifacts: [],
            },
          }),
        }), { status: 202 });
      }
      return null;
    }));

    render(<App />);

    expect(await screen.findByText("先接入一个真实 CLI")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "我已登录，用 Codex CLI 打招呼" }));

    expect(await screen.findByText("张楚岚正在通过 Codex CLI 回复…")).toBeInTheDocument();
    const start = calls.find((call) => call.url === "/api/onboarding/first-chat" && call.init?.method === "POST");
    expect(JSON.parse(String(start?.init?.body))).toEqual({ provider_id: "codex", confirmed_real_call: true });
  });

  it("renders backend MissionDetail payloads returned from mission creation", async () => {
    const detail: MissionDetail = {
      mission: {
        id: "m1",
        goal: "实现 Alpha 集成",
        status: "completed",
        created_at: "2026-04-25T00:00:00Z",
        updated_at: "2026-04-25T00:02:00Z",
        completed_at: "2026-04-25T00:02:00Z",
      },
      events: [
        {
          id: 1,
          mission_id: "m1",
          event_type: "agent.message",
          role: "agent",
          content: "已完成实现并通过验证。",
          created_at: "2026-04-25T00:02:00Z",
          metadata: {
            capability: "implementation",
            provider_label: "Codex CLI",
            persona_label: "冯宝宝",
            turn_id: "turn_1",
          },
        },
      ],
      artifacts: [],
    };
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      if (url.match(/\/api\/conversations\/[^/]+\/missions/) && init?.method === "POST") {
        return new Response(JSON.stringify(makeConvDetail({
          latest_mission_detail: detail,
          messages: detail.events ?? [],
        })), { status: 200 });
      }
      if (url.match(/\/api\/conversations\/[^/]+$/) && (!init || init.method === "GET")) {
        return new Response(JSON.stringify(makeConvDetail({
          latest_mission_detail: detail,
          messages: detail.events ?? [],
        })), { status: 200 });
      }
      return null;
    }));

    render(<App />);
    await userEvent.type(screen.getByLabelText("消息输入"), "实现 Alpha 集成");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByText("已完成实现并通过验证。")).toBeInTheDocument();
    const timeline = screen.getByRole("region", { name: "任务轮次" });
    expect(within(timeline).getByText("冯宝宝（Codex）")).toBeInTheDocument();
    expect(within(timeline).queryByText("via Codex CLI")).not.toBeInTheDocument();
    expect(BACKEND_MISSION_STATUSES).toContain("pending");
  });

  it("confirmed @mention in a running mission sends target_agent_id", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      calls.push({ url, init });
      if (url.match(/\/api\/conversations\/[^/]+\/missions/) && init?.method === "POST") {
        return new Response(JSON.stringify(makeConvDetail({
          latest_mission_detail: {
            mission: { id: "m1", goal: "先做规划", status: "running" as MissionStatus, created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:00:00Z" },
            events: [],
            artifacts: [],
          },
        })), { status: 200 });
      }
      if (url === "/api/missions/m1/interruptions") {
        return new Response(JSON.stringify({
          mission: { id: "m1", goal: "先做规划", status: "running", created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:01:00Z" },
          events: [],
          artifacts: [],
        }), { status: 200 });
      }
      return null;
    }));

    render(<App />);
    await userEvent.type(screen.getByLabelText("消息输入"), "先做规划");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByRole("button", { name: "发送补充" });
    await waitFor(() =>
      expect(screen.getByLabelText("消息输入")).toHaveValue(""),
    );
    await userEvent.type(screen.getByLabelText("消息输入"), "@王");
    await userEvent.keyboard("{Tab}");
    await flushRaf();
    await userEvent.type(screen.getByLabelText("消息输入"), "看风险");
    await userEvent.click(screen.getByRole("button", { name: "发送补充" }));

    const interruptCall = calls.find(
      (call) => call.url === "/api/missions/m1/interruptions",
    );
    expect(JSON.parse(String(interruptCall?.init?.body))).toEqual({
      content: "@王也 看风险",
      target_agent_id: "wangye",
    });
  });

  it("second idle submit calls conversation mission API, not legacy", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      calls.push({ url, init });
      if (url.match(/\/api\/conversations\/[^/]+\/missions/) && init?.method === "POST") {
        return new Response(JSON.stringify(makeConvDetail({
          latest_mission_detail: {
            mission: { id: "m1", goal: "test", status: "completed" as MissionStatus, created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:01:00Z", completed_at: "2026-05-05T00:01:00Z" },
            events: [],
            artifacts: [],
          },
        })), { status: 200 });
      }
      return null;
    }));

    render(<App />);
    await userEvent.type(screen.getByLabelText("消息输入"), "第一轮");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toHaveValue(""));
    await waitFor(() => expect(screen.getByRole("button", { name: "发送" })).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText("消息输入"), "第二轮");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));

    const convMissionCalls = calls.filter(c =>
      c.url.match(/\/api\/conversations\/[^/]+\/missions/) && c.init?.method === "POST"
    );
    expect(convMissionCalls.length).toBe(2);
    const legacyMissionCalls = calls.filter(c =>
      c.url === "/api/missions" && c.init?.method === "POST"
    );
    expect(legacyMissionCalls.length).toBe(0);
  });

  it("idle submit with confirmed mention preserves text and sends target_agent_ids", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      calls.push({ url, init });
      if (url.match(/\/api\/conversations\/[^/]+\/missions/) && init?.method === "POST") {
        return new Response(JSON.stringify(makeConvDetail({
          latest_mission_detail: {
            mission: { id: "m1", goal: "@王也 看风险", status: "completed" as MissionStatus, created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:01:00Z", completed_at: "2026-05-05T00:01:00Z" },
            events: [],
            artifacts: [],
          },
        })), { status: 200 });
      }
      return null;
    }));

    render(<App />);
    await userEvent.type(screen.getByLabelText("消息输入"), "@王");
    await userEvent.keyboard("{Tab}");
    await flushRaf();
    await userEvent.type(screen.getByLabelText("消息输入"), "看风险");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));

    const missionCall = calls.find(c =>
      c.url.match(/\/api\/conversations\/[^/]+\/missions/) && c.init?.method === "POST"
    );
    const body = JSON.parse(String(missionCall?.init?.body));
    expect(body).toEqual({ goal: "@王也 看风险", target_agent_ids: ["wangye"] });
  });

  it("confirmed mention with no body is intercepted before sending", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      calls.push({ url, init });
      if (url.match(/\/api\/conversations\/[^/]+\/missions/) && init?.method === "POST") {
        return new Response(JSON.stringify(makeConvDetail()), { status: 200 });
      }
      return null;
    }));

    render(<App />);
    await userEvent.type(screen.getByLabelText("消息输入"), "@王");
    await userEvent.keyboard("{Tab}");

    await userEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("仅 @成员 不能作为任务内容");
    expect(
      calls.some(c => c.url.match(/\/api\/conversations\/[^/]+\/missions/) && c.init?.method === "POST"),
    ).toBe(false);
  });

  it("leading space before a confirmed mention still aligns coordinates", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      calls.push({ url, init });
      if (url.match(/\/api\/conversations\/[^/]+\/missions/) && init?.method === "POST") {
        return new Response(JSON.stringify(makeConvDetail()), { status: 200 });
      }
      return null;
    }));

    render(<App />);
    await userEvent.type(screen.getByLabelText("消息输入"), " @王");
    await userEvent.keyboard("{Tab}");

    await userEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("仅 @成员 不能作为任务内容");
    expect(
      calls.some(c => c.url.match(/\/api\/conversations\/[^/]+\/missions/) && c.init?.method === "POST"),
    ).toBe(false);
  });

  it("idle submit routes fully typed @mentions without menu confirmation", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      calls.push({ url, init });
      if (url.match(/\/api\/conversations\/[^/]+\/missions/) && init?.method === "POST") {
        return new Response(JSON.stringify(makeConvDetail({
          latest_mission_detail: {
            mission: { id: "m1", goal: "依据经验复盘@张楚岚 @冯宝宝 @王也", status: "completed" as MissionStatus, created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:01:00Z", completed_at: "2026-05-05T00:01:00Z" },
            events: [],
            artifacts: [],
          },
        })), { status: 200 });
      }
      return null;
    }));

    render(<App />);
    await userEvent.type(screen.getByLabelText("消息输入"), "依据经验复盘@张楚岚 @冯宝宝 @王也");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));

    const missionCall = calls.find(c =>
      c.url.match(/\/api\/conversations\/[^/]+\/missions/) && c.init?.method === "POST"
    );
    const body = JSON.parse(String(missionCall?.init?.body));
    expect(body).toEqual({
      goal: "依据经验复盘@张楚岚 @冯宝宝 @王也",
      target_agent_ids: ["chulan", "baobao", "wangye"],
    });
  });

  it("running submit with a typed @mention routes target_agent_id", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      calls.push({ url, init });
      if (url.match(/\/api\/conversations\/[^/]+\/missions/) && init?.method === "POST") {
        return new Response(JSON.stringify(makeConvDetail({
          latest_mission_detail: {
            mission: { id: "m1", goal: "先做规划", status: "running" as MissionStatus, created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:00:00Z" },
            events: [],
            artifacts: [],
          },
        })), { status: 200 });
      }
      if (url === "/api/missions/m1/interruptions") {
        return new Response(JSON.stringify({
          mission: { id: "m1", goal: "先做规划", status: "running", created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:01:00Z" },
          events: [],
          artifacts: [],
        }), { status: 200 });
      }
      return null;
    }));

    render(<App />);
    await userEvent.type(screen.getByLabelText("消息输入"), "先做规划");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByRole("button", { name: "发送补充" });
    await waitFor(() => expect(screen.getByLabelText("消息输入")).toHaveValue(""));

    await userEvent.type(screen.getByLabelText("消息输入"), "@冯宝宝 补一刀");
    await userEvent.click(screen.getByRole("button", { name: "发送补充" }));

    const interruptCall = calls.find(c => c.url === "/api/missions/m1/interruptions");
    expect(JSON.parse(String(interruptCall?.init?.body))).toEqual({
      content: "@冯宝宝 补一刀",
      target_agent_id: "baobao",
    });
  });

  it.each([
    { draft: "", revise: true }, { draft: "尚未发送的补充", revise: true },
    { draft: "", revise: false }, { draft: "尚未发送的补充", revise: false },
  ])("stopping preserves draft '$draft'; revision is explicit: $revise", async ({ draft, revise }) => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const at = "2026-05-05T00:00:00Z";
    const userMessage = { id: "msg_u1", mission_id: "m1", event_type: "message", role: "user" as const, content: "先做规划", created_at: at };
    const blocked: MissionDetail = {
      mission: { id: "m1", goal: "先做规划", status: "blocked" as MissionStatus, created_at: at, updated_at: "2026-05-05T00:02:00Z" },
      events: [], artifacts: [], messages: [userMessage],
    };
    let paused = false;
    let created = 0;
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      calls.push({ url, init });
      if (url.match(/\/api\/conversations\/[^/]+\/missions/) && init?.method === "POST") {
        created += 1;
        return new Response(JSON.stringify(makeConvDetail({
          latest_mission_detail: created === 1
            ? { mission: { id: "m1", goal: "先做规划", status: "running" as MissionStatus, created_at: at, updated_at: at }, events: [], artifacts: [] }
            : { mission: { id: "m2", goal: "改成先做调研", status: "completed" as MissionStatus, created_at: at, updated_at: at }, events: [], artifacts: [] },
        })), { status: 200 });
      }
      if (url === "/api/missions/m1/pause" && init?.method === "POST") {
        paused = true;
        return new Response(JSON.stringify({ ...blocked, mission: { ...blocked.mission, status: "running" } }), { status: 202 });
      }
      if (url === "/api/missions/m1") {
        return new Response(JSON.stringify(paused ? blocked : { ...blocked, mission: { ...blocked.mission, status: "running" } }), { status: 200 });
      }
      if (paused && url.match(/\/api\/conversations\/[^/]+$/) && (!init || init.method === "GET")) {
        return new Response(JSON.stringify(makeConvDetail({ latest_mission_detail: blocked })), { status: 200 });
      }
      return null;
    }));

    render(<App />);
    await userEvent.type(screen.getByLabelText("消息输入"), "先做规划");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByRole("button", { name: "停止" });
    if (draft) await userEvent.type(screen.getByLabelText("消息输入"), draft);
    await userEvent.click(screen.getByRole("button", { name: "停止" }));
    await screen.findByRole("button", { name: "编辑上一条消息" });
    expect(screen.getByLabelText("消息输入")).toHaveValue(draft);
    expect(screen.queryByRole("button", { name: "取消修改" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新发送" })).not.toBeInTheDocument();

    if (revise) {
      await userEvent.click(screen.getByRole("button", { name: "编辑上一条消息" }));
      expect(screen.getByLabelText("消息输入")).toHaveValue("先做规划");
      await userEvent.click(screen.getByRole("button", { name: "取消修改" }));
      expect(screen.getByLabelText("消息输入")).toHaveValue(draft);
      await userEvent.click(screen.getByRole("button", { name: "编辑上一条消息" }));
      await userEvent.clear(screen.getByLabelText("消息输入"));
      await userEvent.type(screen.getByLabelText("消息输入"), "改成先做调研");
      await userEvent.click(screen.getByRole("button", { name: "重新发送" }));
    } else {
      if (!draft) await userEvent.type(screen.getByLabelText("消息输入"), "继续");
      await userEvent.click(screen.getByRole("button", { name: "发送" }));
    }
    const posts = calls.filter(c => c.url.match(/\/api\/conversations\/[^/]+\/missions/) && c.init?.method === "POST");
    expect(JSON.parse(String(posts[1]?.init?.body))).toEqual(revise
      ? { goal: "改成先做调研", revises_message_id: "msg_u1" }
      : { goal: draft || "继续" });
    await waitFor(() => expect(screen.queryByRole("button", { name: "取消修改" })).not.toBeInTheDocument());
  });

  it("clicking new conversation creates and selects an empty conversation", async () => {
    render(<App />);
    await userEvent.click(screen.getByRole("button", { name: "新对话" }));
    await waitFor(() => {
      expect(screen.getByRole("navigation", { name: "会话列表" })).toBeInTheDocument();
    });
  });

  it("renders discussion substance in the conversation and updates it from SSE without a reload", async () => {
    const stamp = "2026-05-05T00:00:00Z";
    const initial: MissionDetail = {
      mission: { id: "m1", goal: "如何判断工具错误已经恢复？", status: "running", created_at: stamp, updated_at: stamp },
      discussion: { protocol: "deliberation_v2", phase: "independent", participant_ids: ["chulan", "wangye"],
        turns_used: 2, turns_limit: 9, revision: 1, results: [] }, events: [], artifacts: [],
    };
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      if (url === "/api/health") return new Response(JSON.stringify({ status: "ok", project_skills: ["discuss"], discussion_protocols: ["deliberation_v2"] }));
      if (url === "/api/conversations/c_1/missions" && init?.method === "POST") {
        return new Response(JSON.stringify(makeConvDetail({ latest_mission_detail: initial })));
      }
      return null;
    }));
    render(<App />);
    await userEvent.type(screen.getByLabelText("消息输入"), "/discuss 如何判断工具错误已经恢复？");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByRole("region", { name: "讨论重点" })).toHaveTextContent("如何判断工具错误已经恢复？");
    const source = (window.EventSource as unknown as { instances: EventTarget[] }).instances[0];
    expect(source).toBeDefined();
    source.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify({ ...initial,
      mission: { ...initial.mission, updated_at: "2026-05-05T00:01:00Z" },
      discussion: { ...initial.discussion, phase: "exchange", revision: 2, results: [
        { id: "opinion", agent_id: "wangye", phase: "independent", kind: "opinion", data: { position_summary: "应区分求助已发出与故障已恢复。" } },
      ], issues: [{ id: "issue", problem: "缺少恢复成功的真实证据。", impact: "影响交付判断。", category: "fact", status: "open" }] },
    }) }));
    expect(await screen.findByText("缺少恢复成功的真实证据。")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("核对分歧与依据");
    await userEvent.click(screen.getByText("成员观点 · 1 位"));
    expect(screen.getByText("应区分求助已发出与故障已恢复。")).toBeVisible();
  });

  it.each(["completed", "blocked", "completed_with_warnings"])(
    "applies SSE snapshots monotonically and closes streams for %s missions",
    async (terminalStatus) => {
    vi.stubGlobal("fetch", stubFetch((url, init) => {
      if (url.match(/\/api\/conversations\/[^/]+\/missions/) && init?.method === "POST") {
        return new Response(JSON.stringify(makeConvDetail({
          latest_mission_detail: {
            mission: { id: "m1", goal: "先做规划", status: "running" as MissionStatus, created_at: "2026-05-05T00:00:00Z", updated_at: "2026-05-05T00:00:00Z" },
            events: [],
            artifacts: [],
          },
        })), { status: 200 });
      }
      return null;
    }));

    render(<App />);
    await userEvent.type(screen.getByLabelText("消息输入"), "先做规划");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByRole("button", { name: "发送补充" });

    const EventSourceMock = window.EventSource as unknown as {
      instances: Array<EventTarget & { close: ReturnType<typeof vi.fn> }>;
    };
    const source = EventSourceMock.instances[0];
    expect(source).toBeDefined();

    source.dispatchEvent(new MessageEvent("snapshot", { data: "not-json" }));
    expect(await screen.findByText("实时流：重连中")).toBeInTheDocument();

    source.dispatchEvent(
      new MessageEvent("snapshot", {
        data: JSON.stringify({
          mission: {
            id: "m1",
            goal: "先做规划",
            status: "running",
            created_at: "2026-05-05T00:00:00Z",
            updated_at: "2026-05-04T23:59:00Z",
          },
          events: [
            {
              id: 1,
              mission_id: "m1",
              event_type: "agent.message",
              role: "agent",
              content: "旧输出",
              created_at: "2026-05-04T23:59:00Z",
            },
          ],
          artifacts: [],
        }),
      }),
    );
    await waitFor(() => expect(screen.queryByText("旧输出")).not.toBeInTheDocument());

    source.dispatchEvent(
      new MessageEvent("snapshot", {
        data: JSON.stringify({
          mission: {
            id: "m1",
            goal: "先做规划",
            status: terminalStatus,
            created_at: "2026-05-05T00:00:00Z",
            updated_at: "2026-05-05T00:02:00Z",
          },
          events: [
            {
              id: 2,
              mission_id: "m1",
              event_type: "agent.message",
              role: "agent",
              content: "新输出",
              created_at: "2026-05-05T00:02:00Z",
              metadata: {
                capability: "review",
                provider_label: "Claude Code",
                persona_label: "王也",
              },
            },
          ],
          artifacts: [],
        }),
      }),
    );

    expect(await screen.findByText("新输出")).toBeInTheDocument();
    const sseTimeline = screen.getByRole("region", { name: "任务轮次" });
    expect(within(sseTimeline).getByText("第 1 轮")).toBeInTheDocument();
    expect(within(sseTimeline).getByText("王也（Claude）")).toBeInTheDocument();
    expect(within(sseTimeline).queryByText("via Claude Code")).not.toBeInTheDocument();
    await waitFor(() => expect(source.close).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "发送" })).toBeInTheDocument();
    },
  );
});
