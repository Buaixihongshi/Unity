"""Backend entry point for the packaged macOS app.

The Swift shell starts ``python -m backend.app.desktop --port N`` with the
bundled interpreter, so ``sys.executable`` stays a real Python for the MCP
servers and watchdogs the backend spawns. Everything writable lives outside
the (signed, read-only) app bundle.
"""
from __future__ import annotations

import argparse
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

APP_NAME = "Unity"
# Finder/Dock launches inherit launchd's minimal PATH; CLIs usually live here.
FALLBACK_PATH_DIRS = (
    "~/.local/bin", "~/.npm-global/bin", "~/.bun/bin", "~/.cargo/bin",
    "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
)
PATH_MARKER = "__UNITY_PATH__"


def app_root() -> Path:
    return Path(__file__).resolve().parents[2]


def support_dir() -> Path:
    override = os.environ.get("UNITY_SUPPORT_DIR")
    return Path(override).expanduser() if override else Path.home() / "Library/Application Support" / APP_NAME


def login_shell_path(timeout: float = 5.0) -> str | None:
    shell = os.environ.get("SHELL") or "/bin/zsh"
    try:
        output = subprocess.run(
            [shell, "-ilc", f'printf "{PATH_MARKER}%s" "$PATH"'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # Shell rc files may print banners; only trust what follows the marker.
    _, found, value = output.rpartition(PATH_MARKER)
    return (value.strip() or None) if found else None


def merge_path(*sources: str | None) -> str:
    seen: list[str] = []
    for source in (*sources, os.pathsep.join(FALLBACK_PATH_DIRS)):
        for entry in (source or "").split(os.pathsep):
            entry = os.path.expanduser(entry.strip())
            if entry and entry not in seen:
                seen.append(entry)
    return os.pathsep.join(seen)


def desktop_env(root: Path, state: Path, base: dict[str, str], shell_path: str | None) -> dict[str, str]:
    env = {k: v for k, v in base.items() if not k.startswith("YIREN_")}
    # The native shell owns the drain token used for an atomic idle restart.
    token = env.pop("UNITY_LIFECYCLE_TOKEN", "") or secrets.token_urlsafe(32)
    env.update({
        "PATH": merge_path(shell_path, base.get("PATH")),
        "YIREN_ENVIRONMENT": "desktop",
        "YIREN_CONFIG_DIR": str(root / "config"),
        "YIREN_FRONTEND_DIST": str(root / "frontend/dist"),
        "YIREN_DATABASE_PATH": str(state / "data/yirenhub.sqlite3"),
        "YIREN_ARTIFACT_ROOT": str(state / "artifacts"),
        "YIREN_MEMORY_ROOT": str(state / "data/memory"),
        "YIREN_RUNTIME_DIR": str(state / "runtime"),
        "YIREN_MAINTENANCE_WORKTREE_ROOT": str(state / "maintenance-worktrees"),
        "YIREN_TASK_WORKTREE_ROOT": str(state / "task-worktrees"),
        # Self-maintenance edits a Git checkout; the bundle is not one.
        "YIREN_MAINTENANCE_ENABLED": "false",
        "YIREN_USE_FAKE_CLI": "false",
        "YIREN_DEMO_ONLY": "false",
        "YIREN_LIFECYCLE_TOKEN": token,
        # Keep bytecode out of the signed bundle, for this process and its children.
        "PYTHONPYCACHEPREFIX": str(state / "cache/pycache") if base.get("UNITY_SUPPORT_DIR") else str(Path.home() / "Library/Caches" / APP_NAME / "pycache"),
    })
    return env


def prepare_state(state: Path) -> Path:
    for sub in ("data", "artifacts", "runtime", "workspace"):
        (state / sub).mkdir(parents=True, exist_ok=True)
    for path in (state, state / "data", state / "runtime"):
        path.chmod(0o700)
    return state / "workspace"


def project_dir(default: Path) -> Path:
    """The repository chosen in the app menu (UNITY_WORKSPACE), else the private workspace.

    A chosen folder that was moved or deleted must not stop the app from starting.
    """
    chosen = os.environ.get("UNITY_WORKSPACE")
    if chosen and Path(chosen).expanduser().is_dir():
        return Path(chosen).expanduser()
    if chosen:
        print(f"UNITY_WORKSPACE is not a directory, using the default workspace: {chosen}", file=sys.stderr)
    return default


def exit_with_parent(parent: int, interval: float = 1.0) -> threading.Thread:
    """The Swift shell owns this backend; never outlive it, even after a crash."""
    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(interval)
        os.kill(os.getpid(), signal.SIGTERM)  # uvicorn's graceful shutdown path
    watcher = threading.Thread(target=watch, daemon=True, name="unity-parent-watch")
    watcher.start()
    return watcher


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="unity-desktop")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)

    root, state = app_root(), support_dir()
    workspace = prepare_state(state)
    env = desktop_env(root, state, dict(os.environ), login_shell_path())
    os.environ.clear()
    os.environ.update(env)
    # Shared work and MCP tools treat the process cwd as the project workspace.
    os.chdir(project_dir(workspace))
    sys.pycache_prefix = os.environ["PYTHONPYCACHEPREFIX"]

    exit_with_parent(os.getppid())
    import uvicorn
    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
