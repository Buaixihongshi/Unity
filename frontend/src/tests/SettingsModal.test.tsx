import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SettingsModal } from "../components/SettingsModal";

vi.mock("../lib/api", () => ({
  fetchMemoryOverview: vi.fn().mockResolvedValue({
    memory_root: "/state/data/memory",
    consistency: { files: 0, indexed: 0, stale: 0, missing_files: 0, applied: false, active_by_agent: {} },
    agents: [],
  }),
  fetchAgentMemory: vi.fn().mockResolvedValue({ entries: [] }),
  fetchMemoryCheckpoints: vi.fn().mockResolvedValue({ checkpoints: [] }),
  fetchMemoryHandovers: vi.fn().mockResolvedValue({ handovers: [] }),
  clearMemoryCheckpoints: vi.fn().mockResolvedValue({ removed: 0 }),
  updateMemoryEntry: vi.fn(),
  setMemoryEntryStatus: vi.fn(),
  purgeMemoryEntry: vi.fn(),
  rebuildMemoryIndex: vi.fn(),
  fetchConnectionModels: vi.fn().mockResolvedValue({
    connection_id: "api-openai-1", status: "error", models: [], fetched_at: null, error: "Catalog has not been fetched",
  }),
  refreshConnectionModels: vi.fn(),
  fetchObservabilityOverview: vi.fn().mockResolvedValue({
    generated_at: "2026-10-20T10:00:00+00:00",
    window: "all",
    totals: { conversations: 0, missions: 0, turns: 0, tool_calls: 0, native_calls: 0, tokens: 0, cost: 0 },
    checks: [],
    turns_passed: 0,
    coverage: { turns: 0, native_calls: 0, native_rate: null, tool_calls: 0, output_rate: null, cost_known_rate: null },
    spend: { total: 0, reported: 0, estimated: 0, unknown_turns: 0, by_provider: [] },
    by_agent: [],
    by_provider: [],
    daily: [],
    recent_turns: [],
    failures: [],
  }),
}));
import type { HubSettingsView, HubSettingsPatch } from "../types";
import { fetchConnectionModels } from "../lib/api";

const baseView: HubSettingsView = {
  runtime: {
    use_fake_cli: { value: true, source: "default" },
    max_turns: { value: 6, source: "default" },
    max_a2a_invocations: { value: 8, source: "default" },
    cli_timeout: { value: 120, source: "default" },
    a2a_idle_timeout: { value: 90, source: "default" },
  },
  providers: [
    { id: "fake", label: "Fake", enabled: { value: true, source: "committed" } },
    { id: "codex", label: "Codex CLI", enabled: { value: true, source: "user" } },
    { id: "claude", label: "Claude", enabled: { value: false, source: "committed" } },
    { id: "gemini", label: "Gemini", enabled: { value: false, source: "committed" } },
    { id: "antigravity", label: "Antigravity", enabled: { value: false, source: "committed" } },
  ],
  agents: [
    {
      id: "wangye",
      display_name: "王也",
      default_provider_id: { value: "antigravity", source: "committed" },
      fallback_provider_ids: { value: ["claude"], source: "committed" },
      base_default_provider_id: "antigravity",
      base_fallback_provider_ids: ["claude"],
    },
  ],
};

describe("SettingsModal", () => {
  it("opens the global observability tab without a save bar", async () => {
    render(<SettingsModal view={baseView} onSave={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "观测" }));
    expect(await screen.findByRole("heading", { name: "观测" })).toBeInTheDocument();
    expect(screen.getByText(/全部会话/)).toBeInTheDocument();
    expect(screen.queryByTestId("settings-save")).not.toBeInTheDocument();
  });

  it("opens the memory tab inside settings without a save bar", async () => {
    render(<SettingsModal view={baseView} onSave={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "记忆" }));
    expect(await screen.findByLabelText("记忆管理")).toBeInTheDocument();
    expect(screen.getByText(/长期记忆以 Markdown 文件为准/)).toBeInTheDocument();
    expect(screen.queryByTestId("settings-save")).not.toBeInTheDocument();
  });

  it("hides internal and retired providers from the provider list", () => {
    render(<SettingsModal view={baseView} onSave={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getAllByText("Claude").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("Fake")).not.toBeInTheDocument();
    expect(screen.queryByText("Gemini")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "团队配置" }));
    expect(screen.queryByText(/演示模式/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /王也 备用连接/ }));
    expect(screen.queryByTestId("agent-fallback-wangye-fake")).not.toBeInTheDocument();
    // The demo runtime switch is only offered while demo mode is on, so it can be turned off.
    fireEvent.click(screen.getByRole("button", { name: "运行参数" }));
    expect(screen.getByText("演示模式（离线，不调用模型）")).toBeInTheDocument();
  });

  it("keeps the demo provider visible only for roles still routed to it", () => {
    const demoView: HubSettingsView = {
      ...baseView,
      runtime: { ...baseView.runtime, use_fake_cli: { value: false, source: "user" } },
      connections: [
        { id: "fake", name: "演示模式", kind: "cli", protocol: null, base_url: null, enabled: true, has_key: false, timeout: 120, allow_private: true },
        { id: "codex", name: "Codex CLI", kind: "cli", protocol: null, base_url: null, enabled: true, has_key: false, timeout: 120, allow_private: true },
      ],
      agents: [{ ...baseView.agents[0], default_provider_id: { value: "fake", source: "user" } }],
    };
    render(<SettingsModal view={demoView} onSave={vi.fn()} onClose={vi.fn()} />);
    expect(screen.queryByTestId("provider-card-fake")).not.toBeInTheDocument();
    expect(screen.getByTestId("agent-default-wangye")).toHaveValue("fake");
    expect(screen.getByRole("option", { name: "演示模式（离线）" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "运行参数" }));
    expect(screen.queryByText(/演示模式/)).not.toBeInTheDocument();
  });

  it("shows every role in one table with its connection, model and fallbacks", () => {
    const view: HubSettingsView = {
      ...baseView,
      agents: [
        baseView.agents[0],
        { ...baseView.agents[0], id: "baobao", display_name: "冯宝宝", default_provider_id: { value: "codex", source: "committed" }, fallback_provider_ids: { value: [], source: "committed" }, provider_models: { value: { codex: "gpt-6.1-sol" }, source: "user" } },
      ],
    };
    render(<SettingsModal view={view} onSave={vi.fn()} onClose={vi.fn()} />);
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByRole("rowheader", { name: /王也/ })).toBeInTheDocument();
    expect(screen.getByLabelText("冯宝宝 codex 模型")).toHaveValue("gpt-6.1-sol");
    expect(screen.getByRole("button", { name: "冯宝宝 备用连接：无" })).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/安全规范审计/)).not.toBeInTheDocument();
  });

  it("toggling a provider builds correct patch", () => {
    const onSave = vi.fn();
    render(<SettingsModal view={baseView} onSave={onSave} onClose={vi.fn()} />);
    const claudeToggle = screen.getByTestId("toggle-claude");
    fireEvent.click(claudeToggle);
    const saveBtn = screen.getByTestId("settings-save");
    fireEvent.click(saveBtn);
    const patch: HubSettingsPatch = onSave.mock.calls[0][0];
    expect(patch.providers).toBeDefined();
    expect(patch.providers!["claude"]).toEqual({ enabled: true });
    expect(patch.providers!["fake"]).toBeUndefined();
  });

  it("extensions tab lists optional features off by default and saves only toggled ones", () => {
    const onSave = vi.fn();
    render(<SettingsModal view={baseView} onSave={onSave} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "扩展" }));
    for (const name of ["discussion", "tasks", "maintenance"]) {
      expect(screen.getByTestId("extension-" + name)).toHaveAttribute("aria-checked", "false");
    }
    fireEvent.click(screen.getByTestId("extension-tasks"));
    expect(screen.getByTestId("extension-tasks")).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByTestId("settings-save"));
    const patch: HubSettingsPatch = onSave.mock.calls[0][0];
    expect(patch.extensions).toEqual({ tasks: true });
    expect(patch.runtime).toBeUndefined();
  });

  it("runtime fields edit produces patch with only changed keys", () => {
    const onSave = vi.fn();
    render(<SettingsModal view={baseView} onSave={onSave} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "运行参数" }));
    const maxTurnsInput = screen.getByTestId("input-max_turns");
    fireEvent.change(maxTurnsInput, { target: { value: "10" } });
    const saveBtn = screen.getByTestId("settings-save");
    fireEvent.click(saveBtn);
    const patch: HubSettingsPatch = onSave.mock.calls[0][0];
    expect(patch.runtime).toBeDefined();
    expect(patch.runtime!.max_turns).toBe(10);
    expect(patch.runtime!.cli_timeout).toBeUndefined();
  });

  it("shows cost warning when enabling a real provider", () => {
    render(<SettingsModal view={baseView} onSave={vi.fn()} onClose={vi.fn()} />);
    const claudeToggle = screen.getByTestId("toggle-claude");
    fireEvent.click(claudeToggle);
    expect(screen.getByText(/产生费用/)).toBeTruthy();
  });

  it("disables save button when no diff", () => {
    render(<SettingsModal view={baseView} onSave={vi.fn()} onClose={vi.fn()} />);
    const saveBtn = screen.getByTestId("settings-save");
    expect(saveBtn).toHaveProperty("disabled", true);
  });

  it("changing agent default provider builds agents patch", () => {
    const onSave = vi.fn();
    render(<SettingsModal view={baseView} onSave={onSave} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "团队配置" }));
    const select = screen.getByTestId("agent-default-wangye") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "claude" } });
    fireEvent.click(screen.getByTestId("settings-save"));
    const patch: HubSettingsPatch = onSave.mock.calls[0][0];
    expect(patch.agents).toBeDefined();
    expect(patch.agents!["wangye"]).toEqual({ default_provider_id: "claude" });
  });

  it("toggling agent fallback updates the list", () => {
    const onSave = vi.fn();
    render(<SettingsModal view={baseView} onSave={onSave} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "团队配置" }));
    fireEvent.click(screen.getByRole("button", { name: /王也 备用连接/ }));
    fireEvent.click(screen.getByTestId("agent-fallback-wangye-codex"));
    fireEvent.click(screen.getByTestId("settings-save"));
    const patch: HubSettingsPatch = onSave.mock.calls[0][0];
    expect(patch.agents!["wangye"].fallback_provider_ids).toEqual(["claude", "codex"]);
  });

  it("reset button only appears when user override is active", () => {
    const overriddenView: HubSettingsView = {
      ...baseView,
      agents: [
        {
          id: "wangye",
          display_name: "王也",
          default_provider_id: { value: "claude", source: "user" },
          fallback_provider_ids: { value: [], source: "user" },
          base_default_provider_id: "antigravity",
          base_fallback_provider_ids: ["claude"],
        },
      ],
    };
    const onSave = vi.fn();
    render(<SettingsModal view={overriddenView} onSave={onSave} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "团队配置" }));
    fireEvent.click(screen.getByTestId("agent-reset-wangye"));
    fireEvent.click(screen.getByTestId("settings-save"));
    const patch: HubSettingsPatch = onSave.mock.calls[0][0];
    expect(patch.agents!["wangye"]).toEqual({ clear: true });
  });

  it("keeps team routing inside settings and can route the team to Codex", () => {
    const onSave = vi.fn();
    render(<SettingsModal view={baseView} onSave={onSave} onClose={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "连接" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "团队配置" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "团队配置" }));
    expect(screen.getByRole("heading", { name: "团队配置" })).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("team-use-codex"));
    fireEvent.click(screen.getByTestId("settings-save"));
    const patch: HubSettingsPatch = onSave.mock.calls[0][0];
    // Routing everyone to Codex keeps real fallbacks and never adds the demo provider.
    expect(patch.agents!["wangye"]).toEqual({ default_provider_id: "codex" });
  });

  it("shows install, verification, and verified lifecycle actions", () => {
    const onVerifyProvider = vi.fn();
    render(
      <SettingsModal
        view={baseView}
        providerStatus={{
          providers: [
            { id: "codex", label: "Codex CLI", enabled: true, available: true, version: "codex 1.0" },
            { id: "claude", label: "Claude", enabled: false, available: false, error: "command not found: claude" },
            { id: "antigravity", label: "Antigravity", enabled: false, available: true, version: "agy 1.1.5" },
          ],
          loading: false,
          error: null,
          refresh: vi.fn(),
        }}
        onboarding={{
          state: "completed",
          guide_agent_id: "chulan",
          verified_provider_ids: ["codex"],
          providers: [
            { id: "codex", label: "Codex CLI", available: true, verified: true, activated: true },
            {
              id: "claude",
              label: "Claude",
              available: false,
              verified: false,
              activated: false,
              install_command: "curl claude | bash",
              install_url: "https://code.claude.com/docs/en/quickstart",
              auth_command: "claude auth login",
            },
            {
              id: "antigravity",
              label: "Antigravity",
              available: true,
              verified: false,
              activated: false,
              auth_command: "agy",
            },
          ],
          dismissed_real_cli_setup: false,
        }}
        onVerifyProvider={onVerifyProvider}
        onSave={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByTestId("provider-card-codex")).toHaveTextContent("已验证");
    expect(screen.getByTestId("provider-card-claude")).toHaveTextContent("未安装");
    expect(screen.getByTestId("provider-card-antigravity")).toHaveTextContent("已安装 · 待验证");
    fireEvent.click(screen.getByRole("button", { name: "验证真实对话" }));
    expect(onVerifyProvider).toHaveBeenCalledWith("antigravity");
  });

  it("supports Scheme B dual-pane: creates custom API connection and submits in patch", () => {
    const onSave = vi.fn();
    render(<SettingsModal view={baseView} onSave={onSave} onClose={vi.fn()} />);

    // Click "+ 新增 API" button in sidebar
    fireEvent.click(screen.getByTestId("add-connection-btn"));

    // Verify detail pane shows API connection editor
    expect(screen.getByText("仅纯文本对话与讨论")).toBeInTheDocument();
    expect(screen.getByText("API 端点与网络出站设置")).toBeInTheDocument();

    // Fill in API connection parameters
    const nameInput = screen.getByDisplayValue("自定义 API 连接");
    fireEvent.change(nameInput, { target: { value: "OpenAI 兼容网关" } });

    const urlInput = screen.getByPlaceholderText("https://api.openai.com/v1");
    fireEvent.change(urlInput, { target: { value: "https://api.example.com/v1" } });

    const keyInput = screen.getByPlaceholderText("在此输入 API Key (保存后加密提交，前端不留存)");
    fireEvent.change(keyInput, { target: { value: "test-secret-key" } });

    fireEvent.click(screen.getByTestId("settings-save"));
    expect(onSave).toHaveBeenCalled();
    const patch: HubSettingsPatch = onSave.mock.calls[0][0];
    expect(patch.connections).toBeDefined();

    const connKeys = Object.keys(patch.connections!);
    expect(connKeys.length).toBe(1);
    expect(connKeys[0]).toMatch(/^api-/);

    const savedConn = patch.connections![connKeys[0]];
    expect(savedConn.name).toBe("OpenAI 兼容网关");
    expect(savedConn.base_url).toBe("https://api.example.com/v1");
    expect(savedConn.key_action).toBe("replace");
    expect(savedConn.api_key).toBe("test-secret-key");
  });

  it("credentials 3-state: displays has_key without key leaks, supports replace and delete", () => {
    const viewWithApi: HubSettingsView = {
      ...baseView,
      connections: [
        {
          id: "api-openai-1",
          name: "OpenAI 线上",
          kind: "api",
          protocol: "openai_responses",
          base_url: "https://api.openai.com/v1",
          enabled: true,
          has_key: true,
          timeout: 60,
          allow_private: false,
        },
      ],
    };
    const onSave = vi.fn();
    render(<SettingsModal view={viewWithApi} onSave={onSave} onClose={vi.fn()} />);

    // Select the connection in sidebar
    fireEvent.click(screen.getByTestId("provider-card-api-openai-1"));

    // Verify no secret text or fragment leaked
    expect(screen.queryByText(/sk-/)).not.toBeInTheDocument();
    expect(screen.getByText("已配置凭据 (后端安全存储，无明文回传)")).toBeInTheDocument();

    // Test Delete Key
    fireEvent.click(screen.getByRole("button", { name: "彻底删除" }));
    expect(screen.getByText("凭据已标记删除 (保存后生效)")).toBeInTheDocument();

    // Revoke delete
    fireEvent.click(screen.getByRole("button", { name: "撤销删除" }));
    expect(screen.getByText("已配置凭据 (后端安全存储，无明文回传)")).toBeInTheDocument();

    // Test Replace Key
    fireEvent.click(screen.getByRole("button", { name: "替换密钥" }));
    expect(screen.getByText("凭据将被新输入的密钥替换")).toBeInTheDocument();
    const newKeyInput = screen.getByPlaceholderText("在此输入 API Key (保存后加密提交，前端不留存)");
    fireEvent.change(newKeyInput, { target: { value: "sk-fresh-new-token" } });

    fireEvent.click(screen.getByTestId("settings-save"));
    const patch: HubSettingsPatch = onSave.mock.calls[0][0];
    expect(patch.connections!["api-openai-1"]).toEqual(
      expect.objectContaining({
        key_action: "replace",
        api_key: "sk-fresh-new-token",
      }),
    );
  });

  it("supports fallback context approval and resets upon fallback provider change", () => {
    const onSave = vi.fn();
    render(<SettingsModal view={baseView} onSave={onSave} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /王也 备用连接/ }));

    const approveCheckbox = screen.getByTestId("agent-fallback-approve-wangye") as HTMLInputElement;
    expect(approveCheckbox.checked).toBe(false);

    // Check approval
    fireEvent.click(approveCheckbox);
    expect(approveCheckbox.checked).toBe(true);

    // Changing fallback resets approval
    fireEvent.click(screen.getByTestId("agent-fallback-wangye-codex"));
    expect(approveCheckbox.checked).toBe(false);

    // Re-check approval
    fireEvent.click(approveCheckbox);
    expect(approveCheckbox.checked).toBe(true);

    fireEvent.click(screen.getByTestId("settings-save"));
    const patch: HubSettingsPatch = onSave.mock.calls[0][0];
    expect(patch.agents!["wangye"].fallback_context_approved).toBe(true);
  });

  it("confirms before closing when there are unsaved changes", () => {
    const onClose = vi.fn();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    render(<SettingsModal view={baseView} onSave={vi.fn()} onClose={onClose} />);

    // Make a change
    fireEvent.click(screen.getByTestId("toggle-claude"));

    // Attempt to close
    fireEvent.click(screen.getByTestId("settings-close"));
    expect(confirmSpy).toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    // Confirm true
    confirmSpy.mockReturnValue(true);
    fireEvent.click(screen.getByTestId("settings-close"));
    expect(onClose).toHaveBeenCalled();

    confirmSpy.mockRestore();
  });
});

it("keeps model choices independent for each CLI and saves them with routing", () => {
  const onSave = vi.fn();
  render(<SettingsModal view={baseView} onSave={onSave} onClose={vi.fn()} />);
  expect(screen.getByRole("heading", { name: "连接" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "团队配置" })).toBeInTheDocument();
  fireEvent.change(screen.getByTestId("agent-default-wangye"), { target: { value: "codex" } });
  fireEvent.click(screen.getByRole("button", { name: /王也 备用连接/ }));
  fireEvent.change(screen.getByLabelText("王也 codex 模型"), { target: { value: "gpt-6.1-sol" } });
  fireEvent.change(screen.getByLabelText("王也 claude 模型"), { target: { value: "sonnet" } });
  fireEvent.click(screen.getByTestId("settings-save"));
  expect(onSave.mock.calls[0][0].agents.wangye).toEqual({
    default_provider_id: "codex",
    provider_models: { codex: "gpt-6.1-sol", claude: "sonnet" },
  });
});

it("lets each role pick a model listed by its CLI and keeps manual entry available", async () => {
  vi.mocked(fetchConnectionModels).mockImplementation(async (id: string) => id === "codex"
    ? { connection_id: id, status: "ready", fetched_at: "2026-10-28T00:00:00Z", error: null,
        models: [{ id: "gpt-6.1-sol", label: "GPT-6.1-Sol", vendor: null }, { id: "gpt-5.5", label: "GPT-5.5", vendor: null }] }
    : { connection_id: id, status: "unsupported", models: [], fetched_at: null, error: "该 CLI 不提供模型列表" });
  const onSave = vi.fn();
  const view: HubSettingsView = { ...baseView, agents: [{ ...baseView.agents[0], default_provider_id: { value: "codex", source: "user" } }] };
  render(<SettingsModal view={view} onSave={onSave} onClose={vi.fn()} />);
  const select = await screen.findByRole("combobox", { name: "王也 codex 模型" });
  expect(within(select).getByRole("option", { name: "沿用 CLI 默认" })).toBeInTheDocument();
  expect(within(select).queryByRole("group")).not.toBeInTheDocument();
  fireEvent.change(select, { target: { value: "gpt-5.5" } });
  fireEvent.change(select, { target: { value: "__manual__" } });
  fireEvent.change(screen.getByRole("textbox", { name: "王也 codex 模型" }), { target: { value: "gpt-custom" } });
  fireEvent.click(screen.getByRole("button", { name: "从列表选择" }));
  expect(screen.getByRole("combobox", { name: "王也 codex 模型" })).toHaveValue("gpt-custom");
  fireEvent.click(screen.getByTestId("settings-save"));
  expect(onSave.mock.calls[0][0].agents.wangye.provider_models).toEqual({ codex: "gpt-custom" });
});

it("shows catalog state in the connection card without an error for CLIs that have no list", async () => {
  vi.mocked(fetchConnectionModels).mockImplementation(async (id: string) => id === "codex"
    ? { connection_id: id, status: "stale", fetched_at: "2026-10-28T00:00:00Z", error: "获取模型列表超时",
        models: [{ id: "gpt-5.5", label: "GPT-5.5", vendor: null }] }
    : { connection_id: id, status: "unsupported", models: [], fetched_at: null, error: "该 CLI 不提供模型列表" });
  render(<SettingsModal view={baseView} onSave={vi.fn()} onClose={vi.fn()} />);
  fireEvent.click(screen.getByTestId("provider-card-claude"));
  expect(await screen.findByText(/这个 CLI 不提供模型列表/)).toBeInTheDocument();
  expect(screen.queryByText(/没能获取/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByTestId("provider-card-codex"));
  expect(await screen.findByText(/这次没能更新，仍显示上次的列表：获取模型列表超时/)).toBeInTheDocument();
  expect(screen.getByText("上次结果")).toBeInTheDocument();
  // A display name that only differs from the id by case is not repeated.
  expect(screen.getByText("GPT-5.5")).toHaveAttribute("title", "gpt-5.5");
});

const effortProviders: HubSettingsView["providers"] = baseView.providers.map((provider) => ({
  ...provider,
  efforts: provider.id === "codex" ? ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    : provider.id === "claude" || provider.id === "antigravity" ? ["low", "medium", "high", "xhigh", "max"] : [],
}));

it("offers each role an effort per connection that follows the chosen model and saves it with routing", async () => {
  vi.mocked(fetchConnectionModels).mockImplementation(async (id: string) => id === "codex"
    ? { connection_id: id, status: "ready", fetched_at: "2026-10-28T00:00:00Z", error: null,
        models: [{ id: "gpt-6.1-sol", label: "GPT-6.1-Sol", vendor: null, efforts: ["low", "medium", "high", "xhigh", "max", "ultra"] },
                 { id: "gpt-5.5", label: "GPT-5.5", vendor: null, efforts: ["low", "medium", "high", "xhigh"] }] }
    // Claude's list never arrives; its levels still come from the settings view.
    : new Promise(() => {}));
  const onSave = vi.fn();
  const view: HubSettingsView = { ...baseView, providers: effortProviders, agents: [{ ...baseView.agents[0], default_provider_id: { value: "codex", source: "user" },
    provider_models: { value: { codex: "gpt-6.1-sol" }, source: "user" } }] };
  render(<SettingsModal view={view} onSave={onSave} onClose={vi.fn()} />);
  await screen.findByRole("combobox", { name: "王也 codex 模型" });
  const effort = screen.getByRole("combobox", { name: "王也 codex 推理强度" });
  expect(effort).toHaveValue("");
  expect(within(effort).getAllByRole("option").map((o) => o.getAttribute("value")))
    .toEqual(["", "low", "medium", "high", "xhigh", "max", "ultra"]);
  fireEvent.change(effort, { target: { value: "ultra" } });
  expect(effort).toHaveClass("is-active");
  // gpt-5.5 has no ultra, so switching model drops the effort back to the CLI default.
  fireEvent.change(screen.getByRole("combobox", { name: "王也 codex 模型" }), { target: { value: "gpt-5.5" } });
  expect(screen.getByRole("combobox", { name: "王也 codex 推理强度" })).toHaveValue("");
  fireEvent.change(screen.getByRole("combobox", { name: "王也 codex 推理强度" }), { target: { value: "xhigh" } });
  fireEvent.click(screen.getByRole("button", { name: /王也 备用连接/ }));
  fireEvent.change(screen.getByRole("combobox", { name: "王也 claude 推理强度" }), { target: { value: "max" } });
  fireEvent.click(screen.getByTestId("settings-save"));
  expect(onSave.mock.calls[0][0].agents.wangye).toEqual({
    provider_models: { codex: "gpt-5.5" },
    provider_efforts: { codex: "xhigh", claude: "max" },
  });
});

it("disables the effort pill for connections without an effort setting and resets saved efforts", async () => {
  vi.mocked(fetchConnectionModels).mockImplementation(async (id: string) =>
    ({ connection_id: id, status: "unsupported", models: [], fetched_at: null, error: null }));
  const onSave = vi.fn();
  const view: HubSettingsView = { ...baseView, providers: effortProviders, agents: [{ ...baseView.agents[0], default_provider_id: { value: "fake", source: "user" },
    provider_efforts: { value: { claude: "high" }, source: "user" }, base_provider_efforts: {} }] };
  render(<SettingsModal view={view} onSave={onSave} onClose={vi.fn()} />);
  const effort = await screen.findByRole("combobox", { name: "王也 fake 推理强度" });
  expect(effort).toBeDisabled();
  expect(effort).toHaveDisplayValue("强度：不可调");
  fireEvent.click(screen.getByTestId("agent-reset-wangye"));
  fireEvent.click(screen.getByTestId("settings-save"));
  expect(onSave.mock.calls[0][0].agents.wangye).toEqual({ clear: true });
});

it("locks the effort for a model that fixes its own and keeps it adjustable for the CLI default", async () => {
  vi.mocked(fetchConnectionModels).mockImplementation(async (id: string) => id === "antigravity"
    ? { connection_id: id, status: "ready", fetched_at: null, error: null,
        models: [{ id: "gemini-3.8-flash-low", label: "Gemini 3.8 Flash (Low)", vendor: null, efforts: [] }] }
    : new Promise(() => {}));
  const onSave = vi.fn();
  const view: HubSettingsView = { ...baseView, providers: effortProviders, agents: [{ ...baseView.agents[0], fallback_provider_ids: { value: [], source: "user" },
    provider_efforts: { value: { antigravity: "high" }, source: "user" } }] };
  render(<SettingsModal view={view} onSave={onSave} onClose={vi.fn()} />);
  const model = await screen.findByRole("combobox", { name: "王也 antigravity 模型" });
  expect(screen.getByRole("combobox", { name: "王也 antigravity 推理强度" })).toHaveValue("high");
  fireEvent.change(model, { target: { value: "gemini-3.8-flash-low" } });
  const effort = screen.getByRole("combobox", { name: "王也 antigravity 推理强度" });
  expect(effort).toHaveValue("");
  expect(effort).toBeDisabled();
  fireEvent.click(screen.getByTestId("settings-save"));
  expect(onSave.mock.calls[0][0].agents.wangye).toEqual({
    provider_models: { antigravity: "gemini-3.8-flash-low" },
    provider_efforts: {},
  });
});
