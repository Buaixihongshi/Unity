"""ClueEval games driven through Unity's own orchestration.

Each seat call from the clueeval game loop becomes one agent turn of a
per-game mission: the MissionRunner scheduling engine runs the turn, the
seat's CLI runs closed-book with only the pilot MCP tools (publish_message,
log_thought, read_context, invoke_agent) against a per-run eval.sqlite3,
and the agent's published chat message is the turn's answer text.

Prompts, seating, card rotation, transcript and scoring stay with the
clueeval harness, so versus the direct round-robin batch the changed
variable is the execution medium itself: Unity personas and host rules, a
shared hub conversation, recorded turns and usage, and (with an A2A budget)
relays. What this layer does NOT change: the phase prompts (the harness
transcript is still embedded, so the hub conversation is a duplicate record,
not the only channel), and the game's validity rules.

Claude seats are rejected: the isolated MCP subprocess machinery (real_pilot)
only drives codex and antigravity today.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from backend.app.domain.agent_registry import AgentRegistry
from backend.app.domain.models import (
    AgentProfile,
    Conversation,
    Message,
    MessageRole,
    Mission,
    MissionStatus,
    OrchestrationConfig,
    OrchestrationMode,
)
from backend.app.evaluation.real_pilot import BudgetedAdapter, IsolatedCLI, Ledger, ROOT
from backend.app.orchestration.runner import MissionRunner
from backend.app.orchestration.speaker import WorkflowStep
from backend.app.orchestration.strategies import OrchestrationPlan
from backend.app.settings import Settings
from backend.app.storage.repository import Repository

HARNESS_LAYER = "unity-orchestrated"

# Same closed-book framing as the direct batch's manifest: the seats discuss,
# they do not see the answer key, and this note says what changed for readers
# of a run's calls.jsonl.
LAYER_NOTE = (
    "Seats execute inside Unity orchestration (per-game mission, hub MCP tools, "
    "shared conversation, recorded turns); prompts and scoring are unchanged "
    "from the direct round-robin batch."
)


class HubLedger(Ledger):
    """Ledger that does not collide with the game harness's calls.jsonl."""

    def __init__(self, output, models, limit_usd):
        super().__init__(output, models, limit_usd)
        self.path = Path(output) / "hub_calls.jsonl"


class GameRunner(MissionRunner):
    """One planned step per harness call: the current seat's phase turn.

    The harness (not the host) owns the game order, so the plan is exactly the
    call being made. A2A stays available only through a positive budget; the
    runner then charges it per accepted invoke like any mission.
    """

    def __init__(self, *args, current_step: WorkflowStep, participants, a2a_budget: int, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_step = current_step
        self._participants = list(participants)
        self._a2a_budget = a2a_budget

    def _plan_orchestration(self, mission: Mission) -> OrchestrationPlan:
        mode = OrchestrationMode.DYNAMIC if self._a2a_budget > 0 else OrchestrationMode.FIXED
        return OrchestrationPlan(mode, [self._current_step], self._participants,
                                 self._a2a_budget, 1)


class OrchestratedGame:
    """One clueeval game as one mission; respond() is the Responder contract.

    run_game may call pre/final seats from threads; turns on one mission are
    serialized (the direct batch ran those phases in parallel — wall time is
    the only difference, and it is recorded in the manifest).
    """

    def __init__(self, q: dict, gid: str, seats, *, settings: Settings,
                 repository: Repository, ledger: HubLedger, a2a_budget: int = 0, factory=None):
        for seat in seats:
            if seat.provider not in ("codex", "antigravity"):
                raise ValueError(
                    f"seat {seat.role}: orchestrated mode drives codex/antigravity, got {seat.provider}")
        self.gid, self.seats = gid, seats
        self.repository, self.settings, self.ledger = repository, settings, ledger
        self.a2a_budget, self.factory = a2a_budget, factory
        self._lock = threading.Lock()
        self._stopped = ""
        conversation = Conversation(id=f"c_clue_{uuid4().hex[:12]}", title=f"CLUEEVAL {gid}")
        repository.create_conversation(conversation)
        goal = (f"隐藏资料讨论局 {gid}。每轮指令包含你的专属材料与当前讨论记录；"
                "按指令发言，最终以题面要求的格式给出选择。")
        self.mission = Mission(
            id=f"m_clue_{uuid4().hex[:12]}", title=f"CLUEEVAL {gid}", goal=goal,
            conversation_id=conversation.id,
            orchestration=OrchestrationConfig(
                mode=OrchestrationMode.DYNAMIC if a2a_budget > 0 else OrchestrationMode.FIXED,
                participant_ids=[seat.role for seat in seats]),
        )
        repository.create_mission(self.mission)
        repository.add_message(Message(id=f"msg_{uuid4().hex[:12]}", mission_id=self.mission.id,
                                       role=MessageRole.USER, content=goal,
                                       conversation_id=conversation.id))
        profiles = [AgentProfile(id=seat.role, display_name=seat.role, role="中性任务参与者",
                                 persona="基于可见材料完成任务，不虚构证据，不泄露本轮指令外的内容。",
                                 default_provider_id=seat.provider, fallback_provider_ids=[])
                    for seat in seats]
        self.registry = AgentRegistry(profiles)
        self.condition = {"id": gid, "mapping": {seat.role: self._model_key(seat) for seat in seats}}

    def _model_key(self, seat) -> str:
        if seat.role not in self.ledger.models:
            known = ", ".join(sorted(self.ledger.models)) or "<none>"
            raise ValueError(f"seat {seat.role}: no billing entry; known: {known}")
        return seat.role

    def respond(self, seat, phase: str, prompt: str) -> dict:
        with self._lock:
            if self._stopped:
                return self._invalid([f"game stopped: {self._stopped}"], None, 0.0)
            adapter = BudgetedAdapter(SimpleNamespace(settings=self.settings), self.ledger,
                                      self.condition, factory=self.factory or IsolatedCLI)
            step = WorkflowStep(seat.role, prompt)
            runner = GameRunner(self.repository, self.settings, adapter, agent_registry=self.registry,
                                current_step=step, participants=[s.role for s in self.seats],
                                a2a_budget=self.a2a_budget)
            started = time.time()
            try:
                mission = runner.run(self.mission.id)
            except Exception as exc:  # adapter/ledger failures void the call, the game, and the batch
                self._stopped = f"runner raised {type(exc).__name__}"
                return self._invalid([f"runner raised {type(exc).__name__}"], None,
                                     round(time.time() - started, 1))
            seconds = round(time.time() - started, 1)
            if self.ledger.stop_reason:
                self._stopped = self.ledger.stop_reason
            if mission is None or mission.status != MissionStatus.COMPLETED:
                reason = self._stopped or f"mission status {mission.status if mission else 'missing'}"
                return self._invalid([reason], None, seconds)
            messages = [m for m in self.repository.list_messages(self.mission.id)
                        if m.role == MessageRole.AGENT and m.agent_id == seat.role]
            text = messages[-1].content if messages else None
            if not text:
                return self._invalid(["empty response"], None, seconds)
            call = self.ledger.calls[-1] if self.ledger.calls else {}
            meta = {"hub": {"mission_id": self.mission.id, "phase": phase,
                            "turn_id": call.get("turn_id"), "usage": call.get("usage"),
                            "native_tools": call.get("native_tools"),
                            "transport_schema_reads": call.get("transport_schema_reads"),
                            "prohibited_native_tools": call.get("prohibited_native_tools"),
                            "api_equivalent_usd": call.get("api_equivalent_usd")}}
            return {"text": text, "rc": 0, "seconds": seconds, "meta": meta,
                    "valid": True, "invalid_reasons": []}

    def _invalid(self, reasons, rc, seconds: float) -> dict:
        return {"text": None, "rc": rc, "seconds": seconds, "meta": {"hub": {"mission_id": self.mission.id}},
                "valid": False, "invalid_reasons": reasons}


def seat_models(seats, protocol_models: dict) -> dict:
    """Per-role billing entries for the spend gate, keyed by seat role.

    Rates come from the frozen cost-aware-v1 table; the CLI effort follows the
    routed seat, not the table. An unknown seat model is an error, not a
    silent zero: the spend gate is only honest with real rates.
    """
    table = {f"{m['provider']}:{m['cli_model_id']}": m for m in protocol_models.values()
             if m.get("provider") in ("codex", "antigravity")}
    models = {}
    for seat in seats:
        entry = table.get(f"{seat.provider}:{seat.model}")
        if entry is None:
            known = ", ".join(sorted(table)) or "<none>"
            raise ValueError(f"seat {seat.role}: no pricing for {seat.provider}:{seat.model}; known: {known}")
        models[seat.role] = {**entry, "reasoning_effort": seat.effort or entry.get("reasoning_effort") or "low"}
    return models
