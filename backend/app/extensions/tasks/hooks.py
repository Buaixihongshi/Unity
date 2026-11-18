"""Tasks hooks: shared/adaptive work attached to the core mission lifecycle."""
from backend.app.adapters.base import TurnOutcome
from backend.app.adapters.turn_budget import remaining_timeout
from backend.app.domain.errors import DiscussionError
from backend.app.domain.models import MissionStatus, TurnStatus
from backend.app.extensions.tasks.decisions import match_answer
from backend.app.extensions.tasks.service import SharedWork, WorkError


def _claimed(repository, mission_id, kinds=('adaptive.claimed',)):
    return any(e.event_type in kinds for e in repository.list_events_after(mission_id, 0))


def mission_held(repository, mission):
    # Queued adaptive actions and recorded human choices run from the work pump.
    return _claimed(repository, mission.id, {'adaptive.queued', 'adaptive.control_received'})


def mission_takeover(runner, mission):
    if _claimed(runner.repository, mission.id):
        from backend.app.extensions.tasks.runner import run_adaptive
        return run_adaptive(runner, mission)
    return None


def mission_owned(repository, mission):
    # The work pump queues, runs and recovers adaptive actions from the board.
    return _claimed(repository, mission.id, {'adaptive.claimed', 'adaptive.queued', 'adaptive.control_received'})


def mission_finished(repo, mission_id):
    work = SharedWork(repo)
    context = work.execution_context(mission_id)
    if context:
        work.finish(repo.get_mission(mission_id).conversation_id, mission_id)


def archive_allowed(repo, mission):
    # Adaptive work continues from its durable board after archiving.
    return _claimed(repo, mission.id)


def mission_created(repo, settings, payload, mission, conversation, targets, registry):
    work = SharedWork(repo)
    existing_work = work.read(conversation.id) if payload.room_mode == 'task' and settings.tasks_enabled else None
    answer = match_answer(existing_work, payload.goal) if existing_work and existing_work.get('protocol') == 'adaptive_v1' else None
    if answer:
        try:
            work.command(conversation.id, 'decide', answer, existing_work['revision'], operation_id='answer:' + mission.id)
        except WorkError as exc:
            raise DiscussionError(str(exc), exc.status_code) from exc
        mission = mission.model_copy(update={'status': MissionStatus.COMPLETED})
        repo.update_mission(mission)
        repo.add_event(mission.id, 'adaptive.control_received', {'decision_id': answer['decision_id'], 'option_id': answer['option_id']})
    elif payload.work_mode:
        from backend.app.extensions.tasks.adaptive import AdaptiveWork
        work = SharedWork(repo)
        view = work.read(conversation.id)
        owner = targets[0] if targets else registry.ids()[0]
        # Source and authority are taken from the human API request, not
        # parameters supplied by a model tool call.
        try:
            view = work.command(conversation.id, 'create_work', {
                'title': payload.title or payload.goal[:200], 'owner_id': owner,
                'agreement': {'source': payload.goal, 'goal': payload.goal, 'scope': payload.goal,
                    'delivery': payload.work_mode, 'criteria': [{'id': 'AC-1', 'text': '完成原始请求并用实际产物和操作结果验证'}]}},
                view['revision'], operation_id='intake:' + mission.id)
            mission = mission.model_copy(update={'target_agent_id': owner, 'target_agent_ids': [owner]})
            repo.update_mission(mission)
            AdaptiveWork(repo).adopt_mission(conversation.id, view['tasks'][-1]['id'], mission)
        except WorkError as exc:
            raise DiscussionError(str(exc), exc.status_code) from exc
    return mission


def attempt_check(runner, mission, tools, provider_id, prior_events):
    if any(e.event_type == 'adaptive.claimed' for e in prior_events):
        from backend.app.extensions.tasks.adaptive import AdaptiveWork
        try:
            AdaptiveWork(runner.repository, tools.workspace_root).authorize_provider(mission.id, provider_id)
        except WorkError as exc:
            return TurnOutcome(status=TurnStatus.BLOCKED, summary=str(exc), retryable=False)
    return None


def demo_turn(adapter, mission_id, agent_id, turn_id, provider_id, tools):
    if _claimed(tools.repository, mission_id, {'work.claimed', 'adaptive.claimed'}):
        board = tools.shared_work(mission_id, agent_id, turn_id, provider_id)
        task = next(task for task in board['tasks'] if task['active_mission_id'] == mission_id)
        reason = '演示模式不执行代码或审核。请接入真实 Provider 后重试，共享任务与责任分工已保留。'
        tools.shared_work(mission_id, agent_id, turn_id, provider_id, 'block',
                          {'task_id': task['id'], 'reason': reason}, board['revision'], operation_id='demo-block:' + mission_id)
        tools.publish_message(mission_id, agent_id, turn_id, reason, provider_id)
        return TurnOutcome(status=TurnStatus.COMPLETED)
    return None


def route_turn(router, mission_id, agent_id, turn_id, provider_id, prompt, tools):
    work_context = (SharedWork(tools.repository, tools.workspace_root).execution_context(mission_id)
                    if router.database_path is not None else None)
    if work_context and provider_id != 'fake':
        if router._api_adapter(agent_id, provider_id):
            return TurnOutcome(status=TurnStatus.BLOCKED, summary='纯文本 API 尚不支持受管工作工具',
                               failure_code='configuration', retryable=False)
        from backend.app.extensions.discussion.deliberation_cli import DeliberationCLIAdapter, isolation_supported
        if not isolation_supported():
            tools.repository.add_event(mission_id, 'adaptive.capability_unavailable', {'reason': 'isolation_not_supported'})
            return TurnOutcome(status=TurnStatus.BLOCKED, summary='当前平台不支持受限工作执行', retryable=False)
        return DeliberationCLIAdapter(router.config_dir, remaining_timeout(router.cli_timeout), router.provider_overrides,
            model=router._model_for(agent_id, provider_id), effort=router._effort_for(agent_id, provider_id),
            work_context=work_context).run_turn(
                mission_id, agent_id, turn_id, provider_id, prompt, tools)
    return None


def prompt_fragments(mission, tasks_enabled):
    if tasks_enabled and mission.conversation_id and mission.room_mode == 'task':
        return ("【持续交付】需要跨轮开发或写文档时，先在聊天里澄清目标、范围和验收条件，再用 propose_work 提议，"
                "等用户在提议卡确认；提议不能自行启动，也不要说成已经开始。")
    return None
