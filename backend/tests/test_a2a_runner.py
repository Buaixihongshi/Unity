from backend.app.domain.agent_registry import AgentRegistry
from backend.app.adapters.base import TurnOutcome
from backend.app.domain.models import Mission, MissionStatus, OrchestrationMode, TurnStatus
from backend.app.mcp.tools import MCPToolError
from backend.app.orchestration.runner import MissionRunner, _extract_reply_text
from backend.app.orchestration.speaker import WorkflowStep
from backend.app.orchestration.strategies import OrchestrationPlan


class WaitReplyTestAdapter:
    """Adapter that scripts agent behavior for wait_reply tests.

    script: dict mapping agent_id -> list of turn behaviors.
    Each behavior is a dict with optional keys:
      text: str - message to publish
      invoke_wait_reply: dict with target_agent_id, purpose
      block: bool - return BLOCKED
    """

    def __init__(self, script=None):
        self.script = script or {}
        self.cursors = {}
        self.prompt_log = {}

    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        if agent_id not in self.prompt_log:
            self.prompt_log[agent_id] = []
        self.prompt_log[agent_id].append(prompt)

        if agent_id not in self.cursors:
            self.cursors[agent_id] = 0
        idx = self.cursors[agent_id]
        behaviors = self.script.get(agent_id, [])
        if idx >= len(behaviors):
            tools.publish_message(mission_id, agent_id, turn_id, "[{a}] default".format(a=agent_id), provider_id)
            return TurnOutcome(status=TurnStatus.COMPLETED)
        behavior = behaviors[idx]
        self.cursors[agent_id] = idx + 1

        if behavior.get("block"):
            self.cursors[agent_id] = idx  # don't advance so fallback also blocks
            return TurnOutcome(status=TurnStatus.BLOCKED, summary=behavior.get("reason", "blocked"))

        if "invoke_wait_reply" in behavior:
            inv = behavior["invoke_wait_reply"]
            tools.invoke_agent(
                mission_id, agent_id, turn_id,
                inv["target_agent_id"], inv["purpose"], provider_id,
                mode="wait_reply",
            )

        text = behavior.get("text", "[{a}] ok".format(a=agent_id))
        tools.publish_message(mission_id, agent_id, turn_id, text, provider_id)
        return TurnOutcome(status=TurnStatus.COMPLETED)


class A2ATestAdapter:
    def __init__(
        self,
        invoke_on_agent: str = "chulan",
        invoke_times: int = 1,
        invoke_target: str = "baobao",
    ):
        self.calls = []
        self.invoke_on_agent = invoke_on_agent
        self.invoke_times = invoke_times
        self.invoke_target = invoke_target

    def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
        self.calls.append(
            {
                "mission_id": mission_id,
                "agent_id": agent_id,
                "turn_id": turn_id,
                "provider_id": provider_id,
                "prompt": prompt,
            }
        )
        if agent_id == self.invoke_on_agent:
            for index in range(self.invoke_times):
                try:
                    tools.invoke_agent(
                        mission_id,
                        agent_id,
                        turn_id,
                        self.invoke_target,
                        "核对异闻物档案 {n}".format(n=index + 1),
                        provider_id,
                    )
                except MCPToolError as exc:
                    return TurnOutcome(status=TurnStatus.BLOCKED, summary="a2a 深度耗尽: {error}".format(error=exc))
        tools.publish_message(mission_id, agent_id, turn_id, "[{agent}] ok".format(agent=agent_id), provider_id)
        return TurnOutcome(status=TurnStatus.COMPLETED)


def test_runner_consumes_a2a_queue_after_declared_workflow(repository, temp_settings):
    # target_agent_id forces a 1-step workflow so that after chulan the declared
    # workflow is exhausted and the a2a queue is consumed next.
    mission = Mission(id="m_a2a_run1", title="Plan", goal="设计方案", target_agent_id="chulan")
    repository.create_mission(mission)
    temp_settings.max_turns = 1
    temp_settings.max_a2a_invocations = 2
    adapter = A2ATestAdapter(invoke_on_agent="chulan", invoke_times=1)

    completed = MissionRunner(repository, temp_settings, adapter=adapter).run(mission.id)

    assert completed.status == MissionStatus.COMPLETED
    assert [call["agent_id"] for call in adapter.calls] == ["chulan", "baobao"]
    events = repository.list_events_after(mission.id, 0)
    assert [event.event_type for event in events if event.event_type == "a2a.invoke"] == ["a2a.invoke"]
    baobao_prompt = adapter.calls[1]["prompt"]
    assert "张楚岚" in baobao_prompt
    assert "核对异闻物档案 1" in baobao_prompt


def test_runner_blocks_when_a2a_budget_is_exhausted(repository, temp_settings):
    mission = Mission(id="m_a2a_run2", title="Plan", goal="设计方案", target_agent_id="chulan")
    repository.create_mission(mission)
    temp_settings.max_turns = 1
    temp_settings.max_a2a_invocations = 1
    adapter = A2ATestAdapter(invoke_on_agent="chulan", invoke_times=2)

    blocked = MissionRunner(repository, temp_settings, adapter=adapter).run(mission.id)

    assert blocked.status == MissionStatus.BLOCKED
    events = repository.list_events_after(mission.id, 0)
    status_summaries = [event.payload.get("summary", "") for event in events if event.event_type == "status"]
    assert any("a2a 深度耗尽" in summary for summary in status_summaries)


def test_interrupt_clears_pending_a2a_queue(repository, temp_settings):
    class InterruptAfterFirstA2AAdapter(A2ATestAdapter):
        def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
            outcome = super().run_turn(mission_id, agent_id, turn_id, provider_id, prompt, tools)
            if agent_id == "chulan":
                repository.add_event(
                    mission_id,
                    "interrupt",
                    {
                        "mission_id": mission_id,
                        "content": "先停一下，让王也说",
                        "target_agent_id": "wangye",
                    },
                )
            return outcome

    mission = Mission(id="m_a2a_run3", title="Plan", goal="设计方案", target_agent_ids=["chulan", "wangye"])
    repository.create_mission(mission)
    temp_settings.max_turns = 1
    temp_settings.max_a2a_invocations = 2
    adapter = InterruptAfterFirstA2AAdapter(invoke_on_agent="chulan", invoke_times=1)

    completed = MissionRunner(
        repository, temp_settings, adapter=adapter,
        agent_registry=_a2a_scenario_registry(),
    ).run(mission.id)

    assert completed.status == MissionStatus.COMPLETED
    assert [call["agent_id"] for call in adapter.calls] == ["chulan", "wangye"]
    events = repository.list_events_after(mission.id, 0)
    assert any(event.event_type == "a2a.cancelled" for event in events)
    assert any(event.event_type == "interrupt.handled" for event in events)


def test_extract_reply_text_returns_last_agent_message(repository):
    repository.create_mission(Mission(id="m_extract1", title="t", goal="g"))
    repository.add_event("m_extract1", "message", {
        "turn_id": "turn_callee", "role": "agent",
        "content": "first", "message_id": "m1",
    })
    repository.add_event("m_extract1", "message", {
        "turn_id": "turn_callee", "role": "agent",
        "content": "second", "message_id": "m2",
    })
    repository.add_event("m_extract1", "message", {
        "turn_id": "turn_other", "role": "agent",
        "content": "ignore", "message_id": "m3",
    })

    result = _extract_reply_text(repository, "m_extract1", "turn_callee")
    assert result.found is True
    assert result.text == "second"
    assert result.message_id == "m2"
    assert result.truncated is False


def test_extract_reply_text_returns_not_found_when_empty(repository):
    repository.create_mission(Mission(id="m_extract2", title="t", goal="g"))
    repository.add_event("m_extract2", "tool.call", {"turn_id": "turn_x", "tool_name": "x"})

    result = _extract_reply_text(repository, "m_extract2", "turn_x")
    assert result.found is False
    assert result.text == ""
    assert result.message_id is None


def test_extract_reply_text_links_real_published_message(repository, temp_settings):
    from backend.app.mcp.tools import MCPTools

    mission = Mission(id="m_real_reply", title="reply", goal="probe")
    repository.create_mission(mission)
    message = MCPTools(repository, temp_settings.artifact_root).publish_message(
        mission.id, "baobao", "callee", "verified", "codex")
    result = _extract_reply_text(repository, mission.id, "callee")
    assert result.found
    assert result.text == message.content
    assert result.message_id == message.id


def test_extract_reply_text_truncates_long_content(repository):
    repository.create_mission(Mission(id="m_extract3", title="t", goal="g"))
    long = "好" * 2000
    repository.add_event("m_extract3", "message", {
        "turn_id": "turn_long", "role": "agent",
        "content": long, "message_id": "mL",
    })

    result = _extract_reply_text(repository, "m_extract3", "turn_long")
    assert result.found is True
    assert result.truncated is True
    assert len(result.text) == 1501


def test_runner_wait_reply_resumes_sender_with_reply(repository, temp_settings):
    mission = Mission(id="m_wr1", title="Plan", goal="问宝宝档案", target_agent_id="chulan")
    repository.create_mission(mission)
    temp_settings.max_turns = 1
    temp_settings.max_a2a_invocations = 4

    adapter = WaitReplyTestAdapter(script={
        "chulan": [
            {"text": "在等宝宝", "invoke_wait_reply": {
                "target_agent_id": "baobao", "purpose": "查档案",
            }},
            {"text": "宝宝说没有，我换条路。"},
        ],
        "baobao": [
            {"text": "档案里没有这个条目。"},
        ],
    })

    result = MissionRunner(repository, temp_settings, adapter=adapter).run(mission.id)

    assert result.status == MissionStatus.COMPLETED
    events = repository.list_events_after(mission.id, 0)
    invokes = [e for e in events if e.event_type == "a2a.invoke"]
    replies = [e for e in events if e.event_type == "a2a.reply"]
    assert len(invokes) == 1
    assert invokes[0].payload["mode"] == "wait_reply"
    assert len(replies) == 1
    assert "档案里没有这个条目" in replies[0].payload["reply_text"]

    chulan_prompts = adapter.prompt_log.get("chulan", [])
    assert len(chulan_prompts) == 2
    assert _wait_scores(repository, mission.id)[0]["value"] == 1
    assert "档案里没有这个条目" in chulan_prompts[1]
    assert "回复" in chulan_prompts[1]


def test_runner_wait_reply_callee_blocked_resumes_with_no_reply(repository, temp_settings):
    mission = Mission(id="m_wr2", title="Plan", goal="g", target_agent_id="chulan")
    repository.create_mission(mission)
    temp_settings.max_turns = 1
    temp_settings.max_a2a_invocations = 4

    adapter = WaitReplyTestAdapter(script={
        "chulan": [
            {"text": "等回复", "invoke_wait_reply": {
                "target_agent_id": "baobao", "purpose": "x",
            }},
            {"text": "宝宝没回复，我自己继续"},
        ],
        "baobao": [
            {"block": True, "reason": "provider all failed"},
        ],
    })

    result = MissionRunner(repository, temp_settings, adapter=adapter).run(mission.id)

    assert result.status == MissionStatus.COMPLETED
    events = repository.list_events_after(mission.id, 0)
    failed = [e for e in events if e.event_type == "a2a.reply.failed"]
    assert len(failed) == 1
    assert "failed" in failed[0].payload["reason"] or "blocked" in failed[0].payload["fallback_reply_text"]

    chulan_prompts = adapter.prompt_log.get("chulan", [])
    assert len(chulan_prompts) == 2
    assert "<no reply" in chulan_prompts[1]


def test_runner_wait_reply_interrupt_clears_pending_replies(repository, temp_settings):
    class InterruptingWaitReplyAdapter(WaitReplyTestAdapter):
        def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
            outcome = super().run_turn(mission_id, agent_id, turn_id, provider_id, prompt, tools)
            if agent_id == "chulan" and self.cursors.get("chulan") == 1:
                repository.add_event(
                    mission_id, "interrupt",
                    {"mission_id": mission_id, "content": "先停一下", "target_agent_id": "wangye"},
                )
            return outcome

    mission = Mission(id="m_wr3", title="Plan", goal="g", target_agent_id="chulan")
    repository.create_mission(mission)
    temp_settings.max_turns = 1
    temp_settings.max_a2a_invocations = 4

    adapter = InterruptingWaitReplyAdapter(script={
        "chulan": [
            {"text": "等宝宝", "invoke_wait_reply": {
                "target_agent_id": "baobao", "purpose": "查资料",
            }},
        ],
        "wangye": [{"text": "插话处理完毕"}],
    })

    result = MissionRunner(repository, temp_settings, adapter=adapter).run(mission.id)

    assert result.status == MissionStatus.COMPLETED
    events = repository.list_events_after(mission.id, 0)
    cancelled = [e for e in events if e.event_type == "a2a.cancelled"]
    assert len(cancelled) == 1
    steps = cancelled[0].payload.get("cancelled_steps", [])
    origins = {s["origin"] for s in steps}
    assert "a2a" in origins
    assert "a2a_resume" in origins
    assert _wait_scores(repository, mission.id)[0]["value"] == 1


def test_runner_wait_reply_resume_counts_against_max_turns(repository, temp_settings):
    """Resume turn uses up a workflow turn slot, not a2a budget."""
    mission = Mission(id="m_wr4", title="Plan", goal="g", target_agent_id="chulan")
    repository.create_mission(mission)
    temp_settings.max_turns = 1
    temp_settings.max_a2a_invocations = 4

    adapter = WaitReplyTestAdapter(script={
        "chulan": [
            {"text": "等", "invoke_wait_reply": {
                "target_agent_id": "baobao", "purpose": "x",
            }},
            {"text": "done"},
        ],
        "baobao": [{"text": "reply"}],
    })

    result = MissionRunner(repository, temp_settings, adapter=adapter).run(mission.id)

    assert result.status == MissionStatus.COMPLETED
    events = repository.list_events_after(mission.id, 0)
    turns = [e for e in events if e.event_type == "turn.started"]
    assert len(turns) == 3


def test_fire_and_forget_a2a_runs_right_after_inviting_turn(repository, temp_settings):
    """A fire-and-forget invoke_agent must be honored right after the inviting
    turn, not drift to the end of a multi-step workflow.

    Mentions give the workflow [chulan, wangye]. chulan invokes baobao on
    its first turn. The baobao a2a turn should land between chulan and wangye,
    so the speaking order is chulan → baobao → wangye — not chulan → wangye →
    baobao (which strands the A2A reply after the discussion already closed).
    """
    mission = Mission(id="m_a2a_near", title="Plan", goal="设计方案", target_agent_ids=["chulan", "wangye"])
    repository.create_mission(mission)
    temp_settings.max_turns = 2
    temp_settings.max_a2a_invocations = 2
    adapter = A2ATestAdapter(invoke_on_agent="chulan", invoke_times=1)

    completed = MissionRunner(
        repository, temp_settings, adapter=adapter,
        agent_registry=_a2a_scenario_registry(),
    ).run(mission.id)

    assert completed.status == MissionStatus.COMPLETED
    assert [call["agent_id"] for call in adapter.calls] == ["chulan", "baobao", "wangye"]


def test_fire_and_forget_a2a_skipped_when_target_still_in_workflow(repository, temp_settings):
    """A redundant fire-and-forget invoke is absorbed when the target still has a
    scheduled turn in the remaining workflow.

    Mentions give the workflow [chulan, wangye]. chulan invokes wangye, but
    wangye is already going to speak (workflow step 2). The invoke should be
    skipped — recorded as a2a.skipped, no a2a.invoke, no extra wangye turn — so
    the speaking order stays chulan → wangye.
    """
    mission = Mission(id="m_a2a_dedup", title="Plan", goal="设计方案", target_agent_ids=["chulan", "wangye"])
    repository.create_mission(mission)
    temp_settings.max_turns = 2
    temp_settings.max_a2a_invocations = 2
    adapter = A2ATestAdapter(invoke_on_agent="chulan", invoke_times=1, invoke_target="wangye")

    completed = MissionRunner(
        repository, temp_settings, adapter=adapter,
        agent_registry=_a2a_scenario_registry(),
    ).run(mission.id)

    assert completed.status == MissionStatus.COMPLETED
    assert [call["agent_id"] for call in adapter.calls] == ["chulan", "wangye"]
    events = repository.list_events_after(mission.id, 0)
    assert [e.event_type for e in events if e.event_type == "a2a.invoke"] == []
    assert any(e.event_type == "a2a.skipped" for e in events)


def test_a2a_queue_frozen_after_workflow_closing_turn(repository, temp_settings):
    """Once the declared workflow's closing turn has run, the mission is wrapping
    up. A fire-and-forget invoke fired from the closing turn must not spawn another
    turn after the conclusion — it would re-litigate a closed discussion. The a2a
    queue is frozen on close.

    Workflow [chulan, baobao, wangye, chulan(closing)], as the evaluation arms use. chulan invokes
    baobao on its closing turn (baobao is no longer in the remaining workflow, so
    #2 dedup does not catch it). With the freeze the order stays the 4 workflow
    turns; without it baobao speaks a 5th time after the conclusion.
    """
    class ClosingInvokeAdapter:
        def __init__(self):
            self.calls = []
            self.chulan_turns = 0

        def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
            self.calls.append({"agent_id": agent_id})
            if agent_id == "chulan":
                self.chulan_turns += 1
                if self.chulan_turns == 2:  # the closing chulan turn
                    tools.invoke_agent(
                        mission_id, agent_id, turn_id, "baobao", "再确认一下产物", provider_id,
                    )
            tools.publish_message(mission_id, agent_id, turn_id, "[{a}] ok".format(a=agent_id), provider_id)
            return TurnOutcome(status=TurnStatus.COMPLETED)

    mission = Mission(id="m_a2a_freeze", title="Plan", goal="设计并实现后端系统架构")
    repository.create_mission(mission)
    temp_settings.max_turns = 4
    temp_settings.max_a2a_invocations = 3
    adapter = ClosingInvokeAdapter()

    class ClosingPlanRunner(MissionRunner):
        def _plan_orchestration(self, mission):
            steps = [WorkflowStep(a, "讨论") for a in ("chulan", "baobao", "wangye")]
            steps.append(WorkflowStep("chulan", "收束", is_closing=True))
            return OrchestrationPlan(OrchestrationMode.FIXED, steps, ["chulan", "baobao", "wangye"], 3, 4)

    completed = ClosingPlanRunner(repository, temp_settings, adapter=adapter).run(mission.id)

    assert completed.status == MissionStatus.COMPLETED
    assert [c["agent_id"] for c in adapter.calls] == ["chulan", "baobao", "wangye", "chulan"]


def test_subprocess_a2a_request_skipped_when_target_still_in_workflow(repository, temp_settings):
    """The subprocess path (real CLI) writes a2a.request events the runner picks up
    after the turn. A request whose target still has a scheduled workflow turn must
    be absorbed at pickup time — no extra turn, recorded as a2a.skipped — mirroring
    the in-process dedup. This is the path the real-world drift bug hit.
    """
    from backend.app.mcp.tools import MCPTools

    class SubprocessRequestAdapter:
        """chulan writes a fire-and-forget a2a.request via a subprocess-style
        MCPTools (a2a_enqueue=None) targeting wangye, who is still in the workflow."""

        def __init__(self):
            self.calls = []

        def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
            self.calls.append({"agent_id": agent_id})
            if agent_id == "chulan":
                subprocess_tools = MCPTools(
                    repository,
                    temp_settings.artifact_root,
                    a2a_enqueue=None,
                    valid_agent_ids={"chulan", "baobao", "wangye"},
                    max_a2a_invocations=temp_settings.max_a2a_invocations,
                )
                subprocess_tools.invoke_agent(
                    mission_id, agent_id, turn_id, "wangye", "核对异闻物档案", provider_id,
                )
            tools.publish_message(mission_id, agent_id, turn_id, "[{a}] ok".format(a=agent_id), provider_id)
            return TurnOutcome(status=TurnStatus.COMPLETED)

    mission = Mission(id="m_a2a_dedup_sub", title="Plan", goal="设计方案", target_agent_ids=["chulan", "wangye"])
    repository.create_mission(mission)
    temp_settings.max_turns = 2
    temp_settings.max_a2a_invocations = 2
    adapter = SubprocessRequestAdapter()

    completed = MissionRunner(
        repository, temp_settings, adapter=adapter,
        agent_registry=_a2a_scenario_registry(),
    ).run(mission.id)

    assert completed.status == MissionStatus.COMPLETED
    assert [call["agent_id"] for call in adapter.calls] == ["chulan", "wangye"]
    events = repository.list_events_after(mission.id, 0)
    assert [e.event_type for e in events if e.event_type == "a2a.invoke"] == []
    assert any(e.event_type == "a2a.skipped" for e in events)


def _a2a_scenario_registry():
    # These scenarios need chulan -> wangye scheduled and baobao unscheduled;
    # make that arrangement explicit rather than relying on default personas.
    registry = AgentRegistry.builtin()
    return AgentRegistry([registry.require(agent_id) for agent_id in ("chulan", "wangye", "baobao")])


def _wait_scores(repository, mission_id):
    from backend.app.observability.service import collect_turns
    turns = collect_turns(repository, mission_id=mission_id)
    return [c for t in turns for c in t.checks if c['name'] == 'a2a_wait_closed' and c['value'] is not None]


def test_wait_reply_repeated_partner_keeps_second_reply_separate(repository, temp_settings):
    mission = Mission(id='m_wr_repeat', title='Repeat', goal='g', target_agent_id='chulan')
    repository.create_mission(mission)
    temp_settings.max_turns, temp_settings.max_a2a_invocations = 1, 4
    ask = {'target_agent_id': 'baobao', 'purpose': '查资料'}
    adapter = WaitReplyTestAdapter({
        'chulan': [{'invoke_wait_reply': ask}, {'invoke_wait_reply': ask}, {'text': '完成'}],
        'baobao': [{'text': 'FIRST_SECRET'}, {'text': 'SECOND_SECRET'}],
    })
    MissionRunner(repository, temp_settings, adapter=adapter).run(mission.id)
    continuation = adapter.prompt_log['chulan'][2].split('对方的回复', 1)[1]
    assert 'SECOND_SECRET' in continuation
    assert 'FIRST_SECRET' not in continuation
    scores = _wait_scores(repository, mission.id)
    assert len(scores) == 2 and all(c['value'] == 1 for c in scores)


def test_wait_reply_timeout_and_empty_explain_failure(repository, temp_settings):
    from backend.app.observability.service import collect_turns
    for mode in ('timeout', 'empty'):
        class NoReplyAdapter(WaitReplyTestAdapter):
            def run_turn(self, mission_id, agent_id, turn_id, provider_id, prompt, tools):
                if agent_id == 'baobao':
                    if mode == 'timeout':
                        return TurnOutcome(status=TurnStatus.BLOCKED, summary='CLI idle timeout', failure_code='idle_timeout')
                    return TurnOutcome(status=TurnStatus.COMPLETED)
                return super().run_turn(mission_id, agent_id, turn_id, provider_id, prompt, tools)
        mission = Mission(id='m_wr_' + mode, title=mode, goal='g', target_agent_id='chulan')
        repository.create_mission(mission)
        temp_settings.max_turns, temp_settings.max_a2a_invocations = 1, 4
        adapter = NoReplyAdapter({'chulan': [
            {'invoke_wait_reply': {'target_agent_id': 'baobao', 'purpose': '确认'}},
            {'text': '宝宝已经确认了，可以继续。'},
        ]})
        result = MissionRunner(repository, temp_settings, adapter=adapter).run(mission.id)
        assert result.status == MissionStatus.COMPLETED
        prompt = adapter.prompt_log['chulan'][1]
        assert '【求助未收到回复】' in prompt and '不要声称已获得对方确认' in prompt
        assert ('<no reply: empty>' if mode == 'empty' else '<no reply: turn blocked>') in prompt
        scores = _wait_scores(repository, mission.id)
        assert len(scores) == 1 and scores[0]['value'] == 1
        resumed = collect_turns(repository, mission_id=mission.id)[-1]
        claim = next(c for c in resumed.checks if c['name'] == 'claims_match_events')
        assert claim['value'] == 0 and 'reply.failed' in claim['note']
        for text in ('宝宝确认了，可以继续。', '已收到宝宝的回复。'):
            from backend.app.observability.checks import check_claims_match_events
            resumed.reply = text
            assert check_claims_match_events(resumed)['value'] == 0
        for text in ('宝宝没有回复，我自己继续。', '宝宝未确认，不能说宝宝已经确认了。', '等宝宝已经确认之后再继续。'):
            from backend.app.observability.checks import check_claims_match_events
            resumed.reply = text
            assert check_claims_match_events(resumed)['value'] != 0
