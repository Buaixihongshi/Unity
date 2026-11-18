import os
import subprocess
import sys
from pathlib import Path

from backend.app.extensions.tasks.environments import TaskEnvironments

ROOT = Path(__file__).resolve().parents[2]


def test_task_worktrees_stay_under_tmp_path(tmp_path):
    # Probe for the subprocess below; also guards the normal suite run.
    assert not [name for name in os.environ if name.startswith("YIREN_")]
    worktree_root = TaskEnvironments(None, tmp_path / "repo").worktree_root
    assert worktree_root.is_relative_to(tmp_path.resolve())


def test_inherited_host_instance_env_does_not_leak_into_tests(tmp_path):
    host = tmp_path / "daily-instance"
    env = {
        **os.environ,
        "YIREN_TASK_WORKTREE_ROOT": str(host / "task-worktrees"),
        "YIREN_DATABASE_PATH": str(host / "data" / "yirenhub.sqlite3"),
    }
    probe = f"{Path(__file__).relative_to(ROOT)}::test_task_worktrees_stay_under_tmp_path"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", probe, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
