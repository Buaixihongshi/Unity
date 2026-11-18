"""Trace checks ask whether a turn's record can be believed; agent checks ask
whether the agent behaved. These tests pin the line between them: which layer
each check is on, when a broken record stops the agent from being graded, and
whose failure a blocked turn is.
"""
from __future__ import annotations

from backend.app.observability import checks, pricing
from backend.app.observability.turns import TOOL, Step, Turn


def turn(status="completed", reply="", mission_status="completed", usage=None, steps=None,
         error="", failure_code="") -> Turn:
    t = Turn(turn_id="t", mission_id="m", mission_title="任务", index=1, agent_id="chulan",
             provider_id="claude", status=status, reply=reply, error=error,
             failure_code=failure_code, mission_status=mission_status, usage=usage)
    t.steps = steps or []
    return t


def by_name(t: Turn) -> dict:
    return {c["name"]: c for c in checks.run_checks(t)}


USAGE = {"input_tokens": 1200, "output_tokens": 300}
ZERO = {"input_tokens": 0, "output_tokens": 0, "total_cost_usd": None}


def test_every_check_has_a_layer():
    assert set(checks.LAYER) == set(checks.CHECKS)
    assert {c["layer"] for c in checks.run_checks(turn(usage=USAGE))} == {"trace", "agent"}


def test_unclosed_turn_in_an_ended_mission_fails_and_gates_agent_checks():
    results = by_name(turn(status="running", reply="已经修复好了。", mission_status="completed"))
    assert results["turn_closed"]["value"] == checks.FAIL
    for name in ("claims_match_events", "tool_errors_handled"):
        assert results[name]["value"] == checks.NA
        assert "trace 不可信" in results[name]["note"]


def test_running_turn_in_a_live_mission_is_not_a_trace_failure():
    results = by_name(turn(status="running", mission_status="running"))
    assert results["turn_closed"]["value"] == checks.NA
    assert "trace 不可信" not in results["claims_match_events"]["note"]


def test_a_vague_block_reason_does_not_gate_the_agent():
    # The record of what the agent said is intact; only the cause is vague.
    results = by_name(turn(status="blocked", reply="已经修复好了。",
                           error='[claude] exit_code=1 {"type":"system","subtype":"init"'))
    assert results["blocked_explained"]["value"] == checks.FAIL
    assert results["claims_match_events"]["value"] == checks.FAIL


def test_usage_reported():
    assert by_name(turn(usage=USAGE, reply="好"))["usage_reported"]["value"] == checks.PASS
    assert by_name(turn(usage=ZERO, reply="好"))["usage_reported"]["value"] == checks.FAIL
    assert by_name(turn(usage=None, steps=[Step(id="1", kind=TOOL, tool="read_context", ts="")]))[
        "usage_reported"]["value"] == checks.FAIL
    # Blocked before doing anything: consumed nothing is a fair reading.
    assert by_name(turn(status="blocked", usage=ZERO))["usage_reported"]["value"] == checks.NA


def test_all_zero_usage_is_unknown_cost_not_free():
    assert pricing.turn_cost("antigravity", ZERO) == (None, None)
    assert pricing.turn_cost("claude", {**ZERO, "total_cost_usd": 0.0}) == (0.0, "provider")
    assert pricing.turn_cost("claude", USAGE)[0] > 0


def test_attribution_keeps_environment_failures_off_the_agent():
    network = turn(status="blocked", error="[claude] exit_code=1 API Error: Connection dropped (ECONNRESET)")
    assert (network.failure_class, network.attribution) == ("network", "environment")
    stopped = turn(status="blocked", error="x", failure_code="stopped")
    assert stopped.attribution == "user"
    vague = turn(status="blocked", error="[claude] exit_code=1")
    assert (vague.failure_class, vague.attribution) == ("unknown", "agent")
    assert turn().attribution is None


def test_health_flags_checks_that_cannot_fail_or_cannot_see():
    assert checks.health(passed=30, failed=0, na=10) is not None
    assert checks.health(passed=30, failed=2, na=10) is None
    assert checks.health(passed=2, failed=0, na=98) is not None   # blind
    assert checks.health(passed=5, failed=0, na=5) is None        # too little data to say


def test_summary_rows_carry_layer_and_health():
    turns = [turn(usage=USAGE, reply="好") for _ in range(25)]
    for t in turns:
        t.checks = checks.run_checks(t)
    rows = {r["name"]: r for r in checks.summarize(turns)["checks"]}
    assert rows["turn_closed"]["layer"] == "trace"
    assert rows["turn_closed"]["health"]  # 25 passes, never a failure
    assert rows["claims_match_events"]["layer"] == "agent"
