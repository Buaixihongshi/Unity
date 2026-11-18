"""Canonical check groups consumed by both local development and CI."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.runtime_identity import source_snapshot
from scripts.environment_common import EnvironmentError, python_for, write_json


def commands(py, output):
    return {
        "backend": [[py, "-m", "pip", "check"], [py, "-m", "pytest", "backend/tests", "-q"]],
        "frontend": [["npm", "--prefix", "frontend", "test"], ["npm", "--prefix", "frontend", "run", "build"]],
        "smoke": [[py, "scripts/" + name, *args] for name, args in (
            ("smoke_onboarding.py", ["--fresh", "--fake"]),
            ("smoke_conversation_flow.py", ["--fake"]),
            ("smoke_real_cli_mission.py", ["--fake"]),
            ("smoke_roundtable.py", ["--fake"]),
        )],
        "eval": [[py, "scripts/run_eval.py", "--mode", "fixture", "--quiet", "--output", str(output / "eval")]],
        "privacy": [[py, "scripts/privacy-check.py", "--git"]],
        "hygiene": [["git", "diff", "--check"], ["git", "diff", "--cached", "--check"]],
        "audit": [[py, "scripts/audit_frontend.py"]],
        "real-cli": [[py, "scripts/smoke_conversation_flow.py", "--real", "--timeout-seconds", "420"],
                     [py, "scripts/smoke_real_cli_mission.py", "--real", "--timeout-seconds", "300"]],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    groups = ("backend", "frontend", "smoke", "eval", "privacy", "hygiene", "audit")
    parser.add_argument("group", choices=(*groups, "all"), nargs="?", default="all")
    parser.add_argument("--real-cli", action="store_true")
    parser.add_argument("--output", type=Path, help="Private local report directory (never commit it)")
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix="agentclub-checks-"))
    py = str(python_for(ROOT, install=False)) if (ROOT / ".venv/bin/python").exists() else sys.executable
    before = source_snapshot()
    selected = list(groups) if args.group == "all" else [args.group]
    if args.real_cli: selected.append("real-cli")
    report = {"source": before, "groups": selected, "started_at": datetime.now(timezone.utc).isoformat(), "results": []}
    checks = commands(py, output)
    failed = False
    for group in selected:
        for index, command in enumerate(checks[group]):
            print(f"[{group}] {' '.join(command)}", flush=True)
            result = subprocess.run(command, cwd=ROOT)
            report["results"].append({"group": group, "check": index, "exit_code": result.returncode})
            if result.returncode:
                failed = True
                break
        if failed: break
    after = source_snapshot()
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["source_changed"] = (before["git_sha"], before["source_digest"]) != (after["git_sha"], after["source_digest"])
    report["passed"] = not failed and not report["source_changed"]
    write_json(output / "checks.json", report)
    print(f"{'PASS' if report['passed'] else 'FAIL'}: {output / 'checks.json'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    try: sys.exit(main())
    except (EnvironmentError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
