import os
from pathlib import Path

import pytest

from backend.app.settings import Settings


@pytest.fixture(autouse=True)
def _reset_agent_registry():
    yield
    from backend.app.domain.agent_registry import reset_agent_registry
    reset_agent_registry()


@pytest.fixture(autouse=True)
def _no_host_instance_env(monkeypatch):
    # Agent sessions inherit the daily backend's YIREN_* paths; tests resolve
    # state from their own fixtures so they never write into that instance (#88).
    for name in [name for name in os.environ if name.startswith("YIREN_")]:
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _no_cli_version_probe(monkeypatch):
    # Turn records probe `<cli> --version`; offline tests must not run real CLIs.
    from backend.app.adapters import turn_record
    monkeypatch.setattr(turn_record, "VERSION_PROBE", False)


@pytest.fixture()
def temp_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "yirenhub.sqlite3",
        artifact_root=tmp_path / "artifacts",
        memory_root=tmp_path / "memory",
        config_dir=Path("config"),
        use_fake_cli=True,
        max_turns=6,
        max_a2a_invocations=8,
        # Most suites exercise the optional extensions; core-only behavior
        # is covered by test_extensions.py with its own settings.
        discussion_enabled=True,
        tasks_enabled=True,
        maintenance_enabled=True,
    )


@pytest.fixture()
def repository(temp_settings: Settings):
    from backend.app.storage.repository import Repository

    repo = Repository(temp_settings.database_path)
    repo.initialize()
    return repo


# Composition roots install the optional extensions; tests that drive the core
# directly (runner, MCP tools) need the same hooks main.py installs.
from backend.app import extensions as _extensions  # noqa: E402

_extensions.install()
