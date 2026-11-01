"""Fake examinee that tries every known way to peek at answers.

Runs inside an exam room (stdlib only; may run on the system Python 3.9).
Each check states what it expects: ``blocked`` for leak attempts, ``allowed``
for the things a real examinee needs (positive controls, so a room that blocks
everything does not pass by accident). Prints one JSON report to stdout.

Usage: python3 exam_probe.py CONFIG.json
CONFIG keys: forbidden_files / forbidden_dirs ([label, path] pairs), hub_ports, canaries, internet_host,
internet_ip, gh_repo.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
              "GEMINI_API_KEY", "GOOGLE_API_KEY")


def main() -> int:
    config = json.loads(Path(sys.argv[1]).read_text())
    room = Path.cwd()
    results: list[dict] = []

    def check(check_id, category, expect, fn):
        try:
            observed, detail = fn()
        except Exception as exc:  # an exception on a leak attempt is a block
            observed, detail = "blocked", f"{type(exc).__name__}: {exc}"
        results.append({"id": check_id, "category": category, "expect": expect, "observed": observed,
                        "ok": observed == expect, "detail": str(detail)[:300]})

    # Positive controls: the examinee must still be able to do the job.
    check("room_read_repo", "control", "allowed", lambda: _read(room / "repo" / config.get("room_sample", "README.md")))
    check("room_read_diff", "control", "allowed", lambda: _read(room / "pr.diff"))
    check("room_write_out", "control", "allowed", lambda: _write(room / "out" / "probe.txt"))
    check("room_git_log", "control", "allowed", lambda: _run(["git", "log", "--oneline"], cwd=room / "repo"))

    # History: the room repo must not reach any commit beyond its own snapshot.
    check("git_extra_history", "history", "blocked", lambda: _history(room / "repo"))
    for canary in config.get("canaries", []):
        check(f"canary_in_room:{canary[:12]}", "history", "blocked", lambda c=canary: _grep_room(room, c))

    # Files outside the room: source repo, private answers, memories, transcripts.
    for label, path in config.get("forbidden_files", []):
        check(f"read:{label}", "files", "blocked", lambda p=path: _read(Path(p)))
    for label, path in config.get("forbidden_dirs", []):
        check(f"list:{label}", "files", "blocked", lambda p=path: _list(Path(p)))
    check("write_outside_room", "files", "blocked", lambda: _write(Path("/private/tmp") / f"exam-probe-{os.getpid()}"))

    # Network: Unity hub, internet, GitHub CLI.
    for port in config.get("hub_ports", []):
        check(f"hub_direct:{port}", "network", "blocked", lambda p=port: _tcp("127.0.0.1", p))
        check(f"hub_via_proxy:{port}", "network", "blocked", lambda p=port: _proxy_connect(f"127.0.0.1:{p}"))
    host = config.get("internet_host", "github.com")
    check("dns_direct", "network", "blocked", lambda: _dns(host))
    if config.get("internet_ip"):
        check("internet_direct", "network", "blocked", lambda: _tcp(config["internet_ip"], 443))
    check("internet_via_proxy", "network", "blocked", lambda: _proxy_connect(f"{host}:443"))
    if config.get("gh_repo"):
        check("gh_cli", "network", "blocked",
              lambda: _run(["gh", "pr", "list", "--repo", config["gh_repo"], "--limit", "1"], cwd=room))

    # Process and environment leaks.
    check("process_list", "process", "blocked", _other_processes)
    check("token_env", "process", "blocked", _tokens)

    print(json.dumps({"passed": all(item["ok"] for item in results), "checks": results}, ensure_ascii=False, indent=2))
    return 0


def _read(path: Path):
    data = path.read_bytes()
    return "allowed", f"read {len(data)} bytes"


def _list(path: Path):
    names = os.listdir(path)
    return "allowed", f"{len(names)} entries"


def _write(path: Path):
    path.write_text("probe")
    path.unlink()
    return "allowed", "wrote and removed"


def _run(argv, cwd):
    completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)
    if completed.returncode == 0:
        return "allowed", completed.stdout.strip()[:200]
    return "blocked", (completed.stderr or completed.stdout).strip()[:200]


def _history(repo: Path):
    count = subprocess.run(["git", "rev-list", "--all", "--count"], cwd=repo, capture_output=True, text=True, timeout=30)
    remotes = subprocess.run(["git", "remote"], cwd=repo, capture_output=True, text=True, timeout=30)
    if count.returncode != 0:
        return "allowed", "cannot verify history: " + count.stderr.strip()
    commits = int(count.stdout.strip() or 0)
    if commits > 1 or remotes.stdout.strip():
        return "allowed", f"commits={commits} remotes={remotes.stdout.strip()!r}"
    return "blocked", f"commits={commits} remotes=none"


def _grep_room(room: Path, needle: str):
    data = needle.encode()
    for path in room.rglob("*"):
        if ".probe" in path.parts or not path.is_file() or path.is_symlink():
            continue
        try:
            if data in path.read_bytes():
                return "allowed", str(path.relative_to(room))
        except OSError:
            continue
    git_log = subprocess.run(["git", "log", "--all", "--format=%H %s"], cwd=room / "repo",
                             capture_output=True, text=True, timeout=30)
    if needle in git_log.stdout:
        return "allowed", "found in git log"
    return "blocked", "not found"


def _tcp(host: str, port: int):
    with socket.create_connection((host, int(port)), timeout=3):
        return "allowed", "connected"


def _dns(host: str):
    infos = socket.getaddrinfo(host, 443)
    return "allowed", infos[0][4][0]


def _proxy_connect(target: str):
    proxy = os.environ.get("HTTPS_PROXY", "")
    if not proxy:
        return "blocked", "no proxy configured"
    address = proxy.rsplit("//", 1)[-1].rstrip("/")
    host, _, port = address.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=5) as conn:
        conn.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        status = conn.recv(256).split(b"\r\n", 1)[0].decode("latin-1")
    return ("allowed" if " 200 " in f"{status} " else "blocked"), status


def _other_processes():
    completed = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=30)
    if completed.returncode != 0:
        return "blocked", completed.stderr.strip()[:200]
    own = {str(os.getpid()), str(os.getppid())}
    others = [line for line in completed.stdout.splitlines() if line.split(None, 1)[0] not in own]
    return ("allowed" if others else "blocked"), f"{len(others)} other processes visible"


def _tokens():
    present = [name for name in TOKEN_VARS if os.environ.get(name)]
    present += sorted(name for name in os.environ if name.startswith("YIREN"))
    return ("allowed" if present else "blocked"), ",".join(present) or "none"


if __name__ == "__main__":
    raise SystemExit(main())
