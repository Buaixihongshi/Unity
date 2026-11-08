"""Discussion hooks: roundtable (RT) and /discuss V2 attached to the core."""
import json

from backend.app.adapters.base import TurnOutcome
from backend.app.adapters.turn_budget import remaining_timeout
from backend.app.domain.models import MissionStatus, TurnStatus
from backend.app.extensions.discussion.deliberation import Deliberation, RULES_VERSION, is_v2
from backend.app.extensions.discussion.deliberation_runner import PURPOSES
from backend.app.extensions.discussion.models import RESULT_TYPES, SECTION_IDS
from backend.app.extensions.discussion.roundtable import Roundtable, state_from_events


def mission_takeover(runner, mission):
    if is_v2(mission):
        from backend.app.extensions.discussion.deliberation_runner import run_deliberation
        return run_deliberation(runner.repository, runner.settings, mission.id, runner.adapter)
    if mission.room_mode == 'roundtable':
        from backend.app.extensions.discussion.roundtable_runner import run_roundtable
        return run_roundtable(runner, mission.id)
    return None


def mission_owned(repo, mission):
    # Roundtable and V2 recover (and resume) their own missions on startup.
    return is_v2(mission) or mission.room_mode == 'roundtable'


def mission_failed(repo, mission):
    if mission.room_mode == 'roundtable':
        Roundtable(repo).fail(mission.id)
        return True
    return False


def mission_interrupt(repo, mission, payload):
    if is_v2(mission):
        Deliberation(repo).interrupt(mission.id, payload.content, payload.target_agent_id, payload.request_id)
        return True
    if mission.room_mode == 'roundtable':
        Roundtable(repo).interrupt(mission.id, payload.content, payload.target_agent_id, payload.request_id)
        return True
    return False


def mission_prepare(repo, settings, payload, registry, valid_ids):
    from backend.app.extensions.discussion.requests import build_config
    return build_config(repo, settings, payload, registry, valid_ids)


def occupies_conversation(mission):
    # A paused V2 discussion keeps its conversation until it has ended.
    return is_v2(mission) and mission.status == MissionStatus.BLOCKED and mission.completed_at is None


def attempt_started(repo, mission, turn_id, provider_id, attempt):
    if mission.room_mode == 'roundtable':
        Roundtable(repo).authorize_attempt(mission.id, turn_id, provider_id, attempt)


def turn_stopped(repo, mission):
    if is_v2(mission):
        state = Deliberation(repo).state(mission)
        return state['pending_control'] == 'stop' or state['phase'] == 'ended'
    if mission.room_mode == 'roundtable':
        state = state_from_events(mission, repo.list_events_after(mission.id, 0))
        return state['pending_control'] == 'stop' or state['phase'] == 'ended'
    return False


def demo_turn(adapter, mission_id, agent_id, turn_id, provider_id, tools):
    if is_v2(tools.repository.get_mission(mission_id)):
        return _demo_discussion_turn(adapter, mission_id, agent_id, turn_id, provider_id, tools)
    return None


def _demo_discussion_turn(adapter, mission_id, agent_id, turn_id, provider_id, tools):
    view = tools.read_context(mission_id, agent_id=agent_id, turn_id=turn_id, provider_id=provider_id)
    for source_id in view.get('required_read_ids', []):
        offset = 0
        while True:
            page = tools.read_discussion_result(mission_id, agent_id, turn_id, provider_id, source_id, offset)
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
    phase = view['phase']
    if phase == 'independent':
        result = {'kind': 'opinion', 'position_summary': f'[演示:{agent_id}] 先明确约束，再验证方案。',
                  'claims': [{'key': 'main', 'decision_key': 'next_step', 'statement': '需要以证据验证方案价值。',
                              'recommendation': '先验证一个真实场景', 'evidence_status': 'unverified'}]}
    elif phase == 'exchange':
        result = {'kind': 'contribution', 'no_addition': True}
    elif phase == 'review':
        result = {'kind': 'review', 'draft_id': view['draft_id'], 'no_findings': True}
    else:
        refs = [c['id'] for r in view['results'] if r['kind'] == 'opinion' for c in r['data']['claims']]
        result = {'kind': 'draft', 'sections': [{'id': sid, 'content': '演示综合：建议先验证真实场景，当前意见一致仍未经过事实验证。',
                                               'source_ids': refs} for sid in SECTION_IDS],
                  'issue_decisions': [{'issue_id': key, 'status': 'resolved' if issue['status'] == 'resolved' else 'unresolved',
                                       'decision': '演示综合：按现有证据保留结论并验证。'}
                                      for key, issue in view['issues'].items() if issue['category'] != 'needs_human']}
    tools.submit_discussion_result(mission_id, agent_id, turn_id, provider_id, result)
    return TurnOutcome(status=TurnStatus.COMPLETED)


class StructuredAPITurn:
    """Text-only API participant of a V2 discussion: frozen input in, JSON out."""

    def prompt(self, tools, mission_id, agent_id, turn_id, provider_id, prompt):
        policy = Deliberation(tools.repository)
        view = tools.read_context(mission_id, agent_id=agent_id, turn_id=turn_id, provider_id=provider_id)
        # Host materializes only the phase-visible, frozen inputs. The
        # normal read ledger and submission validator stay authoritative.
        with policy.transaction(mission_id) as (current, state):
            lease = state['leases'][turn_id]
            full = policy.visible(current, state, lease)
        ids = set(view['required_read_ids']) | {m['id'] for m in full['snapshot']['messages']}
        for source_id in ids:
            offset = 0
            while True:
                page = tools.read_discussion_result(mission_id, agent_id, turn_id, provider_id, source_id, offset)
                if page['next_offset'] is None:
                    break
                offset = page['next_offset']
        for name in full['snapshot']['materials']:
            offset = 0
            while True:
                page = policy.read_material(mission_id, agent_id, turn_id, provider_id, tools.lease_token, name, offset)
                if page['next_offset'] is None:
                    break
                offset = page['next_offset']
        schema = RESULT_TYPES[full['phase']].model_json_schema()
        return ('你是纯文本讨论参与者，无文件写入、终端或工具执行能力。宿主已提供完整冻结输入。'
                '仅返回符合 JSON schema 的单个 JSON 对象，不输出 Markdown 或工具调用。\n'
                + PURPOSES[full['phase']] + '\n角色与讨论规则：\n' + prompt.split('【本轮协议】')[0]
                + '\nJSON schema:\n' + json.dumps(schema, ensure_ascii=False)
                + '\n冻结输入：\n' + json.dumps(full, ensure_ascii=False))

    def submit(self, tools, mission_id, agent_id, turn_id, provider_id, result):
        tools.submit_discussion_result(mission_id, agent_id, turn_id, provider_id, result)


def api_structured_turn(mission):
    return StructuredAPITurn() if is_v2(mission) else None


def run_deliberation_turn(router, mission_id, agent_id, turn_id, provider_id, prompt, tools):
    api = router._api_adapter(agent_id, provider_id)
    if api:
        return api.run_turn(mission_id, agent_id, turn_id, provider_id, prompt, tools)
    if provider_id == 'fake':
        return router.fake_adapter.run_turn(mission_id, agent_id, turn_id, provider_id, prompt, tools)
    from backend.app.extensions.discussion.deliberation_cli import DeliberationCLIAdapter
    return DeliberationCLIAdapter(router.config_dir, remaining_timeout(router.cli_timeout), router.provider_overrides,
        model=router._model_for(agent_id, provider_id), effort=router._effort_for(agent_id, provider_id)).run_turn(
        mission_id, agent_id, turn_id, provider_id, prompt, tools)


def discussion_view(mission, events):
    from backend.app.extensions.discussion.roundtable import discussion_view as view
    return view(mission, events)


def annotate_messages(messages, missions, events, repository, audit_cache):
    from backend.app.extensions.discussion.projection import annotate_discussion_messages
    annotate_discussion_messages(messages, missions, events, repository, audit_cache)


def conversation_preview(repository, missions, messages):
    from backend.app.extensions.discussion.projection import annotate_discussion_messages
    discussion_events = [event for mission in missions if is_v2(mission)
                         for event in repository.list_events_after(mission.id, 0)]
    annotate_discussion_messages(messages, missions, discussion_events)


def mission_detail(repo, mission, result):
    if is_v2(mission):
        result['discussion'] = Deliberation(repo).user_view(mission)
        result['thoughts'] = []
        result.pop('live_partial', None)


def health_fields():
    from backend.app.build_info import digest
    from backend.app.domain.project_skills import PROJECT_SKILLS
    from backend.app.orchestration.discussion_rules import COMMON_DISCUSSION_RULES
    return {
        "discussion_protocols": ["roundtable_v1", "deliberation_v2"],
        "project_skills": list(PROJECT_SKILLS),
        "discussion_rules_version": RULES_VERSION,
        "discussion_rules_hash": digest(COMMON_DISCUSSION_RULES),
    }


def turn_records_refusal(repository, mission):
    # A V2 discussion releases results in batches; its raw streams contain
    # unreleased submissions, so they open only once the discussion ends.
    if is_v2(mission) and Deliberation(repository).state(mission)["phase"] != "ended":
        return "讨论结束后才能查看执行记录"
    return None
