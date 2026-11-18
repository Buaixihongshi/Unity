#!/usr/bin/env python3
"""Code-review exam: room inspection, real CLI examinees and answer grading.

Only ``run`` calls a model; ``probe`` and ``grade`` never do.

Examples:
    # Inspect the room with a fake examinee (sandboxed run + unsandboxed control);
    # --role also opens what that role's real CLI needs, so the check covers it:
    python3 scripts/review_exam.py probe --base <pr-parent> --head <pr-merge> \\
        --canary <fix-sha> --canary "<fix commit title>" --answer-key path/to/key.json \\
        --role chulan --output output/eval/review-exam-probe

    # One real examinee, routed like the daily role (provider, model, arguments):
    python3 scripts/review_exam.py run --role chulan --base <pr-parent> --head <pr-merge> \\
        --canary <fix-sha> --auth-env CLAUDE_CODE_OAUTH_TOKEN --key key.json \\
        --output output/eval/review-exam-run

    # Grade one answer sheet against its private key:
    python3 scripts/review_exam.py grade --sheet answer.txt --key key.json
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.evaluation import exam_cli, exam_room  # noqa: E402
from backend.app.evaluation.review_exam import grade, load_key  # noqa: E402

PROBE_PYTHON = "/usr/bin/python3"
INSTRUCTIONS = (ROOT / "eval/pilots/review-exam-v1/TASK.md")
# Shared roots where an answer key or other state could sit outside the room; each gets a synthetic file.
SYNTHETIC_ROOTS = {"var_tmp": "/private/var/tmp", "tmp": "/private/tmp", "user_tmp": tempfile.gettempdir()}


def plant_synthetic(stack: contextlib.ExitStack) -> list[tuple[str, str]]:
    """Synthetic answer files the examinee must not read, removed when ``stack`` closes."""
    planted, seen = [], set()
    for label, root in SYNTHETIC_ROOTS.items():
        root = os.path.realpath(root)
        if root in seen or not os.path.isdir(root):
            continue
        seen.add(root)
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="review-exam-canary-", dir=root)))
        (directory / "synthetic-answer.json").write_text('{"fixture": "synthetic-answer"}\n')
        planted.append((f"synthetic_answer:{label}", str(directory / "synthetic-answer.json")))
    return planted


def default_forbidden(source_repo: Path, answer_key: Path | None, extra: list[str] = ()) -> tuple[list, list, list]:
    """Existing sensitive paths on this machine. Missing paths are reported as skipped,
    because a read that fails only because the file is absent proves nothing."""
    home = Path.home()
    common = Path(exam_room._git(["rev-parse", "--path-format=absolute", "--git-common-dir"], source_repo).strip())
    env_home = environment_home(source_repo)
    files = [
        ("source_repo_git_head", source_repo / ".git" if (source_repo / ".git").is_file() else source_repo / ".git/HEAD"),
        ("source_repo_readme", source_repo / "README.md"),
        ("source_git_packed_refs", common / "packed-refs"),
        ("source_git_head", common / "HEAD"),
        ("claude_global_config", home / ".claude.json"),
        ("codex_auth", home / ".codex/auth.json"),
        ("gh_hosts", home / ".config/gh/hosts.yml"),
        ("runtime_db", env_home / "runtime-state/data/yirenhub.sqlite3"),
    ]
    if answer_key:
        files.append(("answer_key", answer_key))
    for item in extra:
        label, _, path = item.partition("=")
        files.append((label, Path(path).expanduser()))
    dirs = [
        ("source_repo", source_repo),
        ("source_git_objects", common / "objects"),
        ("claude_projects_memory_and_transcripts", home / ".claude/projects"),
        ("codex_sessions", home / ".codex/sessions"),
        ("codex_memories", home / ".codex/memories"),
        ("runtime_artifacts", env_home / "runtime-state/artifacts"),
    ]
    if answer_key:
        dirs.append(("answer_key_dir", answer_key.parent))
    present_files = [(label, str(path)) for label, path in files if path.is_file()]
    present_dirs = [(label, str(path)) for label, path in dirs if path.is_dir()]
    skipped = [label for label, path in files if not path.is_file()] + [label for label, path in dirs if not path.is_dir()]
    return present_files, present_dirs, skipped


def environment_home(source_repo: Path) -> Path:
    common = Path(exam_room._git(["rev-parse", "--path-format=absolute", "--git-common-dir"], source_repo).strip())
    return Path(os.environ.get("AGENTCLUB_ENV_HOME", common.parent.parent / "agentclub-environments")).expanduser()


def examinee_for(role: str, source: Path) -> exam_cli.Examinee:
    from scripts.environment_cli_config import runtime_cli_snapshot
    return exam_cli.examinee_from_snapshot(runtime_cli_snapshot(source, real=True), role)


def probe(args) -> int:
    source = args.source_repo.resolve()
    access = {"readable": [], "extra_path": [], "allowed_hosts": ()}
    if args.role:
        examinee = examinee_for(args.role, source)
        install = exam_cli.resolve_install(examinee)
        access = {"readable": install["readable"], "extra_path": install["extra_path"],
                  "allowed_hosts": examinee.spec["hosts"]}
    answer_key = args.answer_key.resolve() if args.answer_key else None
    files, dirs, skipped = default_forbidden(source, answer_key, args.forbid)
    stack = contextlib.ExitStack()
    files += plant_synthetic(stack)
    fixture = stack.enter_context(exam_room.synthetic_host_process(secrets.token_hex(4)))
    config = {"forbidden_files": files, "forbidden_dirs": dirs, "hub_ports": args.hub_ports,
              "canaries": args.canary, "internet_host": "github.com", "internet_ip": args.internet_ip,
              "gh_repo": args.gh_repo, "room_sample": args.room_sample, "host_fixture": fixture}
    report = {"model_calls": 0, "base": args.base, "head": args.head, "skipped_absent": skipped, "runs": {},
              "role": args.role, "opened_for_cli": [str(path) for path in access["readable"]],
              "allowed_hosts": list(access["allowed_hosts"])}
    with stack, tempfile.TemporaryDirectory(prefix="review-exam-room-", dir="/private/tmp") as parent:
        room = exam_room.build_room(source, args.base, args.head, Path(parent) / "room",
                                    instructions=INSTRUCTIONS.read_text(encoding="utf-8"))
        report["preflight_canary_hits"] = exam_room.scan_canaries(room, args.canary)
        script = exam_room.copy_probe(room)
        (script.parent / "config.json").write_text(json.dumps(config))
        for label, sandbox in (("sandboxed", True), ("control_unsandboxed", False)):
            result = exam_room.run_in_room([PROBE_PYTHON, str(script), str(script.parent / "config.json")], room,
                                           sandbox=sandbox, timeout=300, **access)
            try:
                parsed = json.loads(result["stdout"])
            except json.JSONDecodeError:
                parsed = {"passed": False, "checks": [], "raw_stdout": result["stdout"][-2000:]}
            parsed.update(returncode=result["returncode"], stderr=result["stderr"][-2000:],
                          proxy_attempts=result["proxy_attempts"])
            report["runs"][label] = parsed
    sandboxed = report["runs"]["sandboxed"]
    report["room_ready"] = bool(sandboxed.get("passed")) and not report["preflight_canary_hits"]
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "probe-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for label, run in report["runs"].items():
        failed = [item["id"] for item in run.get("checks", []) if not item["ok"]]
        print(f"{label}: passed={run.get('passed')} checks={len(run.get('checks', []))} not_ok={failed}")
    print(f"preflight canary hits: {len(report['preflight_canary_hits'])}; skipped absent paths: {skipped}")
    print(f"room_ready={report['room_ready']}  report: {args.output / 'probe-report.json'}")
    return 0 if report["room_ready"] else 1


def run_cmd(args) -> int:
    source = args.source_repo.resolve()
    examinee = examinee_for(args.role, source)
    auth = exam_cli.auth_environment(examinee, args.auth_env, os.environ)
    key = load_key(json.loads(args.key.read_text(encoding="utf-8"))) if args.key else None
    task = INSTRUCTIONS.read_text(encoding="utf-8")
    report = {"model_calls": 0, "base": args.base, "head": args.head}
    with tempfile.TemporaryDirectory(prefix="review-exam-room-", dir="/private/tmp") as parent:
        room = exam_room.build_room(source, args.base, args.head, Path(parent) / "room", instructions=task)
        report["preflight_canary_hits"] = exam_room.scan_canaries(room, args.canary)
        if report["preflight_canary_hits"]:
            print(f"room leaks the answer, examinee not started: {report['preflight_canary_hits']}")
            return 1
        report["model_calls"] = 1
        report["examinee"] = exam_cli.sit_exam(examinee, room, task=task, auth=auth, fake_ip=args.fake_ip,
                                               timeout=args.timeout)
    answer = report["examinee"]["answer"]
    if key:
        report["grade"] = grade(answer, key, tolerance=args.tolerance).as_dict()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "answer.txt").write_text(answer, encoding="utf-8")
    (args.output / "run-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    run = report["examinee"]
    print(f"{run['role']} via {run['provider']}/{run['model'] or 'default'}: returncode={run['returncode']} "
          f"timed_out={run['timed_out']} answer_chars={len(answer)}")
    if key:
        result = report["grade"]
        print(f"valid={result['valid']} hits={len(result['hits'])} missed={len(result['missed'])} "
              f"false_positives={len(result['false_positives'])} {result['error']}")
    print(f"report: {args.output / 'run-report.json'}")
    return 0 if run["returncode"] == 0 and not run["timed_out"] else 1


def grade_cmd(args) -> int:
    key = load_key(json.loads(args.key.read_text(encoding="utf-8")))
    result = grade(args.sheet.read_text(encoding="utf-8"), key, tolerance=args.tolerance)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("probe", help="Run the fake examinee inside a room")
    p.add_argument("--source-repo", type=Path, default=ROOT)
    p.add_argument("--base", required=True, help="Commit before the reviewed PR")
    p.add_argument("--head", required=True, help="Commit after the reviewed PR (the code under review)")
    p.add_argument("--canary", action="append", default=[], help="Answer text that must not be visible (fix SHA, title)")
    p.add_argument("--answer-key", type=Path)
    p.add_argument("--forbid", action="append", default=[], metavar="LABEL=PATH",
                   help="Extra file the examinee must not read (e.g. the daily database)")
    p.add_argument("--hub-ports", type=int, nargs="*", default=[8000, 8100, 8200, 8300])
    p.add_argument("--internet-ip", default="140.82.112.3", help="A GitHub IP for the direct-connect check")
    p.add_argument("--gh-repo", default="Buaixihongshi/AgentClub")
    p.add_argument("--room-sample", default="README.md", help="Repo file the examinee must be able to read")
    p.add_argument("--role", help="Also open what this daily role's real CLI needs (install, API hosts)")
    p.add_argument("--output", type=Path, required=True)
    p.set_defaults(func=probe)
    r = sub.add_parser("run", help="One real CLI examinee, routed like the daily role (calls a model)")
    r.add_argument("--source-repo", type=Path, default=ROOT)
    r.add_argument("--role", required=True, help="Daily role id, e.g. chulan or baobao")
    r.add_argument("--base", required=True)
    r.add_argument("--head", required=True)
    r.add_argument("--canary", action="append", default=[])
    r.add_argument("--auth-env", action="append", default=[], metavar="NAME",
                   help="Host environment variable passed into the room for CLI auth; its value is never recorded")
    r.add_argument("--fake-ip", action="append", default=[], metavar="CIDR",
                   help="Trust this fake-IP DNS range (inside 198.18.0.0/15) for allowlisted hosts")
    r.add_argument("--key", type=Path, help="Grade the answer against this private key (stays outside the room)")
    r.add_argument("--tolerance", type=int, default=5)
    r.add_argument("--timeout", type=int, default=1800)
    r.add_argument("--output", type=Path, required=True)
    r.set_defaults(func=run_cmd)
    g = sub.add_parser("grade", help="Grade one answer sheet")
    g.add_argument("--sheet", type=Path, required=True)
    g.add_argument("--key", type=Path, required=True)
    g.add_argument("--tolerance", type=int, default=5)
    g.set_defaults(func=grade_cmd)
    args = parser.parse_args()
    if args.command in {"probe", "run"} and not exam_room.sandbox_available():
        parser.error(f"{args.command} needs macOS sandbox-exec")
    try:
        return args.func(args)
    except exam_cli.ExamineeError as exc:
        parser.exit(2, f"review_exam.py {args.command}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
