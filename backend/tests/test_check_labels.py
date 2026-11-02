"""Labelled real turns: what a person decided each check should say.

Every case here is a turn from a real instance that a check got wrong, or got
right for a reason worth keeping, reviewed by hand (#107). Replies are cut to
the sentence that matters and local paths are removed. A check change that
flips one of these must change the label on purpose, not by accident.
"""
from __future__ import annotations

import pytest

from backend.app.observability import checks
from backend.app.observability.turns import A2A, NATIVE, TOOL, Step, Turn


def tool(name: str, ok: bool = True, error: str = "", summary: str = "") -> Step:
    return Step(id=name, kind=TOOL, tool=name, ts="", ok=ok, error=error, summary=summary)


def shell(command: str, ok: bool = False, name: str = "shell") -> Step:
    return Step(id=command, kind=NATIVE, tool=name, ts="", ok=ok, summary=command)


def turn(reply: str, steps: list[Step], status: str = "completed") -> Turn:
    t = Turn(turn_id="t", mission_id="m", mission_title="任务", index=1, agent_id="a",
             provider_id="p", status=status, reply=reply)
    t.steps = steps
    return t


PUBLISH = tool("publish_message", summary="Published message")
DEPTH = tool("invoke_agent", ok=False, error="a2a 深度耗尽 (9/8)")

# (case, turn, check, expected value)
CASES = [
    # turn_1_d4e110df: said in plain Chinese that the file was missing.
    ("chinese_admission", turn(
        "卡我先没建，因为 chat-hard-stop-plan.md 没找到。我找过当前任务的 artifacts。",
        [tool("read_artifact", ok=False, error="Artifact not found: chat-hard-stop-plan.md"), PUBLISH]),
     "tool_errors_handled", checks.PASS),
    # turn_1_8e795bf4: could not read the file, then wrote it into this task.
    ("recovered_same_file", turn(
        "找到了，提议卡也建好了，现在等你在卡上确认。",
        [tool("read_artifact", ok=False, error="Artifact not found: chat-hard-stop-plan.md"),
         tool("write_artifact", ok=False, error="Source artifact is not in the current conversation"),
         tool("write_artifact", summary="Wrote chat-hard-stop-plan.md"), PUBLISH]),
     "tool_errors_handled", checks.PASS),
    # turn_1_7b83bfcc: `codex exec --help | head` exited nonzero while exploring.
    ("shell_exit_is_not_a_tool_error", turn(
        "已完成实现：CLI 接入和团队配置合并到同一个页面。",
        [shell("command -v codex; codex exec --help | head -45"), PUBLISH]),
     "tool_errors_handled", checks.NA),
    ("shell_exit_is_not_an_exec_contradiction", turn(
        "已运行全部后端测试，结果通过。",
        [shell("rg -n 'fallback' backend"), shell(".venv/bin/python -m pytest -q", ok=True), PUBLISH]),
     "claims_match_events", checks.NA),
    ("bash_exit_is_not_a_tool_error", turn(
        "两个 P1 都修完了，已推送到 PR。",
        [shell("git branch -a", name="Bash"), PUBLISH]),
     "tool_errors_handled", checks.NA),
    # turn_1_c7f092af: blocked with no reply; blocked_explained owns this turn.
    ("blocked_without_reply", turn(
        "", [tool("read_artifact", ok=False, error="Artifact not found: conversation-trace-spec.md")],
        status="blocked"),
     "tool_errors_handled", checks.NA),
    # turn_1_64c7e2eb: published "已完成" for a sub-step, blocked afterwards.
    ("published_then_blocked", turn(
        "Alpha 环境已完成同步并上线。",
        [tool("log_thought", ok=False, error="content is required"), tool("log_thought"), PUBLISH],
        status="blocked"),
     "claims_match_events", checks.NA),
    # turn_2_d3789464: could not read github_scan.md, praised the list anyway.
    ("praised_a_file_it_could_not_read", turn(
        "得嘞，宝儿姐这手活儿干得就是利索！11个同类项目，清单拉得明明白白。",
        [tool("invoke_agent", summary="Persisted wait_reply_all group"),
         tool("read_artifact", ok=False, error="Artifact not found: github_scan.md"), PUBLISH]),
     "tool_errors_handled", checks.FAIL),
    # turn_6_5adf5fc9: the handoff was refused, the reply never says so.
    ("handoff_refused_silently", turn(
        "得，楚岚点得切中要害。真要证明提效，闭卷排错和开放推演都得拉出来过筛子。",
        [tool("log_thought"),
         tool("invoke_agent", ok=False, error="Insufficient discussion budget"), PUBLISH]),
     "tool_errors_handled", checks.FAIL),
    ("handoff_refused_and_said", turn(
        "这轮交接没成，深度已经耗尽了，宝宝那边需要你直接 @ 她。",
        [DEPTH, PUBLISH]),
     "tool_errors_handled", checks.PASS),
    # A failure word elsewhere in the reply does not cover the handoff.
    ("unrelated_failure_word", turn(
        "旧 CLI 回退夹具改为可重试的网络故障，补上鉴权失败禁止回退的测试。",
        [DEPTH, PUBLISH]),
     "tool_errors_handled", checks.FAIL),
    # A real contradiction stays a contradiction.
    ("delegation_absorbed", turn(
        "我已经让冯宝宝复核过了。",
        [Step(id="s", kind=A2A, tool="skipped", ts="", error="already scheduled")]),
     "claims_match_events", checks.FAIL),
    ("done_but_blocked_unpublished", turn("已经修复好了。", [], status="blocked"),
     "claims_match_events", checks.FAIL),
]


@pytest.mark.parametrize("case,t,name,expected", CASES, ids=[c[0] for c in CASES])
def test_labelled_turn(case, t, name, expected):
    result = checks.CHECKS[name](t)
    assert result["value"] == expected, f"{case}: {result['note']}"
