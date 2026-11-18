import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge } from "../components/StatusBadge";
import type { TurnView } from "../types";

function makeStatusTurn(content: string): TurnView {
  return {
    id: "s1",
    turnIndex: 0,
    role: "system",
    eventType: "status",
    content,
    capability: "unknown",
    providerLabel: "",
    createdAt: "2026-04-25T00:00:00Z",
    metadata: {},
    isStatusEvent: true,
  };
}

describe("StatusBadge", () => {
  it("renders content as centered inline text", () => {
    const { container } = render(
      <StatusBadge turn={makeStatusTurn("Mission started")} />,
    );
    expect(screen.getByText("Mission started")).toBeInTheDocument();
    expect(container.querySelector(".status-badge")).toBeTruthy();
  });

  it("has an accessible label", () => {
    render(<StatusBadge turn={makeStatusTurn("running")} />);
    expect(screen.getByLabelText("状态：running")).toBeInTheDocument();
  });

  it("renders a dot element", () => {
    const { container } = render(
      <StatusBadge turn={makeStatusTurn("completed")} />,
    );
    expect(container.querySelector(".status-badge-dot")).toBeTruthy();
  });

  it("labels a timed-out turn's host progress as a host record, not a role message", () => {
    const turn: TurnView = {
      ...makeStatusTurn("王也本轮超时，未发布回复。"),
      eventType: "turn.failure",
      metadata: {
        host_progress: {
          source: "host",
          reason: "idle_timeout",
          tool_calls: 4,
          tools: [{ name: "Bash", count: 2 }, { name: "Read", count: 1 }],
          artifacts: ["demo/index.html"],
          published: false,
          last_activity: { kind: "provider.activity", label: "Read", at: null, seconds_before_stop: 91 },
        },
      },
    };
    render(<StatusBadge turn={turn} />);
    const note = screen.getByLabelText("宿主进度记录");
    expect(note).toHaveTextContent("宿主记录 · 非角色发言");
    expect(note).toHaveTextContent("接力回合长时间没有新活动");
    expect(note).toHaveTextContent("4 次：Bash ×2、Read ×1");
    expect(note).toHaveTextContent("demo/index.html");
    expect(note).toHaveTextContent("Read · 停止前 91 秒");
  });

  it("keeps the plain failure notice when no host progress was recorded", () => {
    render(<StatusBadge turn={{ ...makeStatusTurn("王也本轮未完成，未发布回复。"), eventType: "turn.failure" }} />);
    expect(screen.getByText("王也本轮未完成，未发布回复。")).toBeInTheDocument();
    expect(screen.queryByLabelText("宿主进度记录")).toBeNull();
  });
});

describe("non-timeout failure reasons", () => {
  it.each([
    ["usage_limit", "账号用量上限", "You've hit your 5-hour limit · resets 17:25:52"],
    ["rate_limit", "请求受到限流", "HTTP 429: retry later"],
    ["authentication", "身份验证失败", "not logged in"],
  ])("shows %s and its host error without requiring tool activity", (reason, label, error) => {
    render(<StatusBadge turn={{
      ...makeStatusTurn("张楚岚本轮未完成，未发布回复。"),
      eventType: "turn.failure",
      metadata: { host_progress: {
        source: "host", reason, error, tool_calls: 0, tools: [], artifacts: [], published: false,
      } },
    }} />);
    const note = screen.getByLabelText("宿主进度记录");
    expect(note).toHaveTextContent(label);
    expect(note).toHaveTextContent("错误摘要");
    expect(note).toHaveTextContent(error);
    expect(note).toHaveTextContent("未记录到活动");
    if (reason !== "authentication") {
      expect(note).toHaveTextContent("也可检查角色路由");
      expect(note).toHaveTextContent("重置提示以上游原文为准");
    }
    expect(note).not.toHaveTextContent("Asia/Shanghai");
    expect(note).not.toHaveTextContent("执行超时");
  });

  it("keeps a published reply notice and timeout explanation with an error summary", () => {
    render(<StatusBadge turn={{
      ...makeStatusTurn("王也本轮超时，已发布的回复已保留。"), eventType: "turn.failure",
      metadata: { host_progress: {
        source: "host", reason: "hard_timeout", error: "timeout after 600s",
        tool_calls: 1, tools: [{ name: "Read", count: 1 }], artifacts: ["result.md"], published: true,
      } },
    }} />);
    expect(screen.getByLabelText("宿主进度记录")).toHaveTextContent("单轮总时长用尽");
    expect(screen.getByText("timeout after 600s")).toBeInTheDocument();
    expect(screen.getByText("王也本轮超时，已发布的回复已保留。")).toBeInTheDocument();
    expect(screen.getByText("result.md")).toBeInTheDocument();
  });
});
