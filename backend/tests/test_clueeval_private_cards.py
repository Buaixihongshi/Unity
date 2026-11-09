import json
import re
import subprocess
import sys
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
    assert list(QUESTIONS) == ["q1-storage", "q2-rootcause", "hb-009-critical_hospital_transfer",
                               "hb-009-critical_hospital_transfer-zh"]
    assert [len(q["cards"]) for q in QUESTIONS.values()] == [3, 3, 4, 4]


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
        context = call["prompt"][: len(C.initial_context(q, [c for c in q["cards"] if c["id"] == own]))]
        assert C.cards_in(q, context) == [own]
        assert KEY["canary"] not in call["prompt"]
        rationale = KEY["answers"][qid].get("rationale")
        if rationale:
            assert rationale[:80] not in call["prompt"]
    shared = C._hb_text(q)[1][0] if C.is_hiddenbench(q) else q["shared"]
    assert all(shared in call["prompt"] for call in calls)


def test_cards_rotate_between_games(tmp_path):
    q = QUESTIONS["q2-rootcause"]
    assert [c[0]["id"] for c in C.seat_cards(q, 0, "private")] != [c[0]["id"] for c in C.seat_cards(q, 1, "private")]


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
        C.audit_context(q, C.initial_context(q, [own]) + "\n" + other["text"], [own])


def test_run_does_not_need_the_answer_key(tmp_path, monkeypatch):
    real_open = Path.read_text

    def guarded(self, *a, **k):
        assert self.name != "answer_key.json", "run phase opened the answer key"
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", guarded)
    _run(tmp_path, "hb-009-critical_hospital_transfer")


@pytest.mark.parametrize("qid", ["hb-009-critical_hospital_transfer", "hb-009-critical_hospital_transfer-zh"])
def test_hiddenbench_seats_are_not_told_others_differ(tmp_path, qid):
    _, _, calls = _run(tmp_path, qid)
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
    ("hb-009-critical_hospital_transfer-zh", '```json\n{"vote": "医院A", "rationale": "山口已通"}\n```', "医院A"),
    ("hb-009-critical_hospital_transfer-zh", '{"vote": "Hospital A"}', None),
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
    routing["agents"]["baobao"]["provider_efforts"] = {"codex": "high", "claude": "low"}
    seats = C.resolve_roster(routing, 4)
    assert [(s.role, s.provider, s.model) for s in seats][-1] == ("role-694a37", "antigravity", "g-low")
    assert [s.effort for s in seats] == ["", "high", "", ""]  # only the routed provider's effort applies
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


# ---------------------------------------------------------------- formal batch: Chinese HiddenBench, efforts, solo baseline

def test_chinese_hiddenbench_is_a_pinned_translation_of_the_same_item():
    zh, en = QUESTIONS["hb-009-critical_hospital_transfer-zh"], QUESTIONS["hb-009-critical_hospital_transfer"]
    assert zh["original"] == en["original"] and zh["origin"]["item_sha256"] == en["origin"]["item_sha256"]
    assert zh["origin"]["translation"]["version"] == "zh-1"
    assert [c["id"] for c in zh["cards"]] == [c["id"] for c in en["cards"]]
    assert KEY["answers"][zh["id"]]["correct"] == "医院A" and KEY["answers"][zh["id"]]["correct"] in zh["options"]
    context = C.initial_context(zh, [zh["cards"][0]])
    assert "Hospital" not in context and context.rstrip().endswith(zh["cards"][0]["text"])
    prompt = C.phase_prompt(zh, context, "final", 0, 4, [])
    assert '"vote"' in prompt and "医院A、医院B、医院C" in prompt


def test_route_effort_reaches_each_cli_command():
    workdir = Path("/tmp/x")
    claude = C.cli_command(C.Seat(0, "r", "claude", "opus", "high"), "p", workdir)
    assert claude[claude.index("--effort") + 1] == "high" and claude[-1] == "p"
    codex = C.cli_command(C.Seat(0, "r", "codex", "m", "high"), "p", workdir)
    assert 'model_reasoning_effort="high"' in codex and codex[-1] == "p"
    assert "--effort" not in C.cli_command(C.Seat(0, "r", "antigravity", "g-high"), "p", workdir)
    assert not any("model_reasoning_effort" in a for a in C.cli_command(C.Seat(0, "r", "codex", "m"), "p", workdir))


@pytest.mark.parametrize("qid", ["q1-storage", "q2-rootcause", "hb-009-critical_hospital_transfer-zh"])
def test_solo_full_seat_gets_every_card_and_answers_once(tmp_path, qid):
    q = QUESTIONS[qid]
    writer = C.GameWriter(tmp_path)
    record = C.run_game(q, 2, "solo_full", 3, [C.Seat(0, "chulan", "fixture", "fixture")], C.fixture_responder(q), writer)
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    ids = [c["id"] for c in q["cards"]]
    assert len(calls) == 1 and calls[0]["phase"] == "solo" and calls[0]["cards_in_initial_context"] == ids
    assert record["valid"] and record["game"] == f"{qid}__solo_full__chulan__r2"
    assert record["seats"][0]["final_pick"] == q["options"][0]
    assert "参与者" not in calls[0]["prompt"] and "你看不到" not in calls[0]["prompt"]
    assert KEY["canary"] not in calls[0]["prompt"]
    row = C.score(tmp_path, DATA / "answer_key.json")["games"][0]
    assert row["condition"] == "solo_full" and row["role"] == "chulan" and row["seats"] == 1


def test_solo_full_refuses_a_table_and_records_a_failed_call(tmp_path):
    q = QUESTIONS["q1-storage"]
    with pytest.raises(ValueError):
        C.run_game(q, 0, "solo_full", 3, _fixture_seats(3), C.fixture_responder(q), C.GameWriter(tmp_path))

    def boom(seat, phase, prompt):
        raise RuntimeError("x")

    record = C.run_game(q, 0, "solo_full", 3, _fixture_seats(1), boom, C.GameWriter(tmp_path))
    assert not record["valid"] and "responder raised RuntimeError" in record["invalid_reason"]
    assert C.score(tmp_path, DATA / "answer_key.json")["invalid_games"][0]["game"] == record["game"]


def test_call_stats_sum_tokens_and_keep_cost_missing_when_a_cli_gives_none(tmp_path):
    rows = [{"game": "g", "provider": "claude", "valid": True, "seconds": 2.0,
             "meta": {"usage": {"input_tokens": 10, "output_tokens": 5}, "cost_usd": 0.01}},
            {"game": "g", "provider": "claude", "valid": True, "seconds": 1.5,
             "meta": {"usage": {"input_tokens": 1}, "cost_usd": 0.02}},
            {"game": "g", "provider": "codex", "valid": False, "seconds": 3.0,
             "meta": {"usage": {"input_tokens": 7}, "cost_usd": None}}]
    (tmp_path / "calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    stats = C.call_stats(tmp_path)["g"]
    assert stats["claude"] == {"calls": 2, "invalid_calls": 0, "call_seconds": 3.5,
                               "tokens": {"input_tokens": 11, "output_tokens": 5}, "cost_usd": 0.03}
    assert stats["codex"]["cost_usd"] is None and stats["codex"]["invalid_calls"] == 1


def test_usage_extraction_keeps_numbers_only():
    assert C._usage({"input_tokens": 3, "cached": True, "server_tool_use": {"x": 1}}) == {"input_tokens": 3}
    assert C._usage(None) is None and C._usage({}) is None


def test_clean_call_stderr_keeps_no_home_or_temp_path(monkeypatch):
    # The reproduction from #144: a valid Codex call whose stderr warns with the home path.
    home = str(Path.home())
    warn = (f'WARN codex_core::shell_snapshot: Failed to delete shell snapshot at AbsolutePathBuf("{home}/.codex/'
            'shell_snapshots/abc.sh") other=' + "/".join(["", "Users", "someone", "x"])
            + ' tmp=/var/folders/zz/T/clueeval-call-1/last.txt\n')

    def run(cmd, cwd, **kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_text("answer")
        proc = _Proc(CODEX_CLEAN)
        proc.stderr = "y" * 590 + warn
        return proc

    monkeypatch.setattr(C.subprocess, "run", run)
    result = _respond("codex")
    tail = result["stderr_tail"]
    assert result["valid"] is True and len(tail) <= 600
    assert home not in tail and "/Users/" not in tail and "/var/folders" not in tail
    assert '"~/.codex/shell_snapshots/abc.sh"' in tail and "<tmp>" in tail
    assert C.stderr_tail(b"err " + home.encode() + b"/a") == "err ~/a"
    # Cutting first would start the tail inside the home path and keep the end of the user name.
    assert C.stderr_tail(home + "/" + "b" * 595) == "~/" + "b" * 595


# Joined at runtime so the privacy check does not read these synthetic homes as committed local paths.
_USERS, _HOMES = "/".join(["", "Users", ""]), "/".join(["", "home", ""])


def _fake_roots(monkeypatch, home, tmp):
    monkeypatch.setattr(C.Path, "home", classmethod(lambda cls: Path(home)))
    monkeypatch.setattr(C.tempfile, "gettempdir", lambda: tmp)


def test_stderr_tail_replaces_known_roots_only_at_a_directory_boundary(monkeypatch):
    # #144 review: a bare replace turned another user's reviewer-private home into ~-private, keeping the name.
    me = _USERS + "reviewer"
    _fake_roots(monkeypatch, me, "/var/folders/ab/T")
    assert C.stderr_tail(f"a {me}-private/work/x b") == "a ~/work/x b"
    assert C.stderr_tail(f"{me}/a {me}\n{me}: x") == "~/a ~\n~: x"
    assert C.stderr_tail("/var/folders/ab/T/c /var/folders/ab/Tmp2/d") == "<tmp>/c <tmp>"


def test_stderr_tail_keeps_no_part_of_a_path_with_spaces(monkeypatch):
    _fake_roots(monkeypatch, _USERS + "Jane Doe", "/tmp/run dir")
    # The caller's own roots are known, so their spaces need no quoting.
    assert C.stderr_tail(f"open {_USERS}Jane Doe/x failed") == "open ~/x failed"
    assert C.stderr_tail("at /tmp/run dir/f.txt now") == "at <tmp>/f.txt now"
    # Other users and temp paths: quoted or backslash-escaped spaces belong to the path.
    for raw, clean in ((f'p="{_USERS}Other Name/x"', 'p="~/x"'), (f"p='{_HOMES}Other Name/x'", "p='~/x'"),
                       (f"p={_USERS}Other\\ Name/x y", "p=~/x y"), ('f="/tmp/my dir/f.txt" z', 'f="<tmp>" z'),
                       ("f=/private/var/folders/q/my\\ dir/f z", "f=<tmp> z")):
        tail = C.stderr_tail(raw)
        assert tail == clean and "Name" not in tail and "my" not in tail
    # Scrubbed before the cut, so the cut cannot start inside a spaced user name.
    assert C.stderr_tail(f'"{_USERS}Other Name/' + "b" * 596 + '"') == '"~/' + "b" * 596 + '"'


@pytest.mark.parametrize("q", ['"', "'"])
def test_quoted_sibling_of_a_known_root_with_a_space_keeps_no_name(monkeypatch, q):
    # #144 review of cf5c872: the known root was replaced first with a space as its end, giving "~ Private/...".
    me, tmp = _USERS + "reviewer", "/tmp/review-run"
    _fake_roots(monkeypatch, me, tmp)
    assert C.stderr_tail(f"open {q}{me} Private/work/x{q} failed") == f"open {q}~/work/x{q} failed"
    assert C.stderr_tail(f"{q}{tmp} Private/file.txt{q}") == f"{q}<tmp>{q}"
    # The roots themselves inside quotes, and a sibling outside them, are unchanged by the split.
    assert C.stderr_tail(f"{q}{me}/a{q} {q}{tmp}/b{q} {me}-private/c") == f"{q}~/a{q} {q}<tmp>/b{q} ~/c"
    # Scrubbed before the cut: the cut cannot start inside the sibling's name.
    tail = C.stderr_tail(f"{q}{me} Private/" + "b" * 596 + q)
    assert tail == f"{q}~/" + "b" * 596 + q and "Private" not in tail
    # A root no generic pattern knows: inside quotes it still ends at the quote, and a spaced sibling is not it.
    _fake_roots(monkeypatch, me, "/scratch/review-run")
    assert C.stderr_tail(f"{q}/scratch/review-run{q}") == f"{q}<tmp>{q}"
    assert "<tmp>" not in C.stderr_tail(f"{q}/scratch/review-run Private/f{q}")


@pytest.mark.parametrize("q,r", [('"', "'"), ("'", '"')])
def test_other_quote_kind_inside_a_quoted_path_keeps_no_name(monkeypatch, q, r):
    # #144 review of 342ff6e: either quote kind closed a quoted stretch, so a quoted O'Brien home gave "~'Brien/...".
    me, tmp = _USERS + "reviewer", "/tmp/review-run"
    _fake_roots(monkeypatch, me, tmp)
    for raw, clean in ((f"{q}{me} O{r}Brien/work/x{q}", f"{q}~/work/x{q}"),
                       (f"{q}{tmp} O{r}Brien/file.txt{q}", f"{q}<tmp>{q}"),
                       (f"{q}{_USERS}Jane O{r}Brien/x{q}", f"{q}~/x{q}"),
                       (f"{q}{_HOMES}Jane O{r}Brien/x{q}", f"{q}~/x{q}"),
                       (f"{q}/var/folders/zz/T O{r}Brien/f{q}", f"{q}<tmp>{q}"),
                       # A sibling whose name goes on with the other quote kind is not the known root.
                       (f"{q}{me}{r}Brien/work/x{q}", f"{q}~/work/x{q}"),
                       # The outer quote escaped inside its own stretch is part of the name, not its end.
                       (f"{q}{me} O\\{q}Brien/work/x{q}", f"{q}~/work/x{q}"),
                       (f"{q}{tmp} O\\{q}Brien/f{q}", f"{q}<tmp>{q}"),
                       # An apostrophe in a word before the path does not open a stretch of its own.
                       (f"can't open {q}{me} O{r}Brien/x{q} now", f"can't open {q}~/x{q} now"),
                       (f"{q}{me}/a{q} {q}{tmp} O{r}Brien/b{q} {me}-private/c", f"{q}~/a{q} {q}<tmp>{q} ~/c")):
        tail = C.stderr_tail(raw)
        assert tail == clean and "Brien" not in tail and "Jane" not in tail
    # Scrubbed before the cut: the cut cannot start inside the other user's name.
    assert C.stderr_tail(f"{q}{me} O{r}Brien/" + "b" * 596 + q) == f"{q}~/" + "b" * 596 + q
    # A quoted temp path is replaced whole, so a long one leaves nothing for the cut either.
    tail = C.stderr_tail("a" * 596 + f" {q}{tmp} O{r}Brien/" + "b" * 596 + q)
    assert tail == ("a" * 596 + f" {q}<tmp>{q}")[-600:] and "Brien" not in tail
    # A root no generic pattern knows ends only at its own outer quote, not at the other kind.
    _fake_roots(monkeypatch, me, "/scratch/review-run")
    assert C.stderr_tail(f"{q}/scratch/review-run{q}") == f"{q}<tmp>{q}"
    assert "<tmp>" not in C.stderr_tail(f"{q}/scratch/review-run{r}Brien/f{q}")


def test_agy_deny_list_uses_only_actions_the_cli_accepts():
    # agy 1.5.3 drops unknown actions with a warning (#145): browser(*) and search_web(*) never blocked anything.
    accepted = re.compile(r"^(command|read_file|write_file|read_url|mcp|execute_url|download_url|unsandboxed)\s*\(.*\)$")
    assert all(accepted.match(rule) for rule in C.AGY_DENY)


def test_agy_call_is_invalid_when_the_cli_ignores_a_deny_entry(monkeypatch):
    log = AGY_LOG + '\nW permission_grant_store.go:526] ignoring invalid deny entry "x(*)": unknown action "x"'
    _fake_cli(monkeypatch, AGY_CLEAN, log=log)
    result = _respond("antigravity")
    assert result["valid"] is False and result["invalid_reasons"] == ["deny rule ignored by CLI"]


def test_agy_search_web_step_voids_the_call(monkeypatch):
    # The step shape from formal-20261109-1: search_web ran (DONE), nothing blocked it.
    step = {"step_type": "tool", "tool_name": "search_web", "state": "DONE",
            "tool_info": {"name": "search_web", "parameters": {"query": "clueeval"}}}
    _fake_cli(monkeypatch, _jsonl({"event": "init"}, {"event": "step_update", "step_update": step}, AGY_RESULT))
    result = _respond("antigravity")
    assert result["valid"] is False and "step tool:search_web" in result["invalid_reasons"]


def _cli(*args, cwd):
    script = Path(__file__).resolve().parents[2] / "scripts/run_clueeval.py"
    return subprocess.run([sys.executable, str(script), *args], cwd=cwd, capture_output=True, text=True, timeout=120)


def test_default_solo_full_selection_is_refused_before_any_call(tmp_path):
    # The reproduction from #148: the default selection includes the English item; it used to stop after 8 calls.
    out = tmp_path / "solo"
    proc = _cli("run", "--mode", "fixture", "--condition", "solo_full", "--output", str(out), cwd=tmp_path)
    assert proc.returncode == 2 and "hb-009-critical_hospital_transfer/solo_full" in proc.stderr
    assert not out.exists()
    proc = _cli("run", "--mode", "fixture", "--question", "nope", "--output", str(out), cwd=tmp_path)
    assert proc.returncode == 2 and "unknown question nope" in proc.stderr and not out.exists()


def test_solo_full_on_the_chinese_selection_finishes_the_batch(tmp_path):
    out = tmp_path / "solo"
    qids = ["q1-storage", "q2-rootcause", "hb-009-critical_hospital_transfer-zh"]
    args = [a for qid in qids for a in ("--question", qid)]
    proc = _cli("run", "--mode", "fixture", "--condition", "solo_full", "--games", "2", *args, "--output", str(out),
                cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["finished_at"] and manifest["invalid_games"] == 0
    calls = (out / "calls.jsonl").read_text().splitlines()
    assert len(calls) == len(qids) * len(C.DEFAULT_ROSTER) * 2
