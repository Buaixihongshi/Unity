"""Shared environment policy and filesystem operations (stdlib only)."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


class EnvironmentError(RuntimeError):
    pass


def run(args, *, cwd=ROOT, **kwargs):
    return subprocess.run([str(a) for a in args], cwd=cwd, check=True, **kwargs)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args],
                                   env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, text=True).strip()


def home(root=ROOT):
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute(): common = root / common
    return Path(os.environ.get("AGENTCLUB_ENV_HOME", str(common.resolve().parent.parent / "agentclub-environments"))).expanduser().resolve()


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink(): raise EnvironmentError(f"Refusing symlink state directory: {path}")
    path.chmod(0o700)
    return path


def write_json(path, data):
    private_dir(path.parent)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp, path)


@contextmanager
def lock(path, wait=False):
    private_dir(path.parent)
    with path.open("a+") as f:
        try: fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc: raise EnvironmentError(f"Environment operation already running: {path}") from exc
        try: yield
        finally: fcntl.flock(f, fcntl.LOCK_UN)


def checkout_listening(root):
    from scripts.environment_status import listeners, process_info
    return any(process_info(proc)["checkout"] in {str(root), str(root / "frontend")}
               for processes in listeners().values() for proc in processes)


def profiles():
    return json.loads((ROOT / "config/environments.json").read_text())


def state_path(checkout, profile, real=False):
    return checkout / ".yiren" / (profile + ("-real" if real and profile != "alpha" else ""))


def environment(checkout, profile, *, real=False, providers="codex", backend=0, frontend=0, state=None,
                inherit_cli=False):
    if profile not in profiles(): raise EnvironmentError("Unknown environment")
    state = state or state_path(checkout, profile, real)
    private_dir(state)
    config = private_dir(state / "config")
    existing_runtime_config = profile == "runtime" and (config / "cli-providers.json").exists()
    for name in ("agents.json", "cli-providers.json"):
        if not (config / name).exists(): shutil.copyfile(checkout / "config" / name, config / name)
    requested = {x.strip() for x in providers.split(",") if x.strip()}
    pp = config / "cli-providers.json"
    data = json.loads(pp.read_text())
    known = {p["id"] for p in data}
    if real and not inherit_cli and (not requested or requested - (known - {"fake"})):
        raise EnvironmentError("--providers must name known real CLI providers")
    # Explicit mode lives in a separate state namespace; no committed config or
    # existing hub settings are rewritten to turn real providers on/off.
    for p in data:
        p["enabled"] = p["id"] == "fake" or (real and p["id"] in requested)
        if real and p["id"] in requested: p["mcp_enabled"] = True
    if not existing_runtime_config and not inherit_cli: write_json(pp, data)
    inherited = None
    if inherit_cli:
        from scripts.environment_cli_config import inherit_runtime_cli
        inherited = inherit_runtime_cli(checkout, state, real=real)
    for name in ("data", "artifacts", "runtime", "maintenance-worktrees"):
        private_dir(state / name)
    env = {k: v for k, v in os.environ.items() if not k.startswith("YIREN_")}
    env.update({
        "YIREN_ENVIRONMENT": profile,
        "YIREN_CONFIG_DIR": str(config),
        "YIREN_DATABASE_PATH": str(state / "data/yirenhub.sqlite3"),
        "YIREN_ARTIFACT_ROOT": str(state / "artifacts"),
        "YIREN_MEMORY_ROOT": str(state / "data/memory"),
        "YIREN_TASK_WORKTREE_ROOT": str(state.parent if profile == "runtime" else state / "task-worktrees"),
        "YIREN_RUNTIME_DIR": str(state / "runtime"),
        "YIREN_MAINTENANCE_WORKTREE_ROOT": str(state / "maintenance-worktrees"),
        # Only explicit demo/test states default to the offline adapter.
        "YIREN_USE_FAKE_CLI": ("true" if inherited["runtime"]["use_fake_cli"] else "false") if inherited else ("false" if real or profile in {"local", "runtime"} else "true"),
        "YIREN_DEMO_ONLY": "false" if inherited or real or profile in {"local", "runtime"} else "true",
        "YIREN_MAINTENANCE_ENABLED": "false",
        "YIREN_BACKEND_ORIGIN": f"http://127.0.0.1:{backend}",
        "PYTHONPATH": str(checkout),
    })
    # Quick-start compatibility: retain local users' existing data/config;
    # managed development/alpha/runtime always use fully isolated state.
    if profile == "local":
        for key, value in {"DATABASE_PATH": "data/yirenhub.sqlite3", "CONFIG_DIR": "config", "ARTIFACT_ROOT": "artifacts", "MEMORY_ROOT": "data/memory"}.items():
            env["YIREN_" + key] = str(checkout / value)
    return env


def available(port):
    try:
        with socket.socket() as s: s.bind(("127.0.0.1", port))
        return True
    except OSError: return False


def choose_ports(profile, backend=None, frontend=None, *, allocate=False, backend_only=False):
    defaults = profiles()[profile]
    pair = (backend or defaults["backend_port"], frontend or defaults["frontend_port"])
    if pair[0] == pair[1] or not all(1 <= p <= 65535 for p in pair):
        raise EnvironmentError("Ports must be distinct and in 1..65535")
    if all(available(p) for p in (pair[:1] if backend_only else pair)): return pair
    if allocate and backend is None and frontend is None:
        for n in range(100):
            pair = (8400 + n, 5180 + n)
            if all(available(p) for p in (pair[:1] if backend_only else pair)): return pair
    raise EnvironmentError(f"Ports are occupied: {pair}; existing processes were not stopped")


def request(port, path="/api/health", *, token=None, timeout=2):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    # Tooling polls must not keep an otherwise unused environment alive (runtime_control.PROBE_HEADER).
    headers = {"X-AgentClub-Probe": "1", **({"X-AgentClub-Control": token} if token else {})}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=b"" if token else None, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=timeout) as response: return json.load(response)


def python_for(root, install=True):
    py = root / ".venv/bin/python"
    if not py.exists():
        if not install: raise EnvironmentError("Missing .venv; omit --no-install to initialize it")
        bootstrap = shutil.which("python3.12") or shutil.which("python3")
        if not bootstrap: raise EnvironmentError("Python 3.11+ is required (CI uses 3.12)")
        version = subprocess.check_output([bootstrap, "-c", "import sys;print(sys.version_info >= (3,11))"], text=True).strip()
        if version != "True": raise EnvironmentError("Default Python is too old; install Python 3.12")
        run([bootstrap, "-m", "venv", py.parent.parent])
    version = subprocess.check_output([py, "-c", "import sys;print(sys.version_info >= (3,11))"], text=True).strip()
    if version != "True": raise EnvironmentError("Existing .venv is older than Python 3.11; recreate it explicitly")
    return py


def setup(root, install=True, backend_only=False, *, output=None):
    py = python_for(root, install)
    if not backend_only:
        run(["node", "-e", "const [a,b]=process.versions.node.split('.').map(Number);process.exit(((a===20&&b>=19)||(a===22&&b>=12)||a>22)?0:1)"])
    if install:
        stamps = private_dir(root / ".yiren/dependencies")
        for name, command, cwd in [
            ("requirements.txt", [py, "-m", "pip", "install", "-r", root / "requirements.txt"], root),
            ("frontend/package-lock.json", ["npm", "ci"], root / "frontend"),
        ]:
            if backend_only and name.startswith("frontend/"): continue
            digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
            stamp = stamps / (Path(name).name + ".stamp")
            missing = name.startswith("frontend/") and not (root / "frontend/node_modules").is_dir()
            if missing or not stamp.exists() or stamp.read_text() != digest:
                run(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT if output else None)
                stamp.write_text(digest)
    elif not backend_only and not (root / "frontend/node_modules").is_dir():
        raise EnvironmentError("Missing frontend/node_modules; omit --no-install")
    return py
