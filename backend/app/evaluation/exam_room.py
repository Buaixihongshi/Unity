"""Isolated exam room for the code-review exam (macOS ``sandbox-exec``).

A room is a throwaway directory holding a fresh single-commit snapshot of the
reviewed code plus the PR diff. Examinee processes run under a sandbox profile
that:

- denies everything by default and imports only Apple's ``system.sb`` baseline
  (system libraries, a short list of system Mach services); the pasteboard,
  keychain, LaunchServices, WindowServer and TCC services stay unreachable;
- reads only the room, a few toolchain paths (``RUNTIME_READ``) and explicitly
  listed runtime directories (CLI installs); metadata only on their ancestors;
- denies writes outside the room;
- denies all outbound network except one loopback port, served by an
  allowlist CONNECT proxy (so a CLI can reach its model API, but nothing
  else: not GitHub, not the local Unity hub). The proxy only connects to
  public addresses it resolved and checked itself (plus, when opted in, the
  198.18.0.0/15 answers of a local fake-IP DNS);
- hides other processes from ps and the sysctl process table, and limits
  process-info-pidinfo to the process itself, so argv and environment of other
  same-user processes (KERN_PROCARGS2 by PID) stay unreadable;
- starts from a scrubbed environment with HOME/TMPDIR inside the room.

The answer key never enters the room. Nothing here calls a model.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import ipaddress
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Sequence

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Toolchain the probe and git need on top of system.sb (/System, /usr/lib, /usr/share).
# /usr/local and /opt stay closed: they can hold databases and caches, not just binaries.
RUNTIME_READ = ("/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/libexec", "/Library/Developer/CommandLineTools",
                "/private/var/select", "/private/var/db/xcode_select_link")
NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
# Benchmark range that fake-IP DNS proxies (Clash, Surge, ...) answer with for every public name.
FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")
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
    # Path walking needs metadata on each ancestor; nothing else outside the allowed trees is stat-able.
    ancestors = sorted({str(parent) for path in [*allowed, *map(Path, RUNTIME_READ)] for parent in path.parents})
    lines = [
        "(version 1)",
        "(deny default)",
        '(import "system.sb")',
        "(allow process-exec* process-fork)",
        "(allow signal (target self))",
        # system.sb allows every sysctl read; the process table (kern.proc.*) lists other PIDs and commands.
        '(deny sysctl-read (sysctl-name-prefix "kern.proc"))',
        # KERN_PROCARGS2/KERN_PROCARGS (argv + environment by PID) are mediated as process-info-pidinfo,
        # not sysctl-read. Denying it outright kills python/git at startup; they only need their own PID.
        "(deny process-info-pidinfo)",
        "(allow process-info-pidinfo (target self))",
        '(allow ipc-posix-shm (ipc-posix-name "apple.shm.notification_center"))',
        "(allow file-read-metadata " + " ".join(_literal(Path(item)) for item in ancestors) + ")",
        "(allow file-read* " + " ".join(_subpath(path) for path in [*map(Path, RUNTIME_READ), *allowed]) + ")",
        '(allow file-write* ' + _subpath(room) + ' (literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/fd/"))',
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


def public_address(text: str) -> bool:
    """True only for globally routable unicast addresses, unwrapping IPv6 forms that embed IPv4."""
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif address.sixtofour:
            address = address.sixtofour
        elif address in NAT64_PREFIX:
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        elif address.teredo:
            return False
    return address.is_global and not address.is_multicast


def fake_ip_networks(ranges: Iterable[str]) -> tuple[ipaddress.IPv4Network, ...]:
    """Parse opt-in fake-IP ranges; anything outside 198.18.0.0/15 is refused, so loopback,
    private and link-local addresses can never be trusted this way."""
    networks = []
    for text in ranges:
        network = ipaddress.ip_network(text, strict=True)
        if not (isinstance(network, ipaddress.IPv4Network) and network.subnet_of(FAKE_IP_RANGE)):
            raise ValueError(f"fake-IP range must lie inside {FAKE_IP_RANGE}: {text}")
        networks.append(network)
    return tuple(networks)


def _in_networks(text: str, networks: Sequence[ipaddress.IPv4Network]) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return isinstance(address, ipaddress.IPv4Address) and any(address in network for network in networks)


async def _system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _open_upstream(address: str, port: int):
    return await asyncio.open_connection(address, port)


@dataclass
class AllowlistProxy:
    """Loopback HTTPS CONNECT proxy that only tunnels to allowlisted hosts on 443.

    The host is resolved once here; if any answer is not a public address the
    request is refused, otherwise the tunnel goes to the checked address itself
    (no second lookup, so DNS rebinding between check and connect does not apply).
    ``fake_ip`` (see ``fake_ip_networks``) additionally trusts answers from a local
    fake-IP DNS, whose TUN routes them to the requested name; off by default.
    """

    allowed_hosts: frozenset[str] = frozenset()
    attempts: list[dict] = field(default_factory=list)
    port: int = 0
    resolve: Callable[[str, int], Awaitable[list[str]]] = _system_resolve
    fake_ip: tuple[ipaddress.IPv4Network, ...] = ()

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
            # Cancel open tunnels and let them finish, so no task is destroyed while pending.
            server.close()
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(server.wait_closed())
            self._loop.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            method, target, *_ = request.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
            host, _, port_text = target.rpartition(":")
            port = int(port_text) if port_text.isdigit() else 0
            attempt = {"method": method, "target": target, "allowed": False}
            self.attempts.append(attempt)
            if method == "CONNECT" and self.permits(host, port):
                addresses = await asyncio.wait_for(self.resolve(host, port), 10)
                attempt["resolved"] = addresses
                attempt["allowed"] = bool(addresses) and all(
                    public_address(item) or _in_networks(item, self.fake_ip) for item in addresses)
            if not attempt["allowed"]:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            attempt["connected"] = addresses[0]
            upstream_reader, upstream_writer = await asyncio.wait_for(_open_upstream(addresses[0], port), 15)
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
                sandbox: bool = True, timeout: int = 600, stdin: str | None = None,
                fake_ip: Iterable[str] = ()) -> dict:
    """Run ``command`` inside the room. ``sandbox=False`` is only for probe controls."""
    if sandbox and not sandbox_available():
        raise RuntimeError("sandbox-exec is unavailable; refusing to run an unsandboxed exam")
    room = Path(os.path.realpath(room))
    with AllowlistProxy(frozenset(allowed_hosts), fake_ip=fake_ip_networks(fake_ip)) as proxy:
        env = room_env(room, proxy.port, extra_path, extra_env)
        argv = list(command)
        with tempfile.TemporaryDirectory(prefix="exam-profile-") as directory:
            if sandbox:
                profile = Path(directory) / "room.sb"
                profile.write_text(render_profile(room, proxy.port, readable))
                argv = [SANDBOX_EXEC, "-f", str(profile), *argv]
            # Own process group: on timeout the whole examinee tree is killed, not just its launcher.
            process = subprocess.Popen(argv, cwd=room, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                stdout, stderr = process.communicate(stdin, timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
                timed_out = True
        return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr, "timed_out": timed_out,
                "proxy_attempts": list(proxy.attempts), "sandboxed": sandbox}


@contextlib.contextmanager
def synthetic_host_process(marker: str):
    """A same-user process outside the room carrying ``marker`` in argv and env (probe runs only)."""
    arg, env_value = f"EXAM_SYNTHETIC_ARG_{marker}", f"fixture_{marker}"
    process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()", arg],
                               stdin=subprocess.PIPE, env={"EXAM_SYNTHETIC_ENV": env_value})
    try:
        yield {"pid": process.pid, "markers": [arg, env_value]}
    finally:
        process.kill()
        process.wait(5)


def copy_probe(room: Path) -> Path:
    """Place the fake examinee in the room (probe runs only, never real exams)."""
    target = Path(room) / ".probe" / "exam_probe.py"
    target.parent.mkdir(exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("exam_probe.py"), target)
    return target


def _quote(path: Path) -> str:
    return '"{}"'.format(str(path).replace("\\", "\\\\").replace('"', '\\"'))


def _subpath(path: Path) -> str:
    return f"(subpath {_quote(path)})"


def _literal(path: Path) -> str:
    return f"(literal {_quote(path)})"
