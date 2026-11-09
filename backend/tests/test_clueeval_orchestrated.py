"""Chain tests for orchestrated clueeval games: no CLI, no network, no model.

The fake CLI adapter plays one seat turn: it publishes a chat message via the
pilot MCP tools surface and emits a turn.usage event, exactly what the real
isolated CLIs do through the hub. These tests verify the wiring (mission,
scheduling, message extraction, billing, isolation), not model behavior.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.adapters.base import TurnOutcome
from backend.app.domain.models import TurnStatus
from backend.app.evaluation import clueeval as C
from backend.app.evaluation import clueeval_hub as H
from backend.app.evaluation.real_pilot import ROOT
from backend.app.mcp.tools import MCPToolError
from backend.app.settings import Settings
from backend.app.storage.repository import Repository

PROTOCOL_MODELS = json.loads((ROOT / "eval/pilots/cost-aware-v1/protocol.json").read_text())["models"]
# Deterministic per-turn cost: 1000 in + 100 out at these rates = $0.0012 per call.
FAKE_PRICES = {"input_usd_per_million": 1.0, "output_usd_per_million": 2.0}
QUESTIONS = C.load_questions(ROOT / "eval/clueeval/questions.json")


class FakeCLI:
    """Stands in for IsolatedCLI: publishes and bills, records the prompt."""

    calls = []  # shared across instances; reset by the harness factory

    def __init__(self, *args, **kwargs):
        pass

    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        FakeCLI.calls.append({"mission_id": mission_id, "agent_id": agent_id, "prompt": prompt})
        content = f"结论：{QUESTIONS['q1-storage']['options'][0]}（{agent_id} 的专属材料支持）"
        tools.publish_message(mission_id, agent_id, turn_id, content, provider_id)
        tools.repository.add_event(mission_id, "turn.usage", {
            "agent_id": agent_id, "provider_id": provider_id, "turn_id": turn_id,
            "usage": {"input_tokens": 1000, "output_tokens": 100}})
        return TurnOutcome(status=TurnStatus.COMPLETED)


class ProbeCLI(FakeCLI):
    """Publishes a phase marker plus its own private card, and records what the hub showed it."""

    seen = []

    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        history = tools.read_context(mission_id, limit=100)["messages"]
        ProbeCLI.seen.append({"agent_id": agent_id, "prompt": prompt, "mission_id": mission_id,
                              "read": "\n".join(m["content"] for m in history)})
        phase = next(p for p in ("pre", "final", "discuss") if f"PHASE={p}" in prompt)
        card = next(c["text"] for c in QUESTIONS["q1-storage"]["cards"] if c["text"] in prompt)
        option = QUESTIONS["q1-storage"]["options"][0]
        tools.publish_message(mission_id, agent_id, turn_id, f"[{agent_id}/{phase}] {card} 结论：{option}", provider_id)
        tools.repository.add_event(mission_id, "turn.usage", {
            "agent_id": agent_id, "provider_id": provider_id, "turn_id": turn_id,
            "usage": {"input_tokens": 1000, "output_tokens": 100}})
        return TurnOutcome(status=TurnStatus.COMPLETED)


class PreOnlyCLI(FakeCLI):
    """Publishes on each seat's first call only; later turns finish silently but still bill."""

    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        if any(call["agent_id"] == agent_id for call in FakeCLI.calls):
            FakeCLI.calls.append({"mission_id": mission_id, "agent_id": agent_id, "prompt": prompt})
            tools.repository.add_event(mission_id, "turn.usage", {
                "agent_id": agent_id, "provider_id": provider_id, "turn_id": turn_id,
                "usage": {"input_tokens": 1000, "output_tokens": 100}})
            return TurnOutcome(status=TurnStatus.COMPLETED)
        return super().run_turn(mission_id, agent_id, turn_id, provider_id, prompt, tools)


class SilentCLI(FakeCLI):
    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        FakeCLI.calls.append({"mission_id": mission_id, "agent_id": agent_id, "prompt": prompt})
        return TurnOutcome(status=TurnStatus.COMPLETED)  # completes without publishing


def fake_models(seats):
    return {seat.role: {**FAKE_PRICES, "provider": seat.provider, "model_id": "fake-model",
                        "cli_model_id": "fake-model", "reasoning_effort": seat.effort or "low"}
            for seat in seats}


@pytest.fixture
def harness(tmp_path):
    def build(seats, *, limit_usd=10.0, factory_cli=FakeCLI):
        settings = Settings(database_path=tmp_path / "hub" / "eval.sqlite3",
                            artifact_root=tmp_path / "hub" / "artifacts",
                            config_dir=ROOT / "config", use_fake_cli=False, max_turns=1,
                            max_a2a_invocations=0, cli_timeout=60)
        settings.artifact_root.mkdir(parents=True)
        repository = Repository(settings.database_path)
        repository.initialize()
        ledger = H.HubLedger(tmp_path, fake_models(seats), limit_usd)
        FakeCLI.calls = []
        return ledger, lambda q, gid, table: H.OrchestratedGame(
            q, gid, table, settings=settings, repository=repository, ledger=ledger,
            factory=lambda *a, **k: factory_cli())
    return build


def seat_table(n=2):
    return [C.Seat(i, role, "codex", "gpt-6.1-sol", "low")
            for i, role in enumerate(C.DEFAULT_ROSTER[:n])]


def harness_calls(tmp_path):
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


def test_full_game_through_hub(harness, tmp_path):
    q = QUESTIONS["q1-storage"]
    n = len(q["cards"])
    seats = seat_table(n)
    ledger, make = harness(seats)
    game = make(q, "q1-storage-private-g0", seats)
    writer = C.GameWriter(tmp_path)
    record = C.run_game(q, 0, "private", 1, seats, game.respond, writer)

    assert record["valid"], record["invalid_reason"]
    # pre(n) + rounds*n discussion + final(n) host calls, every seat served.
    assert len(FakeCLI.calls) == n * 3
    assert {call["agent_id"] for call in FakeCLI.calls} == {seat.role for seat in seats}
    # Published chat messages are the answer text; picks parse; game stays valid.
    assert record["transcript"] and all("的专属材料支持" in entry["text"] for entry in record["transcript"])
    assert all(seat["pre_pick"] and seat["final_pick"] for seat in record["seats"])
    # The harness's calls.jsonl carries the hub view per call (mission linkage).
    rows = harness_calls(tmp_path)
    assert len(rows) == n * 3
    # Discussion turns share the game mission; each pre/final answer has its own (#151).
    assert all((row["meta"]["hub"]["mission_id"] == game.mission.id) == (row["phase"] == "discuss") for row in rows)
    assert len({row["meta"]["hub"]["mission_id"] for row in rows if row["phase"] != "discuss"}) == n * 2
    # The manifest wording says the same as the layout just observed (#154).
    assert "isolated" in H.SERIALIZED_NOTE and "on one mission" not in H.SERIALIZED_NOTE
    assert "serialized_phases\"] = H.SERIALIZED_NOTE" in (ROOT / "scripts" / "run_clueeval.py").read_text()
    assert all(row["valid"] for row in rows)
    # Billing: one priced usage row per host call, on the hub ledger file only.
    hub_rows = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    assert len(hub_rows) == n * 3
    from decimal import Decimal
    assert ledger.spent == Decimal("0.0012") * (n * 3)
    assert ledger.path.name == "hub_calls.jsonl"


def test_delivered_prompts_carry_the_audited_context_and_no_foreign_cards(harness, tmp_path):
    q = QUESTIONS["q1-storage"]
    n = len(q["cards"])
    seats = seat_table(n)
    ledger, make = harness(seats)
    game = make(q, "iso-g0", seats)
    card_texts = [card["text"] for card in q["cards"]]
    seen = {}

    def spying(seat, phase, prompt):
        seen[seat.role] = prompt
        return game.respond(seat, phase, prompt)

    record = C.run_game(q, 0, "private", 1, seats, spying, C.GameWriter(tmp_path))
    assert record["valid"]
    delivered = {call["agent_id"]: call["prompt"] for call in FakeCLI.calls}
    for seat in seats:
        # The hub delivers the harness's audited prompt for this seat verbatim...
        assert seen[seat.role] in delivered[seat.role]
        own = [text for text in card_texts if text in seen[seat.role]]
        # ...which carries exactly this seat's private card, never the others'.
        assert len(own) == 1
        for text in card_texts:
            if text != own[0]:
                assert text not in delivered[seat.role]
    # The mission lives in the run's own database, not any service path.
    assert str(game.settings.database_path).endswith("hub/eval.sqlite3")


def test_spend_gate_voids_game_without_further_calls(harness, tmp_path):
    q = QUESTIONS["q1-storage"]
    seats = seat_table(len(q["cards"]))
    # A call costs $0.0012; the pre-call reservation (36000 in / 4500 out) is
    # $0.045, so a $0.046 limit admits exactly one call and then stops the gate.
    ledger, make = harness(seats, limit_usd=0.046)
    game = make(q, "gate-g0", seats)
    record = C.run_game(q, 0, "private", 1, seats, game.respond, C.GameWriter(tmp_path))

    assert not record["valid"]
    assert record["invalid_reason"]
    assert len(FakeCLI.calls) == 1
    assert ledger.stop_reason
    rows = harness_calls(tmp_path)
    assert any(row["valid"] is False for row in rows)


def test_unpublished_turn_is_invalid(harness, tmp_path):
    q = QUESTIONS["q1-storage"]
    seats = seat_table(len(q["cards"]))
    ledger, make = harness(seats, factory_cli=SilentCLI)
    game = make(q, "silent-g0", seats)
    record = C.run_game(q, 0, "private", 1, seats, game.respond, C.GameWriter(tmp_path))

    assert not record["valid"]
    rows = harness_calls(tmp_path)
    assert rows and rows[0]["valid"] is False


def test_claude_seat_rejected(harness):
    seats = [C.Seat(0, "chulan", "claude", "claude-x", "")]
    _, make = harness(seats)
    with pytest.raises(ValueError, match="codex/antigravity"):
        make(QUESTIONS["q1-storage"], "x", seats)


def test_seat_models_follow_routed_effort():
    seats = [C.Seat(0, "chulan", "codex", "gpt-6.1-sol", "high"),
             C.Seat(1, "baobao", "antigravity", "gemini-3.8-flash-low", "")]
    models = H.seat_models(seats, PROTOCOL_MODELS)
    assert models["chulan"]["reasoning_effort"] == "high"
    assert models["baobao"]["input_usd_per_million"] > 0  # frozen rate carried over
    # Routed empty stays empty, so the argv sets no effort, like the direct batch (#153).
    assert models["baobao"]["reasoning_effort"] == ""
    with pytest.raises(ValueError, match="no pricing"):
        H.seat_models([C.Seat(0, "chulan", "codex", "unknown-model", "")], PROTOCOL_MODELS)


def _tagged(respond):
    """Marks each prompt with its phase so the probe CLI can label what it publishes."""
    return lambda seat, phase, prompt: respond(seat, phase, f"{prompt}\nPHASE={phase}")


def test_independent_answers_see_no_other_seat_in_prompt_or_read_context(harness, tmp_path):
    # #151: pre/final ran on the shared mission, so the second seat's pre saw the first seat's full answer
    # (and its card) both in the runner prompt and through read_context.
    q = QUESTIONS["q1-storage"]
    seats = seat_table(len(q["cards"]))
    ledger, make = harness(seats, factory_cli=ProbeCLI)
    game = make(q, "frozen-g0", seats)
    ProbeCLI.seen = []
    record = C.run_game(q, 0, "private", 1, seats, _tagged(game.respond), C.GameWriter(tmp_path))
    assert record["valid"], record["invalid_reason"]
    assert len(ProbeCLI.seen) == len(seats) * 3
    cards = {c["text"] for c in q["cards"]}
    own_card = {seat.role: card[0]["text"] for seat, card in zip(seats, C.seat_cards(q, 0, "private"))}
    for seen in ProbeCLI.seen:
        role, phase = seen["agent_id"], next(p for p in ("pre", "final", "discuss") if f"PHASE={p}" in seen["prompt"])
        visible = seen["prompt"] + "\n" + seen["read"]
        # No pre or final answer of another seat is ever visible, in any phase.
        for other in (s.role for s in seats if s.role != role):
            assert f"[{other}/pre]" not in visible and f"[{other}/final]" not in visible
        if phase == "pre":
            # Before discussion no foreign card reaches the seat at all, and the hub history is just the goal.
            assert not [c for c in cards - {own_card[role]} if c in visible]
            assert seen["read"] == game.goal
        else:
            assert f"[{role}/pre]" not in visible  # nor is the seat's own pre replayed
        assert (seen["mission_id"] == game.mission.id) == (phase == "discuss")
    discuss = [s for s in ProbeCLI.seen if "PHASE=discuss" in s["prompt"]]
    # Discussion stays shared: the last speaker reads the earlier speakers' discussion messages.
    assert all(f"[{s['agent_id']}/discuss]" in discuss[-1]["read"] for s in discuss[:-1])


def test_silent_later_turns_do_not_reuse_an_earlier_answer(harness, tmp_path):
    # #152: the answer was the seat's last message anywhere in the mission, so turns that billed usage but
    # published nothing replayed the pre answer and the whole game stayed valid.
    q = QUESTIONS["q1-storage"]
    seats = seat_table(len(q["cards"]))
    ledger, make = harness(seats, factory_cli=PreOnlyCLI)
    game = make(q, "replay-g0", seats)
    record = C.run_game(q, 0, "private", 1, seats, game.respond, C.GameWriter(tmp_path))
    assert not record["valid"]
    rows = harness_calls(tmp_path)
    assert [r["phase"] for r in rows if r["valid"]] == ["pre"] * len(seats)
    silent = [r for r in rows if not r["valid"]]
    assert len(silent) == 1 and silent[0]["phase"] == "discuss" and silent[0]["text"] is None
    assert silent[0]["invalid_reasons"] == ["no message published in this call's own turns"]


def test_answer_is_bound_to_its_own_turn_on_the_shared_mission(harness, tmp_path):
    # #152, same-mission case: a discussion turn that bills but stays silent must not reuse the seat's previous
    # discussion message on the shared mission.
    q = QUESTIONS["q1-storage"]
    seats = seat_table(len(q["cards"]))
    ledger, make = harness(seats)
    game = make(q, "rounds-g0", seats)
    seat = seats[0]
    first = game.respond(seat, "discuss", "第一轮")
    assert first["valid"] and first["meta"]["hub"]["turn_id"] in first["meta"]["hub"]["own_turn_ids"]
    game.factory = lambda *a, **k: PreOnlyCLI()
    second = game.respond(seat, "discuss", "第二轮")
    assert second["valid"] is False and second["text"] is None
    assert len(ledger.calls) == 2  # the silent turn was still billed


class RelayCLI(FakeCLI):
    """Scripted A2A: chulan's root turn invokes baobao; what each turn publishes is set per test."""

    script = {}
    refused = []

    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        FakeCLI.calls.append({"mission_id": mission_id, "agent_id": agent_id, "prompt": prompt})
        action = RelayCLI.script.get((agent_id, sum(c["agent_id"] == agent_id for c in FakeCLI.calls)))
        if action and action.get("invoke"):
            try:  # a real CLI gets a refused tool call back as a tool error and goes on
                tools.invoke_agent(mission_id, agent_id, turn_id, action["invoke"], "补一条", provider_id,
                                   mode=action.get("mode", "fire_and_forget"))
            except MCPToolError as exc:
                RelayCLI.refused.append(str(exc))
        if action and action.get("say"):
            tools.publish_message(mission_id, agent_id, turn_id, action["say"], provider_id)
        tools.repository.add_event(mission_id, "turn.usage", {
            "agent_id": agent_id, "provider_id": provider_id, "turn_id": turn_id,
            "usage": {"input_tokens": 1000, "output_tokens": 100}})
        return TurnOutcome(status=TurnStatus.COMPLETED)


def _relay_game(harness, tmp_path, script):
    q = QUESTIONS["q1-storage"]
    seats = seat_table(len(q["cards"]))
    ledger, make = harness(seats, factory_cli=RelayCLI)
    RelayCLI.script, RelayCLI.refused = script, []
    game = make(q, "relay-g0", seats)
    game.a2a_budget = 1
    return game, seats


def test_a_reply_given_as_callee_is_not_the_seats_own_answer(harness, tmp_path):
    # #152 A2A attribution: baobao spoke only as chulan's invitee; its own later call is silent and must not
    # pick up that reply.
    game, seats = _relay_game(harness, tmp_path, {
        ("chulan", 1): {"invoke": "baobao", "say": "chulan 发言"},
        ("baobao", 1): {"say": "baobao 作为被叫的回复"}})
    chulan, baobao = seats[0], seats[1]
    assert chulan.role == "chulan" and baobao.role == "baobao"
    first = game.respond(chulan, "discuss", "第一轮")
    assert first["valid"] and first["text"] == "chulan 发言"
    assert [c["agent_id"] for c in FakeCLI.calls] == ["chulan", "baobao"]
    second = game.respond(baobao, "discuss", "第一轮")
    assert second["valid"] is False and second["text"] is None


def test_a_wait_reply_resume_is_the_seats_answer(harness, tmp_path):
    # The inviting seat's resume turn belongs to its own call: its message there is the answer.
    game, seats = _relay_game(harness, tmp_path, {
        ("chulan", 1): {"invoke": "baobao", "mode": "wait_reply"},
        ("baobao", 1): {"say": "baobao 的回复"},
        ("chulan", 2): {"say": "chulan 收到回复后的发言"}})
    result = game.respond(seats[0], "discuss", "第一轮")
    assert [c["agent_id"] for c in FakeCLI.calls] == ["chulan", "baobao", "chulan"]
    assert result["valid"] and result["text"] == "chulan 收到回复后的发言"
    hub = result["meta"]["hub"]
    assert len(hub["own_turn_ids"]) == 2 and hub["turn_id"] == hub["own_turn_ids"][-1]
    assert hub["usage"] == {"input_tokens": 1000, "output_tokens": 100}


def test_independent_phases_cannot_relay(harness, tmp_path):
    # An invoke from a pre turn would hand the answer to another seat before discussion: A2A is off there.
    game, seats = _relay_game(harness, tmp_path, {("chulan", 1): {"invoke": "baobao", "say": "chulan 初答"}})
    result = game.respond(seats[0], "pre", "初答")
    assert result["valid"], result["invalid_reasons"]
    assert RelayCLI.refused and result["text"] == "chulan 初答"
    assert [c["agent_id"] for c in FakeCLI.calls] == ["chulan"]


def test_cli_argv_carries_the_routed_effort(harness, tmp_path):
    # #153: seat_models recorded high, but the codex argv was hard-coded to low.
    from backend.app.evaluation.real_pilot import model_args
    seats = [C.Seat(0, "chulan", "codex", "gpt-6.1-sol", "high"),
             C.Seat(1, "baobao", "codex", "gpt-6.1-sol", ""),
             C.Seat(2, "wangye", "antigravity", "gemini-3.8-flash-low", "medium")]
    models = H.seat_models(seats, PROTOCOL_MODELS)
    codex_high = model_args(models["chulan"])[1]
    assert 'model_reasoning_effort="high"' in codex_high and 'model_reasoning_effort="low"' not in codex_high
    # No routed effort: no flag, the CLI default, as in the direct batch's argv.
    assert not any("model_reasoning_effort" in arg for arg in model_args(models["baobao"])[1])
    agy = model_args(models["wangye"])[1]
    assert agy[agy.index("--effort") + 1] == "medium"
    # And that argv is what the hub adapter hands the CLI.
    built = []

    class Capture(FakeCLI):
        def __init__(self, command, args, **kwargs):
            built.append([command, *args])

    ledger, make = harness(seats[:1])
    ledger.models.update(models)
    game = make(QUESTIONS["q1-storage"], "effort-g0", seats[:1])
    game.factory = Capture
    assert game.respond(seats[0], "solo", "单人作答")["valid"]
    assert built and 'model_reasoning_effort="high"' in built[0]
