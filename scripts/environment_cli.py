"""One lifecycle entrypoint. Existing launch scripts are compatibility aliases."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.environment_common import EnvironmentError, checkout_listening, git, home, lock, profiles, state_path
from scripts.environment_supervisor import alive, read_record, running_instances, start, stop, supervise


def managed_checkout(path):
    if path.is_symlink(): raise EnvironmentError("Managed checkout may not be a symlink")
    if path.exists():
        if not (path / ".git").is_file(): raise EnvironmentError(f"Refusing to adopt unrelated existing directory: {path}")
        if Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve() != Path(git(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve():
            raise EnvironmentError("Checkout belongs to another repository")


def sync_alpha():
    path = home() / "alpha"
    with lock(home() / ".alpha-sync.lock"):
        managed_checkout(path)
        if path.exists():
            record = read_record(path, "alpha")
            if alive(record) or checkout_listening(path): raise EnvironmentError("Alpha is running; stop its managed instance before syncing")
            if git(path, "status", "--porcelain", "--untracked-files=all"):
                raise EnvironmentError("Alpha has local modifications; preserve them before syncing")
        git(ROOT, "fetch", "origin", "main")
        sha = git(ROOT, "rev-parse", "origin/main")
        if not path.exists():
            git(ROOT, "worktree", "add", "--detach", str(path), sha)
        else:
            git(path, "checkout", "--detach", sha)
        print(f"Alpha synced to origin/main: {sha}")
    return path


def feature(name, ref=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,59}", name):
        raise EnvironmentError("Task name must be 1..60 lowercase letters/digits/_/-")
    path = home() / "features" / name
    with lock(home() / ".feature-worktrees.lock"):
        managed_checkout(path)
        branch = "codex/task-" + name
        if path.exists():
            if git(path, "branch", "--show-current") != branch:
                raise EnvironmentError("Task directory has an unexpected branch")
        else:
            git(ROOT, "fetch", "origin", "main")
            git(ROOT, "worktree", "add", "-b", branch, str(path), ref or "origin/main")
    return path


def options(parser, checkout=True):
    if checkout: parser.add_argument("--checkout", type=lambda v: Path(v).expanduser().resolve(), default=ROOT)
    parser.add_argument("--backend-port", type=int)
    parser.add_argument("--frontend-port", type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--real-cli", action="store_true", help="Use separate real-CLI state; with --providers, test specific providers")
    mode.add_argument("--demo", action="store_true", help="Explicit offline demo (development/test only)")
    parser.add_argument("--inherit-runtime-cli", action="store_true", help="Use daily CLI routes/models with isolated task state; development/alpha do this by default")
    parser.add_argument("--providers", help="Specific real providers instead of daily routes (requires --real-cli outside development)")
    parser.add_argument("--no-install", action="store_true")
    parser.add_argument("--no-open", action="store_true", help="Compatibility: browser is opened only with --open")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--detach", action="store_true")
    parser.add_argument("--idle-stop", type=int, metavar="MINUTES",
                        help="Stop after this many minutes without requests and with no unfinished work; 0 disables (development default: config)")
    parser.add_argument("--backend-only", action="store_true")
    parser.add_argument("--reload", action="store_true", default=None)
    parser.add_argument("--reset-onboarding", action="store_true")


def cli_mode(args, profile):
    demo = getattr(args, "demo", False)
    inherit = getattr(args, "inherit_runtime_cli", False)
    providers = getattr(args, "providers", None)
    if demo and (profile not in {"development", "test"} or inherit or providers):
        raise EnvironmentError("--demo is for development/test and cannot inherit or select real CLI providers")
    real = args.real_cli or (profile == "development" and not demo)
    # Explicit --real-cli retains the standalone provider-probe path. Normal
    # development uses the team's effective daily routes rather than Codex-only.
    inherit = profile == "alpha" or inherit or (profile == "development" and not demo and not args.real_cli and providers is None)
    return real, inherit


def guard_running(profile):
    """Each environment is a full backend (+ Vite); unbounded parallel launches exhausted host memory."""
    limit = int(os.environ.get("AGENTCLUB_MAX_RUNNING") or profiles()[profile].get("max_running") or 0)
    live = running_instances(profile)
    if not limit or len(live) < limit: return
    lines = [f"{len(live)} managed {profile} environments are already running (limit {limit}); "
             "stop one that is no longer needed before starting another:"]
    for record in live:
        demo = "" if record.get("real_cli") else " --demo"
        lines.append(f"  ./scripts/env.sh stop {profile} --checkout {record['checkout']}{demo}"
                     f"  # since {record.get('supervisor_started_at')}")
    raise EnvironmentError("\n".join(lines))


def launch(args, root, profile, allocate=False):
    if profile == "runtime": raise EnvironmentError("Runtime activation is reserved for the owner release command")
    if getattr(args, "inherit_runtime_cli", False) and profile not in {"development", "test", "alpha"}:
        raise EnvironmentError("Daily CLI inheritance is only available to isolated development/test/alpha")
    real, inherit = cli_mode(args, profile)
    protected = home() / "releases"
    if root.resolve().is_relative_to(protected.resolve()) or (profile != "alpha" and root.resolve() == (home() / "alpha").resolve()):
        raise EnvironmentError("Release/alpha source is protected; create a feature worktree instead")
    if profile == "alpha" and args.reload: raise EnvironmentError("Alpha must not reload mutable source")
    if profile == "alpha":
        if root.resolve() != (home() / "alpha").resolve():
            raise EnvironmentError("Alpha may only run from its dedicated main mirror; use env.sh alpha start")
        if git(root, "branch", "--show-current") or git(root, "status", "--porcelain", "--untracked-files=all"):
            raise EnvironmentError("Alpha must be a clean detached main checkout")
        if git(root, "rev-parse", "HEAD") != git(ROOT, "rev-parse", "origin/main"):
            raise EnvironmentError("Alpha is not at refreshed origin/main; sync it before starting")
    if checkout_listening(root) and not alive(read_record(root, profile, real)):
        raise EnvironmentError("Checkout is serving another instance; do not develop in a running service checkout")
    if profile == "development" and not alive(read_record(root, profile, real)):
        guard_running(profile)
    if not (root / "backend/app/runtime_control.py").exists():
        raise EnvironmentError("Checkout does not include the managed lifecycle yet; merge PR 2 before using alpha/main features")
    record = start(root, profile, detach=args.detach,
        real=real, providers=args.providers or "codex", backend=args.backend_port, frontend=args.frontend_port,
        inherit_cli=inherit,
        allocate=allocate, install=not args.no_install, backend_only=args.backend_only, reload=args.reload,
        open_browser=args.open and not args.no_open, reset_onboarding=args.reset_onboarding,
        idle_stop=args.idle_stop)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"status", "identity"}:
        from scripts.environment_status import main as status
        status()
        return
    if argv and argv[0] == "_supervise":
        spec = json.loads(Path(argv[1]).read_text())
        spec["root"] = Path(spec["root"])
        if spec.get("state"): spec["state"] = Path(spec["state"])
        supervise(**spec)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status", help="Read-only service/worktree inventory; supports --json/--checkout/--ports")
    commands.add_parser("identity", help="Public source metadata used by the frontend")
    start_parser = commands.add_parser("start")
    start_parser.add_argument("profile", choices=profiles())
    options(start_parser)
    dev = commands.add_parser("dev")
    dev.add_argument("task")
    dev.add_argument("--ref", help="Candidate ref for pre-merge validation; defaults to refreshed origin/main")
    options(dev, checkout=False)
    end = commands.add_parser("stop")
    end.add_argument("profile", choices=profiles())
    end.add_argument("--checkout", type=lambda v: Path(v).expanduser().resolve(), default=ROOT)
    mode = end.add_mutually_exclusive_group()
    mode.add_argument("--real-cli", action="store_true")
    mode.add_argument("--demo", action="store_true", help="Stop the explicitly started demo state")
    alpha = commands.add_parser("alpha")
    alpha.add_argument("operation", choices=("sync", "start", "stop", "status"))
    options(alpha, checkout=False)
    args = parser.parse_args(argv)
    if args.action == "dev": launch(args, feature(args.task, args.ref), "development", allocate=True)
    elif args.action == "start": launch(args, args.checkout, args.profile)
    elif args.action == "stop": stop(args.checkout, args.profile, cli_mode(args, args.profile)[0])
    elif args.operation == "sync": sync_alpha()
    elif args.operation == "stop": stop(home() / "alpha", "alpha")
    elif args.operation == "status":
        print(json.dumps(read_record(home() / "alpha", "alpha"), ensure_ascii=False, indent=2))
    else:
        path = home() / "alpha"
        if path.exists() and alive(read_record(path, "alpha")):
            print("Alpha is already running; use status, or stop + sync to update explicitly")
            return
        launch(args, sync_alpha(), "alpha")


if __name__ == "__main__":
    try: main()
    except (EnvironmentError, subprocess.CalledProcessError, urllib.error.URLError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
