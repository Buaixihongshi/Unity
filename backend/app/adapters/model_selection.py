"""Apply a role's provider-specific model and reasoning effort without mutating shared CLI config."""
from backend.app.domain.models import CLIProvider

# Effort levels each CLI accepts on its command line. A model may support
# fewer (Codex lists them per model); the settings UI narrows the choice.
EFFORT_LEVELS = {
    "codex": ("minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "antigravity": ("low", "medium", "high", "xhigh", "max"),
}
_CODEX_EFFORT_KEY = "model_reasoning_effort="


def _insert_before_prompt(args: list[str], options: list[str]) -> list[str]:
    # Place options before -p, which some CLIs use as a prompt delimiter.
    index = args.index("-p") if "-p" in args else len(args)
    args[index:index] = options
    return args


def with_model(provider: CLIProvider, model: str) -> CLIProvider:
    if not model or provider.id not in {"codex", "claude", "antigravity"}:
        return provider
    args = []
    source = iter(provider.args)
    for arg in source:
        if arg in {"--model", "-m"}:
            next(source, None)
        elif not arg.startswith(("--model=", "-m=")):
            args.append(arg)
    return provider.model_copy(update={"args": _insert_before_prompt(args, ["--model", model])})


def with_effort(provider: CLIProvider, effort: str) -> CLIProvider:
    """Replace any effort already in the CLI args; an empty effort keeps them as-is."""
    if not effort or effort not in EFFORT_LEVELS.get(provider.id, ()):
        return provider
    args = []
    source = list(provider.args)
    index = 0
    while index < len(source):
        arg = source[index]
        if provider.id == "codex":
            if arg in {"-c", "--config"} and index + 1 < len(source) and source[index + 1].startswith(_CODEX_EFFORT_KEY):
                index += 2
                continue
            if arg.startswith(("-c=" + _CODEX_EFFORT_KEY, "--config=" + _CODEX_EFFORT_KEY)):
                index += 1
                continue
        elif arg == "--effort":
            index += 2
            continue
        elif arg.startswith("--effort="):
            index += 1
            continue
        args.append(arg)
        index += 1
    options = ["-c", f'{_CODEX_EFFORT_KEY}"{effort}"'] if provider.id == "codex" else ["--effort", effort]
    return provider.model_copy(update={"args": _insert_before_prompt(args, options)})


def with_model_and_effort(provider: CLIProvider, model: str, effort: str) -> CLIProvider:
    return with_effort(with_model(provider, model), effort)
