import os
import subprocess
from pathlib import Path

from backend.app import desktop


def test_merge_path_keeps_shell_order_and_adds_cli_fallbacks():
    merged = desktop.merge_path("/custom/bin:/usr/bin", "/usr/bin:/bin").split(os.pathsep)
    assert merged[:3] == ["/custom/bin", "/usr/bin", "/bin"]
    assert "/opt/homebrew/bin" in merged and len(merged) == len(set(merged))


def test_login_shell_path_ignores_rc_banner(monkeypatch):
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, stdout=f"welcome!\n{desktop.PATH_MARKER}/a:/b\n")
    monkeypatch.setattr(desktop.subprocess, "run", fake_run)
    assert desktop.login_shell_path() == "/a:/b"

    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="no marker"))
    assert desktop.login_shell_path() is None


def test_desktop_env_keeps_state_outside_bundle_and_drops_inherited_yiren(tmp_path):
    root, state = tmp_path / "Unity.app/Contents/Resources/app", tmp_path / "support"
    env = desktop.desktop_env(root, state, {"PATH": "/usr/bin", "YIREN_DATABASE_PATH": "/leak.sqlite3"}, "/shell/bin")

    assert env["YIREN_DATABASE_PATH"] == str(state / "data/yirenhub.sqlite3")
    assert env["YIREN_CONFIG_DIR"] == str(root / "config")
    assert env["YIREN_FRONTEND_DIST"] == str(root / "frontend/dist")
    assert env["YIREN_MAINTENANCE_ENABLED"] == "false" and env["YIREN_USE_FAKE_CLI"] == "false"
    assert env["PATH"].startswith("/shell/bin:/usr/bin")
    assert not Path(env["PYTHONPYCACHEPREFIX"]).is_relative_to(root)
    assert all(not v.startswith(str(root)) for k, v in env.items()
               if k.startswith("YIREN_") and k not in {"YIREN_CONFIG_DIR", "YIREN_FRONTEND_DIST"})


def test_prepare_state_creates_private_dirs(tmp_path):
    workspace = desktop.prepare_state(tmp_path / "support")
    assert workspace.is_dir()
    assert (tmp_path / "support/data").stat().st_mode & 0o777 == 0o700


def test_packaged_source_reports_frozen_build_identity(tmp_path):
    import json
    from backend.app.runtime_identity import BUILD_IDENTITY, source_snapshot

    frozen = {"git_sha": "a" * 40, "branch": "main", "dirty": False, "source_digest": "d" * 64,
              "checkout_id": "c" * 16, "environment": "desktop", "captured_at": "2026-10-28T00:00:00+00:00"}
    (tmp_path / BUILD_IDENTITY).write_text(json.dumps(frozen), encoding="utf-8")
    snapshot = source_snapshot(tmp_path)
    assert snapshot == {k: frozen[k] for k in ("git_sha", "branch", "dirty", "source_digest", "checkout_id")}


def test_backend_stops_when_shell_process_disappears(monkeypatch):
    import signal
    parents = iter([100, 100, 1])
    killed = []
    monkeypatch.setattr(desktop.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(desktop.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    desktop.exit_with_parent(100, interval=0.01).join(timeout=2)
    assert killed == [(os.getpid(), signal.SIGTERM)]


def test_project_dir_prefers_chosen_repository_and_survives_a_missing_one(tmp_path, monkeypatch):
    default, chosen = tmp_path / "workspace", tmp_path / "repo"
    default.mkdir()
    chosen.mkdir()
    monkeypatch.setenv("UNITY_WORKSPACE", str(chosen))
    assert desktop.project_dir(default) == chosen

    monkeypatch.setenv("UNITY_WORKSPACE", str(tmp_path / "moved-away"))
    assert desktop.project_dir(default) == default
    monkeypatch.delenv("UNITY_WORKSPACE")
    assert desktop.project_dir(default) == default


def test_every_scripts_module_the_backend_imports_is_bundled():
    """Workflow imports repository scripts lazily; the DMG must ship each of them (#49)."""
    import re

    repo = Path(__file__).resolve().parents[2]
    pattern = re.compile(r"^\s*from scripts(?:\.(\w+))? import ([\w, ]+?)(?:\s+as\s+\w+)?\s*$", re.M)

    def imported(source):
        for package_module, names in pattern.findall(source):
            yield from ([package_module] if package_module else [n.strip() for n in names.split(",")])

    pending = [m for path in (repo / "backend/app").rglob("*.py") for m in imported(path.read_text())]
    seen = set()
    while pending:
        module = pending.pop()
        if module not in seen:
            seen.add(module)
            pending.extend(imported((repo / "scripts" / f"{module}.py").read_text()))

    assert "environment_common" in seen
    assert all(re.fullmatch(r"environment_\w+", m) for m in seen), sorted(seen)
    packaging = (repo / "scripts/package-macos.sh").read_text()
    assert 'cp "$ROOT"/scripts/environment_*.py "$RES/app/scripts/"' in packaging
    assert "--exclude 'environments.json'" not in packaging  # environment_common reads it


def test_desktop_workflow_without_repository_points_to_the_menu(tmp_path, monkeypatch):
    from backend.app.extensions.tasks.environments import TaskEnvironments

    folder = tmp_path / "workspace"
    folder.mkdir()
    environments = TaskEnvironments(None, folder, tmp_path / "task-worktrees")

    monkeypatch.setenv("YIREN_ENVIRONMENT", "desktop")
    failed = environments.prepare(environments.initial("wt_0123456789abcdef"))
    assert failed["status"] == "failed"
    assert "选择项目仓库" in failed["failure_reason"]

    monkeypatch.setenv("YIREN_ENVIRONMENT", "development")
    assert "选择项目仓库" not in environments.prepare(environments.initial("wt_0123456789abcdef"))["failure_reason"]


def test_packaging_keeps_its_interpreter_off_the_users_path():
    """uv would otherwise link the build cache's python into ~/.local/bin (#50)."""
    packaging = (Path(__file__).resolve().parents[2] / "scripts/package-macos.sh").read_text()
    installs = [line for line in packaging.splitlines() if line.lstrip().startswith("uv python install")]
    assert installs and all("--no-bin" in line for line in installs)


def test_macos_status_mobius_path_covers_both_loops_and_shares_cruise():
    """Issue #115: macOS UnityApp.swift cruise path must cover both loops and share mobiusPath."""
    import re

    swift_path = Path(__file__).resolve().parents[2] / "packaging/macos/UnityApp.swift"
    content = swift_path.read_text(encoding="utf-8")

    mobius_match = re.search(r'private static let mobiusPath\s*=\s*"([^"]+)"', content)
    assert mobius_match, "mobiusPath constant must be defined in UnityApp.swift"
    mobius_path = mobius_match.group(1)

    # Extract all coordinate numbers to verify geometric loop coverage
    coords = [float(num) for num in re.findall(r"[-+]?\b\d+(?:\.\d+)?\b", mobius_path)]
    x_coords = coords[0::2]
    assert min(x_coords) <= 60, "mobiusPath must reach outer boundary of left loop (x <= 60)"
    assert max(x_coords) >= 450, "mobiusPath must reach outer boundary of right loop (x >= 450)"
    assert mobius_path.startswith("M256,256"), "mobiusPath must condense from center (256, 256)"
    assert mobius_path.endswith("Z"), "mobiusPath must properly close with Z"

    # Both mobius and cruise must strictly share Self.mobiusPath
    assert '<path class="mobius" pathLength="1000" d="\\(Self.mobiusPath)" />' in content
    assert '<path class="cruise \\(cruiseClass)" pathLength="1000" stroke-dasharray="140 860" stroke-dashoffset="1000" d="\\(Self.mobiusPath)" />' in content

    # Dynamic cruise animation and accessibility checks
    assert "@keyframes cruise" in content
    assert ".cruise.active" in content
    assert "@media (prefers-reduced-motion: reduce)" in content
    assert ".cruise.active { display: none !important; }" in content
