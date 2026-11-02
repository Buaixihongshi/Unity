import json
from pathlib import Path

import pytest

from backend.app.adapters import antigravity_config as agc


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Point Path.home() at a tmp dir so we never touch the real ~/.gemini."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


SPEC = {"command": "py", "args": ["-m", "backend.app.mcp.server"], "env": {"X": "1"}}


def test_workspace_plugin_registers_only_this_workspace(fake_home, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = agc.write_workspace_plugin(workspace, SPEC)
    # agy reads {"entries": [...]}; a bare list silently fails to parse.
    entries = json.loads((workspace / ".agents" / "plugins.json").read_text())["entries"]
    assert entries == [{"path": str(workspace / ".unity-plugins"), "include_only": [agc.PLUGIN_NAME]}]
    plugin = Path(entries[0]["path"]) / agc.PLUGIN_NAME
    assert json.loads((plugin / ".antigravity-plugin" / "plugin.json").read_text())["name"] == agc.PLUGIN_NAME
    assert config == plugin / "mcp_config.json"
    assert json.loads(config.read_text()) == {"mcpServers": {agc.PLUGIN_SERVER: SPEC}}
    assert agc.SERVER_NAME == "unity_hub"  # agy's <plugin>_<server>; must not collide with legacy yiren_hub
    assert not (fake_home / ".gemini").exists()


def test_recover_if_crashed_restores_from_sentinel(fake_home):
    cfg = agc.global_mcp_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    # Simulate a crash mid-turn: sentinel says original did not exist, and a
    # stale yiren config was left behind pointing at a dead temp server.
    agc._sentinel_path().write_text(json.dumps({"existed": False, "content": None}), encoding="utf-8")
    cfg.write_text('{"mcpServers": {"yiren_hub": {"command": "dead"}}}', encoding="utf-8")
    agc.recover_antigravity_global_config()
    assert not cfg.exists()  # restored to "did not exist"
    assert not agc._sentinel_path().exists()


def test_recover_is_noop_when_config_dir_absent(fake_home):
    # User who never ran gemini/agy: ~/.gemini/config does not exist.
    assert not (fake_home / ".gemini" / "config").exists()
    agc.recover_antigravity_global_config()  # must not create the dir
    assert not (fake_home / ".gemini" / "config").exists()


def test_recover_if_crashed_restores_preexisting_content(fake_home):
    cfg = agc.global_mcp_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    original = '{"mcpServers": {"user_server": {"command": "x"}}}\n'
    agc._sentinel_path().write_text(
        json.dumps({"existed": True, "content": original}), encoding="utf-8"
    )
    cfg.write_text('{"mcpServers": {"yiren_hub": {"command": "dead"}}}', encoding="utf-8")
    agc.recover_antigravity_global_config()
    assert cfg.read_text(encoding="utf-8") == original
    assert not agc._sentinel_path().exists()


def test_recover_creates_no_lock_file_when_no_sentinel(fake_home):
    # Config dir exists (user uses gemini) but there was no crash (no sentinel).
    cfg_dir = fake_home / ".gemini" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    agc.recover_antigravity_global_config()
    # Fast path must not acquire the lock, so no lock file is created.
    assert not agc._lock_path().exists()


# -- read_agy_log_hint ------------------------------------------------------

def _write_log(fake_home, name: str, body: str, mtime: float | None = None) -> Path:
    log_dir = fake_home / ".gemini" / "antigravity-cli" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / name
    p.write_text(body, encoding="utf-8")
    if mtime is not None:
        import os as _os
        _os.utime(p, (mtime, mtime))
    return p


def test_log_hint_returns_none_when_dir_missing(fake_home):
    import time
    assert agc.read_agy_log_hint(time.time() - 5) is None


def test_log_hint_returns_none_when_no_recent_log(fake_home):
    # Old log written 1 hour before run_start must be ignored.
    import time
    now = time.time()
    _write_log(fake_home, "cli-20260101_000000.log", "RESOURCE_EXHAUSTED",
               mtime=now - 3600)
    assert agc.read_agy_log_hint(now) is None


def test_log_hint_extracts_quota_message(fake_home):
    import time
    now = time.time()
    body = (
        "I0528 15:42:49 server.go:1083] Sending user message\n"
        "E0528 15:42:51 log.go:398] agent executor error: "
        "RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
        "Contact your administrator to enable overages. Resets in 18m20s.\n"
        "I0528 15:42:51 manager.go:459] CLI store manager shutting down\n"
    )
    _write_log(fake_home, "cli-20260528_154242.log", body, mtime=now)
    hint = agc.read_agy_log_hint(now - 1)
    assert hint is not None
    assert "quota exhausted" in hint
    assert "Resets in 18m20s." in hint


def test_log_hint_extracts_unauthenticated(fake_home):
    import time
    now = time.time()
    body = (
        "E ... server.go:609] Failed to get OAuth token: error getting "
        "token source from auth provider: You are not logged into Antigravity.\n"
        "I ... CLI program exited, shutting down\n"
    )
    _write_log(fake_home, "cli-20260528_153538.log", body, mtime=now)
    hint = agc.read_agy_log_hint(now - 1)
    assert hint == "agy not authenticated — run `agy` interactively to OAuth login"


def test_log_hint_falls_back_to_executor_error(fake_home):
    import time
    now = time.time()
    body = (
        "I ... server happy happy\n"
        "E ... log.go:398] agent executor error: something obscure happened\n"
    )
    _write_log(fake_home, "cli-20260528_120000.log", body, mtime=now)
    hint = agc.read_agy_log_hint(now - 1)
    assert hint is not None
    assert hint.startswith("agy agent executor error:")
    assert "something obscure happened" in hint


def test_log_hint_picks_latest_file(fake_home):
    import time
    now = time.time()
    _write_log(fake_home, "cli-20260528_100000.log",
               "not logged into Antigravity", mtime=now - 10)
    _write_log(fake_home, "cli-20260528_154242.log",
               "RESOURCE_EXHAUSTED (code 429): boom", mtime=now)
    hint = agc.read_agy_log_hint(now - 30)
    assert hint is not None
    # Latest file wins -> quota, not auth.
    assert "quota exhausted" in hint


@pytest.mark.parametrize("success", [
    "ChainedAuth: authenticated via keyring",
    "OAuth: authenticated successfully",
    "silent auth succeeded",
])
def test_auth_probe_failure_before_success_is_not_a_login_failure(fake_home, success):
    import time
    now = time.time()
    _write_log(fake_home, "cli-recovered.log",
               "not logged into Antigravity\n" + success + "\nprint timeout after 3m50s", mtime=now)
    assert agc.read_agy_log_hint(now) is None


def test_new_login_failure_after_success_is_reported(fake_home):
    import time
    now = time.time()
    _write_log(fake_home, "cli-expired.log",
               "authenticated via keyring\nnot logged into Antigravity", mtime=now)
    assert "not authenticated" in agc.read_agy_log_hint(now)


# -- coexistence with older Unity builds --------------------------------------

def _legacy_turn(release_after, injected):
    """What an older build does per turn: exclusive lock, inject yiren_hub, restore."""
    import threading

    def run():
        with agc._flock():
            cfg = agc.global_mcp_config_path()
            agc._sentinel_path().write_text(json.dumps({"existed": False, "content": None}))
            cfg.write_text(json.dumps({"mcpServers": {"yiren_hub": {"command": "legacy"}}}))
            injected.set()
            import time
            time.sleep(release_after)
            cfg.unlink()
            agc._sentinel_path().unlink()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_new_runs_share_the_lock_and_shut_out_older_builds(fake_home):
    with agc.legacy_config_excluded():
        with agc.legacy_config_excluded(timeout=0.2):  # another new instance
            pass
        with pytest.raises(TimeoutError):
            with agc._flock(timeout=0.2):  # an older build's per-turn lock
                pass


def test_agy_starts_only_after_older_build_restored_global_config(fake_home):
    import threading
    injected = threading.Event()
    waits = []
    legacy = _legacy_turn(0.3, injected)
    injected.wait(2)
    with agc.legacy_config_excluded(timeout=3, on_wait=lambda: waits.append(1)):
        # Had agy started now it would only find its own plugin server.
        assert not agc.global_mcp_config_path().exists()
    legacy.join(2)
    assert waits == [1]


def test_crash_leftover_is_restored_before_agy_starts(fake_home):
    cfg = agc.global_mcp_config_path()
    cfg.parent.mkdir(parents=True)
    agc._sentinel_path().write_text(json.dumps({"existed": False, "content": None}))
    cfg.write_text('{"mcpServers": {"yiren_hub": {"command": "dead"}}}')
    with agc.legacy_config_excluded(timeout=1):
        assert not cfg.exists() and not agc._sentinel_path().exists()


def test_stop_while_waiting_for_older_build(fake_home):
    import threading
    import time
    injected = threading.Event()
    legacy = _legacy_turn(1.5, injected)
    injected.wait(2)
    started = time.monotonic()
    with pytest.raises(agc.WaitCancelled):
        with agc.legacy_config_excluded(cancelled=lambda: True):
            pass
    assert time.monotonic() - started < 0.5
    legacy.join(3)
