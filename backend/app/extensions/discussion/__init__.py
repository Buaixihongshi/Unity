"""Discussion extension: roundtable (RT) and the /discuss V2 deliberation."""


def install():
    from backend.app import extension_points as ep
    from backend.app.extensions.discussion import hooks, tools
    for name in ('mission_takeover', 'mission_owned', 'mission_failed', 'mission_interrupt', 'mission_prepare',
                 'occupies_conversation', 'attempt_started', 'turn_stopped', 'demo_turn',
                 'api_structured_turn', 'attachment_message_ids', 'discussion_view', 'annotate_messages', 'conversation_preview',
                 'mission_detail', 'health_fields', 'turn_records_refusal'):
        ep.register(name, getattr(hooks, name))
    ep.register('tool_guard', tools.roundtable_guard)
    ep.register('tool_override', tools.override)
    ep.register('mcp_tools_list', tools.mcp_tools_list)
    ep.register('mcp_call_refusal', tools.mcp_call_refusal)
    ep.tool_methods.update(read_discussion_result=tools.read_discussion_result,
                           submit_discussion_result=tools.submit_discussion_result)
    ep.mcp_handlers.update(tools.MCP_HANDLERS)


def recover(repository, settings):
    from backend.app.extensions.discussion.roundtable import Roundtable
    Roundtable(repository).recover()


def routers(settings, repository, adapter):
    from backend.app.extensions.discussion.api import build_router
    return [build_router(settings, repository, adapter)]


def resume(repository, settings, adapter):
    """A persisted human choice may have committed just before a crash and
    before its worker started. Reservations make rescheduling idempotent."""
    import threading
    from backend.app.api.missions import _run_mission_in_background
    from backend.app.domain.models import MissionStatus
    from backend.app.extensions.discussion.deliberation import is_v2
    for pending in repository.list_missions():
        if is_v2(pending) and pending.status == MissionStatus.PENDING:
            threading.Thread(target=_run_mission_in_background,
                             args=(repository, settings, pending.id, adapter), daemon=True).start()
