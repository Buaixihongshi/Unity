import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { OnboardingCard, type OnboardingCardProps } from "../components/OnboardingCard";

const health = {
  status: "ok" as const,
  version: "0.1.0",
  database: "ready" as const,
  config: "ready" as const,
  fake_provider: "ready" as const,
};

function props(overrides: Partial<OnboardingCardProps> = {}): OnboardingCardProps {
  return {
    health,
    healthLoading: false,
    healthError: null,
    onboarding: {
      state: "ready_for_first_chat",
      guide_agent_id: "chulan",
      conversation_id: null,
      mission_id: null,
      first_reply_completed_at: null,
      completed_at: null,
      verified_provider_ids: [],
      providers: [],
      dismissed_real_cli_setup: false,
    },
    actionBusy: false,
    actionError: null,
    onStart: vi.fn(),
    onRetryHealth: vi.fn(),
    onComplete: vi.fn(),
    onSkipRealCli: vi.fn(),
    onUseProviderForTeam: vi.fn(),
    onRefreshProviders: vi.fn(),
    onVerifyProvider: vi.fn(),
    onActivateProvider: vi.fn(),
    ...overrides,
  };
}

const codex = {
  id: "codex",
  label: "Codex CLI",
  available: true,
  version: "Codex CLI 9.9.9",
  verified: false,
  activated: false,
  install_command: "curl codex | sh",
  install_url: "https://learn.chatgpt.com/docs/codex/cli",
  auth_command: "codex login",
};
const claude = {
  id: "claude",
  label: "Claude Code",
  available: false,
  verified: false,
  activated: false,
  install_command: "curl claude | bash",
  install_url: "https://code.claude.com/docs/en/quickstart",
  auth_command: "claude auth login",
};

describe("OnboardingCard", () => {
  it("starts the first chat with a detected real CLI instead of a demo", async () => {
    const onStart = vi.fn();
    const base = props({ onStart });
    render(
      <OnboardingCard
        {...base}
        onboarding={{
          ...base.onboarding!,
          recommended_provider_id: "codex",
          providers: [codex, claude],
        }}
      />,
    );

    expect(screen.getByText("先接入一个真实 CLI")).toBeInTheDocument();
    expect(screen.getByText("Codex CLI 9.9.9")).toBeInTheDocument();
    expect(screen.getByText("推荐")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("可能计入套餐额度或产生费用");
    expect(screen.queryByText(/演示/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "我已登录，用 Codex CLI 打招呼" }));

    expect(onStart).toHaveBeenCalledWith("codex");
    expect(screen.queryByRole("button", { name: /用 Claude Code 打招呼/ })).not.toBeInTheDocument();
  });

  it("shows install guidance when no CLI is installed", async () => {
    const onRefreshProviders = vi.fn();
    const onSkipRealCli = vi.fn();
    const base = props({ onRefreshProviders, onSkipRealCli });
    render(
      <OnboardingCard
        {...base}
        onboarding={{
          ...base.onboarding!,
          recommended_provider_id: "codex",
          providers: [{ ...codex, available: false, version: null }, claude],
        }}
      />,
    );

    expect(screen.getByText("还没有检测到可用的 CLI")).toBeInTheDocument();
    expect(screen.getByText("curl codex | sh")).toBeInTheDocument();
    expect(screen.getByText("codex login")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "官方安装说明" })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /打招呼/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/演示/)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "重新检测" }));
    expect(onRefreshProviders).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByRole("button", { name: "稍后再接入" }));
    expect(onSkipRealCli).toHaveBeenCalledOnce();
  });

  it("explains a failed first chat and lets the user retry", async () => {
    const onStart = vi.fn();
    const base = props({ onStart });
    render(
      <OnboardingCard
        {...base}
        onboarding={{
          ...base.onboarding!,
          state: "first_chat_failed",
          first_chat_provider_id: "codex",
          first_chat_error: "Codex authentication required",
          recommended_provider_id: "codex",
          providers: [codex],
        }}
      />,
    );

    expect(screen.getByText("Codex CLI 没有成功回复")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Codex authentication required");
    await userEvent.click(screen.getByRole("button", { name: "我已登录，用 Codex CLI 打招呼" }));
    expect(onStart).toHaveBeenCalledWith("codex");
  });

  it("does not offer real onboarding calls in an offline demo environment", () => {
    const base = props();
    render(
      <OnboardingCard
        {...base}
        onboarding={{ ...base.onboarding!, real_cli_blocked: true, providers: [codex] }}
      />,
    );

    expect(screen.getByText("这个环境不会调用真实 CLI")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /打招呼/ })).not.toBeInTheDocument();
  });

  it("moves the team to the CLI that answered the first chat", async () => {
    const onActivateProvider = vi.fn();
    const onUseProviderForTeam = vi.fn();
    const base = props({ onActivateProvider, onUseProviderForTeam });
    render(
      <OnboardingCard
        {...base}
        onboarding={{
          ...base.onboarding!,
          state: "first_chat_succeeded",
          first_chat_provider_id: "codex",
          verified_provider_ids: ["codex"],
          providers: [{ ...codex, verified: true }],
        }}
      />,
    );

    expect(screen.getByText("张楚岚已经完成第一次真实对话")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("2 次真实模型请求");
    await userEvent.click(screen.getByRole("button", { name: "让三位角色使用 Codex CLI 并认识团队" }));
    expect(onActivateProvider).toHaveBeenCalledWith("codex");
    await userEvent.click(screen.getByRole("button", { name: "只配置角色，跳过介绍" }));
    expect(onUseProviderForTeam).toHaveBeenCalledWith("codex");
  });

  it("keeps onboarding open until baobao and wangye have introduced themselves", async () => {
    const onComplete = vi.fn();
    const onOpenTeamSettings = vi.fn();
    const onVerifyProvider = vi.fn();
    const base = props();
    const { rerender } = render(
      <OnboardingCard
        {...base}
        onboarding={{
          ...base.onboarding!,
          state: "team_intro_running",
          activated_provider_id: "codex",
          team_intro_mission_id: "m_intro",
          providers: [{ ...codex, verified: true, activated: true }],
        }}
        onComplete={onComplete}
        onOpenTeamSettings={onOpenTeamSettings}
        onVerifyProvider={onVerifyProvider}
      />,
    );

    expect(screen.getByText("宝儿姐和王也正在进场…")).toBeInTheDocument();
    expect(screen.getByText(/通过 Codex CLI 各用一句话介绍自己/)).toBeInTheDocument();
    expect(screen.queryByText(/演示/)).not.toBeInTheDocument();

    rerender(
      <OnboardingCard
        {...base}
        onboarding={{
          ...base.onboarding!,
          state: "team_intro_completed",
          activated_provider_id: "codex",
          team_intro_mission_id: "m_intro",
          team_intro_completed_at: "2026-07-30T00:00:00Z",
          providers: [{ ...codex, verified: true, activated: true }, claude, {
            id: "antigravity",
            label: "Antigravity CLI",
            available: true,
            version: "1.1.5",
            verified: false,
            activated: false,
            install_command: "curl agy | bash",
            install_url: "https://antigravity.google/docs/cli/install",
            auth_command: "agy",
          }],
        }}
        onComplete={onComplete}
        onOpenTeamSettings={onOpenTeamSettings}
        onVerifyProvider={onVerifyProvider}
      />,
    );

    expect(screen.getByText("三位角色都到齐了")).toBeInTheDocument();
    expect(screen.getByText("冯宝宝 · 坦率好奇 · Codex CLI")).toBeInTheDocument();
    expect(screen.getByText("王也 · 松弛笃定 · Codex CLI")).toBeInTheDocument();
    expect(screen.getByText("可选扩展")).toBeInTheDocument();
    expect(screen.getByText("Claude Code")).toBeInTheDocument();
    expect(screen.getByText("Antigravity CLI")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("每次真实 CLI 验证都会使用对应账号");
    expect(screen.getByRole("link", { name: "官方安装说明" })).toHaveAttribute(
      "href",
      "https://code.claude.com/docs/en/quickstart",
    );
    await userEvent.click(screen.getByRole("button", { name: "我已登录，验证 Antigravity CLI" }));
    expect(onVerifyProvider).toHaveBeenCalledWith("antigravity");
    expect(onComplete).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "打开团队配置" }));
    expect(onOpenTeamSettings).toHaveBeenCalledOnce();

    await userEvent.click(screen.getByRole("button", { name: "开始聊天" }));
    expect(onComplete).toHaveBeenCalledOnce();
  });

  it("returns an additionally verified CLI to persistent team settings", async () => {
    const onOpenTeamSettings = vi.fn();
    const base = props();
    render(
      <OnboardingCard
        {...base}
        onboarding={{
          ...base.onboarding!,
          state: "provider_verified",
          activated_provider_id: "codex",
          verification_provider_id: "claude",
          verified_provider_ids: ["codex", "claude"],
          providers: [{
            id: "claude",
            label: "Claude Code",
            available: true,
            verified: true,
            activated: false,
          }],
        }}
        onOpenTeamSettings={onOpenTeamSettings}
      />,
    );

    expect(screen.getByText("Claude Code 已经能够通过 Unity 发言")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "完成并打开团队配置" }));
    expect(onOpenTeamSettings).toHaveBeenCalledOnce();
  });

  it("offers a retry when the backend is unavailable", async () => {
    const onRetryHealth = vi.fn();
    render(
      <OnboardingCard
        {...props({
          health: null,
          healthError: "Unity 后端尚未就绪",
          onRetryHealth,
        })}
      />,
    );

    expect(screen.getByText("还没有连接到 Unity 后端")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重新连接" }));

    expect(onRetryHealth).toHaveBeenCalledOnce();
  });

  it("renders the mobius animation waiting state while health is loading", () => {
    const { container } = render(
      <OnboardingCard
        {...props({
          health: null,
          healthLoading: true,
          healthError: null,
        })}
      />,
    );

    expect(screen.getByText("正在确认 Unity 状态…")).toBeInTheDocument();
    expect(screen.getByText("马上就好，我们在检查聊天室和本机可用的 CLI。")).toBeInTheDocument();
    expect(container.querySelector(".onboarding-mobius-wrap")).toBeInTheDocument();
    expect(container.querySelector(".mobius-track-loop")).toBeInTheDocument();
  });
});
