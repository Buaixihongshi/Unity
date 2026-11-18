import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ObservabilityOverview } from "../components/ObservabilityOverview";
import { fetchObservabilityOverview, fetchObservabilityTurn } from "../lib/api";
import type { ObservabilityOverviewPayload, ObservabilityTurnDetailPayload } from "../types";

vi.mock("../lib/api", () => ({
  fetchObservabilityOverview: vi.fn(),
  fetchObservabilityTurn: vi.fn(),
}));

const mockedFetch = vi.mocked(fetchObservabilityOverview);
const mockedTurn = vi.mocked(fetchObservabilityTurn);
const STAMP = "2026-10-20T10:00:00+00:00";

function turnDetail(): ObservabilityTurnDetailPayload {
  return {
    mission: { id: "m_1", title: "测试任务" },
    turn: {
      turn_id: "turn_1", mission_id: "m_1", mission_title: "测试任务", index: 1,
      agent_id: "chulan", agent_name: "张楚岚", provider_id: "codex", model: "", status: "completed",
      purpose: "", started_at: STAMP, ended_at: STAMP, duration_ms: 31_000,
      reply: "做完了。", error: "", tokens: 183_600, usage: null,
      cost_usd: 0.27, cost_source: "provider",
      checks: [
        { name: "claims_match_events", source: "code", value: null, note: "无声明" },
        { name: "tool_errors_handled", source: "code", value: 1, note: "已处理" },
      ],
      coverage: { has_reply: true, native_visible: false, tool_calls: 1, tool_outputs: 1, cost_known: true },
      steps: [{
        id: "1", kind: "native", tool: "Bash", ts: STAMP, call_id: "call_1", ok: true, error: "",
        args: { command: "ls" }, output: "a.py\nb.py", summary: "Bash · ls", duration_ms: 12,
        phase: "completed", agent_id: "chulan", provider_id: "codex", source: "provider",
      }],
    },
  };
}

function payload(): ObservabilityOverviewPayload {
  return {
    generated_at: STAMP,
    window: "all",
    totals: {
      conversations: 61, missions: 150, turns: 342, tool_calls: 919,
      native_calls: 91, tokens: 43_712_959, cost: 74.31,
    },
    checks: [
      { name: "claims_match_events", what: "声明不矛盾", pass: 7, fail: 0, na: 335, rate: 1 },
      { name: "tool_errors_handled", what: "失败被处理", pass: 11, fail: 8, na: 323, rate: 0.579 },
      { name: "blocked_explained", what: "阻塞有原因", pass: 31, fail: 0, na: 311, rate: 1 },
    ],
    turns_passed: 40,
    coverage: { turns: 342, native_calls: 91, native_rate: 0.061, tool_calls: 919, output_rate: 0.111, cost_known_rate: 0.693 },
    spend: {
      total: 74.31, reported: 39.45, estimated: 34.86, unknown_turns: 105,
      by_provider: [{ provider: "codex", usd: 33.09, turns: 138, unknown: 18, tokens: 21_800_000 }],
    },
    by_agent: [
      { agent: "chulan", turns: 160, tokens: 25_000_000, cost: 43.18, tools: 373, native: 40 },
      { agent: "baobao", turns: 99, tokens: 16_200_000, cost: 28.91, tools: 255, native: 30 },
    ],
    by_provider: [{ provider: "codex", usd: 33.09, turns: 138, unknown: 18, tokens: 21_800_000 }],
    daily: Array.from({ length: 14 }, (_, index) => ({
      date: `2026-10-${String(index + 7).padStart(2, "0")}`, turns: index + 1, cost: 1,
    })),
    recent_turns: [{
      turn_id: "turn_1", mission_id: "m_1", mission: "测试任务", agent: "chulan",
      agent_name: "张楚岚", provider: "codex", model: "", status: "completed",
      duration_ms: 31_000, tokens: 183_600, cost: 0.27,
      checks: [
        { name: "claims_match_events", source: "code", value: null, note: "无声明" },
        { name: "tool_errors_handled", source: "code", value: 1, note: "已处理" },
      ],
      at: STAMP,
    }],
    failures: [{
      check: "tool_errors_handled", note: "未重试也未提及：invoke_agent",
      agent: "wangye", turn_id: "turn_2", mission: "失败任务", at: STAMP,
    }],
  };
}

describe("ObservabilityOverview", () => {
  beforeEach(() => mockedFetch.mockReset());

  it("renders the global KPI, checks and breakdowns", async () => {
    mockedFetch.mockResolvedValue(payload());
    render(<ObservabilityOverview />);
    expect(await screen.findByText(/61 个会话 \/ 150 个任务 \/ 342 个回合/)).toBeInTheDocument();
    expect(screen.getByText("$74.31")).toBeInTheDocument();
    expect(screen.getByTitle("claims_match_events")).toBeInTheDocument();
    expect(screen.getByText("声明一致")).toBeInTheDocument();
    expect(screen.getAllByText("chulan").length).toBeGreaterThan(0);
    expect(screen.getAllByText("codex").length).toBeGreaterThan(0);
    expect(screen.getByText(/未重试也未提及/)).toBeInTheDocument();
  });

  it("drills from the overview into a single turn and back", async () => {
    mockedFetch.mockResolvedValue(payload());
    mockedTurn.mockResolvedValue(turnDetail());
    render(<ObservabilityOverview />);
    await screen.findByText(/61 个会话 \/ 150 个任务 \/ 342 个回合/);

    fireEvent.click(screen.getByRole("button", { name: /全部回合/ }));
    const table = screen.getByText("最近回合").closest(".obsv-panel") as HTMLElement;
    expect(within(table).getByText("183.6k")).toBeInTheDocument();

    fireEvent.click(within(table).getByRole("button", { name: /查看 chulan 的回合细节/ }));
    expect(await screen.findByText(/a\.py/)).toBeInTheDocument();
    expect(screen.getByText(/command/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    expect(await screen.findByText("最近回合")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回总览" }));
    expect(await screen.findByText("Trace 覆盖")).toBeInTheDocument();
  });

  it("opens a turn from the failure list", async () => {
    mockedFetch.mockResolvedValue(payload());
    mockedTurn.mockResolvedValue(turnDetail());
    render(<ObservabilityOverview />);
    await screen.findByText(/61 个会话 \/ 150 个任务 \/ 342 个回合/);

    fireEvent.click(screen.getByRole("button", { name: /tool_errors_handled · wangye/ }));
    await waitFor(() => expect(mockedTurn).toHaveBeenCalledWith("turn_2"));
    expect(await screen.findByText(/a\.py/)).toBeInTheDocument();
  });

  it("groups checks by layer and keeps environment failures off the agent", async () => {
    const data = payload();
    data.checks = [
      { name: "turn_closed", layer: "trace", health: null, what: "有终态", pass: 300, fail: 2, na: 40, rate: 0.993 },
      { name: "claims_match_events", layer: "agent", health: "31 次评判从未失败，可能没有判别力",
        what: "声明不矛盾", pass: 31, fail: 0, na: 311, rate: 1 },
    ];
    data.by_agent = [{ agent: "chulan", turns: 160, tokens: 1, cost: 1, tools: 3, native: 0,
      blocked: 1, blocked_environment: 4, blocked_user: 2 }];
    data.failures = [{ check: "turn_closed", layer: "trace", note: "任务已 completed，回合却没有终态事件",
      agent: "chulan", turn_id: "turn_3", mission: "任务", at: STAMP }];
    mockedFetch.mockResolvedValue(data);
    render(<ObservabilityOverview />);

    const trace = await screen.findByRole("region", { name: "Trace 可信度" });
    expect(within(trace).getByText("回合终态")).toBeInTheDocument();
    const agent = screen.getByRole("region", { name: "Agent 行为" });
    expect(within(agent).getByText("声明一致")).toBeInTheDocument();
    expect(within(agent).getByText(/从未失败，可能没有判别力/)).toBeInTheDocument();
    expect(screen.getByText("4/2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /trace\s*turn_closed · chulan/ })).toBeInTheDocument();
  });

  it("requests the selected window", async () => {
    mockedFetch.mockResolvedValue(payload());
    render(<ObservabilityOverview />);
    await screen.findByText(/61 个会话 \/ 150 个任务 \/ 342 个回合/);

    fireEvent.click(screen.getByRole("button", { name: "7 天" }));
    await waitFor(() => expect(mockedFetch).toHaveBeenLastCalledWith("7d"));
  });

  it("shows a retry when loading fails", async () => {
    mockedFetch.mockRejectedValueOnce(new Error("加载观测数据失败")).mockResolvedValueOnce(payload());
    render(<ObservabilityOverview />);

    expect(await screen.findByRole("alert")).toHaveTextContent("加载观测数据失败");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(2));
  });
});
