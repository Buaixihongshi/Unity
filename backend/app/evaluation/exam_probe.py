"""Fake examinee that tries every known way to peek at answers.

Runs inside an exam room (stdlib only; may run on the system Python 3.9).
Each check states what it expects: ``blocked`` for leak attempts, ``allowed``
for the things a real examinee needs (positive controls, so a room that blocks
everything does not pass by accident). Prints one JSON report to stdout.

Usage: python3 exam_probe.py CONFIG.json
CONFIG keys: forbidden_files / forbidden_dirs ([label, path] pairs), hub_ports, canaries, internet_host,
internet_ip, gh_repo, mach_services (defaults to HOST_SERVICES), host_fixture ({pid, markers}: a synthetic
same-user process started outside the room whose argv/env carry the markers).
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
              "GEMINI_API_KEY", "GOOGLE_API_KEY")
# Host Mach services that expose state outside the room: clipboard, keychain, opening URLs/apps, screen, privacy DB.
# A lookup is checked directly, so an empty clipboard or keychain cannot make the check pass by accident.
HOST_SERVICES = ("com.apple.pasteboard.1", "com.apple.SecurityServer", "com.apple.securityd.xpc",
                 "com.apple.coreservices.launchservicesd", "com.apple.lsd.mapdb", "com.apple.windowserver.active",
                 "com.apple.tccd")


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
    check("proxy_reachable", "control", "allowed", _proxy_reachable)

    # History: the room repo must not reach any commit beyond its own snapshot.
    check("git_extra_history", "history", "blocked", lambda: _history(room / "repo"))
    for canary in config.get("canaries", []):
        check(f"canary_in_room:{canary[:12]}", "history", "blocked", lambda c=canary: _grep_room(room, c))

    # Files outside the room: source repo, private answers, memories, transcripts.
    for label, path in config.get("forbidden_files", []):
        check(f"read:{label}", "files", "blocked", lambda p=path: _read(Path(p)))
        check(f"stat:{label}", "files", "blocked", lambda p=path: _stat(Path(p)))
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

    # Host state reachable over Mach IPC rather than the file system.
    for name in config.get("mach_services", HOST_SERVICES):
        check(f"mach:{name}", "ipc", "blocked", lambda n=name: _mach_lookup(n))
    check("clipboard_pbpaste", "ipc", "blocked", lambda: _run(["/usr/bin/pbpaste"], cwd=room, show=False))
    check("keychain_list", "ipc", "blocked", lambda: _run(["/usr/bin/security", "list-keychains"], cwd=room, show=False))
    check("host_preferences", "ipc", "blocked", lambda: _run(["/usr/bin/defaults", "read", "-g"], cwd=room, show=False))

    # Process and environment leaks.
    check("process_list", "process", "blocked", _other_processes)
    check("process_list_sysctl", "process", "blocked", _sysctl_process_table)
    fixture = config.get("host_fixture")
    if fixture:
        check("host_procargs", "process", "blocked", lambda: _procargs(int(fixture["pid"]), fixture["markers"]))
    check("token_env", "process", "blocked", _tokens)

    print(json.dumps({"passed": all(item["ok"] for item in results), "checks": results}, ensure_ascii=False, indent=2))
    return 0


def _read(path: Path):
    data = path.read_bytes()
    return "allowed", f"read {len(data)} bytes"


def _stat(path: Path):
    info = path.stat()
    return "allowed", f"size={info.st_size}"


def _list(path: Path):
    names = os.listdir(path)
    return "allowed", f"{len(names)} entries"


def _write(path: Path):
    path.write_text("probe")
    path.unlink()
    return "allowed", "wrote and removed"


def _run(argv, cwd, show=True):
    completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)
    if completed.returncode == 0:
        # Host state (clipboard, preferences) is counted, never echoed into the report.
        return "allowed", completed.stdout.strip()[:200] if show else f"exit 0, {len(completed.stdout)} chars"
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


def _mach_lookup(name: str):
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    port = ctypes.c_uint(0)
    result = libc.bootstrap_look_up(ctypes.c_uint.in_dll(libc, "bootstrap_port"), name.encode(), ctypes.byref(port))
    return ("allowed" if result == 0 else "blocked"), f"bootstrap_look_up={result}"


def _proxy_reachable():
    status = _proxy_connect("exam-control.invalid:443")[1]
    return ("allowed" if status.startswith("HTTP/") else "blocked"), status


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


def _sysctl(mib):
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    name = (ctypes.c_int * len(mib))(*mib)
    size = ctypes.c_size_t(0)
    if libc.sysctl(name, len(mib), None, ctypes.byref(size), None, 0) != 0:
        return ctypes.get_errno(), b""
    buffer = ctypes.create_string_buffer(size.value)
    if libc.sysctl(name, len(mib), buffer, ctypes.byref(size), None, 0) != 0:
        return ctypes.get_errno(), b""
    return 0, buffer.raw[:size.value]


def _sysctl_process_table():
    errno, data = _sysctl([1, 14, 0])  # KERN_PROC_ALL: the PID table ps would print
    return ("allowed" if not errno else "blocked"), f"errno={errno} size={len(data)}"


def _procargs(pid: int, markers):
    # KERN_PROCARGS2 returns another process's argv and environment; only hits are reported, never contents.
    errno, data = _sysctl([1, 49, pid])
    seen = [bool(marker) and marker.encode() in data for marker in markers]
    return ("allowed" if any(seen) else "blocked"), f"errno={errno} size={len(data)} markers_seen={seen}"


def _tokens():
    present = [name for name in TOKEN_VARS if os.environ.get(name)]
    present += sorted(name for name in os.environ if name.startswith("YIREN"))
    return ("allowed" if present else "blocked"), ",".join(present) or "none"


if __name__ == "__main__":
    raise SystemExit(main())
