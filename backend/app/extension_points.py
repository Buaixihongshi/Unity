"""The few places where optional extensions attach to the core.

Core modules call these hooks and never import ``backend.app.extensions``. The
composition roots install the extensions: ``main.py`` for the web app, and the
MCP subprocess through ``load()`` with the module names its parent passes down.
With nothing installed every hook is a no-op and Unity runs as the bare core.

Each hook is a list of callbacks, run in installation order. ``first`` returns
the first non-None result (a takeover), ``any`` is true when one callback says
so, ``collect`` gathers every truthy result, ``each`` runs them all for their
side effects.
"""
from importlib import import_module
from typing import Any, Callable, Dict, List

HOOKS = (
    # Missions: take over a mission run, its failure/cleanup, interruptions,
    # creation, and whether it still occupies its conversation.
    'mission_held',          # (repository, mission) -> bool: queued elsewhere, do not run yet
    'mission_takeover',      # (runner, mission) -> Mission | None
    'mission_failed',        # (repository, mission) -> bool: failure handled by the extension
    'mission_finished',      # (repository, mission_id) -> None, always after a run
    'mission_interrupt',     # (repository, mission, payload) -> bool: interruption handled
    'mission_prepare',       # (repository, settings, payload, registry, valid_ids) -> DiscussionConfig | None
    'mission_created',       # (repository, settings, payload, mission, conversation, targets, registry) -> Mission | None
    'occupies_conversation',  # (mission) -> bool, on top of the core active statuses
    'archive_allowed',       # (repository, mission) -> bool: an occupying mission that may be archived
    'mission_owned',         # (repository, mission) -> bool: run and restart recovery belong to the extension
    # Turns: per-attempt checks, cancellation, demo/API/router turn handling.
    'attempt_check',         # (runner, mission, tools, provider_id, prior_events) -> TurnOutcome | None
    'attempt_started',       # (repository, mission, turn_id, provider_id, attempt) -> None
    'turn_stopped',          # (repository, mission) -> bool
    'demo_turn',             # (adapter, mission_id, agent_id, turn_id, provider_id, tools) -> TurnOutcome | None
    'attachment_message_ids', # (repository, mission, agent_id, turn_id, provider_id, token) -> set[str] | None
    'api_structured_turn',   # (mission) -> handler with prompt(...) / submit(...), or None
    'route_turn',            # (router, mission_id, agent_id, turn_id, provider_id, prompt, tools) -> TurnOutcome | None
    # MCP tools: guards around core tools, takeovers, extra tools and context.
    'tool_guard',            # (tools, name, arguments, call) -> result; must call() to proceed
    'tool_check',            # (tools, name, arguments) -> None, raises MCPToolError to refuse
    'tool_override',         # (tools, name, arguments) -> result | None
    'context_extras',        # (tools, mission, conversation_id) -> dict | None, merged into read_context
    'mcp_tools_list',        # (tools, mission, descriptors) -> descriptors
    'mcp_call_refusal',      # (tools, mission, name) -> str | None
    # Prompt fragments for ordinary chat turns.
    'prompt_fragments',      # (mission, tasks_enabled) -> str | None
    # Presentation of extension state inside core views.
    'discussion_view',       # (mission, events) -> dict | None
    'annotate_messages',     # (message_events, missions, events, repository, cache) -> list | None
    'conversation_preview',  # (repository, missions, message_events) -> list | None
    'mission_detail',        # (repository, mission, detail) -> None, may adjust detail in place
    'health_fields',         # () -> dict | None
    'turn_records_refusal',  # (repository, mission) -> str | None
)

_hooks: Dict[str, List[Callable]] = {name: [] for name in HOOKS}
# Extra tools contributed by extensions: MCPTools methods and MCP handlers by
# tool name, and MCP descriptors listed ahead of the core tools.
tool_methods: Dict[str, Callable] = {}
mcp_handlers: Dict[str, Callable] = {}
mcp_descriptors: List[dict] = []
_modules: List[str] = []


def register(name: str, fn: Callable) -> None:
    if fn not in _hooks[name]:
        _hooks[name].append(fn)


def installed(module: str) -> None:
    """Remember an extension module so subprocesses can load the same set."""
    if module not in _modules:
        _modules.append(module)


def modules() -> List[str]:
    return list(_modules)


def load(names) -> None:
    """Install extensions named by the parent process (MCP subprocess entry)."""
    for name in names:
        if name:
            import_module(name).install()


def first(name: str, *args, **kwargs) -> Any:
    for fn in _hooks[name]:
        result = fn(*args, **kwargs)
        if result is not None:
            return result
    return None


def any(name: str, *args, **kwargs) -> bool:  # noqa: A001 - reads as extension_points.any(...)
    for fn in _hooks[name]:
        if fn(*args, **kwargs):
            return True
    return False


def collect(name: str, *args, **kwargs) -> list:
    return [result for fn in _hooks[name] for result in [fn(*args, **kwargs)] if result]


def each(name: str, *args, **kwargs) -> None:
    for fn in _hooks[name]:
        fn(*args, **kwargs)


def pipe(name: str, value, *args):
    """Thread ``value`` through every callback (callbacks take it last)."""
    for fn in _hooks[name]:
        value = fn(*args, value)
    return value


def guarded(name: str, tools, arguments: dict, call: Callable) -> Any:
    """Run ``call`` inside every installed tool guard, first installed outermost."""
    for guard in reversed(_hooks['tool_guard']):
        call = (lambda g, inner: lambda: g(tools, name, arguments, inner))(guard, call)
    return call()
