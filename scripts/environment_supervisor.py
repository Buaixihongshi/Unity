"""Own the processes we start; stop only after authenticated atomic draining."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time
import urllib.error
import uuid

from scripts.environment_common import (
    ROOT, EnvironmentError, choose_ports, environment, home, lock, private_dir,
    profiles, request, setup, state_path, write_json,
)


def process_start(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True,
                            env={**os.environ, "LC_ALL": "C"})
    return result.stdout.strip() if result.returncode == 0 else None


def record_path(root, profile, real=False, state=None):
    return (state or state_path(root, profile, real)) / "runtime/instance.json"


def read_record(root, profile, real=False, state=None):
    path = record_path(root, profile, real, state)
    return json.loads(path.read_text()) if path.exists() else None


def alive(record):
    return bool(record and record.get("status") in {"starting", "running"}
                and process_start(record["supervisor_pid"]) == record["supervisor_started_at"])


def verify(record):
    started = request(record["backend_port"])["runtime"]["started"]
    if started.get("instance_id") != record["instance_id"] or started["checkout"] != record["checkout"]:
        raise EnvironmentError("Service identity no longer matches its ownership record")
    return started


def stop(root, profile, real=False, state=None):
    record = read_record(root, profile, real, state)
    if not alive(record): raise EnvironmentError("No live managed supervisor; stale/unmanaged processes are never killed")
    if record["checkout"] != str(root) or record["environment"] != profile:
        raise EnvironmentError("State belongs to a different managed checkout/environment")
    verify(record)
    token = json.loads(Path(record["token_file"]).read_text())["token"]
    request(record["backend_port"], "/api/runtime/drain", token=token)
    # Revalidate PID birth after the network roundtrip, not just the PID number.
    if not alive(record): raise EnvironmentError("Supervisor identity changed during drain")
    os.kill(record["supervisor_pid"], signal.SIGTERM)
    for _ in range(300):
        current = read_record(root, profile, real, state)
        if current and current.get("status") == "stopped":
            print(f"Stopped managed {profile}: {root}")
            return
        time.sleep(0.1)
    raise EnvironmentError("Shutdown still in progress; inspect status rather than killing by port")


def terminate(children):
    for child in reversed(children):
        try: os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError: pass
    deadline = time.monotonic() + 10
    for child in children:
        try: child.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            child.wait(timeout=5)


IDLE_CHECK_SECONDS = 60


def idle_limit(profile, idle_stop=None):
    """Minutes without user requests before a supervisor stops itself; None disables it."""
    minutes = profiles()[profile].get("idle_stop_minutes") if idle_stop is None else idle_stop
    return minutes or None


def idle_for(port, limit):
    """True when the backend has had no user request for `limit` minutes and holds no work.

    None means the backend predates idle tracking, so idle stop cannot be applied to it.
    """
    runtime = request(port)["runtime"]
    if "idle_seconds" not in runtime: return None
    if any((runtime.get("activity") or {}).values()): return False
    return runtime["idle_seconds"] >= limit * 60


def running_instances(profile):
    """Live managed supervisors of `profile`, from the shared instance registry."""
    found = []
    for entry in sorted((home() / ".instances").glob("*.json")):
        try:
            record = json.loads(Path(json.loads(entry.read_text())["record"]).read_text())
        except (OSError, ValueError, KeyError):
            continue
        if record.get("environment") == profile and alive(record): found.append(record)
    return found


def backend_command(py, port, reload, state_dirs=()):
    args = [str(py), "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(port)]
    if reload:
        args.append("--reload")
        # Task worktrees, artifacts and logs live in the checkout's state; watching them would restart
        # the server mid-turn. uvicorn only honours existing absolute dirs here (and ignores
        # --reload-dir below the cwd), so pass resolved paths.
        for directory in dict.fromkeys(str(Path(d).resolve()) for d in state_dirs):
            args += ["--reload-exclude", directory]
    return args


def supervise(root, profile, *, real=False, providers="codex", backend=None, frontend=None,
              allocate=False, backend_only=False, install=True, reload=None, state=None, open_browser=False,
              reset_onboarding=False, inherit_cli=False, idle_stop=None):
    state = state or state_path(root, profile, real)
    limit = idle_limit(profile, idle_stop)
    private_dir(state / "runtime")
    with lock(state / "runtime/owner.lock"):
        py = setup(root, install, backend_only)
        with lock(home(root) / ".port-allocation.lock", wait=True):
            backend, frontend = choose_ports(profile, backend, frontend, allocate=allocate, backend_only=backend_only)
            env = environment(root, profile, real=real, providers=providers,
                              backend=backend, frontend=frontend, state=state, inherit_cli=inherit_cli)
            instance_id = uuid.uuid4().hex
            token_file = state / "runtime/control.json"
            token = secrets.token_urlsafe(32)
            write_json(token_file, {"token": token})
            env.update(YIREN_LIFECYCLE_TOKEN=token, YIREN_INSTANCE_ID=instance_id)
            record = {
                "instance_id": instance_id, "checkout": str(root), "environment": profile,
                "backend_port": backend, "frontend_port": None if backend_only else frontend,
                "supervisor_pid": os.getpid(), "supervisor_started_at": process_start(os.getpid()),
                "token_file": str(token_file), "status": "starting", "state": str(state),
                "real_cli": env["YIREN_USE_FAKE_CLI"] == "false",
                "cli_config_source": "runtime" if inherit_cli else "isolated",
                "idle_stop_minutes": limit,
            }
            path = record_path(root, profile, real, state)
            write_json(path, record)
            registry = private_dir(home(root) / ".instances")
            key = hashlib.sha256(str(path).encode()).hexdigest()[:24]
            write_json(registry / (key + ".json"), {"record": str(path)})
            children = []
            logs = []
            stopping = False

            def on_signal(_signum, _frame):
                nonlocal stopping
                stopping = True

            old_handlers = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
            try:
                log = (state / "runtime/backend.log").open("a")
                logs.append(log)
                reloading = reload if reload is not None else profiles()[profile]["reload"]
                if reloading:
                    # Packaging writes a full Python runtime under output/; uvicorn only excludes
                    # directories that exist when it starts, so create the (ignored) folder first.
                    (root / "output").mkdir(exist_ok=True)
                args = backend_command(py, backend, reloading,
                                       [d for d in (root / ".yiren", state, root / "output") if d.is_dir()])
                children.append(subprocess.Popen(args, cwd=root, env=env, stdout=log, stderr=log,
                                                       stdin=subprocess.DEVNULL, start_new_session=True))
                if not backend_only:
                    production = profile in {"alpha", "runtime"}
                    if profile == "alpha":
                        subprocess.run(["npm", "run", "build"], cwd=root / "frontend", env=env, check=True)
                    if production and not (root / "frontend/dist/__environment").is_file():
                        raise EnvironmentError("Production frontend has not been staged with its source identity")
                    log = (state / "runtime/frontend.log").open("a")
                    logs.append(log)
                    front_command = ["npm", "exec", "vite", "--", "preview"] if production else ["npm", "run", "dev", "--"]
                    children.append(subprocess.Popen([*front_command, "--host", "127.0.0.1", "--port", str(frontend), "--strictPort"],
                        cwd=root / "frontend", env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True))
                deadline = time.monotonic() + 45
                while True:
                    if any(c.poll() is not None for c in children): raise EnvironmentError(f"Service exited; inspect {state / 'runtime'}")
                    try:
                        health = request(backend)
                        started = health["runtime"]["started"]
                        if started.get("instance_id") != instance_id: raise EnvironmentError("Unexpected service owns backend port")
                        if not backend_only:
                            front = request(frontend, "/__environment")
                            proxy = request(frontend)["runtime"]["started"]
                            if any(front.get(k) != started.get(k) for k in ("git_sha", "source_digest", "checkout_id", "environment")):
                                raise EnvironmentError("Frontend/backend source identities differ")
                            if proxy.get("instance_id") != instance_id: raise EnvironmentError("Frontend proxy targets another backend")
                        record.update(status="running", backend_pid=started["pid"], git_sha=started["git_sha"], source_digest=started["source_digest"])
                        write_json(path, record)
                        if reset_onboarding: request(backend, "/api/onboarding/reset", token=token)
                        break
                    except (OSError, ValueError, KeyError, urllib.error.URLError):
                        if time.monotonic() > deadline: raise EnvironmentError(f"Startup identity/health unavailable; inspect {state / 'runtime'}")
                        time.sleep(0.2)
            except BaseException:
                terminate(children)
                record["status"] = "stopped"
                write_json(path, record)
                for log in logs: log.close()
                for s, handler in old_handlers.items(): signal.signal(s, handler)
                raise
        print(f"{profile} ready: http://127.0.0.1:{frontend if not backend_only else backend} · {record['git_sha'][:7]} · {state}", flush=True)
        if open_browser and not backend_only:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{frontend}")
        if limit: print(f"Idle stop: after {limit} min without requests and with no unfinished work", flush=True)
        next_idle_check = time.monotonic() + IDLE_CHECK_SECONDS
        try:
            while True:
                for idx, c in enumerate(children):
                    if c.poll() is not None:
                        raise EnvironmentError(f"Managed service {idx} (PID {c.pid}) exited unexpectedly with code {c.poll()}")
                if limit and not stopping and time.monotonic() >= next_idle_check:
                    next_idle_check = time.monotonic() + IDLE_CHECK_SECONDS
                    try:
                        idle = idle_for(backend, limit)
                        if idle is None:
                            print("Idle stop unavailable: this checkout's backend does not report idle time", flush=True)
                            limit = None
                        elif idle:
                            # Same authenticated drain as `env.sh stop`; work that appeared meanwhile wins (409).
                            request(backend, "/api/runtime/drain", token=token)
                            record["stopped_reason"] = "idle"
                            print(f"Stopping: idle for {record['idle_stop_minutes']} min with no unfinished work", flush=True)
                            break
                    except urllib.error.HTTPError as exc:
                        if exc.code != 409: raise
                    except (OSError, ValueError, KeyError, urllib.error.URLError):
                        pass  # A reloading backend answers on the next check; exits are caught above.
                if stopping:
                    try:
                        request(backend, "/api/runtime/drain", token=token)
                        break
                    except urllib.error.HTTPError as exc:
                        if exc.code != 409: raise
                        print("Waiting for active work to finish before stopping…", flush=True)
                time.sleep(0.5)
        finally:
            terminate(children)
            record["status"] = "stopped"
            write_json(path, record)
            token_file.unlink(missing_ok=True)
            for log in logs: log.close()
            for s, handler in old_handlers.items(): signal.signal(s, handler)


def start(root, profile, *, detach=False, **kwargs):
    record = read_record(root, profile, kwargs.get("real", False), kwargs.get("state"))
    if alive(record):
        if record["checkout"] != str(root) or record["environment"] != profile:
            raise EnvironmentError("State already belongs to another managed checkout/environment")
        verify(record)
        if kwargs.get("reset_onboarding"):
            token = json.loads(Path(record["token_file"]).read_text())["token"]
            request(record["backend_port"], "/api/onboarding/reset", token=token)
        print(f"Already running: {root} ({profile})")
        return record
    if not detach:
        supervise(root, profile, **kwargs)
        return
    py = setup(root, kwargs.get("install", True), kwargs.get("backend_only", False))
    state = kwargs.get("state") or state_path(root, profile, kwargs.get("real", False))
    private_dir(state / "runtime")
    spec = {"root": str(root), "profile": profile, **kwargs, "install": False}
    if spec.get("state"): spec["state"] = str(spec["state"])
    spec_path = state / "runtime/launch.json"
    write_json(spec_path, spec)
    with (state / "runtime/supervisor.log").open("a") as log:
        child = subprocess.Popen([py, ROOT / "scripts/environment_cli.py", "_supervise", str(spec_path)],
                                  cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(500):
        record = read_record(root, profile, kwargs.get("real", False), kwargs.get("state"))
        if record and record.get("supervisor_pid") == child.pid and record.get("status") == "running":
            print(f"{profile} ready: http://127.0.0.1:{record['frontend_port'] or record['backend_port']} · {record['git_sha'][:7]}")
            return record
        if child.poll() is not None: raise EnvironmentError(f"Supervisor failed; inspect {state / 'runtime/supervisor.log'}")
        time.sleep(0.1)
    raise EnvironmentError("Supervisor startup still pending; inspect its live record before retrying")
