import json
from pathlib import Path

import pytest

from backend.app.evaluation import clueeval as C

DATA = Path(__file__).resolve().parents[2] / "eval/clueeval"
QUESTIONS = C.load_questions(DATA / "questions.json")
KEY = json.loads((DATA / "answer_key.json").read_text())


def _fixture_seats(n):
    return [C.Seat(i, f"r{i}", "fixture", "fixture") for i in range(n)]


def _run(tmp_path, qid, condition="private", game=0, rounds=2):
    q = QUESTIONS[qid]
    writer = C.GameWriter(tmp_path)
    record = C.run_game(q, game, condition, rounds, _fixture_seats(len(q["cards"])), C.fixture_responder(q), writer)
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    return q, record, calls


def test_three_questions_with_original_seat_counts():
    assert list(QUESTIONS) == ["q1-storage", "q2-rootcause", "hb-009-critical_hospital_transfer"]
    assert [len(q["cards"]) for q in QUESTIONS.values()] == [3, 3, 4]


def test_hiddenbench_item_is_pinned_and_reassembles_to_recorded_hash():
    q = QUESTIONS["hb-009-critical_hospital_transfer"]
    answer = KEY["answers"][q["id"]]
    item = dict(q["original"], correct_answer=answer["correct"], rationale=answer["rationale"])
    assert C.canonical_sha256(item) == q["origin"]["item_sha256"]
    assert q["origin"]["revision"].startswith("1e3c25b")
    assert q["origin"]["file_sha256"].startswith("2815afffca4e")


def test_question_file_holds_no_answer_side_fields():
    raw = (DATA / "questions.json").read_text()
    assert KEY["canary"] not in raw
    assert KEY["answers"]["hb-009-critical_hospital_transfer"]["rationale"][:80] not in raw
    for q in QUESTIONS.values():
        assert not C._find_keys(q, C.FORBIDDEN_QUESTION_KEYS)


def test_loader_rejects_answer_fields(tmp_path):
    q = dict(QUESTIONS["q1-storage"], correct_answer="石塔")
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"questions": [q]}, ensure_ascii=False))
    with pytest.raises(C.IsolationError):
        C.load_questions(path)


@pytest.mark.parametrize("qid", list(QUESTIONS))
@pytest.mark.parametrize("game", [0, 1])
def test_each_seat_starts_with_shared_plus_only_its_own_card(tmp_path, qid, game):
    q, record, calls = _run(tmp_path, qid, game=game)
    assigned = [s["card"] for s in record["seats"]]
    assert sorted(assigned) == sorted(c["id"] for c in q["cards"])  # one distinct card per seat
    for call in calls:
        own = assigned[call["seat"] - 1]
        assert call["cards_in_initial_context"] == [own]
        context = call["prompt"][: len(C.initial_context(q, next(c for c in q["cards"] if c["id"] == own)))]
        assert C.cards_in(q, context) == [own]
        assert KEY["canary"] not in call["prompt"]
        rationale = KEY["answers"][qid].get("rationale")
        if rationale:
            assert rationale[:80] not in call["prompt"]
    shared = q["shared"] if q["lang"] == "zh" else q["original"]["shared_information"][0]
    assert all(shared in call["prompt"] for call in calls)


def test_cards_rotate_between_games(tmp_path):
    q = QUESTIONS["q2-rootcause"]
    assert [c["id"] for c in C.seat_cards(q, 0, "private")] != [c["id"] for c in C.seat_cards(q, 1, "private")]


def test_other_cards_reach_a_seat_only_after_their_holder_speaks(tmp_path):
    q, record, calls = _run(tmp_path, "q2-rootcause", rounds=1)
    order = record["speaking_order"]
    holder = {s["card"]: s["seat"] for s in record["seats"]}
    spoken = []
    discuss = [c for c in calls if c["phase"] == "discuss"]
    for call in discuss:
        allowed = {cid for cid, seat in holder.items() if seat in spoken}
        assert set(call["cards_in_transcript"]) <= allowed
        spoken.append(call["seat"])
    assert [c["seat"] for c in discuss] == order
    final = [c for c in calls if c["phase"] == "final"]
    assert all(set(c["cards_in_transcript"]) == set(holder) for c in final)


def test_shared_only_control_gives_no_card(tmp_path):
    _, record, calls = _run(tmp_path, "q1-storage", condition="shared_only")
    assert all(s["card"] is None for s in record["seats"])
    assert all(c["cards_in_initial_context"] == [] and c["cards_in_transcript"] == [] for c in calls)


def test_audit_refuses_a_context_that_carries_someone_elses_card():
    q = QUESTIONS["q1-storage"]
    own, other = q["cards"][0], q["cards"][1]
    with pytest.raises(C.IsolationError):
        C.audit_context(q, C.initial_context(q, own) + "\n" + other["text"], own)


def test_run_does_not_need_the_answer_key(tmp_path, monkeypatch):
    real_open = Path.read_text

    def guarded(self, *a, **k):
        assert self.name != "answer_key.json", "run phase opened the answer key"
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", guarded)
    _run(tmp_path, "hb-009-critical_hospital_transfer")


def test_hiddenbench_seats_are_not_told_others_differ(tmp_path):
    _, _, calls = _run(tmp_path, "hb-009-critical_hospital_transfer")
    assert all("different" not in c["prompt"] and "你看不到" not in c["prompt"] for c in calls)


def test_score_reads_key_after_run(tmp_path):
    _run(tmp_path, "q2-rootcause")
    result = C.score(tmp_path, DATA / "answer_key.json")
    row = result["games"][0]
    assert row["correct"] == "CHG-7730" and row["final_correct"] == 3 and row["unanimous"]


@pytest.mark.parametrize("qid,text,pick", [
    ("q2-rootcause", "根因：v2.8.0\n…", "RL-5512"),
    ("q1-storage", "\n结论：**石塔**", "石塔"),
    ("q1-storage", "我觉得\n结论：石塔", None),
    ("hb-009-critical_hospital_transfer", '```json\n{"vote": "hospital a", "rationale": "x"}\n```', "Hospital A"),
    ("hb-009-critical_hospital_transfer", '{"vote": "Hospital D"}', None),
])
def test_parse_pick(qid, text, pick):
    assert C.parse_pick(QUESTIONS[qid], text) == pick


def test_global_instruction_digests_report_presence_without_content(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/AGENTS.md").write_text("用中文回复")
    found = C.global_instructions(tmp_path)
    assert found["claude"] is None and found["antigravity"] is None
    assert found["codex"] == C.sha256_bytes("用中文回复".encode())[:12]


def test_roster_follows_routing_and_seats_four_roles():
    routing = {"agents": {"chulan": {"default_provider_id": "claude", "provider_models": {"claude": "opus"}},
                          "baobao": {"default_provider_id": "codex", "provider_models": {"codex": "m"}},
                          "wangye": {"default_provider_id": "antigravity", "provider_models": {"antigravity": "g"}}},
               "custom_agents": [{"id": "role-694a37", "default_provider_id": "antigravity",
                                  "provider_models": {"antigravity": "g-low"}}]}
    seats = C.resolve_roster(routing, 4)
    assert [(s.role, s.provider, s.model) for s in seats][-1] == ("role-694a37", "antigravity", "g-low")
    with pytest.raises(ValueError):
        C.resolve_roster({"agents": {"chulan": {"default_provider_id": "claude"}}}, 1)


# ---------------------------------------------------------------- #140: tool-free calls fail closed
# Event shapes below are trimmed from real probe runs (claude 2.x stream-json, codex-cli 0.162 --json, agy 1.5 stream-json).

def _jsonl(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


CODEX_NOTICE = {"type": "item.completed", "item": {"id": "item_0", "type": "error",
                                                   "message": C.CODEX_CODE_MODE_NOTICE + ". Code mode will fail closed"}}
CODEX_CLEAN = _jsonl({"type": "thread.started"}, CODEX_NOTICE, {"type": "turn.started"},
                     {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
                     {"type": "turn.completed", "usage": {}})
CODEX_TOOL = _jsonl({"type": "thread.started"}, {"type": "turn.started"},
                    {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
                    {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "TOKEN"}},
                    {"type": "turn.completed", "usage": {}})
AGY_LOG = "CLI settings initialized: permissions=&{Allow:[] Deny:[" + " ".join(C.AGY_DENY) + "] Ask:[]}, toolPermission=strict"
AGY_STEP = {"event": "step_update", "step_update": {"state": "DONE", "step_type": "agent_response"}}
AGY_RESULT = {"event": "result", "result": {"status": "SUCCESS", "response": "ICU capacity.", "num_turns": 1}}
AGY_CLEAN = _jsonl({"event": "init", "init": {"tools": ["read_file"]}},
                   {"event": "step_update", "step_update": {"state": "DONE", "step_type": "user_input"}},
                   AGY_STEP, AGY_RESULT)
CLAUDE_INIT = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}
CLAUDE_RESULT = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "OK",
                 "modelUsage": {"claude-opus-5-5": {}}}
CLAUDE_CLEAN = _jsonl(CLAUDE_INIT, {"type": "assistant", "message": {"content": [{"type": "thinking"}, {"type": "text"}]}},
                      CLAUDE_RESULT)


class _Proc:
    def __init__(self, stdout, rc=0):
        self.stdout, self.returncode, self.stderr = stdout, rc, ""


def _fake_cli(monkeypatch, stdout, rc=0, last="answer", log=AGY_LOG):
    seen = []

    def run(cmd, cwd, **kwargs):
        seen.append(cmd)
        if "-o" in cmd and last is not None:
            Path(cmd[cmd.index("-o") + 1]).write_text(last)
        if "--log-file" in cmd and log is not None:
            Path(cmd[cmd.index("--log-file") + 1]).write_text(log)
        return _Proc(stdout, rc)

    monkeypatch.setattr(C.subprocess, "run", run)
    return seen


def _respond(provider):
    return C.cli_responder()(C.Seat(0, "r", provider, "m"), "pre", "prompt")


def test_codex_command_execution_with_rc0_is_rejected(monkeypatch):
    # The reproduction from #140: rc=0, a valid last.txt and a command_execution item.
    _fake_cli(monkeypatch, CODEX_TOOL, last="TOKEN")
    result = _respond("codex")
    assert result["valid"] is False and "item command_execution" in result["invalid_reasons"]
    assert result["meta"]["tool_items"] == ["command_execution"]
    assert result["meta"]["tool_detail"] == [{"type": "command_execution", "args": '{"command": "ls"}'}]


def test_denied_antigravity_tool_call_is_invalid_and_keeps_what_it_tried(monkeypatch):
    # From a real smoke: a seat called run_command unprompted; the deny rule stopped it.
    tool = {"step_type": "tool", "tool_name": "run_command", "tool_info": {"name": "run_command",
            "parameters": {"CommandLine": "ls -la"}}}
    stdout = _jsonl({"event": "init"}, {"event": "step_update", "step_update": dict(tool, state="ACTIVE")},
                    {"event": "step_update", "step_update": dict(tool, state="ERROR", tool_info=dict(
                        tool["tool_info"], error={"message": "Matches user-configured deny rule."}))}, AGY_RESULT)
    _fake_cli(monkeypatch, stdout)
    result = _respond("antigravity")
    assert result["valid"] is False and result["text"] == "ICU capacity."
    assert result["meta"]["tool_detail"] == [{"step": "tool", "tool": "run_command", "state": "ERROR",
                                              "args": '{"CommandLine": "ls -la"}',
                                              "error": "Matches user-configured deny rule."}]


@pytest.mark.parametrize("provider,stdout", [("codex", CODEX_CLEAN), ("antigravity", AGY_CLEAN), ("claude", CLAUDE_CLEAN)])
def test_clean_tool_free_calls_are_valid(monkeypatch, provider, stdout):
    _fake_cli(monkeypatch, stdout)
    result = _respond(provider)
    assert result["valid"] is True and result["invalid_reasons"] == [] and result["text"]


@pytest.mark.parametrize("provider,stdout,kwargs,reason", [
    ("codex", CODEX_CLEAN.replace('"turn.completed"', '"turn.failed"'), {}, "0 completed turns"),
    ("codex", CODEX_CLEAN, {"last": None}, "no last message file"),
    ("codex", CODEX_CLEAN.replace(C.CODEX_CODE_MODE_NOTICE, "something else"), {}, "item error"),
    ("codex", CODEX_CLEAN, {"rc": 1}, "rc 1"),
    ("codex", "not json", {"last": None}, "no last message file"),
    ("antigravity", AGY_CLEAN, {"log": None}, "deny rules not confirmed in log"),
    ("antigravity", AGY_CLEAN, {"log": AGY_LOG.replace("strict", "always-proceed")}, "deny rules not confirmed in log"),
    ("antigravity", _jsonl({"event": "init"}, {"event": "step_update", "step_update": {
        "step_type": "tool", "tool_name": "read_file", "state": "ERROR"}}, AGY_RESULT), {}, "step tool:read_file"),
    ("antigravity", _jsonl({"event": "init"}, {"event": "step_update", "step_update": {
        "step_type": "subagent", "tool_name": "invoke_subagent"}}, AGY_RESULT), {}, "step subagent:invoke_subagent"),
    ("antigravity", _jsonl({"event": "init"}, AGY_STEP), {}, "no result event"),
    ("claude", _jsonl(dict(CLAUDE_INIT, tools=["Read"]), CLAUDE_RESULT), {}, "tools available: 1 tools, 0 mcp servers"),
    ("claude", _jsonl(CLAUDE_INIT, {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}},
                      CLAUDE_RESULT), {}, "content block tool_use"),
    ("claude", _jsonl(CLAUDE_RESULT), {}, "no init event"),
    ("claude", _jsonl(CLAUDE_INIT, dict(CLAUDE_RESULT, num_turns=2)), {}, "result is_error=False num_turns=2"),
])
def test_tool_events_or_unconfirmed_state_fail_closed(monkeypatch, provider, stdout, kwargs, reason):
    _fake_cli(monkeypatch, stdout, **kwargs)
    result = _respond(provider)
    assert result["valid"] is False and reason in result["invalid_reasons"]


def test_cli_commands_switch_tools_off():
    workdir = Path("/tmp/x")
    claude = C.cli_command(C.Seat(0, "r", "claude", "m"), "p", workdir)
    assert claude[claude.index("--tools") + 1] == "" and "stream-json" in claude
    codex = C.cli_command(C.Seat(0, "r", "codex", "m"), "p", workdir)
    disabled = {codex[i + 1] for i, arg in enumerate(codex) if arg == "--disable"}
    assert {"shell_tool", "unified_exec", "code_mode_host"} <= disabled and "web_search=disabled" in codex
    agy = C.cli_command(C.Seat(0, "r", "antigravity", "m"), "p", workdir)
    settings = json.loads(agy[agy.index("--settings") + 1])
    assert settings["toolPermission"] == "strict" and {"command(*)", "read_file(*)"} <= set(settings["permissions"]["deny"])
    assert "--dangerously-skip-permissions" not in agy and "stream-json" in agy


def test_invalid_call_stops_the_game_and_score_leaves_it_out(tmp_path):
    q = QUESTIONS["q2-rootcause"]
    base = C.fixture_responder(q)
    calls = []

    def tainted(seat, phase, prompt):
        calls.append(phase)
        result = base(seat, phase, prompt)
        if phase == "discuss" and seat.index == 1:
            return dict(result, valid=False, invalid_reasons=["item command_execution"])
        return result

    writer = C.GameWriter(tmp_path)
    record = C.run_game(q, 0, "private", 2, _fixture_seats(3), tainted, writer)
    assert record["valid"] is False and "item command_execution" in record["invalid_reason"]
    assert "final" not in calls and all(s["final_pick"] is None for s in record["seats"])
    assert "不计分" in (tmp_path / "games" / f"{record['game']}.md").read_text()
    logged = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert logged[-1]["valid"] is False  # the rejected call is kept for audit
    C.run_game(QUESTIONS["q1-storage"], 0, "private", 1, _fixture_seats(3), C.fixture_responder(QUESTIONS["q1-storage"]),
               writer)
    result = C.score(tmp_path, DATA / "answer_key.json")
    assert [r["game"] for r in result["games"]] == ["q1-storage__private__g0"]
    assert [g["game"] for g in result["invalid_games"]] == [record["game"]]


def test_responder_without_validity_is_treated_as_invalid(tmp_path):
    q = QUESTIONS["q1-storage"]
    base = C.fixture_responder(q)

    def legacy(seat, phase, prompt):
        result = base(seat, phase, prompt)
        result.pop("valid")
        return result

    record = C.run_game(q, 0, "private", 1, _fixture_seats(3), legacy, C.GameWriter(tmp_path))
    assert record["valid"] is False and "validity not reported" in record["invalid_reason"]


def test_score_treats_games_without_a_validity_field_as_invalid(tmp_path):
    _run(tmp_path, "q2-rootcause")
    path = next((tmp_path / "games").glob("*.json"))
    game = json.loads(path.read_text())
    game.pop("valid")
    path.write_text(json.dumps(game))
    result = C.score(tmp_path, DATA / "answer_key.json")
    assert result["games"] == [] and result["invalid_games"][0]["reason"] == "validity not recorded"


# ---------------------------------------------------------------- #141: CLI timeout / launch failure is recorded

def _cli_seats(n):
    return [C.Seat(i, f"r{i}", "claude", "m") for i in range(n)]


@pytest.mark.parametrize("error,reason", [
    (C.subprocess.TimeoutExpired(["claude"], 5, stderr=b"partial output"), "timeout after 5s"),
    (FileNotFoundError(2, "No such file or directory", "/opt/private-tools/bin/claude"), "launch failed: FileNotFoundError errno=2"),
])
def test_cli_failure_voids_the_game_and_is_kept_in_calls_games_and_score(tmp_path, monkeypatch, error, reason):
    # The reproduction from #141: subprocess.run raising used to abort the batch with nothing written.
    def run(cmd, cwd, **kwargs):
        raise error

    monkeypatch.setattr(C.subprocess, "run", run)
    q = QUESTIONS["q1-storage"]
    record = C.run_game(q, 0, "private", 1, _cli_seats(3), C.cli_responder(timeout=5), C.GameWriter(tmp_path))
    assert record["valid"] is False and reason in record["invalid_reason"]
    logged = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert len(logged) == 3 and {c["phase"] for c in logged} == {"pre"}
    assert all(c["valid"] is False and c["invalid_reasons"] == [reason] and c["rc"] is None for c in logged)
    assert "/opt/private-tools" not in (tmp_path / "calls.jsonl").read_text()
    assert (tmp_path / "games" / f"{record['game']}.json").exists()
    result = C.score(tmp_path, DATA / "answer_key.json")
    assert result["games"] == [] and result["invalid_games"][0]["game"] == record["game"]
    assert reason in result["invalid_games"][0]["reason"]


def test_timeout_keeps_a_decoded_stderr_tail(monkeypatch):
    def run(cmd, cwd, **kwargs):
        raise C.subprocess.TimeoutExpired(cmd, kwargs["timeout"], stderr=b"x" * 700 + b"stalled")

    monkeypatch.setattr(C.subprocess, "run", run)
    result = C.cli_responder(timeout=7)(C.Seat(0, "r", "codex", "m"), "pre", "prompt")
    assert result["valid"] is False and result["invalid_reasons"] == ["timeout after 7s"]
    assert result["stderr_tail"].endswith("stalled") and len(result["stderr_tail"]) == 600


def test_a_responder_that_raises_still_leaves_an_invalid_record(tmp_path):
    q = QUESTIONS["q2-rootcause"]
    base = C.fixture_responder(q)

    def crashing(seat, phase, prompt):
        if phase == "discuss":
            raise RuntimeError("boom")
        return base(seat, phase, prompt)

    record = C.run_game(q, 0, "private", 1, _fixture_seats(3), crashing, C.GameWriter(tmp_path))
    assert record["valid"] is False and "responder raised RuntimeError" in record["invalid_reason"]
    assert all(s["pre_pick"] for s in record["seats"])  # what finished before the failure is kept
    logged = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert logged[-1]["phase"] == "discuss" and logged[-1]["valid"] is False
