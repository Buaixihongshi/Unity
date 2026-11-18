import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsModal } from "../components/SettingsModal";
import { fetchConnectionModels } from "../lib/api";
import { TurnTimeline } from "../components/TurnTimeline";
import { MENTION_OPTIONS, parseMentionTargets, setMentionOptions } from "../lib/mentions";
import { buildAgentParticipants, buildTurnViews, formatAgentName } from "../lib/missionView";
import { getPersonaColor, PERSONA_COLORS } from "../lib/personaColors";
import type { HubSettingsAgentView, HubSettingsView } from "../types";

const api = vi.hoisted(() => ({
  createCustomAgent: vi.fn(),
  updateCustomAgent: vi.fn(),
  deleteCustomAgent: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...api,
  fetchConnectionModels: vi.fn().mockResolvedValue({
    connection_id: "api-x", status: "error", models: [], fetched_at: null, error: "not fetched",
  }),
  refreshConnectionModels: vi.fn(),
}));

function agentView(id: string, displayName: string, custom = false): HubSettingsAgentView {
  return {
    id,
    display_name: displayName,
    custom,
    role: custom ? "测试工程师" : "首席架构师",
    persona: custom ? "细致耐心" : "务实",
    aliases: custom ? ["玲珑"] : [],
    trigger_points: custom ? ["先复现"] : [],
    default_provider_id: { value: "codex", source: "committed" },
    fallback_provider_ids: { value: [], source: "committed" },
    provider_models: { value: {}, source: "committed" },
    base_default_provider_id: "codex",
    base_fallback_provider_ids: [],
  };
}

function view(agents: HubSettingsAgentView[]): HubSettingsView {
  return {
    runtime: {
      use_fake_cli: { value: false, source: "user" },
      max_turns: { value: 6, source: "default" },
      max_a2a_invocations: { value: 8, source: "default" },
      cli_timeout: { value: 300, source: "default" },
      a2a_idle_timeout: { value: 90, source: "default" },
    },
    providers: [
      { id: "codex", label: "Codex CLI", enabled: { value: true, source: "user" }, efforts: ["minimal", "low", "high", "ultra"] },
      { id: "claude", label: "Claude Code", enabled: { value: true, source: "user" } },
    ],
    agents,
  };
}

const builtinOnly = view([agentView("chulan", "张楚岚")]);
const withCustom = view([agentView("chulan", "张楚岚"), agentView("linglong", "陆玲珑", true)]);

describe("custom roles in team settings", () => {
  beforeEach(() => {
    Object.values(api).forEach((fn) => fn.mockReset());
  });

  it("adds a role with identity, duties and its own route", async () => {
    api.createCustomAgent.mockResolvedValue(withCustom);
    const onViewChange = vi.fn();
    render(<SettingsModal view={builtinOnly} onSave={vi.fn()} onClose={vi.fn()} onViewChange={onViewChange} />);

    fireEvent.click(screen.getByTestId("add-role-btn"));
    const form = screen.getByTestId("role-editor");
    const submit = within(form).getByRole("button", { name: "添加角色" });
    expect(submit).toBeDisabled();

    fireEvent.change(within(form).getByPlaceholderText("如：陆玲珑"), { target: { value: " 陆玲珑 " } });
    fireEvent.change(within(form).getByPlaceholderText("如：玲珑、linglong"), { target: { value: "@玲珑、linglong" } });
    fireEvent.change(within(form).getByPlaceholderText(/一句话说明主要职责/), { target: { value: "测试工程师" } });
    fireEvent.change(within(form).getByPlaceholderText(/说话方式/), { target: { value: "细致耐心" } });
    fireEvent.change(within(form).getByPlaceholderText(/先写出复现步骤/), { target: { value: "先复现\n\n再修复" } });
    fireEvent.change(within(form).getByLabelText("Codex CLI（CLI） 模型"), { target: { value: "gpt-5.5" } });
    fireEvent.click(within(form).getByRole("checkbox", { name: "Claude Code（CLI）" }));
    fireEvent.click(submit);

    await waitFor(() => expect(onViewChange).toHaveBeenCalledWith(withCustom));
    expect(api.createCustomAgent).toHaveBeenCalledWith({
      display_name: "陆玲珑",
      role: "测试工程师",
      persona: "细致耐心",
      aliases: ["玲珑", "linglong"],
      trigger_points: ["先复现", "再修复"],
      default_provider_id: "codex",
      fallback_provider_ids: ["claude"],
      provider_models: { codex: "gpt-5.5" },
      provider_efforts: {},
    });
    expect(screen.queryByTestId("role-editor")).not.toBeInTheDocument();
  });

  it("keeps the form open and shows the server's reason when saving fails", async () => {
    api.createCustomAgent.mockRejectedValue(new Error("“宝宝”已被 冯宝宝 使用，请换一个名称或别名"));
    render(<SettingsModal view={builtinOnly} onSave={vi.fn()} onClose={vi.fn()} onViewChange={vi.fn()} />);
    fireEvent.click(screen.getByTestId("add-role-btn"));
    const form = screen.getByTestId("role-editor");
    fireEvent.change(within(form).getByPlaceholderText("如：陆玲珑"), { target: { value: "宝宝" } });
    fireEvent.change(within(form).getByPlaceholderText(/一句话说明主要职责/), { target: { value: "x" } });
    fireEvent.change(within(form).getByPlaceholderText(/说话方式/), { target: { value: "y" } });
    fireEvent.click(within(form).getByRole("button", { name: "添加角色" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("冯宝宝");
  });

  it("offers the CLI catalog while editing a custom role and saves the selected model and effort", async () => {
    vi.mocked(fetchConnectionModels).mockResolvedValueOnce({
      connection_id: "codex", status: "ready", fetched_at: null, error: null,
      models: [{ id: "gpt-5.5", label: "GPT-5.5", vendor: null, efforts: ["low", "high"] }],
    });
    api.updateCustomAgent.mockResolvedValue(withCustom);
    render(<SettingsModal view={withCustom} onSave={vi.fn()} onClose={vi.fn()} onViewChange={vi.fn()} />);
    fireEvent.click(screen.getByTestId("agent-edit-linglong"));
    const form = screen.getByTestId("role-editor");
    const modelInput = within(form).getByLabelText("Codex CLI（CLI） 模型");
    await waitFor(() => expect(modelInput).toHaveAttribute("list"));
    const suggestions = document.getElementById(modelInput.getAttribute("list")!);
    expect(suggestions?.querySelector('option[value="gpt-5.5"]')).not.toBeNull();
    const effort = within(form).getByRole("combobox", { name: "Codex CLI（CLI） 推理强度" });
    fireEvent.change(effort, { target: { value: "ultra" } });
    // The chosen model lists only low/high, so ultra is dropped rather than sent to the CLI.
    fireEvent.change(modelInput, { target: { value: "gpt-5.5" } });
    expect(effort).toHaveValue("");
    expect(within(effort).getAllByRole("option").map((o) => o.getAttribute("value"))).toEqual(["", "low", "high"]);
    fireEvent.change(effort, { target: { value: "high" } });
    fireEvent.click(within(form).getByRole("button", { name: "保存角色" }));
    await waitFor(() => expect(api.updateCustomAgent).toHaveBeenCalledWith(
      "linglong", expect.objectContaining({ provider_models: { codex: "gpt-5.5" }, provider_efforts: { codex: "high" } }),
    ));
  });

  it("edits and deletes only custom roles", async () => {
    api.updateCustomAgent.mockResolvedValue(withCustom);
    api.deleteCustomAgent.mockResolvedValue(builtinOnly);
    const onViewChange = vi.fn();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<SettingsModal view={withCustom} onSave={vi.fn()} onClose={vi.fn()} onViewChange={onViewChange} />);

    expect(screen.queryByTestId("agent-delete-chulan")).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-edit-chulan")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("agent-row-linglong")).getByText("自定义")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("agent-edit-linglong"));
    const form = screen.getByTestId("role-editor");
    expect(within(form).getByDisplayValue("细致耐心")).toBeInTheDocument();
    fireEvent.change(within(form).getByDisplayValue("测试工程师"), { target: { value: "质量负责人" } });
    fireEvent.click(within(form).getByRole("button", { name: "保存角色" }));
    await waitFor(() => expect(api.updateCustomAgent).toHaveBeenCalledWith(
      "linglong",
      expect.objectContaining({ role: "质量负责人", aliases: ["玲珑"], trigger_points: ["先复现"] }),
    ));

    fireEvent.click(screen.getByTestId("agent-delete-linglong"));
    expect(confirm).toHaveBeenCalled();
    await waitFor(() => expect(api.deleteCustomAgent).toHaveBeenCalledWith("linglong"));
    expect(onViewChange).toHaveBeenLastCalledWith(builtinOnly);
    confirm.mockRestore();
  });
});

describe("custom roles across the conversation UI", () => {
  const builtins = MENTION_OPTIONS.map((option) => ({ ...option }));
  beforeEach(() => {
    setMentionOptions([
      ...builtins,
      { id: "linglong", label: "陆玲珑", aliases: ["玲珑", "linglong"], role: "测试工程师", custom: true },
    ]);
  });
  afterEach(() => setMentionOptions(builtins));

  it("resolves @mentions, names and the participant strip from the live directory", () => {
    expect(parseMentionTargets("@玲珑 @张楚岚 看看").targetAgentIds).toEqual(["linglong", "chulan"]);
    expect(formatAgentName("linglong")).toBe("陆玲珑");
    expect(buildAgentParticipants([]).map((p) => p.agentId)).toEqual(["chulan", "baobao", "wangye", "linglong"]);
  });

  it("names custom roles in A2A hand-offs and while a reply streams", () => {
    const [turn] = buildTurnViews([{
      id: 1, mission_id: "m1", role: "agent", agent_id: "linglong", provider_id: "codex",
      event_type: "message.partial", content: "", created_at: "2026-10-28T00:00:00Z",
    } as never]);
    expect(turn.personaLabel).toBe("陆玲珑");
    render(<TurnTimeline turns={[{
      ...turn, eventType: "message", content: "收到",
      a2aInvocation: {
        sender_agent_id: "chulan", target_agent_id: "linglong", purpose: "补一条测试建议",
        mode: "fire_and_forget", target_turn_id: null,
      } as never,
    }]} />);
    expect(screen.getByText("张楚岚 → 陆玲珑")).toBeInTheDocument();
  });

  it("gives custom roles a design-token colour and initial avatar", () => {
    const color = getPersonaColor("linglong");
    expect(color.border).toMatch(/^var\(--/);
    expect(color.avatar).toBe("陆");
    expect(getPersonaColor("chulan")).toEqual(PERSONA_COLORS.chulan);
    expect(getPersonaColor("someone-else").border).toBe("#1A1A1A");
  });
});
