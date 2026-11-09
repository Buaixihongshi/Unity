#!/usr/bin/env python3
"""Run hidden-profile discussions where each seat gets the shared materials plus its own private card.

Examples:
    # Offline isolation check, no model calls:
    python3 scripts/run_clueeval.py run --mode fixture --output output/clueeval/fixture

    # Real CLIs on the routes from a routing file (hub_settings.json of the task environment or daily app):
    python3 scripts/run_clueeval.py run --mode real --routing <hub_settings.json> --rounds 2 \
        --output output/clueeval/smoke

    # Single-agent baseline: every routed role alone with all materials, 3 repeats each:
    python3 scripts/run_clueeval.py run --mode real --routing <hub_settings.json> --condition solo_full --games 3 \
        --output output/clueeval/solo

    # Score afterwards; this is the only step that opens the answer key:
    python3 scripts/run_clueeval.py score --output output/clueeval/smoke
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.evaluation import clueeval as C  # noqa: E402

DATA = ROOT / "eval/clueeval"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def _cli_version(command: str) -> str:
    try:
        return subprocess.run([command, "--version"], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL).stdout.strip()[:80]
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def run(args) -> int:
    questions = C.load_questions(args.questions)
    selected = args.question or list(questions)
    out = args.output
    if (out / "calls.jsonl").exists():
        print(f"{out} already has calls.jsonl; pick a new --output", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    routing = json.loads(args.routing.read_text()) if args.routing else None
    if args.mode == "real" and routing is None:
        print("--mode real needs --routing", file=sys.stderr)
        return 2
    manifest = {
        "started_at": datetime.now(timezone.utc).isoformat(), "mode": args.mode, "rounds": args.rounds,
        "games": args.game_index or args.games, "conditions": args.condition, "questions": selected,
        "git_head": _git("rev-parse", "HEAD"), "git_dirty": bool(_git("status", "--porcelain")),
        "questions_sha256": C.sha256_bytes(args.questions.read_bytes()),
        "harness_sha256": C.sha256_bytes(Path(C.__file__).read_bytes()),
        "routing_sha256": C.sha256_bytes(args.routing.read_bytes()) if args.routing else None,
        # What this script did, not an audit of the CLI subprocesses; those are checked per call (valid/invalid_reasons).
        "answer_key_opened_by_harness": False,
        # Which layer the numbers speak for: these CLIs on their daily routes in a fixed round-robin discussion.
        # Unity's own orchestration (personas, publish_message, invoke_agent, memory) is not on this path.
        "harness_layer": "direct-cli-round-robin",
        "seats": {},
    }
    if args.mode == "real":
        manifest["cli_versions"] = {c: _cli_version(c) for c in ("claude", "codex", "agy")}
        # These files reach the model besides our prompt; recorded so runs stay comparable.
        manifest["cli_global_instructions_sha256"] = C.global_instructions()
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    writer = C.GameWriter(out)
    invalid = 0
    # A rerun of a voided game keeps its game number, so its card rotation and speaking order stay the same.
    game_indices = args.game_index or list(range(args.games))
    for qid in selected:
        q = questions[qid]
        respond = C.cli_responder(args.cli_timeout) if args.mode == "real" else C.fixture_responder(q)
        for condition in args.condition:
            # The table seats one role per card; the solo baseline seats every routed role, one at a time.
            n = len(C.DEFAULT_ROSTER) if condition == "solo_full" else len(q["cards"])
            if routing is not None:
                seats = C.resolve_roster(routing, n)
            else:
                seats = [C.Seat(i, f"fixture-{i + 1}", "fixture", "fixture") for i in range(n)]
            manifest["seats"][f"{qid}/{condition}"] = [
                {"role": s.role, "provider": s.provider, "model": s.model, "effort": s.effort} for s in seats]
            (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
            tables = [[C.Seat(0, s.role, s.provider, s.model, s.effort)] for s in seats] \
                if condition == "solo_full" else [seats]
            for game, table in ((g, t) for g in game_indices for t in tables):
                record = C.run_game(q, game, condition, args.rounds, table, respond, writer)
                print(record["game"], [(s["card"], s["pre_pick"], s["final_pick"]) for s in record["seats"]],
                      "" if record["valid"] else f"INVALID: {record['invalid_reason']}", flush=True)
                invalid += not record["valid"]
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    manifest["invalid_games"] = invalid
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    if invalid:
        print(f"{invalid} invalid game(s); score leaves them out", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=["fixture", "real"], default="fixture")
    r.add_argument("--questions", type=Path, default=DATA / "questions.json")
    r.add_argument("--question", action="append", help="Question id; repeatable; default all")
    r.add_argument("--condition", action="append", choices=C.CONDITIONS, help="Repeatable; default private")
    r.add_argument("--games", type=int, default=1)
    r.add_argument("--game-index", type=int, action="append",
                   help="Run only these game numbers (repeatable), e.g. to rerun a voided game; overrides --games")
    r.add_argument("--rounds", type=int, default=3)
    r.add_argument("--routing", type=Path, help="hub_settings.json whose role routes and models seat the table")
    r.add_argument("--cli-timeout", type=int, default=600)
    r.add_argument("--output", type=Path, required=True)
    s = sub.add_parser("score")
    s.add_argument("--output", type=Path, required=True)
    s.add_argument("--answer-key", type=Path, default=DATA / "answer_key.json")
    args = parser.parse_args()
    if args.command == "score":
        print(json.dumps(C.score(args.output, args.answer_key), ensure_ascii=False, indent=1))
        return 0
    args.condition = args.condition or ["private"]
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
