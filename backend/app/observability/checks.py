"""Read-time turn checks: cheap questions, answered from the trace alone.

These are not the offline evaluation battery in `backend/app/evaluation/`.
That one grades a final answer against a rubric and needs a predefined task.
These grade one turn against its own event stream, so they apply to every real
turn — including the ones nobody wrote a task for.

Three rules keep them honest:

- **`n/a` is a real answer.** A check that cannot see enough says so. A check
  that fails on missing evidence trains everyone to ignore it.
- **Fail means contradiction, not absence.** "Said it delegated, and the trace
  shows the delegation was absorbed" is a failure. "Said it delegated and we
  cannot see the tool" is `n/a`.
- **Nothing is written.** Checks run on every read, so fixing one fixes every
  historical turn at once.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from backend.app.adapters.failure_text import is_informative
from backend.app.observability.turns import A2A, NATIVE, TOOL, Turn

PASS, FAIL, NA = 1, 0, None

# ---- claim extraction -------------------------------------------------------
def _delegation_pattern(names) -> re.Pattern:
    alternation = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
    return re.compile(
        rf"已委派|委派给|已通知|交给({alternation})|"
        rf"(让|请|叫)({alternation})[^，。；]{{0,6}}(复核|看|查|确认|处理)")


_BUILTIN_NAMES = ("冯宝宝", "宝宝", "楚岚", "张楚岚", "王也")


def _team_names() -> tuple:
    """Display names and non-latin aliases of the current team, user roles included."""
    from backend.app.domain.agent_registry import get_agent_registry
    names = {n for a in get_agent_registry().all() for n in (a.display_name, *a.aliases) if n and not n.isascii()}
    return tuple(sorted(names | set(_BUILTIN_NAMES)))


# High precision on purpose: a false claim is worse than a missed one. Each
# pattern requires completed aspect ("已/了") so plans and advice do not count.
# claims() rebuilds the delegation entry with the live team names.
_CLAIM_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("delegation", _delegation_pattern(_BUILTIN_NAMES)),
    ("artifact", re.compile(
        r"已写入|写入了|写入到|保存到|已保存|生成(了)?(文件|报告|方案)|已创建|产出了|artifacts/")),
    ("exec", re.compile(
        r"已运行|运行了|执行了|跑了|已跑|测试通过|全部通过|已提交|已推送|已合并|已安装|已构建|构建通过")),
    ("done", re.compile(r"已完成|已修复|修复好了|已经修复|已解决|搞定了|已处理|已经修好")),
]
_PLAN = re.compile(r"建议|可以|应该|将会|下一步|如果|若|假如|一旦|需要|计划|打算|不妨|待|设想|原则是|预计")
_DENIAL = re.compile(r"没有|没|未|无法|不能|尚未|并未")
_CONTRAST = re.compile(r"但|不过|却|而是")
_SENTENCE = re.compile(r"[。！？\n]+")
_MARKUP = re.compile(r"[*_`#>]+")
_FAILURE_WORDS = re.compile(
    r"失败|报错|错误|未能|无法|超时|拒绝|没跑|未跑|跳过|不可用|异常|error|failed|timeout",
    re.IGNORECASE,
)


def _sentences(reply: str) -> list[str]:
    out = []
    for raw in _SENTENCE.split(reply or ""):
        text = _MARKUP.sub("", raw).strip(" -—·")
        if len(text) >= 6:
            out.append(text)
    return out


def _negated(sentence: str, match: re.Match) -> bool:
    head = sentence[max(0, match.start() - 20):match.start()]
    return bool(_DENIAL.search(head)) and not _CONTRAST.search(head)


def claims(reply: str) -> list[tuple[str, str]]:
    """(kind, sentence) for each sentence that asserts something happened."""
    found = []
    patterns = [("delegation", _delegation_pattern(_team_names())), *_CLAIM_PATTERNS[1:]]
    for sentence in _sentences(reply):
        if _PLAN.search(sentence):
            continue
        for kind, pattern in patterns:
            if any(not _negated(sentence, m) for m in pattern.finditer(sentence)):
                found.append((kind, sentence))
                break
    return found


# ---- the checks -------------------------------------------------------------

def _result(name: str, value, note: str) -> dict:
    return {"name": name, "source": "code", "value": value, "note": note}


def check_claims_match_events(turn: Turn) -> dict:
    """A claim about an action must not be contradicted by this turn's events.

    Contradictions are narrow and defensible: the reply says it delegated and
    the turn only has an `a2a.skipped`; it says it finished and the turn
    `blocked`/`failed`; it says it wrote an artifact and `write_artifact`
    errored. A claim with no event either way is `n/a`, because the CLI's own
    tools are not always traced.
    """
    found = claims(turn.reply)
    if not found:
        return _result("claims_match_events", NA, "回复没有可核对的行动声明")

    skipped = [s for s in turn.a2a if s.tool == "skipped"]
    invoked = [s for s in turn.a2a if s.tool == "invoke"]
    write_errors = [s for s in turn.tools if s.tool == "write_artifact" and not s.ok]
    failed = turn.status in ("blocked", "failed")

    contradictions: list[str] = []
    confirmed = 0
    for kind, _sentence in found:
        if kind == "delegation":
            if skipped and not invoked:
                contradictions.append("声称已委派，但 a2a.skipped")
            elif invoked:
                confirmed += 1
        elif kind == "artifact":
            if write_errors:
                contradictions.append("声称已写入产物，但 write_artifact 报错")
            elif turn.artifacts:
                confirmed += 1
        elif kind == "done":
            if failed:
                contradictions.append(f"声称已完成，但回合 {turn.status}")
            elif turn.status == "completed":
                confirmed += 1
        elif kind == "exec":
            if any(not s.ok and any(k in s.tool.lower() for k in ("run", "exec", "shell", "test", "git"))
                   for s in turn.steps):
                contradictions.append("声称已执行，但执行类工具失败")
            elif any(s.kind == NATIVE and any(k in s.tool.lower() for k in ("run", "command", "exec"))
                     for s in turn.steps):
                confirmed += 1
    if contradictions:
        return _result("claims_match_events", FAIL, "；".join(contradictions[:2]))
    if confirmed:
        return _result("claims_match_events", PASS, f"{confirmed} 条声明与本回合事件一致")
    return _result("claims_match_events", NA, f"{len(found)} 条声明无事件可判（原生工具可能未入 trace）")


def check_tool_errors_handled(turn: Turn) -> dict:
    """Every failed tool call was retried in the turn, or the reply names it.

    Same rule as the failure check in the agent's own instructions: an error
    the user never hears about is worse than the error.
    """
    failed = [s for s in turn.tools if not s.ok]
    if not failed:
        return _result("tool_errors_handled", NA, "本回合没有失败的工具调用")

    unhandled = []
    for step in failed:
        retried = any(other is not step and other.tool == step.tool and other.ok
                      for other in turn.tools)
        mentioned = bool(_FAILURE_WORDS.search(turn.reply)) and step.tool in turn.reply
        if not (retried or mentioned):
            unhandled.append(step.tool or "unknown")
    if unhandled:
        return _result("tool_errors_handled", FAIL,
                       "未重试也未提及：" + "、".join(sorted(set(unhandled))[:3]))
    return _result("tool_errors_handled", PASS, f"{len(failed)} 次失败已重试或已说明")


def check_blocked_explained(turn: Turn) -> dict:
    """A blocked or failed turn must say why — in its error or in its reply.

    A non-empty error is not an explanation. "exit_code=1" followed by the
    CLI's init event or a startup banner names no cause, and passing it made
    this check unable to fail.
    """
    if turn.status not in ("blocked", "failed"):
        return _result("blocked_explained", NA, "回合正常结束")
    if is_informative(turn.error):
        return _result("blocked_explained", PASS,
                       f"[{turn.failure_class}] {turn.error.strip()[:80]}")
    if turn.reply.strip():
        return _result("blocked_explained", PASS, "回复里说明了失败")
    if turn.error.strip():
        return _result("blocked_explained", FAIL,
                       f"记录的只有退出码或启动输出：{turn.error.strip()[:60]}")
    return _result("blocked_explained", FAIL, "回合失败且没有任何说明")


CHECKS: dict[str, Callable[[Turn], dict]] = {
    "claims_match_events": check_claims_match_events,
    "tool_errors_handled": check_tool_errors_handled,
    "blocked_explained": check_blocked_explained,
}

WHAT: dict[str, str] = {
    "claims_match_events": "回复里的行动声明不与本回合事件矛盾",
    "tool_errors_handled": "失败的工具调用被重试或在回复中说明",
    "blocked_explained": "失败/阻塞的回合给出了可读的原因",
}


def run_checks(turn: Turn) -> list[dict]:
    """All checks for one turn. A crashing check becomes `n/a`, never an error.

    Observability must not be able to break the page it feeds. If a check has a
    bug, that is a bug to fix in the check, not a reason for a blank screen.
    """
    out = []
    for name, check in CHECKS.items():
        try:
            out.append(check(turn))
        except Exception:  # noqa: BLE001 - a check must never break the reader
            out.append(_result(name, NA, "check error"))
    return out


def summarize(turns: list[Turn]) -> dict:
    """Per-check pass/fail/n/a across turns, with the newest failure."""
    rows = []
    for name, what in WHAT.items():
        passed = failed = na = 0
        newest: Optional[dict] = None
        for turn in turns:
            score = next((c for c in turn.checks if c["name"] == name), None)
            value = score["value"] if score else NA
            if value is None:
                na += 1
            elif value >= 1:
                passed += 1
            else:
                failed += 1
                newest = {"turn_id": turn.turn_id, "agent_id": turn.agent_id,
                          "note": score["note"], "started_at": turn.started_at}
        scored = passed + failed
        rows.append({
            "name": name,
            "what": what,
            "passed": passed,
            "failed": failed,
            "na": na,
            "pass_rate": round(passed / scored, 3) if scored else None,
            "newest_failure": newest,
        })
    return {"checks": rows, "turns": len(turns),
            "turns_passed": sum(1 for t in turns if t.checks and all(
                c["value"] != FAIL for c in t.checks))}
