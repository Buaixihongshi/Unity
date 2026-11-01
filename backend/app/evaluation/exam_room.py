"""Isolated exam room for the code-review exam (macOS ``sandbox-exec``).

A room is a throwaway directory holding a fresh single-commit snapshot of the
reviewed code plus the PR diff. Examinee processes run under a sandbox profile
that:

- denies reading anything under /Users, /Volumes and the shared temp roots
  except the room and explicitly listed runtime directories (CLI installs);
- denies writes outside the room;
- denies all outbound network except one loopback port, served by an
  allowlist CONNECT proxy (so a CLI can reach its model API, but nothing
  else: not GitHub, not the local Unity hub);
- hides other processes;
- starts from a scrubbed environment with HOME/TMPDIR inside the room.

The answer key never enters the room. Nothing here calls a model.
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
DENIED_READ_ROOTS = ("/Users", "/Volumes", "/private/tmp", "/private/var/folders")
BASE_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/opt/homebrew/bin", "/usr/local/bin")


def sandbox_available() -> bool:
    return sys.platform == "darwin" and Path(SANDBOX_EXEC).exists()


def _git(args: Sequence[str], cwd: Path, *, binary: bool = False):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, *args], cwd=cwd, env=env,
                            capture_output=True, text=not binary, check=True, timeout=120)
    return result.stdout


def build_room(source_repo: Path, base: str, head: str, room: Path, *, instructions: str) -> Path:
    """Create ``room`` with ``repo/`` at ``head`` and ``pr.diff`` = base..head.

    ``repo/`` gets fresh history (one commit, no remotes), so later fixes in the
    source repository are not reachable from inside the room.
    """
    source_repo, room = Path(source_repo), Path(room)
    room.mkdir(parents=True, exist_ok=False)
    repo = room / "repo"
    repo.mkdir()
    archive = _git(["archive", "--format=tar", head], source_repo, binary=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
        handle.extractall(repo, filter="data")
    _git(["init", "-q", "--initial-branch=exam"], repo)
    _git(["add", "--all"], repo)
    _git(["-c", "user.name=Exam", "-c", "user.email=exam@localhost", "commit", "-q", "-m", "Exam snapshot"], repo)
    (room / "pr.diff").write_bytes(_git(["diff", "--binary", base, head], source_repo, binary=True))
    (room / "TASK.md").write_text(instructions, encoding="utf-8")
    (room / "out").mkdir()
    return room


def scan_canaries(room: Path, canaries: Iterable[str]) -> list[dict]:
    """Return every room file containing a canary (fix SHA, fix title, ...).

    Run before an examinee enters. Any hit means the room leaks the answer.
    """
    needles = [item.encode() for item in canaries if item]
    hits = []
    for path in Path(room).rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        for needle in needles:
            if needle in data:
                hits.append({"file": str(path.relative_to(room)), "canary": needle.decode()})
    return hits


def render_profile(room: Path, proxy_port: int, readable: Iterable[Path] = ()) -> str:
    room = Path(os.path.realpath(room))
    allowed = [room, *(Path(os.path.realpath(path)) for path in readable)]
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny process-info* (target others))",
        "(deny file-read* " + " ".join(_subpath(Path(item)) for item in DENIED_READ_ROOTS) + ")",
        # Stat on known paths (e.g. path walking) is fine; listing and reading are not.
        "(allow file-read-metadata)",
        "(allow file-read* " + " ".join(_subpath(path) for path in allowed) + ")",
        '(deny file-write* (subpath "/"))',
        '(allow file-write* ' + _subpath(room) + ' (literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/fd/"))',
        "(deny network-outbound)",
        f'(allow network-outbound (remote ip "localhost:{int(proxy_port)}"))',
    ]
    return "\n".join(lines) + "\n"


def room_env(room: Path, proxy_port: int, extra_path: Iterable[str] = (), extra_env: dict | None = None) -> dict:
    """A from-scratch environment: no tokens, no YIREN_* paths, no user HOME."""
    room = Path(os.path.realpath(room))
    home, tmp = room / ".home", room / ".tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    proxy = f"http://127.0.0.1:{int(proxy_port)}"
    env = {
        "PATH": os.pathsep.join([*extra_path, *BASE_PATH]),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "en_US.UTF-8",
        "TERM": "dumb",
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "ALL_PROXY": proxy,
        "https_proxy": proxy,
        "http_proxy": proxy,
        "NO_PROXY": "",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    env.update(extra_env or {})
    return env


@dataclass
class AllowlistProxy:
    """Loopback HTTPS CONNECT proxy that only tunnels to allowlisted hosts on 443."""

    allowed_hosts: frozenset[str] = frozenset()
    attempts: list[dict] = field(default_factory=list)
    port: int = 0

    def __post_init__(self):
        self.allowed_hosts = frozenset(host.lower() for host in self.allowed_hosts)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def permits(self, host: str, port: int) -> bool:
        return host.lower() in self.allowed_hosts and port == 443

    def __enter__(self) -> "AllowlistProxy":
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("proxy did not start")
        return self

    def __exit__(self, *exc) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(5)

    def _serve(self) -> None:
        self._loop = asyncio.new_event_loop()
        server = self._loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
        self.port = server.sockets[0].getsockname()[1]
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            server.close()
            self._loop.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            method, target, *_ = request.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
            host, _, port_text = target.rpartition(":")
            port = int(port_text) if port_text.isdigit() else 0
            allowed = method == "CONNECT" and self.permits(host, port)
            self.attempts.append({"method": method, "target": target, "allowed": allowed})
            if not allowed:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


def run_in_room(command: Sequence[str], room: Path, *, allowed_hosts: Iterable[str] = (),
                readable: Iterable[Path] = (), extra_path: Iterable[str] = (), extra_env: dict | None = None,
                sandbox: bool = True, timeout: int = 600, stdin: str | None = None) -> dict:
    """Run ``command`` inside the room. ``sandbox=False`` is only for probe controls."""
    if sandbox and not sandbox_available():
        raise RuntimeError("sandbox-exec is unavailable; refusing to run an unsandboxed exam")
    room = Path(os.path.realpath(room))
    with AllowlistProxy(frozenset(allowed_hosts)) as proxy:
        env = room_env(room, proxy.port, extra_path, extra_env)
        argv = list(command)
        with tempfile.TemporaryDirectory(prefix="exam-profile-") as directory:
            if sandbox:
                profile = Path(directory) / "room.sb"
                profile.write_text(render_profile(room, proxy.port, readable))
                argv = [SANDBOX_EXEC, "-f", str(profile), *argv]
            completed = subprocess.run(argv, cwd=room, env=env, input=stdin, capture_output=True,
                                       text=True, timeout=timeout)
        return {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr,
                "proxy_attempts": list(proxy.attempts), "sandboxed": sandbox}


def copy_probe(room: Path) -> Path:
    """Place the fake examinee in the room (probe runs only, never real exams)."""
    target = Path(room) / ".probe" / "exam_probe.py"
    target.parent.mkdir(exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("exam_probe.py"), target)
    return target


def _subpath(path: Path) -> str:
    return '(subpath "{}")'.format(str(path).replace("\\", "\\\\").replace('"', '\\"'))
