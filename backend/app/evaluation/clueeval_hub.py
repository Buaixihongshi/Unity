"""ClueEval games driven through Unity's own orchestration.

Each seat call from the clueeval game loop becomes one agent turn of a
per-game mission: the MissionRunner scheduling engine runs the turn, the
seat's CLI runs closed-book with only the pilot MCP tools (publish_message,
log_thought, read_context, invoke_agent) against a per-run eval.sqlite3,
and the agent's published chat message is the turn's answer text.

Visible history is frozen per phase, as in the direct batch (#151): only the
discussion turns share the game's mission and conversation; every pre, final
and solo answer runs in a mission and conversation of its own, A2A off, so
neither the turn prompt nor read_context shows another seat's independent
answer. The answer is the last message the seat published in this call's own
root turn or its resumes, never an earlier turn's (#152).

Prompts, seating, card rotation, transcript and scoring stay with the
clueeval harness, so versus the direct round-robin batch the changed
variable is the execution medium itself: Unity personas and host rules, a
shared hub conversation, recorded turns and usage, and (with an A2A budget)
relays. What this layer does NOT change: the phase prompts (the harness
transcript is still embedded, so the hub conversation is a duplicate record,
not the only channel), and the game's validity rules.

Seats run on their daily routes for claude, codex and antigravity alike; each
CLI's closed-book setup and its checks live in real_pilot.
"""
from __future__ import annotations

import json
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

# CLIs real_pilot can run closed-book behind the hub.
SEAT_PROVIDERS = ("claude", "codex", "antigravity")

# Rates for daily routes the frozen cost-aware-v1 table lacks (claude, gemini -high).
PRICING_PATH = ROOT / "eval/clueeval/pricing.json"

# The only phase whose turns see each other in the hub; pre/final/solo are
# independent answers and get an isolated mission each (#151).
SHARED_PHASES = frozenset({"discuss"})

# Same closed-book framing as the direct batch's manifest: the seats discuss,
# they do not see the answer key, and this note says what changed for readers
# of a run's calls.jsonl.
LAYER_NOTE = (
    "Seats execute inside Unity orchestration (hub MCP tools, recorded turns; "
    "discussion turns share one per-game mission, each pre/final answer runs in "
    "its own isolated mission); prompts and scoring are unchanged from the "
    "direct round-robin batch."
)

# Manifest wording for the phase layout; kept beside SHARED_PHASES so the
# evidence text cannot drift from what respond() actually does (#154).
SERIALIZED_NOTE = (
    "pre/final turns run one at a time, each in its own isolated one-seat mission "
    "(A2A off); only discussion turns share the per-game mission. "
    "Direct mode ran pre/final in parallel."
)


def _mode(participants, a2a_budget: int) -> OrchestrationMode:
    """An isolated answer is a one-seat mission; a table with relays on is dynamic."""
    if len(participants) == 1:
        return OrchestrationMode.SINGLE
    return OrchestrationMode.DYNAMIC if a2a_budget > 0 else OrchestrationMode.FIXED


class HubLedger(Ledger):
    """Ledger that does not collide with the game harness's calls.jsonl."""

    def __init__(self, output, models, limit_usd):
        super().__init__(output, models, limit_usd)
        self.path = Path(output) / "hub_calls.jsonl"


class HubBudgetedAdapter(BudgetedAdapter):
    """Seat turns keep the daily idle rule: none on a seat's own turn, the A2A window on a relay.

    The pilot's flat 45 s cut killed opus at max effort mid-thought: thinking
    deltas do not count as activity, and such a turn can think longer than that.
    The hard timeout (--cli-timeout) still bounds every call.
    """

    def idle_timeout(self, tools):
        if getattr(tools, "current_step_origin", None) in {"a2a", "a2a_resume"}:
            return self.runner.settings.a2a_idle_timeout
        return 0


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
        return OrchestrationPlan(_mode(self._participants, self._a2a_budget), [self._current_step], self._participants,
                                 self._a2a_budget, 1)


class OrchestratedGame:
    """One clueeval game as one mission; respond() is the Responder contract.

    run_game may call pre/final seats from threads; calls are serialized
    (the direct batch ran those phases in parallel — wall time is the only
    difference, and it is recorded in the manifest). Serialization does not
    leak answers: independent phases never share a conversation.
    """

    def __init__(self, q: dict, gid: str, seats, *, settings: Settings,
                 repository: Repository, ledger: HubLedger, a2a_budget: int = 0, factory=None):
        for seat in seats:
            if seat.provider not in SEAT_PROVIDERS:
                raise ValueError(
                    f"seat {seat.role}: orchestrated mode drives {'/'.join(SEAT_PROVIDERS)}, got {seat.provider}")
        self.gid, self.seats = gid, seats
        self.repository, self.settings, self.ledger = repository, settings, ledger
        self.a2a_budget, self.factory = a2a_budget, factory
        self._lock = threading.Lock()
        self._stopped = ""
        self.goal = (f"隐藏资料讨论局 {gid}。每轮指令包含你的专属材料与当前讨论记录；"
                     "按指令发言，最终以题面要求的格式给出选择。")
        self.mission = self._new_mission(f"CLUEEVAL {gid}", [seat.role for seat in seats], a2a_budget)
        self.isolated = {}  # (role, phase) -> mission of that independent answer
        profiles = [AgentProfile(id=seat.role, display_name=seat.role, role="中性任务参与者",
                                 persona="基于可见材料完成任务，不虚构证据，不泄露本轮指令外的内容。",
                                 default_provider_id=seat.provider, fallback_provider_ids=[])
                    for seat in seats]
        self.registry = AgentRegistry(profiles)
        self.condition = {"id": gid, "mapping": {seat.role: self._model_key(seat) for seat in seats}}

    def _new_mission(self, title: str, participants, a2a_budget: int) -> Mission:
        conversation = Conversation(id=f"c_clue_{uuid4().hex[:12]}", title=title)
        self.repository.create_conversation(conversation)
        mission = Mission(
            id=f"m_clue_{uuid4().hex[:12]}", title=title, goal=self.goal, conversation_id=conversation.id,
            orchestration=OrchestrationConfig(
                mode=_mode(participants, a2a_budget), participant_ids=list(participants)),
        )
        self.repository.create_mission(mission)
        self.repository.add_message(Message(id=f"msg_{uuid4().hex[:12]}", mission_id=mission.id,
                                            role=MessageRole.USER, content=self.goal,
                                            conversation_id=conversation.id))
        return mission

    def _mission_for(self, seat, phase: str):
        """The discussion mission, or a fresh isolated one per independent answer, and its A2A budget."""
        if phase in SHARED_PHASES:
            return self.mission, [s.role for s in self.seats], self.a2a_budget
        key = (seat.role, phase)
        if key in self.isolated:
            raise ValueError(f"seat {seat.role}: {phase} answered twice in one game")
        self.isolated[key] = self._new_mission(f"CLUEEVAL {self.gid} {seat.role} {phase}", [seat.role], 0)
        return self.isolated[key], [seat.role], 0

    def _own_turns(self, mission_id: str, role: str, after_id: int) -> list:
        """This call's root turn for `role`, then its resumes, in start order.

        A resume runs as the inviting agent with the inviting turn as sender;
        an A2A callee turn has another sender, so a reply the seat gave when
        some other seat invoked it is not this call's answer.
        """
        own = []
        for event in self.repository.list_events_after(mission_id, after_id):
            if event.event_type != "turn.started" or event.payload.get("agent_id") != role:
                continue
            turn_id, sender = event.payload.get("turn_id"), event.payload.get("a2a_sender_turn_id")
            if turn_id in own:
                continue  # a fallback attempt of a turn already counted
            if (sender is None and not own) or (sender is not None and sender in own):
                own.append(turn_id)
        return own

    def _last_event_id(self, mission_id: str) -> int:
        events = self.repository.list_events_after(mission_id, 0)
        return int(events[-1].id) if events else 0

    def _model_key(self, seat) -> str:
        if seat.role not in self.ledger.models:
            known = ", ".join(sorted(self.ledger.models)) or "<none>"
            raise ValueError(f"seat {seat.role}: no billing entry; known: {known}")
        return seat.role

    def respond(self, seat, phase: str, prompt: str) -> dict:
        with self._lock:
            if self._stopped:
                return self._invalid([f"game stopped: {self._stopped}"], None, 0.0)
            adapter = HubBudgetedAdapter(SimpleNamespace(settings=self.settings), self.ledger,
                                         self.condition, factory=self.factory or IsolatedCLI)
            step = WorkflowStep(seat.role, prompt)
            target, participants, a2a_budget = self._mission_for(seat, phase)
            runner = GameRunner(self.repository, self.settings, adapter, agent_registry=self.registry,
                                current_step=step, participants=participants, a2a_budget=a2a_budget)
            after_id = self._last_event_id(target.id)
            started = time.time()
            try:
                mission = runner.run(target.id)
            except Exception as exc:  # adapter/ledger failures void the call, the game, and the batch
                self._stopped = f"runner raised {type(exc).__name__}"
                return self._invalid([f"runner raised {type(exc).__name__}"], None,
                                     round(time.time() - started, 1), target.id)
            seconds = round(time.time() - started, 1)
            if self.ledger.stop_reason:
                self._stopped = self.ledger.stop_reason
            if mission is None or mission.status != MissionStatus.COMPLETED:
                reason = self._stopped or f"mission status {mission.status if mission else 'missing'}"
                return self._invalid([reason], None, seconds, target.id)
            own = self._own_turns(target.id, seat.role, after_id)
            messages = [m for m in self.repository.list_messages(target.id)
                        if m.role == MessageRole.AGENT and m.agent_id == seat.role and m.turn_id in own]
            text = messages[-1].content if messages else None
            if not text:
                return self._invalid(["no message published in this call's own turns"], None, seconds, target.id)
            answer_turn = messages[-1].turn_id
            calls = [c for c in self.ledger.calls if c.get("mission_id") == target.id and c.get("turn_id") == answer_turn]
            if len(calls) != 1:
                return self._invalid([f"answer turn has {len(calls)} ledger records"], None, seconds, target.id)
            call = calls[0]
            meta = {"hub": {"mission_id": target.id, "shared": phase in SHARED_PHASES, "phase": phase,
                            "turn_id": answer_turn, "own_turn_ids": own, "usage": call.get("usage"),
                            "native_tools": call.get("native_tools"),
                            "transport_schema_reads": call.get("transport_schema_reads"),
                            "prohibited_native_tools": call.get("prohibited_native_tools"),
                            "api_equivalent_usd": call.get("api_equivalent_usd")}}
            return {"text": text, "rc": 0, "seconds": seconds, "meta": meta,
                    "valid": True, "invalid_reasons": []}

    def _invalid(self, reasons, rc, seconds: float, mission_id: str = "") -> dict:
        return {"text": None, "rc": rc, "seconds": seconds, "meta": {"hub": {"mission_id": mission_id or self.mission.id}},
                "valid": False, "invalid_reasons": reasons}


def priced_models(protocol_models: dict) -> dict:
    """The frozen cost-aware-v1 entries plus this pack's supplement; no entry overrides another."""
    extra = json.loads(PRICING_PATH.read_text())["models"]
    clash = sorted(set(extra) & set(protocol_models))
    if clash:
        raise ValueError("pricing supplement redefines: " + ", ".join(clash))
    return {**protocol_models, **extra}


def seat_models(seats, protocol_models: dict) -> dict:
    """Per-role billing entries for the spend gate, keyed by seat role.

    Rates come from the frozen tables (priced_models); the CLI effort is the
    routed seat's, empty when the route sets none, so the argv matches the
    direct batch (#153). An unknown seat model is an error, not a silent
    zero: the spend gate is only honest with real rates.
    """
    table = {}
    for m in protocol_models.values():
        if m.get("provider") not in SEAT_PROVIDERS:
            continue
        key = f"{m['provider']}:{m['cli_model_id']}"
        if key in table:
            raise ValueError(f"two pricing entries for {key}")
        table[key] = m
    models = {}
    for seat in seats:
        entry = table.get(f"{seat.provider}:{seat.model}")
        if entry is None:
            known = ", ".join(sorted(table)) or "<none>"
            raise ValueError(f"seat {seat.role}: no pricing for {seat.provider}:{seat.model}; known: {known}")
        models[seat.role] = {**entry, "reasoning_effort": seat.effort or ""}
    return models
