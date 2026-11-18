import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from backend.app.api.mission_detail import (
    _raw_event,
    _artifact_records,
    _derive_turns,
    _derive_tool_events,
    _derive_interrupts,
    _derive_thoughts,
)
from backend.app.domain.maintenance import MaintenanceRun, RegisteredRepository
from backend.app.domain.models import Mission, MissionEvent, format_iso_utc
from backend.app.extensions.maintenance.service import MaintenanceService
from backend.app.storage.repository import Repository


def test_mission_event_model_timezone_handling():
    # 1. Naive string from historical database
    event_naive = MissionEvent(
        id="1",
        mission_id="m1",
        event_type="test",
        payload={},
        created_at="2026-11-09 02:53:00",
    )
    assert event_naive.created_at.tzinfo == timezone.utc
    assert event_naive.created_at.hour == 2
    assert event_naive.created_at.minute == 53

    # 2. Naive datetime object
    event_dt = MissionEvent(
        id="2",
        mission_id="m1",
        event_type="test",
        payload={},
        created_at=datetime(2026, 11, 9, 2, 53, 0),
    )
    assert event_dt.created_at.tzinfo == timezone.utc

    # 3. Aware UTC string
    event_utc = MissionEvent(
        id="3",
        mission_id="m1",
        event_type="test",
        payload={},
        created_at="2026-11-09T02:53:00Z",
    )
    assert event_utc.created_at.tzinfo == timezone.utc

    # 4. Existing non-UTC offset: must preserve the instant in time
    offset_cst = timezone(timedelta(hours=8))
    event_offset = MissionEvent(
        id="4",
        mission_id="m1",
        event_type="test",
        payload={},
        created_at="2026-11-09T10:53:00+08:00",
    )
    assert event_offset.created_at.tzinfo is not None
    assert event_offset.created_at.utcoffset() == timedelta(hours=8)
    # The instant matches 02:53:00 UTC exactly
    expected_utc = datetime(2026, 11, 9, 2, 53, 0, tzinfo=timezone.utc)
    assert event_offset.created_at.timestamp() == expected_utc.timestamp()


def test_format_iso_utc():
    assert format_iso_utc(None) == ""
    assert format_iso_utc("") == ""

    # Naive string -> UTC
    assert format_iso_utc("2026-11-09 02:53:00") == "2026-11-09T02:53:00+00:00"

    # Naive datetime -> UTC
    dt_naive = datetime(2026, 11, 9, 2, 53, 0)
    assert format_iso_utc(dt_naive) == "2026-11-09T02:53:00+00:00"

    # UTC with Z string
    assert format_iso_utc("2026-11-09T02:53:00Z") == "2026-11-09T02:53:00+00:00"

    # Non-UTC offset string: instant preserved
    res = format_iso_utc("2026-11-09T10:53:00+08:00")
    assert res == "2026-11-09T10:53:00+08:00"
    assert datetime.fromisoformat(res).timestamp() == datetime(2026, 11, 9, 2, 53, 0, tzinfo=timezone.utc).timestamp()


def test_repository_add_event_and_maintenance_event_store_iso_utc_with_z(repository, temp_settings):
    repository.create_mission(Mission(id="m_write", title="w", goal="w"))
    event_id = repository.add_event("m_write", "tool.call", {"name": "test"})

    with sqlite3.connect(temp_settings.database_path) as db:
        raw_created_at = db.execute("SELECT created_at FROM events WHERE id = ?", (event_id,)).fetchone()[0]

    # ISO-8601 with millisecond precision and Z suffix
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$", raw_created_at)

    event = [e for e in repository.list_events_after("m_write", after_id=0) if e.event_type == "tool.call"][0]
    assert event.created_at.tzinfo == timezone.utc

    # Maintenance event writing check
    m_id = repository.add_maintenance_event("run_w", "step", {"ok": True})
    with sqlite3.connect(temp_settings.database_path) as db:
        raw_m_created_at = db.execute(
            "SELECT created_at FROM maintenance_events WHERE id = ?", (m_id,)
        ).fetchone()[0]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$", raw_m_created_at)


def test_historical_naive_sqlite_events_read_and_serialized_as_utc(repository, temp_settings):
    repository.create_mission(Mission(id="m_hist", title="h", goal="h"))

    # Direct SQLite insert simulating legacy naive timestamp
    with sqlite3.connect(temp_settings.database_path) as db:
        db.execute(
            "INSERT INTO events (mission_id, event_type, payload, created_at) "
            "VALUES ('m_hist', 'turn.completed', ?, '2026-11-09 02:53:00')",
            (json.dumps({"turn_id": "turn_1", "duration_ms": 100}),),
        )
        db.execute(
            "INSERT INTO events (mission_id, event_type, payload, created_at) "
            "VALUES ('m_hist', 'artifact', ?, '2026-11-09 02:53:00')",
            (json.dumps({"path": "output.txt", "title": "output"}),),
        )
        db.execute(
            "INSERT INTO events (mission_id, event_type, payload, created_at) "
            "VALUES ('m_hist', 'interrupt', ?, '2026-11-09 02:53:00')",
            (json.dumps({"content": "stop"}),),
        )
        db.execute(
            "INSERT INTO events (mission_id, event_type, payload, created_at) "
            "VALUES ('m_hist', 'agent.thought', ?, '2026-11-09 02:53:00')",
            (json.dumps({"thought": "thinking"}),),
        )

    all_events = repository.list_events_after("m_hist", after_id=0)
    # Filter to only the 4 legacy events
    events = [e for e in all_events if e.event_type != "mission_created"]
    assert len(events) == 4
    for event in events:
        assert event.created_at.tzinfo == timezone.utc
        assert event.created_at.hour == 2
        assert event.created_at.minute == 53

    # Check API serialization outlets
    raw_ev = _raw_event(events[0])
    assert raw_ev["created_at"] == "2026-11-09T02:53:00+00:00"

    turns = _derive_turns(events)
    assert turns[0]["completed_at"] == "2026-11-09T02:53:00+00:00"

    artifacts = _artifact_records(temp_settings, "m_hist", events)
    assert artifacts[0]["created_at"] == "2026-11-09T02:53:00+00:00"

    interrupts = _derive_interrupts(events)
    assert interrupts[0]["created_at"] == "2026-11-09T02:53:00+00:00"

    thoughts = _derive_thoughts(events)
    assert thoughts[0]["created_at"] == "2026-11-09T02:53:00+00:00"


def test_maintenance_events_legacy_and_mixed_read(repository, temp_settings, tmp_path):
    run_id = "run_mix"
    repo_model = RegisteredRepository(
        id="repo_1",
        name="test-repo",
        local_path=tmp_path,
        github_repo="owner/test-repo",
        validation_commands=[["pytest"]],
    )
    repository.save_registered_repository(repo_model)
    repository.create_maintenance_run(MaintenanceRun(id=run_id, repository_id="repo_1", goal="test"))

    # 1. Historical naive record inserted directly
    with sqlite3.connect(temp_settings.database_path) as db:
        db.execute(
            "INSERT INTO maintenance_events (run_id, event_type, payload, created_at) "
            "VALUES (?, 'step_naive', ?, '2026-11-09 02:53:00')",
            (run_id, json.dumps({"step": 1})),
        )

    # 2. New record via repository API
    repository.add_maintenance_event(run_id, "step_new", {"step": 2})

    # 3. Explicit offset record
    with sqlite3.connect(temp_settings.database_path) as db:
        db.execute(
            "INSERT INTO maintenance_events (run_id, event_type, payload, created_at) "
            "VALUES (?, 'step_offset', ?, '2026-11-09T10:53:00+08:00')",
            (run_id, json.dumps({"step": 3})),
        )

    all_events = repository.list_maintenance_events(run_id)
    # Event 0 is run.created from create_maintenance_run
    assert all_events[0]["event_type"] == "run.created"
    assert "+00:00" in all_events[0]["created_at"] or all_events[0]["created_at"].endswith("Z")

    events = [e for e in all_events if e["event_type"] != "run.created"]
    assert len(events) == 3

    # Historical naive record was normalized with +00:00 UTC
    assert events[0]["created_at"] == "2026-11-09T02:53:00+00:00"

    # New record contains timezone
    assert "+00:00" in events[1]["created_at"] or events[1]["created_at"].endswith("Z")

    # Offset record preserves instant
    assert events[2]["created_at"] == "2026-11-09T10:53:00+08:00"
    ts_naive_utc = datetime(2026, 11, 9, 2, 53, 0, tzinfo=timezone.utc).timestamp()
    assert datetime.fromisoformat(events[2]["created_at"]).timestamp() == ts_naive_utc

    # Verify through MaintenanceService detail output
    service = MaintenanceService(repository=repository, settings=temp_settings)
    detail = service.detail(run_id)
    detail_custom_events = [e for e in detail["events"] if e["event_type"] != "run.created"]
    assert detail_custom_events[0]["created_at"] == "2026-11-09T02:53:00+00:00"
    assert detail_custom_events[2]["created_at"] == "2026-11-09T10:53:00+08:00"
