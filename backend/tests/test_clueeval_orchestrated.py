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
    assert all(row["meta"]["hub"]["mission_id"] == game.mission.id for row in rows)
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
    assert models["baobao"]["reasoning_effort"]  # routed empty falls back to the table's
    with pytest.raises(ValueError, match="no pricing"):
        H.seat_models([C.Seat(0, "chulan", "codex", "unknown-model", "")], PROTOCOL_MODELS)
