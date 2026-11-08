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
