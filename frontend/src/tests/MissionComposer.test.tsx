import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MissionComposer } from "../components/MissionComposer";
import type { ConfirmedMention } from "../lib/inputConfirmations";

/** 真实受控输入：value/confirmations 由父级状态驱动，贴近 App 的用法。 */
function Harness(props: {
  initialValue?: string;
  hasActiveMission?: boolean;
  disabled?: boolean;
  mentionsEnabled?: boolean;
  skillsEnabled?: boolean;
  onSubmit?: () => void;
  onChange?: (value: string) => void;
  onConfirmationsChange?: (mentions: ConfirmedMention[]) => void;
}) {
  const [value, setValue] = useState(props.initialValue ?? "");
  const [confirmations, setConfirmations] = useState<ConfirmedMention[]>([]);
  return (
    <MissionComposer
      value={value}
      busy={false}
      hasActiveMission={props.hasActiveMission ?? false}
      disabled={props.disabled}
      mentionsEnabled={props.mentionsEnabled}
      skillsEnabled={props.skillsEnabled}
      onChange={(v) => {
        setValue(v);
        props.onChange?.(v);
      }}
      confirmations={confirmations}
      onConfirmationsChange={(m) => {
        setConfirmations(m);
        props.onConfirmationsChange?.(m);
      }}
      onSubmit={props.onSubmit ?? (() => {})}
    />
  );
}

/** 模拟发送成功后父级清空 value 与 confirmations 的复位行为。 */
function SendableComposer(props: {
  onSend?: (payload: { value: string; confirmations: ConfirmedMention[] }) => void;
  onConfirmationsChange?: (mentions: ConfirmedMention[]) => void;
}) {
  const [value, setValue] = useState("");
  const [confirmations, setConfirmations] = useState<ConfirmedMention[]>([]);
  const send = () => {
    props.onSend?.({ value, confirmations });
    setValue("");
    setConfirmations([]);
  };
  return (
    <>
      <MissionComposer
        value={value}
        busy={false}
        hasActiveMission={false}
        onChange={setValue}
        confirmations={confirmations}
        onConfirmationsChange={(m) => {
          setConfirmations(m);
          props.onConfirmationsChange?.(m);
        }}
        onSubmit={send}
      />
      <button onClick={send}>send</button>
    </>
  );
}

describe("MissionComposer mention autocomplete", () => {
  it("shows all three agents when @ is typed", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText("消息输入"), "@");

    const listbox = screen.getByRole("listbox", { name: "Agent 候选" });
    const options = within(listbox).getAllByRole("option");
    expect(options).toHaveLength(3);
    expect(options[0]).toHaveTextContent("张楚岚");
    expect(options[1]).toHaveTextContent("冯宝宝");
    expect(options[2]).toHaveTextContent("王也");
  });

  it("filters to 王也 when @王 is typed", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText("消息输入"), "@王");

    const listbox = screen.getByRole("listbox", { name: "Agent 候选" });
    const options = within(listbox).getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveTextContent("王也");
  });

  it("clicking an option inserts mention and reports its confirmation range", async () => {
    const user = userEvent.setup();
    const onConfirmationsChange = vi.fn();
    render(<Harness onConfirmationsChange={onConfirmationsChange} />);
    await user.type(screen.getByLabelText("消息输入"), "@");

    const listbox = screen.getByRole("listbox", { name: "Agent 候选" });
    await user.click(within(listbox).getAllByRole("option")[2]);

    expect(screen.getByLabelText("消息输入")).toHaveValue("@王也 ");
    expect(onConfirmationsChange).toHaveBeenLastCalledWith([
      { agentId: "wangye", text: "@王也 ", start: 0, end: 4 },
    ]);
  });

  it("keyboard ArrowDown/Enter confirms without submitting", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);
    await user.type(screen.getByLabelText("消息输入"), "@");

    await user.keyboard("{ArrowDown}");
    await user.keyboard("{Enter}");

    expect(screen.getByLabelText("消息输入")).toHaveValue("@冯宝宝 ");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("Escape closes the menu", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText("消息输入"), "@");
    expect(screen.getByRole("listbox")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("Tab confirms the first mention without submitting", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);
    await user.type(screen.getByLabelText("消息输入"), "@");

    await user.keyboard("{Tab}");
    expect(screen.getByLabelText("消息输入")).toHaveValue("@张楚岚 ");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("ignores Enter during IME composition instead of confirming or submitting", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const onChange = vi.fn();
    render(<Harness onSubmit={onSubmit} onChange={onChange} />);
    const textarea = screen.getByLabelText("消息输入");
    await user.type(textarea, "@");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    const changeCountBefore = onChange.mock.calls.length;

    fireEvent.keyDown(textarea, { key: "Enter", isComposing: true });

    expect(onSubmit).not.toHaveBeenCalled();
    expect(onChange.mock.calls.length).toBe(changeCountBefore);
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });

  it("submit button works when menu is closed", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness initialValue="hello" onSubmit={onSubmit} />);

    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(onSubmit).toHaveBeenCalled();
  });

  it("submits with Enter when mention menu is closed", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness initialValue="hello" onSubmit={onSubmit} />);

    screen.getByLabelText("消息输入").focus();
    await user.keyboard("{Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("keeps Shift+Enter as newline input", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness initialValue="hello" onSubmit={onSubmit} />);

    screen.getByLabelText("消息输入").focus();
    await user.keyboard("{Shift>}{Enter}{/Shift}");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("stops independently of sending and explains when a supplement takes effect", async () => {
    const onStop = vi.fn(), onSubmit = vi.fn();
    const props = { value: "补充信息", busy: false, hasActiveMission: true,
      onChange: vi.fn(), onSubmit, onStop, submitLabel: "发送补充" };
    const { rerender } = render(<MissionComposer {...props} />);
    expect(screen.getByText("补充消息将在当前回合结束后处理。")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "停止" }));
    expect(onStop).toHaveBeenCalledOnce();
    expect(onSubmit).not.toHaveBeenCalled();
    rerender(<MissionComposer {...props} stopping submitDisabled />);
    expect(screen.getByRole("button", { name: "停止中…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "发送补充" })).toBeDisabled();
    expect(screen.getByLabelText("消息输入")).not.toBeDisabled();
    await userEvent.type(screen.getByLabelText("消息输入"), "{Enter}");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("shows 发送插话 when has active mission", () => {
    render(<Harness initialValue="x" hasActiveMission />);
    expect(screen.getByRole("button", { name: "发送插话" })).toBeInTheDocument();
  });

  it("shows the skill menu when / is typed at the start", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText("消息输入"), "/");

    const listbox = screen.getByRole("listbox", { name: "技能命令" });
    expect(within(listbox).getAllByRole("option")).toHaveLength(1);
    expect(within(listbox).getByRole("option")).toHaveTextContent("/discuss");
  });

  it("does not offer /discuss when the discussion extension is off", async () => {
    const user = userEvent.setup();
    render(<Harness skillsEnabled={false} />);
    await user.type(screen.getByLabelText("消息输入"), "/");
    expect(screen.queryByRole("listbox", { name: "技能命令" })).toBeNull();
  });

  it("does not open the skill menu after a member mention", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText("消息输入"), "@王也/");

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("Tab confirms the skill command without submitting", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);
    await user.type(screen.getByLabelText("消息输入"), "/");

    await user.keyboard("{Tab}");
    expect(screen.getByLabelText("消息输入")).toHaveValue("/discuss ");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("Enter confirms the skill command without submitting", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} />);
    await user.type(screen.getByLabelText("消息输入"), "/");

    await user.keyboard("{Enter}");
    expect(screen.getByLabelText("消息输入")).toHaveValue("/discuss ");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("renders the demo-styled member option with avatar, pinyin and role", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText("消息输入"), "@");

    const listbox = screen.getByRole("listbox", { name: "Agent 候选" });
    const first = within(listbox).getAllByRole("option")[0];
    expect(first).toHaveTextContent("zhangchulan · zcl");
    expect(first).toHaveTextContent("通透机敏");
    expect(first.querySelector(".mention-avatar img")).toHaveAttribute("src", expect.stringContaining("chulan.png"));
  });

  it("renders the demo-styled skill option with confirm key", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText("消息输入"), "/");

    const listbox = screen.getByRole("listbox", { name: "技能命令" });
    const option = within(listbox).getByRole("option");
    expect(option).toHaveTextContent("/discuss");
    expect(option).toHaveTextContent("讨论");
    expect(within(option).getByText("↵")).toBeInTheDocument();
  });

  it("re-confirming after a send reset starts fresh without stale bindings", async () => {
    const user = userEvent.setup();
    const sent: Array<{ value: string; confirmations: ConfirmedMention[] }> = [];
    const confirmationsLog: ConfirmedMention[][] = [];
    render(
      <SendableComposer
        onSend={(p) => sent.push(p)}
        onConfirmationsChange={(m) => confirmationsLog.push(m)}
      />,
    );
    const textarea = screen.getByLabelText("消息输入");

    await user.type(textarea, "@王");
    await user.keyboard("{Tab}");
    expect(screen.getByLabelText("消息输入")).toHaveValue("@王也 ");

    await user.click(screen.getByRole("button", { name: "send" }));
    expect(screen.getByLabelText("消息输入")).toHaveValue("");
    expect(sent[0].confirmations).toEqual([
      { agentId: "wangye", text: "@王也 ", start: 0, end: 4 },
    ]);

    await user.type(textarea, "@王");
    await user.keyboard("{Tab}");
    expect(confirmationsLog[confirmationsLog.length - 1]).toEqual([
      { agentId: "wangye", text: "@王也 ", start: 0, end: 4 },
    ]);
  });
});
