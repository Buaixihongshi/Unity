import json
import os
from pathlib import Path

import pytest

from backend.app.evaluation import exam_cli, exam_room

CLAUDE_ARGS = ["--dangerously-skip-permissions", "--no-session-persistence", "--output-format", "stream-json",
               "--include-partial-messages", "--verbose", "-p"]
CODEX_ARGS = ["exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "--ephemeral"]


def snapshot(**agents):
    return {
        "providers": [
            {"id": "claude", "command": "claude", "args": CLAUDE_ARGS, "enabled": True},
            {"id": "codex", "command": "codex", "args": CODEX_ARGS, "enabled": True},
            {"id": "antigravity", "command": "agy", "args": ["-p"], "enabled": True},
            {"id": "disabled", "command": "nope", "args": [], "enabled": False},
        ],
        "agents": agents or {
            "chulan": {"default_provider_id": "claude", "fallback_provider_ids": ["codex"],
                       "provider_models": {"claude": "opus", "codex": "gpt-x"}},
            "baobao": {"default_provider_id": "codex", "fallback_provider_ids": [],
                       "provider_models": {"codex": "gpt-x"}},
        },
        "digest": "d1",
    }


def test_examinee_uses_the_daily_primary_route_and_model():
    chulan = exam_cli.examinee_from_snapshot(snapshot(), "chulan")
    assert (chulan.provider, chulan.model, chulan.command, chulan.snapshot_digest) == ("claude", "opus", "claude", "d1")
    baobao = exam_cli.examinee_from_snapshot(snapshot(), "baobao")
    assert (baobao.provider, baobao.model, baobao.args) == ("codex", "gpt-x", tuple(CODEX_ARGS))


@pytest.mark.parametrize("route, message", [
    ({"default_provider_id": "antigravity", "provider_models": {}}, "does not support"),
    ({"default_provider_id": "disabled", "provider_models": {}}, "not support"),
])
def test_unsupported_or_disabled_route_is_refused_without_fallback(route, message):
    with pytest.raises(exam_cli.ExamineeError, match=message):
        exam_cli.examinee_from_snapshot(snapshot(wangye={**route, "fallback_provider_ids": ["claude"]}), "wangye")
    with pytest.raises(exam_cli.ExamineeError, match="unknown role"):
        exam_cli.examinee_from_snapshot(snapshot(), "nobody")


def test_disabled_daily_provider_is_refused():
    data = snapshot()
    data["providers"][0]["enabled"] = False
    with pytest.raises(exam_cli.ExamineeError, match="disabled"):
        exam_cli.examinee_from_snapshot(data, "chulan")


def test_claude_argv_keeps_daily_args_and_puts_model_before_print_flag():
    examinee = exam_cli.examinee_from_snapshot(snapshot(), "chulan")
    argv = exam_cli.build_argv(examinee, "/bin/claude", "PROMPT")
    assert argv[0] == "/bin/claude" and argv[-1] == "PROMPT"
    assert argv[-4:] == ["--model", "opus", "-p", "PROMPT"]
    assert all(arg in argv for arg in CLAUDE_ARGS)


def test_codex_argv_replaces_daily_model_and_skips_git_check():
    data = snapshot()
    data["providers"][1]["args"] = [*CODEX_ARGS, "-m", "old-model"]
    argv = exam_cli.build_argv(exam_cli.examinee_from_snapshot(data, "baobao"), "codex", "PROMPT")
    assert "old-model" not in argv
    assert argv[-4:] == ["--model", "gpt-x", "--skip-git-repo-check", "PROMPT"]


def test_auth_environment_only_passes_named_known_variables():
    examinee = exam_cli.examinee_from_snapshot(snapshot(), "chulan")
    host = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-123", "GH_TOKEN": "gh-456"}
    assert exam_cli.auth_environment(examinee, ["CLAUDE_CODE_OAUTH_TOKEN"], host) == {"CLAUDE_CODE_OAUTH_TOKEN": "tok-123"}
    assert exam_cli.auth_environment(examinee, [], host) == {}
    with pytest.raises(exam_cli.ExamineeError, match="does not read"):
        exam_cli.auth_environment(examinee, ["GH_TOKEN"], host)
    with pytest.raises(exam_cli.ExamineeError, match="not set"):
        exam_cli.auth_environment(examinee, ["ANTHROPIC_API_KEY"], host)


def test_final_answer_reads_last_message_and_ignores_cli_errors():
    claude = "\n".join(json.dumps(item) for item in [
        {"type": "stream_event"}, {"type": "result", "is_error": False, "result": "SHEET"}])
    assert exam_cli.final_answer("claude", "noise\n" + claude) == "SHEET"
    error = json.dumps({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"})
    assert exam_cli.final_answer("claude", error) == ""
    codex = "\n".join(json.dumps(item) for item in [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "draft"}},
        {"type": "item.completed", "item": {"type": "command_execution", "text": "ls"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "SHEET"}}])
    assert exam_cli.final_answer("codex", codex) == "SHEET"


def test_sit_exam_opens_only_cli_paths_and_scrubs_credentials(tmp_path, monkeypatch):
    prefix = tmp_path / "node"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/codex").write_text("#!/bin/sh\n")
    (prefix / "bin/codex").chmod(0o755)
    room = tmp_path / "room"
    room.mkdir()
    seen = {}
    secret = "sk-secret-" + "9" * 40

    def fake_run(argv, room_path, **kwargs):
        seen.update(argv=argv, **kwargs)
        body = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": f"key {secret}"}})
        return {"returncode": 0, "stdout": "x" * 5000 + secret + "\n" + body, "stderr": f"auth {secret}",
                "timed_out": False, "proxy_attempts": [{"target": "api.openai.com:443", "allowed": True, "resolved": ["1.2.3.4"]}]}
    monkeypatch.setattr(exam_room, "run_in_room", fake_run)
    examinee = exam_cli.examinee_from_snapshot(snapshot(), "baobao")
    record = exam_cli.sit_exam(examinee, room, task="T", auth={"CODEX_API_KEY": secret}, fake_ip=["198.18.0.0/15"],
                               path=str(prefix / "bin"))
    assert secret not in json.dumps(record)
    assert record["auth_env"] == ["CODEX_API_KEY"] and record["answer"] == "key [redacted]"
    assert seen["readable"] == [Path(os.path.realpath(prefix)), Path(exam_cli.CA_BUNDLE)]
    assert seen["extra_path"] == [str(prefix / "bin")]
    assert seen["allowed_hosts"] == ("chatgpt.com", "api.openai.com") and seen["fake_ip"] == ["198.18.0.0/15"]
    env = seen["extra_env"]
    assert env["CODEX_API_KEY"] == secret and env["SSL_CERT_FILE"] == exam_cli.CA_BUNDLE
    assert env["CODEX_HOME"] == str(Path(os.path.realpath(room)) / ".home/.codex")
    assert Path(env["CODEX_HOME"]).is_dir()


def test_sit_exam_scrubs_credentials_smuggled_into_a_rejected_proxy_target(tmp_path, monkeypatch):
    # #100: the examinee puts the token in a CONNECT host; the proxy rejects it, but the raw
    # target is recorded in proxy_attempts and must not reach the report in the clear.
    prefix = tmp_path / "node"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/codex").write_text("#!/bin/sh\n")
    (prefix / "bin/codex").chmod(0o755)
    room = tmp_path / "room"
    room.mkdir()
    secret = "sk-secret-" + "7" * 40

    def fake_run(argv, room_path, **kwargs):
        return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False,
                "proxy_attempts": [{"target": f"{secret}.api.openai.com:443", "allowed": False}]}
    monkeypatch.setattr(exam_room, "run_in_room", fake_run)
    examinee = exam_cli.examinee_from_snapshot(snapshot(), "baobao")
    record = exam_cli.sit_exam(examinee, room, task="T", auth={"CODEX_API_KEY": secret}, path=str(prefix / "bin"))
    assert secret not in json.dumps(record)
    assert record["proxy_attempts"][0]["target"] == "[redacted].api.openai.com:443"
    assert record["proxy_attempts"][0]["allowed"] is False


def test_single_binary_cli_opens_only_its_resolved_executable(tmp_path):
    versions = tmp_path / "share/claude/versions"
    versions.mkdir(parents=True)
    (versions / "2.0").write_text("#!/bin/sh\n")
    (versions / "2.0").chmod(0o755)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/claude").symlink_to(versions / "2.0")
    (tmp_path / "bin/other-tool").write_text("secret neighbour\n")
    install = exam_cli.resolve_install(exam_cli.examinee_from_snapshot(snapshot(), "chulan"), str(tmp_path / "bin"))
    assert install["readable"] == [Path(os.path.realpath(versions / "2.0"))] and install["extra_path"] == []
    env = exam_cli.room_settings(exam_cli.examinee_from_snapshot(snapshot(), "chulan"), tmp_path)
    # Claude Code ignores TMPDIR; without this it would share /tmp/claude-<uid> with the host.
    assert env["CLAUDE_CODE_TMPDIR"] == str(Path(os.path.realpath(tmp_path)) / ".tmp")
