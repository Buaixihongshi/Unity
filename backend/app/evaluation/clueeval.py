"""Hidden-profile discussion harness: everyone sees the shared materials, each seat holds one private card.

Isolation boundary: running a game only reads questions.json. The answer key (correct option,
decoy, generator rationale) lives in a separate file that only score() opens, after the run.
Every call records which cards were actually found in its initial context; a call whose context
does not contain exactly its own card is refused before any CLI starts.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

CONDITIONS = ("private", "shared_only")
FORBIDDEN_QUESTION_KEYS = {"correct_answer", "rationale", "correct", "decoy", "answer"}
# Daily roles that sit at the table, in seat order; four-seat questions add the custom role.
DEFAULT_ROSTER = ("chulan", "baobao", "wangye", "role-694a37")


class IsolationError(RuntimeError):
    pass


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_sha256(obj) -> str:
    return sha256_bytes(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def load_questions(path: Path) -> Dict[str, dict]:
    data = json.loads(Path(path).read_text())
    questions = {}
    for q in data["questions"]:
        leaked = _find_keys(q, FORBIDDEN_QUESTION_KEYS)
        if leaked:
            raise IsolationError(f"{q['id']}: answer-side fields in question file: {sorted(leaked)}")
        ids = [c["id"] for c in q["cards"]]
        if len(set(ids)) != len(ids) or not ids:
            raise ValueError(f"{q['id']}: card ids must be unique and non-empty")
        if q["lang"] == "en":
            original = q["original"]
            if [c["text"] for c in q["cards"]] != list(original["hidden_information"]):
                raise ValueError(f"{q['id']}: cards differ from the pinned hidden_information")
            if q["options"] != original["possible_answers"]:
                raise ValueError(f"{q['id']}: options differ from the pinned possible_answers")
        questions[q["id"]] = q
    return questions


def _find_keys(obj, keys) -> set:
    found = set()
    if isinstance(obj, dict):
        found |= keys & set(obj)
        for value in obj.values():
            found |= _find_keys(value, keys)
    elif isinstance(obj, list):
        for value in obj:
            found |= _find_keys(value, keys)
    return found


# ---------------------------------------------------------------- prompts

def seat_cards(q: dict, game: int, condition: str) -> List[Optional[dict]]:
    """One distinct card per seat, rotated by game so a card does not stay with one provider."""
    n = len(q["cards"])
    if condition == "shared_only":
        return [None] * n
    return [q["cards"][(i + game) % n] for i in range(n)]


def initial_context(q: dict, card: Optional[dict]) -> str:
    if q["lang"] == "en":
        items = list(q["original"]["shared_information"]) + ([card["text"]] if card else [])
        return q["original"]["description"] + "\n\nInformation you have:\n" + "\n".join("- " + i for i in items)
    text = q["head"] + q["stem"] + "\n\n" + q["shared"]
    if card:
        text += "\n\n【你的私有材料】\n" + card["text"]
    if q.get("tell_private_exists"):
        text += "\n\n队友手里的材料可能和你不同，你看不到。"
    return text


def cards_in(q: dict, text: str) -> List[str]:
    return [c["id"] for c in q["cards"] if c["text"] in text]


def audit_context(q: dict, context: str, card: Optional[dict]) -> List[str]:
    found = cards_in(q, context)
    expected = [card["id"]] if card else []
    if found != expected:
        raise IsolationError(f"{q['id']}: initial context holds {found}, expected {expected}")
    return found


def render_transcript(q: dict, transcript: List[dict]) -> str:
    label = "Participant" if q["lang"] == "en" else "参与者"
    if not transcript:
        return "(no messages yet)" if q["lang"] == "en" else "（还没有人发言）"
    return "\n\n".join(f"{label} {m['seat'] + 1}: {m['text'].strip()}" for m in transcript)


def phase_prompt(q: dict, context: str, phase: str, seat: int, n: int, transcript: List[dict],
                 round_no: int = 0, rounds: int = 0) -> str:
    k = seat + 1
    log = render_transcript(q, transcript)
    if q["lang"] == "en":
        vote = ('Please decide and provide your rationale in the following JSON format: '
                '{"vote": "<one of: ' + ", ".join(q["options"]) + '>", "rationale": "<your reasoning>"}')
        if phase == "pre":
            return f"{context}\n\nYou are Participant {k}. Before the discussion starts, decide on your own. {vote}"
        if phase == "discuss":
            return (f"{context}\n\nYou are Participant {k}, discussing with {n - 1} other participants. "
                    f"This is round {round_no} of {rounds}.\n\nDiscussion so far:\n{log}\n\n"
                    "Write your next message to the group (at most 150 words). Output only the message.")
        return (f"{context}\n\nYou are Participant {k}. The discussion has ended. Full discussion:\n{log}\n\n"
                f"Now decide on your own. {vote}")
    if phase == "pre":
        return f"{context}\n\n你是参与者{k}。讨论开始前，先独立写出你的初步答案，格式按题目要求。"
    if phase == "discuss":
        return (f"{context}\n\n你是参与者{k}，正在和另外 {n - 1} 位参与者讨论这道题，现在是第 {round_no}/{rounds} 轮。"
                f"\n\n【讨论记录】\n{log}\n\n请发言：说出你认为对决策重要的信息和看法，可以回应或追问别人；"
                "讨论中途不要求下结论。只输出发言内容，不超过 300 字。")
    return (f"{context}\n\n你是参与者{k}。讨论已经结束，完整记录如下。\n\n【讨论记录】\n{log}\n\n"
            "现在独立写出你的最终答案，格式按题目要求。")


def parse_pick(q: dict, text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    if q["lang"] == "en":
        for match in re.finditer(r"\{[^{}]*\}", text, re.S):
            try:
                vote = str(json.loads(match.group(0)).get("vote", "")).strip()
            except (ValueError, AttributeError):
                continue
            for option in q["options"]:
                if vote.lower() == option.lower():
                    return option
        return None
    first = next((line for line in text.splitlines() if line.strip()), "")
    match = re.search(q["answer_pattern"], first)
    if not match:
        return None
    return q.get("aliases", {}).get(match.group(1), match.group(1))


# ---------------------------------------------------------------- responders

@dataclass
class Seat:
    index: int
    role: str
    provider: str
    model: str


Responder = Callable[[Seat, str, str], dict]


def fixture_responder(q: dict) -> Responder:
    """Deterministic offline stand-in: answers the first option and repeats its own card when speaking."""
    def respond(seat: Seat, phase: str, prompt: str) -> dict:
        if phase == "discuss":
            mine = [c for c in q["cards"] if c["text"] in prompt.split("【讨论记录】")[0].split("Discussion so far:")[0]]
            text = "我的材料：" + (mine[0]["text"] if mine else "只有共享材料")
        elif q["lang"] == "en":
            text = json.dumps({"vote": q["options"][0], "rationale": "fixture"})
        else:
            text = q["answer_format"].format(option=q["options"][0]) + "\n- fixture"
        return {"text": text, "rc": 0, "seconds": 0.0, "meta": {"fixture": True}}
    return respond


_SEMAPHORES = {"claude": threading.Semaphore(3), "codex": threading.Semaphore(3), "antigravity": threading.Semaphore(2)}


def cli_command(seat: Seat, prompt: str, workdir: Path) -> List[str]:
    """Stateless, tool-free calls in an empty temp dir; model comes from the daily route."""
    if seat.provider == "claude":
        return ["claude", "-p", "--model", seat.model, "--output-format", "json", "--tools", "",
                "--strict-mcp-config", "--no-session-persistence", "--setting-sources", "", prompt]
    if seat.provider == "codex":
        return ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-m", seat.model,
                "--json", "-o", str(workdir / "last.txt"), prompt]
    if seat.provider == "antigravity":
        return ["agy", "--model", seat.model, "--new-project", "--disable-slash-commands",
                "--output-format", "json", "--log-file", str(workdir / "agy.log"), "-p=" + prompt]
    raise ValueError(f"unsupported provider {seat.provider}")


def cli_responder(timeout: int = 600) -> Responder:
    def respond(seat: Seat, phase: str, prompt: str) -> dict:
        with _SEMAPHORES[seat.provider], tempfile.TemporaryDirectory(prefix="clueeval-call-") as tmp:
            workdir = Path(tmp)
            started = time.time()
            proc = subprocess.run(cli_command(seat, prompt, workdir), cwd=workdir, capture_output=True,
                                  text=True, stdin=subprocess.DEVNULL, timeout=timeout)
            seconds = round(time.time() - started, 1)
            text, meta = None, {}
            try:
                if seat.provider == "claude":
                    out = json.loads(proc.stdout)
                    text = out.get("result")
                    meta = {"model_usage": list(out.get("modelUsage", {})), "num_turns": out.get("num_turns")}
                elif seat.provider == "codex":
                    text = (workdir / "last.txt").read_text()
                    events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
                    items = [e.get("item", {}).get("type") for e in events if e.get("type") == "item.completed"]
                    meta = {"tool_items": [t for t in items if t not in ("agent_message", "reasoning")]}
                else:
                    text = json.loads(proc.stdout).get("response")
                    log = workdir / "agy.log"
                    meta = {"tool_calls_in_log": len(re.findall(r"tool[_ ]?call", log.read_text(errors="replace"), re.I))
                            if log.exists() else None}
            except (ValueError, OSError, AttributeError) as exc:
                meta["parse_error"] = type(exc).__name__
            return {"text": text, "rc": proc.returncode, "seconds": seconds, "meta": meta,
                    "stderr_tail": proc.stderr[-600:]}
    return respond


def resolve_roster(hub_settings: dict, n: int, roles=DEFAULT_ROSTER) -> List[Seat]:
    if n > len(roles):
        raise ValueError(f"need {n} seats, roster has {len(roles)} roles")
    agents = dict(hub_settings.get("agents", {}))
    agents.update({a["id"]: a for a in hub_settings.get("custom_agents", [])})
    seats = []
    for i, role in enumerate(roles[:n]):
        route = agents.get(role)
        if not route or not route.get("default_provider_id"):
            raise ValueError(f"role {role} has no route in the routing file")
        provider = route["default_provider_id"]
        model = (route.get("provider_models") or {}).get(provider)
        if provider not in _SEMAPHORES or not model:
            raise ValueError(f"role {role}: route {provider}/{model} is not a pinned CLI model")
        seats.append(Seat(i, role, provider, model))
    return seats


# ---------------------------------------------------------------- run

@dataclass
class GameWriter:
    out: Path
    lock: threading.Lock = field(default_factory=threading.Lock)

    def call(self, record: dict) -> None:
        with self.lock, (self.out / "calls.jsonl").open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_game(q: dict, game: int, condition: str, rounds: int, seats: List[Seat], respond: Responder,
             writer: GameWriter) -> dict:
    n = len(q["cards"])
    if len(seats) != n:
        raise ValueError(f"{q['id']} needs {n} seats, got {len(seats)}")
    cards = seat_cards(q, game, condition)
    contexts = [initial_context(q, card) for card in cards]
    received = [audit_context(q, ctx, card) for ctx, card in zip(contexts, cards)]
    gid = f"{q['id']}__{condition}__g{game}"
    transcript: List[dict] = []

    def call(seat: Seat, phase: str, round_no: int = 0) -> dict:
        prompt = phase_prompt(q, contexts[seat.index], phase, seat.index, n, transcript, round_no, rounds)
        if not prompt.startswith(contexts[seat.index]):
            raise IsolationError("prompt must start with the audited context")
        result = respond(seat, phase, prompt)
        writer.call({"game": gid, "seat": seat.index + 1, "role": seat.role, "provider": seat.provider,
                     "model": seat.model, "phase": phase, "round": round_no,
                     "cards_in_initial_context": received[seat.index],
                     "cards_in_transcript": cards_in(q, prompt[len(contexts[seat.index]):]),
                     "context_sha256": sha256_bytes(contexts[seat.index].encode()),
                     "prompt": prompt, **result, "pick": parse_pick(q, result["text"]) if phase != "discuss" else None})
        return result

    def everyone(phase: str) -> List[dict]:
        with ThreadPoolExecutor(n) as pool:
            return list(pool.map(lambda s: call(s, phase), seats))

    pre = everyone("pre")
    start = game % n
    for round_no in range(1, rounds + 1):
        for offset in range(n):
            seat = seats[(start + offset) % n]
            result = call(seat, "discuss", round_no)
            transcript.append({"round": round_no, "seat": seat.index, "text": result["text"] or ""})
    final = everyone("final")
    record = {
        "game": gid, "question": q["id"], "condition": condition, "game_index": game, "rounds": rounds,
        "speaking_order": [(start + o) % n + 1 for o in range(n)],
        "seats": [{"seat": s.index + 1, "role": s.role, "provider": s.provider, "model": s.model,
                   "card": cards[s.index]["id"] if cards[s.index] else None,
                   "cards_in_initial_context": received[s.index],
                   "pre_pick": parse_pick(q, pre[s.index]["text"]), "final_pick": parse_pick(q, final[s.index]["text"]),
                   "rc": [pre[s.index]["rc"], final[s.index]["rc"]]} for s in seats],
        "transcript": transcript,
    }
    games = writer.out / "games"
    games.mkdir(exist_ok=True)
    (games / f"{gid}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1))
    (games / f"{gid}.md").write_text(_markdown(q, record))
    return record


def _markdown(q: dict, record: dict) -> str:
    lines = [f"# {record['game']}", "", "| 席位 | 角色 | 模型 | 私卡 | 讨论前 | 讨论后 |", "|---|---|---|---|---|---|"]
    for s in record["seats"]:
        lines.append(f"| {s['seat']} | {s['role']} | {s['provider']}/{s['model']} | {s['card'] or '—'} "
                     f"| {s['pre_pick']} | {s['final_pick']} |")
    lines += ["", f"发言顺序：{record['speaking_order']}，共 {record['rounds']} 轮", ""]
    for m in record["transcript"]:
        lines += [f"**第 {m['round']} 轮 · 参与者 {m['seat'] + 1}**", "", m["text"].strip(), ""]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- score (answer key opened only here)

def score(out: Path, answer_key_path: Path) -> dict:
    key = json.loads(Path(answer_key_path).read_text())["answers"]
    rows = []
    for path in sorted((Path(out) / "games").glob("*.json")):
        game = json.loads(path.read_text())
        answer = key[game["question"]]
        picks = [s["final_pick"] for s in game["seats"]]
        majority, votes = Counter(picks).most_common(1)[0] if picks else (None, 0)
        rows.append({"game": game["game"], "correct": answer["correct"],
                     "pre_correct": sum(s["pre_pick"] == answer["correct"] for s in game["seats"]),
                     "final_correct": sum(p == answer["correct"] for p in picks), "seats": len(picks),
                     "majority": majority if votes * 2 > len(picks) else None,
                     "unanimous": len(set(picks)) == 1, "unparsed_final": picks.count(None)})
    result = {"answer_key_sha256": sha256_bytes(Path(answer_key_path).read_bytes()), "games": rows}
    (Path(out) / "score.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    return result
