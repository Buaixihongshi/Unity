"""A failed turn must record why it failed, not what the CLI printed first.

The samples are real outputs from blocked turns (paths and ids removed). Each
one used to be recorded by its head — an init event or a startup banner — and
the observability check then counted that as an explanation.
"""
from __future__ import annotations

import json

from backend.app.adapters.deliberation_errors import failure_code
from backend.app.adapters.failure_text import diagnostic_detail, is_informative, is_noise
from backend.app.adapters.subprocess_cli import build_failure_summary
from backend.app.observability import checks
from backend.app.observability.turns import Turn

RESET = ("API Error: Connection dropped (ECONNRESET). A VPN, firewall, or security "
         "software can cause this. Check those or switch networks, then try again.")

CLAUDE_STDOUT = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "cwd": "/work", "session_id": "s1",
                "tools": ["Task", "Bash", "CronCreate", "CronDelete", "CronList"]}),
    json.dumps({"type": "system", "subtype": "status"}),
    *[json.dumps({"type": "system", "subtype": "api_retry", "attempt": n}) for n in range(1, 11)],
    json.dumps({"type": "assistant", "error": "server_error", "is_api_error_message": True,
                "message": {"content": [{"type": "text", "text": RESET}]}}),
    json.dumps({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
                "terminal_reason": "api_error", "result": RESET}),
])
BANNER = ("⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY or another "
          "auth source is set")
AGY = ('error: Eligibility check failed: Post '
       '"https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": EOF')
OLD_CLAUDE_REASON = ('[claude] exit_code=1 {"type":"system","subtype":"init","cwd":"/work",'
                     '"session_id":"s1","tools":["Task","Bash","CronCreate","CronDelete","CronLi…')


def test_claude_failure_records_the_result_not_the_init_event():
    summary = build_failure_summary("claude", exit_code=1, stdout_snippet=CLAUDE_STDOUT)
    assert summary.startswith("[claude] exit_code=1 API Error: Connection dropped (ECONNRESET)")
    assert '"init"' not in summary


def test_startup_banner_is_skipped_for_the_line_after_it():
    stderr = f"{BANNER}\nError: Request failed: getaddrinfo ENOTFOUND api.anthropic.com"
    assert diagnostic_detail(stderr) == "Error: Request failed: getaddrinfo ENOTFOUND api.anthropic.com"


def test_only_noise_gives_no_detail():
    assert diagnostic_detail(BANNER, json.dumps({"type": "system", "subtype": "init"})) == ""
    assert build_failure_summary("claude", exit_code=1, stderr_snippet=BANNER) == "[claude] exit_code=1"


def test_codex_error_event_wins_over_stdin_notice():
    stdout = "\n".join([json.dumps({"type": "thread.started"}),
                        json.dumps({"type": "turn.failed", "error": {"message": "stream disconnected"}})])
    assert diagnostic_detail("Reading additional input from stdin...", stdout) == "stream disconnected"


def test_truncated_init_event_is_still_noise():
    assert is_noise('{"type":"system","subtype":"init","tools":["Task","CronLi…')
    assert not is_noise('{"type":"result","is_error":true,"result":"API Er…')


def test_informative_reasons():
    assert not is_informative(OLD_CLAUDE_REASON)
    assert is_informative(f"[claude] timeout after 600s {BANNER}")  # a timeout is a cause
    assert not is_informative("[claude] exit_code=1")
    assert not is_informative("")
    assert is_informative(f"[antigravity] exit_code=1 {AGY}")
    assert is_informative("用户停止，本回合已中止")


def test_network_failures_are_classified():
    assert failure_code(RESET) == "network"
    assert failure_code(AGY) == "network"
    assert failure_code("getaddrinfo ENOTFOUND api.anthropic.com") == "dns"
    assert failure_code("HTTP error: 401 Unauthorized, failed to connect to websocket") == "authentication"
    assert failure_code("用户停止，本回合已中止") == "stopped"
    assert failure_code("agent did not call publish_message via MCP") == "missing_submission"


def blocked(error: str, reply: str = "", code: str = "") -> Turn:
    return Turn(turn_id="t1", mission_id="m_1", mission_title="任务", index=1, agent_id="chulan",
                provider_id="claude", status="blocked", error=error, reply=reply, failure_code=code)


def test_blocked_explained_fails_when_the_reason_is_only_startup_output():
    turn = blocked(OLD_CLAUDE_REASON)
    result = checks.check_blocked_explained(turn)
    assert result["value"] == checks.FAIL
    assert turn.failure_class == "unknown"


def test_blocked_explained_passes_with_a_classified_cause():
    turn = blocked(build_failure_summary("claude", exit_code=1, stdout_snippet=CLAUDE_STDOUT))
    result = checks.check_blocked_explained(turn)
    assert result["value"] == checks.PASS
    assert result["note"].startswith("[network]")


def test_adapter_failure_code_wins_over_text():
    turn = blocked("[claude] idle timeout after 90s A2A idle deadline exceeded", code="idle_timeout")
    assert turn.failure_class == "idle_timeout"
    assert turn.to_dict()["failure_class"] == "idle_timeout"


def test_completed_turn_has_no_failure_class():
    turn = blocked("via MCP")
    turn.status = "completed"
    assert turn.failure_class is None
