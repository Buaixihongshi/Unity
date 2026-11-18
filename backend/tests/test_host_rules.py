"""The host rule catalog: constraints are enforced, hints never plan tool order."""
import pytest

from backend.app import host_rules
from backend.app.adapters.mcp_subprocess import _augment_prompt_for_mcp
from backend.app.domain.agent_registry import AgentRegistry
from backend.app.domain.models import Mission
from backend.app.extensions.discussion import rules as discussion_rules
from backend.app.extensions.tasks import rules as task_rules
from backend.app.extensions.tasks.tools import SHARED_WORK_DESCRIPTOR
from backend.app.orchestration.runner import _build_prompt

ALL_RULES = host_rules.RULES + task_rules.RULES + discussion_rules.RULES
PLANNING_WORDS = ('必须', '务必', '先调', '先用', '再调', '再用', '然后调')


def test_rule_ids_are_unique():
    ids = [rule.id for rule in ALL_RULES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize('rule', [r for r in ALL_RULES if r.kind in host_rules.CONSTRAINT_KINDS], ids=lambda r: r.id)
def test_constraints_name_their_enforcement(rule):
    assert rule.enforcement in {'hard', 'partial'}
    assert rule.enforced_by


@pytest.mark.parametrize('rule', [r for r in ALL_RULES if r.kind == 'hint'], ids=lambda r: r.id)
def test_hints_do_not_plan_tool_order(rule):
    assert rule.enforcement == 'none'
    assert not [word for word in PLANNING_WORDS if word in rule.text]


def test_chat_prompt_states_handoff_reason_instead_of_tool_order():
    agent = AgentRegistry.builtin().require('baobao')
    prompt = _augment_prompt_for_mcp(_build_prompt(Mission(id='m', title='聊', goal='你好'), '回应', agent,
                                                   max_a2a_invocations=2))
    assert host_rules.SPEAK.text in prompt
    assert '发言前登记' in prompt and '先 invoke_agent 再 publish_message' not in prompt
    assert '本任务最多接力 2 次' in prompt
    assert 'log_thought' not in prompt


def test_adaptive_prompt_leaves_tool_routing_to_the_tool_description():
    prompt = task_rules.adaptive_prompt_rules()
    for removed in ('先 shared_work(read)', 'refine_agreement', 'continue_work', 'accept_transfer'):
        assert removed not in prompt
    description = SHARED_WORK_DESCRIPTOR['description']
    assert 'refine_agreement(' in description and 'accept_transfer(' in description


def test_deliberation_prompt_relies_on_the_read_gate():
    text = ''.join(rule.text for rule in discussion_rules.DELIBERATION_RULES)
    assert '先调用 read_context' not in text
    assert '宿主拒绝提交' in text


def test_recovery_and_handover_keep_constraint_without_steps():
    recovery = host_rules.NO_REPLAY.text.format(last_action='写入 a.py')
    assert '不得整轮重放' in recovery and '先核对' not in recovery
    assert '先读' not in host_rules.MODEL_HANDOVER.text


def test_agy_server_name_hint_matches_plugin_namespace():
    from backend.app.adapters import antigravity_config
    assert antigravity_config.SERVER_NAME in host_rules.AGY_SERVER_NAME.text
