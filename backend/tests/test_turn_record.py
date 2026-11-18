"""Complete private turn records: what the CLI received, emitted and ran under."""
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from backend.app.adapters import turn_record
from backend.app.adapters.turn_record import TurnRecord, read_records, records_root
from backend.app.domain.models import Conversation, Mission, TurnStatus


def _entries(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _record(tmp_path, **kwargs):
    return TurnRecord(tmp_path / "data" / "db.sqlite3", mission_id="m_1", turn_id="t_1", agent_id="chulan",
                      provider_id="claude", route="chat", conversation_id="c_1", **kwargs)


def test_record_keeps_prompt_stream_and_environment_privately_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("UNCHANGED", "same")
    monkeypatch.setenv("SECRET_TOKEN", "value-must-not-be-stored")
    record = _record(tmp_path)
    fake_key = "sk-" + "hidden-prompt-key"  # assembled so the privacy scan sees no key literal
    prompt = "完整提示词：记忆、其他角色发言 api_key=" + fake_key
    env = {k: v for k, v in os.environ.items() if k != "SECRET_TOKEN"}
    env["CODEX_HOME"] = "/tmp/isolated-home"
    record.launch(command="claude", argv=["claude", "-p", prompt], prompt=prompt, cwd=tmp_path, launch_env=env,
                  isolation="macos-sandbox+host-mcp-bridge", sandbox_profile="(version 1)", model="opus")
    record.stream("stdout", '{"type":"system","session_id":"s1"}')
    record.stream("stderr", "Authorization: Bearer secret-bearer user@example.com")
    record.session("s1", "claude-opus")
    record.finish(exit_code=0, session_id="s1")

    assert records_root(tmp_path / "data" / "db.sqlite3").stat().st_mode & 0o777 == 0o700
    assert record.path.parent.stat().st_mode & 0o777 == 0o700
    assert record.path.stat().st_mode & 0o777 == 0o600
    text = record.path.read_text()
    for secret in (fake_key, "secret-bearer", "user@example.com", "value-must-not-be-stored"):
        assert secret not in text
    entries = _entries(record.path)
    assert [e["kind"] for e in entries] == ["header", "environment", "input", "stdout", "stderr", "session", "exit"]
    header, environment, given, *_ , finished = entries
    assert header["schema"] == 1 and header["route"] == "chat" and header["conversation_id"] == "c_1"
    assert environment["argv"] == ["claude", "-p", "<prompt>"]
    assert environment["cwd"] == str(tmp_path)
    assert environment["env"] == {"set": ["CODEX_HOME"], "removed": ["SECRET_TOKEN"]}
    assert environment["sandbox_profile"] == "(version 1)" and len(environment["sandbox_profile_sha256"]) == 64
    assert given["prompt"].startswith("完整提示词：记忆、其他角色发言") and given["chars"] == len(prompt)
    assert finished["exit_code"] == 0 and finished["lines"] == {"stdout": 1, "stderr": 1}
    assert finished["truncated"] is False


def test_record_stops_growing_at_its_cap_and_says_so(tmp_path):
    record = _record(tmp_path, max_bytes=2000)
    record.launch(command="codex", argv=["codex", "x"], prompt="x", cwd=None, launch_env=dict(os.environ))
    for i in range(100):
        record.stream("stdout", f"line {i} " + "y" * 50)
    record.finish(exit_code=1)
    entries = _entries(record.path)
    finished = entries[-1]
    assert finished["kind"] == "exit" and finished["truncated"] is True
    assert finished["dropped_lines"] > 0 and finished["lines"]["stdout"] == 100
    assert record.path.stat().st_size < 2000 + 1000


def test_concurrent_stream_writers_produce_whole_lines(tmp_path):
    record = _record(tmp_path)
    record.launch(command="codex", argv=["codex"], prompt="p", cwd=None, launch_env=dict(os.environ))
    threads = [threading.Thread(target=lambda n=n: [record.stream("stdout" if n % 2 else "stderr", f"{n}:{i}")
                                                   for i in range(200)]) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    record.finish(exit_code=0)
    entries = _entries(record.path)
    assert len([e for e in entries if e["kind"] in {"stdout", "stderr"}]) == 800


def test_store_prunes_oldest_records_but_keeps_the_current_one(tmp_path):
    database = tmp_path / "data" / "db.sqlite3"
    old = []
    for i in range(3):
        record = TurnRecord(database, mission_id="m_old", turn_id=f"t_{i}", agent_id="a", provider_id="codex",
                            route="chat")
        record.stream("stdout", "x" * 400)
        record.finish(exit_code=0)
        os.utime(record.path, (1000 + i, 1000 + i))
        old.append(record.path)
    current = TurnRecord(database, mission_id="m_new", turn_id="t_new", agent_id="a", provider_id="codex",
                         route="chat", max_total_bytes=1)
    current.stream("stdout", "z" * 400)
    current.finish(exit_code=0)
    assert not any(path.exists() for path in old)
    assert current.path.exists()


def test_record_io_failure_never_raises(tmp_path):
    blocker = tmp_path / "data"
    blocker.mkdir()
    (blocker / "turn-records").write_text("not a directory")
    record = _record(tmp_path)
    record.launch(command="codex", argv=["codex"], prompt="p", cwd=None, launch_env=dict(os.environ))
    record.stream("stdout", "x")
    record.session("s")
    record.finish(exit_code=0)
    assert not record.active
    assert (blocker / "turn-records").read_text() == "not a directory"


def test_reader_returns_records_in_order_and_ignores_foreign_files(tmp_path):
    database = tmp_path / "data" / "db.sqlite3"
    first = TurnRecord(database, mission_id="m_1", turn_id="t_1", agent_id="a", provider_id="gemini", route="chat")
    first.finish(exit_code=1)
    second = TurnRecord(database, mission_id="m_1", turn_id="t_1", agent_id="a", provider_id="claude", route="chat")
    second.stream("stdout", "ok")
    directory = second.path.parent
    (directory / "t_1.tr_link.jsonl").symlink_to(first.path)
    (directory / "t_1.tr_forged.jsonl").write_text(json.dumps({"kind": "header", "turn_id": "other"}) + "\n")
    records = read_records(database, "m_1", "t_1")
    assert [r["provider_id"] for r in records] == ["gemini", "claude"]
    assert [r["finished"] for r in records] == [True, False]
    assert records[1]["entries"][-1] == {"kind": "stdout", "t": records[1]["entries"][-1]["t"], "line": "ok"}


def test_strict_reader_keeps_every_file_of_the_turn_it_cannot_read(tmp_path):
    database = tmp_path / "data" / "db.sqlite3"
    good = TurnRecord(database, mission_id="m_1", turn_id="t_1", agent_id="a", provider_id="claude", route="chat")
    good.finish(exit_code=0)
    directory = good.path.parent
    (directory / "t_1.tr_empty.jsonl").write_text("")  # its header's write failed
    (directory / "t_1.tr_link.jsonl").symlink_to(good.path)
    (directory / "t_1.tr_forged.jsonl").write_text(json.dumps({"kind": "header", "turn_id": "other"}) + "\n")
    (directory / "t_1.tr_binary.jsonl").write_bytes(b"\xff\n")
    header = {"kind": "header", "turn_id": "t_1", "record_id": "tr_list"}
    (directory / "t_1.tr_list.jsonl").write_text(json.dumps(header) + "\n[]\n")
    # The records page skips what it cannot read, and a line that is not an entry, without raising.
    assert sorted(r["record_id"] for r in read_records(database, "m_1", "t_1")) == sorted([good.record_id, "tr_list"])
    # The closed-book audit has to account for every file of the turn (#161).
    strict = read_records(database, "m_1", "t_1", strict=True)
    assert [r["record_id"] for r in strict if "error" not in r] == [good.record_id]
    assert sorted(r["error"] for r in strict if "error" in r) == [
        "a line is not an entry", "could not be read", "header names another turn", "no header", "not a regular file"]


@pytest.mark.parametrize("damage", ["missing-schema", "wrong-mission", "wrong-record-id", "bad-order",
                                   "late-header", "duplicate-header", "missing-provider"])
def test_strict_reader_rejects_bad_headers_beside_a_good_record(tmp_path, damage):
    good, bad = _record(tmp_path), _record(tmp_path)
    good.finish(exit_code=0)
    bad.finish(exit_code=0)
    entries = _entries(bad.path)
    header = entries[0]
    if damage == "missing-schema":
        del header["schema"]
    elif damage == "wrong-mission":
        header["mission_id"] = "another-mission"
    elif damage == "wrong-record-id":
        header["record_id"] = good.record_id
    elif damage == "bad-order":
        header["started_ns"] = []  # must be rejected before sorting against the good record
    elif damage == "late-header":
        entries.reverse()
    elif damage == "duplicate-header":
        entries.append(header)
    elif damage == "missing-provider":
        del header["provider_id"]
    bad.path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    records = read_records(tmp_path / "data" / "db.sqlite3", "m_1", "t_1", strict=True)
    assert len(records) == 2
    assert [r["record_id"] for r in records if "error" not in r] == [good.record_id]
    assert [r["error"] for r in records if "error" in r] == ["invalid header"]


def test_reader_reports_open_failure_only_in_strict_mode(tmp_path, monkeypatch):
    good, unreadable = _record(tmp_path), _record(tmp_path)
    good.finish(exit_code=0)
    unreadable.finish(exit_code=0)
    original_open = Path.open

    def failed_open(path, *args, **kwargs):
        if path == unreadable.path:
            raise PermissionError("fixture read failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failed_open)
    database = tmp_path / "data" / "db.sqlite3"
    assert [r["record_id"] for r in read_records(database, "m_1", "t_1")] == [good.record_id]
    strict = read_records(database, "m_1", "t_1", strict=True)
    assert len(strict) == 2
    assert [r["error"] for r in strict if "error" in r] == ["could not be read"]


def test_version_probe_is_cached_per_installed_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_record, "VERSION_PROBE", True)
    calls = tmp_path / "calls"
    executable = tmp_path / "fakecli"
    executable.write_text(f"#!/bin/sh\necho x >> {calls}\necho 'fakecli 9.9.9'\n")
    executable.chmod(0o700)
    assert turn_record.cli_version(str(executable)) == "fakecli 9.9.9"
    assert turn_record.cli_version(str(executable)) == "fakecli 9.9.9"
    assert calls.read_text().count("x") == 1
    failing = tmp_path / "broken"
    failing.write_text("#!/bin/sh\nexit 3\n")
    failing.chmod(0o700)
    assert turn_record.cli_version(str(failing)) is None


@pytest.fixture
def chat_mission(repository):
    repository.create_conversation(Conversation(id="c_rec", title="t"))
    mission = Mission(id="m_rec", title="P", goal="g", conversation_id="c_rec")
    repository.create_mission(mission)
    return mission


def _chat_turn(repository, temp_settings, mission, tmp_path):
    from backend.app.adapters.mcp_subprocess import MCPSubprocessAdapter
    from backend.app.mcp.tools import MCPTools

    fake_mcp = Path(__file__).with_name("_fake_mcp_cli.py")
    script = tmp_path / "cli.py"
    script.write_text(
        "import json,os,runpy,sys\n"
        "print(json.dumps({'type':'thread.started','thread_id':'th_rec'}),flush=True)\n"
        "print(json.dumps({'type':'item.started','item':{'id':'i1','type':'command_execution','command':'ls'}}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'i1','type':'command_execution','command':'ls',"
        "'aggregated_output':'README.md\\n'+'o'*5000,'exit_code':0,'status':'completed'}}),flush=True)\n"
        "print('narration between tools',flush=True)\n"
        "print('warning on stderr',file=sys.stderr,flush=True)\n"
        "os.environ['YIREN_FAKE_MCP_MODE']='publish'\n"
        f"runpy.run_path({str(fake_mcp)!r},run_name='__main__')\n"
    )
    adapter = MCPSubprocessAdapter(sys.executable, [str(script)], db_path=temp_settings.database_path,
                                   artifact_root=temp_settings.artifact_root, timeout=20)
    outcome = adapter.run_turn(mission.id, "baobao", "t_rec", "codex", "请发声：完整提示词",
                               MCPTools(repository, temp_settings.artifact_root))
    assert outcome.status == TurnStatus.COMPLETED, outcome.summary
    return outcome


def test_chat_turn_record_has_full_prompt_untruncated_stream_and_session(repository, temp_settings, chat_mission,
                                                                         tmp_path):
    _chat_turn(repository, temp_settings, chat_mission, tmp_path)
    [record] = read_records(temp_settings.database_path, chat_mission.id, "t_rec")
    kinds = [e["kind"] for e in record["entries"]]
    assert kinds[:3] == ["header", "environment", "input"] and kinds[-1] == "exit"
    assert record["route"] == "chat" and record["finished"]
    given = record["entries"][2]
    assert given["prompt"].startswith("请发声：完整提示词") and "publish_message" in given["prompt"]
    environment = record["entries"][1]
    assert environment["argv"][-1] == "<prompt>" and environment["isolation"] == "none"
    assert "CODEX_HOME" in environment["env"]["set"]
    stdout = [e["line"] for e in record["entries"] if e["kind"] == "stdout"]
    completed = json.loads(next(line for line in stdout if '"item.completed"' in line))
    # The trace event is trimmed; the record keeps the complete tool output.
    assert len(completed["item"]["aggregated_output"]) > 5000
    assert "narration between tools" in stdout
    assert any(e["kind"] == "stderr" and e["line"] == "warning on stderr" for e in record["entries"])
    assert any(e["kind"] == "session" and e["session_id"] == "th_rec" for e in record["entries"])
    finished = record["entries"][-1]
    assert finished["exit_code"] == 0 and finished["session_id"] == "th_rec"
    activity = [e for e in repository.list_events_after(chat_mission.id, 0) if e.event_type == "provider.activity"]
    assert len(activity) == 2 and len(activity[-1].payload["output"]) <= 2000
    assert [e.event_type for e in repository.list_events_after(chat_mission.id, 0)].count("provider.session") == 1


def test_records_api_serves_chat_turns_and_waits_for_discussions_to_end(repository, temp_settings, chat_mission,
                                                                        tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.app.main import create_app
    from backend.app.extensions.discussion.deliberation import Deliberation
    from backend.app.orchestration.events import record_turn_started
    from backend.tests.test_deliberation import create

    _chat_turn(repository, temp_settings, chat_mission, tmp_path)
    client = TestClient(create_app(temp_settings))
    body = client.get("/api/observability/turns/t_rec/records").json()
    assert body["mission_id"] == chat_mission.id and len(body["records"]) == 1
    assert client.get("/api/observability/turns/t_missing/records").status_code == 404

    discussion = create(repository, temp_settings)
    record_turn_started(repository, discussion.id, "t_disc", "chulan", "codex", 1, "opinion")
    TurnRecord(temp_settings.database_path, mission_id=discussion.id, turn_id="t_disc", agent_id="chulan",
               provider_id="codex", route="discussion").finish(exit_code=0)
    blocked = client.get("/api/observability/turns/t_disc/records")
    assert blocked.status_code == 409
    original = Deliberation.state
    monkeypatch.setattr(Deliberation, "state", lambda self, mission: {**original(self, mission), "phase": "ended"})
    assert len(client.get("/api/observability/turns/t_disc/records").json()["records"]) == 1
