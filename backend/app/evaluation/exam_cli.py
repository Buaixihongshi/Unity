"""Real CLI examinees for the code-review exam.

The examinee is picked by daily role: provider, model and CLI arguments come
from the effective daily CLI settings (``runtime_cli_snapshot``, the same
snapshot development inherits). Only the primary route is used; an exam never
falls back to another model silently.

Inside the room the CLI gets its own binary or install tree, the provider's API
hosts through the allowlist proxy, and nothing else. Credentials are never
copied from the host's keychain or login files: they enter only as environment
variables the caller names explicitly, and their values are scrubbed from
everything recorded.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from backend.app.evaluation import exam_room

# install: "binary" opens only the resolved executable (single-file builds);
# "prefix" opens the install prefix of the command (bin/ + lib/node_modules for npm installs).
# env values may use {home} and {tmp}, the room's HOME and TMPDIR; files are extra read-only paths.
# Without keychain access (#94) the Security framework finds no root certificates, so CLIs whose TLS
# goes through it read the public CA bundle instead.
CA_BUNDLE = "/private/etc/ssl/cert.pem"
PROVIDERS = {
    "claude": {
        "hosts": ("api.anthropic.com",),
        "install": "binary",
        # Claude Code ignores TMPDIR and would share /tmp/claude-<uid> with the host.
        "env": {"CLAUDE_CODE_TMPDIR": "{tmp}", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1"},
        "args": (),
        "files": (),
        "auth_env": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    },
    "codex": {
        "hosts": ("chatgpt.com", "api.openai.com"),
        "install": "prefix",
        "env": {"CODEX_HOME": "{home}/.codex", "SSL_CERT_FILE": CA_BUNDLE},
        "files": (CA_BUNDLE,),
        # The room itself is not a git repository; repo/ is.
        "args": ("--skip-git-repo-check",),
        "auth_env": ("CODEX_API_KEY", "OPENAI_API_KEY"),
    },
}

PROMPT = (
    "当前工作目录是一间代码评审考场。下面是考生须知（与目录里的 TASK.md 相同）。"
    "请按须知完成评审，最终回答只包含答卷 JSON。\n\n{task}"
)


class ExamineeError(ValueError):
    """The requested role cannot sit the exam with a real CLI."""


@dataclass(frozen=True)
class Examinee:
    role: str
    provider: str
    model: str
    command: str
    args: tuple[str, ...]
    snapshot_digest: str

    @property
    def spec(self) -> dict:
        return PROVIDERS[self.provider]


def examinee_from_snapshot(snapshot: Mapping, role: str) -> Examinee:
    route = snapshot["agents"].get(role)
    if route is None:
        raise ExamineeError(f"unknown role: {role}")
    provider_id = route["default_provider_id"]
    if provider_id not in PROVIDERS:
        raise ExamineeError(f"role {role} uses {provider_id}, which the exam room does not support yet")
    provider = next((item for item in snapshot["providers"] if item["id"] == provider_id), None)
    if provider is None or not provider.get("enabled"):
        raise ExamineeError(f"provider {provider_id} is disabled in the daily settings")
    return Examinee(role=role, provider=provider_id, model=route.get("provider_models", {}).get(provider_id, ""),
                    command=provider["command"], args=tuple(provider.get("args", ())),
                    snapshot_digest=snapshot.get("digest", ""))


def build_argv(examinee: Examinee, binary: str, prompt: str) -> list[str]:
    """Daily arguments with the role's model and the provider's exam options, prompt last.

    Options go before ``-p`` because Claude treats what follows it as the prompt.
    """
    args, source = [], iter(examinee.args)
    for arg in source:
        if arg in {"--model", "-m"}:
            next(source, None)
        elif not arg.startswith(("--model=", "-m=")):
            args.append(arg)
    extra = [*(["--model", examinee.model] if examinee.model else []), *examinee.spec["args"]]
    index = args.index("-p") if "-p" in args else len(args)
    args[index:index] = extra
    return [binary, *args, prompt]


def resolve_install(examinee: Examinee, path: str | None = None) -> dict:
    """Locate the CLI on the host and the paths the room must open for it."""
    found = shutil.which(examinee.command, path=path)
    if found is None:
        raise ExamineeError(f"{examinee.command} is not on PATH")
    files = [Path(item) for item in examinee.spec["files"]]
    binary = Path(os.path.realpath(found))
    if examinee.spec["install"] == "binary":
        return {"binary": str(binary), "readable": [binary, *files], "extra_path": []}
    prefix = Path(os.path.realpath(Path(found).parent.parent))
    return {"binary": found, "readable": [prefix, *files], "extra_path": [str(Path(found).parent)]}


def room_settings(examinee: Examinee, room: Path) -> dict:
    """Provider environment inside the room; directories it expects are created here."""
    room = Path(os.path.realpath(room))
    values = {"home": str(room / ".home"), "tmp": str(room / ".tmp")}
    env = {key: value.format(**values) for key, value in examinee.spec["env"].items()}
    if "CODEX_HOME" in env:
        Path(env["CODEX_HOME"]).mkdir(parents=True, exist_ok=True)
    return env


def auth_environment(examinee: Examinee, names: Iterable[str], source: Mapping[str, str]) -> dict:
    names = list(names)
    unknown = [name for name in names if name not in examinee.spec["auth_env"]]
    if unknown:
        raise ExamineeError(f"{examinee.provider} does not read {unknown}; allowed: {examinee.spec['auth_env']}")
    missing = [name for name in names if not source.get(name)]
    if missing:
        raise ExamineeError(f"auth variables not set on the host: {missing}")
    return {name: source[name] for name in names}


def scrub(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def scrub_tree(value, secrets: Iterable[str]):
    """Scrub every string reachable in the record, so no field leaks a credential
    even if the examinee smuggles one into a place we did not anticipate (#100:
    a token placed in a rejected CONNECT target still lands in ``proxy_attempts``)."""
    secrets = list(secrets)
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, dict):
        return {key: scrub_tree(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_tree(item, secrets) for item in value]
    return value


def final_answer(provider: str, stdout: str) -> str:
    """The examinee's last message from the CLI's JSON event stream ("" if none)."""
    answer = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if provider == "claude" and event.get("type") == "result" and isinstance(event.get("result"), str):
            # An error result ("Not logged in", API failure) is the CLI talking, not an answer sheet.
            answer = "" if event.get("is_error") else event["result"]
        elif provider == "codex" and event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                answer = item["text"]
    return answer


def sit_exam(examinee: Examinee, room: Path, *, task: str, auth: Mapping[str, str], fake_ip: Iterable[str] = (),
             timeout: int = 1800, path: str | None = None) -> dict:
    """Run one real examinee in ``room``. Returns a record with credentials scrubbed."""
    install = resolve_install(examinee, path)
    argv = build_argv(examinee, install["binary"], PROMPT.format(task=task))
    result = exam_room.run_in_room(argv, room, allowed_hosts=examinee.spec["hosts"], readable=install["readable"],
                                   extra_path=install["extra_path"], extra_env={**room_settings(examinee, room), **auth},
                                   fake_ip=fake_ip, timeout=timeout)
    secrets = list(auth.values())
    record = {
        "role": examinee.role, "provider": examinee.provider, "model": examinee.model,
        "cli_config_digest": examinee.snapshot_digest, "returncode": result["returncode"],
        "timed_out": result["timed_out"], "answer": scrub(final_answer(examinee.provider, result["stdout"]), secrets),
        # Scrub before cutting, so a token split by the cut cannot survive half-redacted.
        "stdout_tail": scrub(result["stdout"], secrets)[-4000:], "stderr_tail": scrub(result["stderr"], secrets)[-4000:],
        "proxy_attempts": [{key: item.get(key) for key in ("target", "allowed")} for item in result["proxy_attempts"]],
        "auth_env": sorted(auth),
    }
    # Final safety net: scrub every string in the record, so nothing textual escapes half-redacted.
    return scrub_tree(record, secrets)
