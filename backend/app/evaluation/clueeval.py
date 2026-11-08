"""Hidden-profile discussion harness: everyone sees the shared materials, each seat holds one private card.

Isolation boundary: running a game only reads questions.json. The answer key (correct option,
decoy, generator rationale) lives in a separate file that only score() opens, after the run.
Every call records which cards were actually found in its initial context; a call whose context
does not contain exactly its own card is refused before any CLI starts.

Calls must be tool-free. A responder marks a call invalid when it sees a tool event or cannot confirm
there was none; the game stops there, is written with its reasons, and score() leaves it out.
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


class InvalidCall(RuntimeError):
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
        return {"text": text, "rc": 0, "seconds": 0.0, "meta": {"fixture": True}, "valid": True,
                "invalid_reasons": []}
    return respond


_SEMAPHORES = {"claude": threading.Semaphore(3), "codex": threading.Semaphore(3), "antigravity": threading.Semaphore(2)}

# Codex has no "no tools" switch: turn off every feature that can run code or reach outside the prompt.
# A read-only sandbox alone still lets the model run shell commands that read any local file.
CODEX_DISABLED_FEATURES = ("shell_tool", "unified_exec", "code_mode_host", "apps", "plugins", "multi_agent",
                           "browser_use", "computer_use", "in_app_browser", "image_generation", "view_image",
                           "memories", "hooks", "skill_search", "tool_suggest", "goals", "sleep_tool")
# Emitted on every call once code_mode_host is off; the only non-message item a clean call may carry.
CODEX_CODE_MODE_NOTICE = "Code Mode is unavailable because code-mode host is disabled"
# Antigravity cannot drop its tools, so every tool class is denied. The daily settings may say
# always-proceed; --settings overrides that for this call only.
AGY_DENY = ("command(*)", "read_file(*)", "read_file(/)", "write_file(*)", "mcp(*)", "read_url(*)",
            "execute_url(*)", "browser(*)")
AGY_SETTINGS = json.dumps({"toolPermission": "strict", "permissions": {"deny": list(AGY_DENY)}})


def cli_command(seat: Seat, prompt: str, workdir: Path) -> List[str]:
    """Stateless, tool-free calls in an empty temp dir; model comes from the daily route.

    Each CLI streams its events so inspect_* can prove no tool ran; anything it cannot prove fails closed.
    """
    if seat.provider == "claude":
        return ["claude", "-p", "--model", seat.model, "--output-format", "stream-json", "--verbose", "--tools", "",
                "--strict-mcp-config", "--no-session-persistence", "--setting-sources", "", prompt]
    if seat.provider == "codex":
        disabled = [arg for feature in CODEX_DISABLED_FEATURES for arg in ("--disable", feature)]
        return ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "-m", seat.model,
                *disabled, "-c", "web_search=disabled", "-c", "mcp_servers={}",
                "--json", "-o", str(workdir / "last.txt"), prompt]
    if seat.provider == "antigravity":
        return ["agy", "--model", seat.model, "--new-project", "--disable-slash-commands",
                "--output-format", "stream-json", "--settings", AGY_SETTINGS,
                "--log-file", str(workdir / "agy.log"), "-p=" + prompt]
    raise ValueError(f"unsupported provider {seat.provider}")


def _events(stdout: str) -> List[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip().startswith("{")]


def inspect_claude(stdout: str):
    """(text, meta, violations) from claude stream-json; a clean call has no tools and one turn."""
    events = _events(stdout)
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
    result = next((e for e in events if e.get("type") == "result"), None)
    violations = []
    if init is None:
        violations.append("no init event")
    elif init.get("tools") != [] or init.get("mcp_servers") != []:
        violations.append(f"tools available: {len(init.get('tools') or [])} tools, "
                          f"{len(init.get('mcp_servers') or [])} mcp servers")
    blocks = [b.get("type") for e in events if e.get("type") == "assistant"
              for b in (e.get("message") or {}).get("content") or []]
    violations += [f"content block {t}" for t in blocks if t not in ("text", "thinking", "redacted_thinking")]
    if result is None:
        violations.append("no result event")
        return None, {}, violations
    if result.get("is_error") or result.get("num_turns") != 1:
        violations.append(f"result is_error={result.get('is_error')} num_turns={result.get('num_turns')}")
    meta = {"model_usage": list(result.get("modelUsage") or {}), "num_turns": result.get("num_turns")}
    return result.get("result"), meta, violations


def inspect_codex(stdout: str, last_text: Optional[str]):
    events = _events(stdout)
    items = [e.get("item") or {} for e in events if e.get("type") == "item.completed"]
    started = [e.get("item") or {} for e in events if e.get("type") == "item.started"]
    violations = []
    for item in items + started:
        kind = item.get("type")
        if kind in ("agent_message", "reasoning"):
            continue
        if kind == "error" and str(item.get("message", "")).startswith(CODEX_CODE_MODE_NOTICE):
            continue
        violations.append(f"item {kind}")
    detail = [{"type": i.get("type"), "args": json.dumps({k: v for k, v in i.items() if k not in ("id", "type", "text")},
                                                          ensure_ascii=False)[:200]}
              for i in items if i.get("type") not in ("agent_message", "reasoning", "error")]
    turns = sum(e.get("type") == "turn.completed" for e in events)
    if turns != 1:
        violations.append(f"{turns} completed turns")
    if last_text is None:
        violations.append("no last message file")
    meta = {"tool_items": sorted({v[5:] for v in violations if v.startswith("item ")}), "tool_detail": detail}
    return last_text, meta, violations


def inspect_antigravity(stdout: str, log_text: Optional[str]):
    events = _events(stdout)
    steps = [e.get("step_update") or {} for e in events if e.get("event") == "step_update"]
    result = next((e.get("result") or {} for e in events if e.get("event") == "result"), None)
    violations = []
    if not any(e.get("event") == "init" for e in events):
        violations.append("no init event")
    detail = []
    for step in steps:
        if step.get("tool_name") or step.get("step_type") not in ("user_input", "agent_response"):
            violations.append(f"step {step.get('step_type')}:{step.get('tool_name')}")
            if step.get("state") != "ACTIVE":  # the finished step repeats the call with its outcome
                info = step.get("tool_info") or step.get("subagent_info") or {}
                detail.append({"step": step.get("step_type"), "tool": step.get("tool_name"), "state": step.get("state"),
                               "args": json.dumps(info.get("parameters", info), ensure_ascii=False)[:200],
                               "error": ((info.get("error") or {}).get("message") or "")[:200]})
    # The deny rules only count if the CLI says it loaded them.
    if not log_text or "toolPermission=strict" not in log_text or "Deny:[" + " ".join(AGY_DENY) + "]" not in log_text:
        violations.append("deny rules not confirmed in log")
    if result is None:
        violations.append("no result event")
        return None, {}, violations
    if result.get("status") != "SUCCESS":
        violations.append(f"status {result.get('status')}")
    meta = {"tool_steps": sorted({v[5:] for v in violations if v.startswith("step ")}), "tool_detail": detail,
            "num_turns": result.get("num_turns")}
    return result.get("response"), meta, violations


def failed_call(reason: str, seconds: Optional[float] = None, stderr=None) -> dict:
    """A call that produced no usable answer; kept as an invalid record instead of an exception."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    return {"text": None, "rc": None, "seconds": seconds, "meta": {"failure": reason}, "valid": False,
            "invalid_reasons": [reason], "stderr_tail": (stderr or "")[-600:]}


def cli_responder(timeout: int = 600) -> Responder:
    def respond(seat: Seat, phase: str, prompt: str) -> dict:
        with _SEMAPHORES[seat.provider], tempfile.TemporaryDirectory(prefix="clueeval-call-") as tmp:
            workdir = Path(tmp)
            started = time.time()
            try:
                proc = subprocess.run(cli_command(seat, prompt, workdir), cwd=workdir, capture_output=True,
                                      text=True, stdin=subprocess.DEVNULL, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                # run() has already killed the CLI; partial output is not inspected, the call is void either way.
                return failed_call(f"timeout after {timeout}s", round(time.time() - started, 1), exc.stderr)
            except OSError as exc:
                # Error class and errno only: the message can carry the executable's full path.
                return failed_call(f"launch failed: {type(exc).__name__} errno={exc.errno}",
                                   round(time.time() - started, 1))
            seconds = round(time.time() - started, 1)
            text, meta, violations = None, {}, []
            try:
                if seat.provider == "claude":
                    text, meta, violations = inspect_claude(proc.stdout)
                elif seat.provider == "codex":
                    last = workdir / "last.txt"
                    text, meta, violations = inspect_codex(proc.stdout, last.read_text() if last.exists() else None)
                else:
                    log = workdir / "agy.log"
                    text, meta, violations = inspect_antigravity(
                        proc.stdout, log.read_text(errors="replace") if log.exists() else None)
            except (ValueError, OSError, AttributeError, TypeError) as exc:
                meta["parse_error"] = type(exc).__name__
                violations.append(f"unparseable output: {type(exc).__name__}")
            if proc.returncode != 0:
                violations.append(f"rc {proc.returncode}")
            if not text:
                violations.append("empty response")
            return {"text": text, "rc": proc.returncode, "seconds": seconds, "meta": meta,
                    "valid": not violations, "invalid_reasons": violations, "stderr_tail": proc.stderr[-600:]}
    return respond


# User-level instruction files the CLIs load on their own, outside the prompt we build.
GLOBAL_INSTRUCTION_FILES = {"claude": ".claude/CLAUDE.md", "codex": ".codex/AGENTS.md", "antigravity": ".gemini/GEMINI.md"}


def global_instructions(home: Optional[Path] = None) -> Dict[str, Optional[str]]:
    """Digest (not content or path) of each CLI's user-level instructions; None when absent."""
    home = home or Path.home()
    return {provider: sha256_bytes((home / rel).read_bytes())[:12] if (home / rel).is_file() else None
            for provider, rel in GLOBAL_INSTRUCTION_FILES.items()}


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
        try:
            result = respond(seat, phase, prompt)
        except Exception as exc:  # a crashing responder still leaves a logged, invalid call and a voided game
            result = failed_call(f"responder raised {type(exc).__name__}")
        writer.call({"game": gid, "seat": seat.index + 1, "role": seat.role, "provider": seat.provider,
                     "model": seat.model, "phase": phase, "round": round_no,
                     "cards_in_initial_context": received[seat.index],
                     "cards_in_transcript": cards_in(q, prompt[len(contexts[seat.index]):]),
                     "context_sha256": sha256_bytes(contexts[seat.index].encode()),
                     "prompt": prompt, **result, "pick": parse_pick(q, result["text"]) if phase != "discuss" else None})
        if result.get("valid") is not True:
            raise InvalidCall(f"seat {seat.index + 1} {phase} r{round_no}: "
                              + "; ".join(result.get("invalid_reasons") or ["validity not reported"]))
        return result

    def everyone(phase: str) -> List[dict]:
        with ThreadPoolExecutor(n) as pool:
            return list(pool.map(lambda s: call(s, phase), seats))

    start = game % n
    pre: List[dict] = []
    final: List[dict] = []
    invalid = None
    try:
        pre = everyone("pre")
        for round_no in range(1, rounds + 1):
            for offset in range(n):
                seat = seats[(start + offset) % n]
                result = call(seat, "discuss", round_no)
                transcript.append({"round": round_no, "seat": seat.index, "text": result["text"] or ""})
        final = everyone("final")
    except InvalidCall as exc:
        invalid = str(exc)

    def pick(results: List[dict], i: int) -> Optional[str]:
        return parse_pick(q, results[i]["text"]) if results else None

    record = {
        "game": gid, "question": q["id"], "condition": condition, "game_index": game, "rounds": rounds,
        "valid": invalid is None, "invalid_reason": invalid,
        "speaking_order": [(start + o) % n + 1 for o in range(n)],
        "seats": [{"seat": s.index + 1, "role": s.role, "provider": s.provider, "model": s.model,
                   "card": cards[s.index]["id"] if cards[s.index] else None,
                   "cards_in_initial_context": received[s.index],
                   "pre_pick": pick(pre, s.index), "final_pick": pick(final, s.index),
                   "rc": [r[s.index]["rc"] if r else None for r in (pre, final)]} for s in seats],
        "transcript": transcript,
    }
    games = writer.out / "games"
    games.mkdir(exist_ok=True)
    (games / f"{gid}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1))
    (games / f"{gid}.md").write_text(_markdown(q, record))
    return record


def _markdown(q: dict, record: dict) -> str:
    lines = [f"# {record['game']}", ""]
    if not record["valid"]:
        lines += [f"> **无效局，不计分**：{record['invalid_reason']}", ""]
    lines += ["| 席位 | 角色 | 模型 | 私卡 | 讨论前 | 讨论后 |", "|---|---|---|---|---|---|"]
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
    rows, invalid = [], []
    for path in sorted((Path(out) / "games").glob("*.json")):
        game = json.loads(path.read_text())
        if game.get("valid") is not True:
            invalid.append({"game": game["game"], "reason": game.get("invalid_reason") or "validity not recorded"})
            continue
        answer = key[game["question"]]
        picks = [s["final_pick"] for s in game["seats"]]
        majority, votes = Counter(picks).most_common(1)[0] if picks else (None, 0)
        rows.append({"game": game["game"], "correct": answer["correct"],
                     "pre_correct": sum(s["pre_pick"] == answer["correct"] for s in game["seats"]),
                     "final_correct": sum(p == answer["correct"] for p in picks), "seats": len(picks),
                     "majority": majority if votes * 2 > len(picks) else None,
                     "unanimous": len(set(picks)) == 1, "unparsed_final": picks.count(None)})
    result = {"answer_key_sha256": sha256_bytes(Path(answer_key_path).read_bytes()), "games": rows,
              "invalid_games": invalid}
    (Path(out) / "score.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    return result
