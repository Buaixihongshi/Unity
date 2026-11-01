import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from uuid import uuid4

from backend.app.domain.history import MessageHistory
from backend.app.domain.maintenance import (
    EvolutionBatch,
    MaintenanceApproval,
    MaintenanceFeedback,
    MaintenanceRun,
    MemoryCandidate,
    RegisteredRepository,
    ValidationResult,
)
from backend.app.domain.models import (
    Conversation, Message, MessageRole, Mission, MissionEvent, is_provider_verification_mission,
)


class Repository:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self._local = threading.local()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS missions (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_creation_operations (
                    operation_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    task_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_boards (
                    conversation_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_check_logs (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    output BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_snapshot_references (
                    conversation_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, snapshot_id)
                );
                CREATE TABLE IF NOT EXISTS work_operations (
                    conversation_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    result TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, operation_id)
                );
                CREATE TABLE IF NOT EXISTS discussion_results (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_discussion_results_mission
                    ON discussion_results(mission_id);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS registered_repositories (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS maintenance_runs (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_maintenance_runs_repository
                    ON maintenance_runs(repository_id);
                CREATE TABLE IF NOT EXISTS validation_results (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_validation_results_run
                    ON validation_results(run_id);
                CREATE TABLE IF NOT EXISTS maintenance_approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS maintenance_feedback (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evolution_batches (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS maintenance_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_maintenance_events_run
                    ON maintenance_events(run_id, id);
                CREATE TABLE IF NOT EXISTS memory_entries (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    revision INTEGER NOT NULL DEFAULT 1,
                    digest TEXT NOT NULL,
                    body_chars INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_entries_agent
                    ON memory_entries(agent_id, status);
                CREATE TABLE IF NOT EXISTS context_checkpoints (
                    conversation_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source_message_ids TEXT NOT NULL DEFAULT '[]',
                    cursor_event_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS model_state (
                    agent_id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_handovers (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    previous TEXT NOT NULL DEFAULT '',
                    current TEXT NOT NULL DEFAULT '',
                    mission_id TEXT,
                    conversation_id TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_model_handovers_agent
                    ON model_handovers(agent_id, id);
                """
            )

    def create_mission(self, mission: Mission) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO missions (id, data) VALUES (?, ?)",
                (mission.id, mission.model_dump_json()),
            )
            self.add_event(mission.id, "mission_created", mission.model_dump(mode="json"), db=db)

    def update_mission(self, mission: Mission) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE missions SET data = ? WHERE id = ?",
                (mission.model_dump_json(), mission.id),
            )
            self.add_event(mission.id, "mission_updated", mission.model_dump(mode="json"), db=db)

    def get_mission(self, mission_id: str) -> Optional[Mission]:
        with self._transaction() as db:
            row = db.execute("SELECT data FROM missions WHERE id = ?", (mission_id,)).fetchone()
        return Mission.model_validate_json(row["data"]) if row else None

    def list_missions(self) -> list[Mission]:
        with self._transaction() as db:
            rows = db.execute("SELECT data FROM missions ORDER BY id DESC").fetchall()
        return [Mission.model_validate_json(row["data"]) for row in rows]

    def add_message(self, message: Message, db: Optional[sqlite3.Connection] = None) -> None:
        if db is None:
            with self._transaction() as connection:
                self.add_message(message, db=connection)
            return
        db.execute(
            "INSERT INTO messages (id, mission_id, data) VALUES (?, ?, ?)",
            (message.id, message.mission_id, message.model_dump_json()),
        )
        self.add_event(message.mission_id, "message", message.model_dump(mode="json"), db=db)

    def add_interruption(self, mission_id: str, content: str, target_agent_id: Optional[str] = None, *, request_id: Optional[str] = None, request: Optional[dict] = None) -> Message:
        """Commit the visible user message and its routing event together."""
        with self._transaction() as db:
            row = db.execute("SELECT data FROM missions WHERE id = ?", (mission_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown mission: {mission_id}")
            mission = Mission.model_validate_json(row["data"])
            message = Message(
                id=f"msg_{uuid4().hex}", mission_id=mission_id,
                conversation_id=mission.conversation_id, role=MessageRole.USER, content=content,
            )
            self.add_message(message, db=db)
            self.add_event(mission_id, "interrupt", {
                "mission_id": mission_id, "message_id": message.id,
                "content": content, "target_agent_id": target_agent_id,
                **({"request_id": request_id, "request": request} if request_id else {}),
            }, db=db)
        return message

    def read_message_history(
        self, *, conversation_id: Optional[str] = None, mission_id: Optional[str] = None,
    ) -> MessageHistory:
        """Read messages and events from one SQLite snapshot.

        Conversation reads exclude provider verification. An explicit mission read
        is isolated to that mission (including verification and legacy missions).
        """
        if mission_id:
            scope, parameters = "m.id = ?", (mission_id,)
        elif conversation_id:
            scope, parameters = "json_extract(m.data, '$.conversation_id') = ?", (conversation_id,)
        else:
            return MessageHistory([], [], [])
        with self._transaction() as db:
            # SELECT alone does not start a snapshot in sqlite3's legacy mode.
            if not db.in_transaction:
                db.execute("BEGIN")
            rows = db.execute(f"SELECT m.data FROM missions m WHERE {scope}", parameters).fetchall()
            missions = [Mission.model_validate_json(row["data"]) for row in rows]
            if not mission_id:
                missions = [m for m in missions if not is_provider_verification_mission(m)]
            allowed = {m.id for m in missions}
            rows = db.execute(
                f"SELECT x.mission_id, x.data FROM messages x JOIN missions m ON m.id = x.mission_id WHERE {scope}",
                parameters,
            ).fetchall()
            messages = [Message.model_validate_json(row["data"]) for row in rows if row["mission_id"] in allowed]
            # Full mission events are also used by HTTP/SSE; dialogue reads across
            # missions do not need tool traces or other control events. A revision
            # marks which user wording was superseded, so it belongs to the dialogue.
            event_filter = "" if mission_id else " AND e.event_type IN ('message', 'interrupt', 'message.revised')"
            rows = db.execute(
                f"SELECT e.* FROM events e JOIN missions m ON m.id = e.mission_id WHERE {scope}{event_filter} ORDER BY e.id",
                parameters,
            ).fetchall()
            events = [MissionEvent(
                id=str(row["id"]), mission_id=row["mission_id"], event_type=row["event_type"],
                payload=json.loads(row["payload"]), created_at=row["created_at"],
            ) for row in rows if row["mission_id"] in allowed]
        return MessageHistory(missions, messages, events)

    def list_messages(self, mission_id: str) -> list[Message]:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT data FROM messages WHERE mission_id = ?",
                (mission_id,),
            ).fetchall()
        messages = [Message.model_validate_json(row["data"]) for row in rows]
        return sorted(messages, key=lambda message: (message.created_at, message.id))

    def add_event(
        self,
        mission_id: str,
        event_type: str,
        payload: dict[str, Any],
        db: Optional[sqlite3.Connection] = None,
    ) -> str:
        owns_connection = db is None
        if owns_connection:
            with self._transaction() as connection:
                return self.add_event(mission_id, event_type, payload, db=connection)

        cursor = db.execute(
            "INSERT INTO events (mission_id, event_type, payload, created_at) VALUES (?, ?, ?, datetime('now'))",
            (mission_id, event_type, json.dumps(payload, ensure_ascii=False)),
        )
        return str(cursor.lastrowid)

    def list_artifact_events(self, limit: int, before_id: int | None = None) -> list[MissionEvent]:
        # Select latest identity before pagination, including across page boundaries.
        with self._transaction() as db:
            rows = db.execute("""
                SELECT id, mission_id, event_type, payload, created_at FROM events
                WHERE event_type='artifact' AND (? IS NULL OR id < ?)
                  AND id IN (SELECT MAX(id) FROM events WHERE event_type='artifact'
                             GROUP BY mission_id, json_extract(payload, '$.path'))
                ORDER BY id DESC LIMIT ?
            """, (before_id, before_id, limit)).fetchall()
        return [MissionEvent(id=str(row['id']), mission_id=row['mission_id'],
                             event_type=row['event_type'], payload=json.loads(row['payload']),
                             created_at=row['created_at']) for row in rows]

    def list_events_after(self, mission_id: str, after_id: int) -> list[MissionEvent]:
        with self._transaction() as db:
            rows = db.execute(
                """
                SELECT id, mission_id, event_type, payload, created_at
                FROM events
                WHERE mission_id = ? AND id > ?
                ORDER BY id ASC
                """,
                (mission_id, after_id),
            ).fetchall()
        events: list[MissionEvent] = []
        for row in rows:
            events.append(
                MissionEvent(
                    id=str(row["id"]),
                    mission_id=row["mission_id"],
                    event_type=row["event_type"],
                    payload=json.loads(row["payload"]),
                    created_at=row["created_at"],
                )
            )
        return events

    def mission_id_for_turn(self, turn_id: str) -> Optional[str]:
        """Which mission owns this turn. Used by the global observability drill-down."""
        if not turn_id:
            return None
        with self._transaction() as db:
            row = db.execute(
                "SELECT mission_id FROM events WHERE json_extract(payload, '$.turn_id') = ? "
                "ORDER BY id ASC LIMIT 1",
                (turn_id,),
            ).fetchone()
        return row["mission_id"] if row else None

    def list_events_for_turn(self, mission_id: str, turn_id: str) -> list[MissionEvent]:
        with self._transaction() as db:
            rows = db.execute(
                """
                SELECT id, mission_id, event_type, payload, created_at
                FROM events
                WHERE mission_id = ?
                ORDER BY id ASC
                """,
                (mission_id,),
            ).fetchall()
        events: list[MissionEvent] = []
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("turn_id") != turn_id:
                continue
            events.append(
                MissionEvent(
                    id=str(row["id"]),
                    mission_id=row["mission_id"],
                    event_type=row["event_type"],
                    payload=payload,
                    created_at=row["created_at"],
                )
            )
        return events

    def create_conversation(self, conversation: Conversation) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO conversations (id, data) VALUES (?, ?)",
                (conversation.id, conversation.model_dump_json()),
            )

    def update_conversation(self, conversation: Conversation) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE conversations SET data = ? WHERE id = ?",
                (conversation.model_dump_json(), conversation.id),
            )

    def delete_conversation(self, conversation_id: str) -> None:
        """Remove a conversation and every database row scoped to it.

        Callers check that nothing is still running; files on disk (artifacts,
        task worktrees, snapshots) are left untouched.
        """
        with self._transaction(immediate=True) as db:
            mission_ids = [(row["id"],) for row in db.execute(
                "SELECT id FROM missions WHERE json_extract(data, '$.conversation_id') = ?",
                (conversation_id,),
            ).fetchall()]
            board = db.execute(
                "SELECT data FROM work_boards WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()
            task_ids = [(task["id"],) for task in json.loads(board["data"]).get("tasks", [])] if board else []
            tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            for table in ("messages", "events", "discussion_results", "execution_operations", "execution_grants"):
                if table in tables:
                    db.executemany(f"DELETE FROM {table} WHERE mission_id = ?", mission_ids)
            db.executemany("DELETE FROM missions WHERE id = ?", mission_ids)
            db.executemany("DELETE FROM task_creation_operations WHERE task_id = ?", task_ids)
            for table in ("work_boards", "work_check_logs", "work_snapshot_references",
                          "work_operations", "context_checkpoints"):
                db.execute(f"DELETE FROM {table} WHERE conversation_id = ?", (conversation_id,))
            db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def get_conversation(self, conversation_id: str) -> Optional[Conversation]:
        with self._transaction() as db:
            row = db.execute(
                "SELECT data FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return Conversation.model_validate_json(row["data"]) if row else None

    def list_conversations(self, *, include_task_contexts: bool = False) -> list[Conversation]:
        with self._transaction() as db:
            rows = db.execute("SELECT data FROM conversations").fetchall()
        conversations = [Conversation.model_validate_json(row["data"]) for row in rows]
        if not include_task_contexts:
            conversations = [c for c in conversations if c.kind == "chat"]
        conversations.sort(key=lambda c: c.updated_at, reverse=True)
        return conversations

    def list_missions_for_conversation(self, conversation_id: str) -> list[Mission]:
        with self._transaction() as db:
            rows = db.execute("SELECT data FROM missions").fetchall()
        missions = [Mission.model_validate_json(row["data"]) for row in rows]
        return sorted(
            [m for m in missions if m.conversation_id == conversation_id],
            key=lambda m: m.created_at,
        )

    def list_messages_for_conversation(self, conversation_id: str) -> list[Message]:
        with self._transaction() as db:
            rows = db.execute("SELECT data FROM messages").fetchall()
        messages = [Message.model_validate_json(row["data"]) for row in rows]
        return sorted(
            [m for m in messages if m.conversation_id == conversation_id],
            key=lambda m: (m.created_at, m.id),
        )

    def save_registered_repository(self, repository: RegisteredRepository) -> None:
        with self._transaction() as db:
            db.execute(
                """
                INSERT INTO registered_repositories (id, data) VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET data = excluded.data
                """,
                (repository.id, repository.model_dump_json()),
            )

    def get_registered_repository(self, repository_id: str) -> Optional[RegisteredRepository]:
        with self._transaction() as db:
            row = db.execute(
                "SELECT data FROM registered_repositories WHERE id = ?", (repository_id,)
            ).fetchone()
        return RegisteredRepository.model_validate_json(row["data"]) if row else None

    def list_registered_repositories(self) -> list[RegisteredRepository]:
        with self._transaction() as db:
            rows = db.execute("SELECT data FROM registered_repositories ORDER BY id").fetchall()
        return [RegisteredRepository.model_validate_json(row["data"]) for row in rows]

    def create_maintenance_run(self, run: MaintenanceRun) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO maintenance_runs (id, repository_id, data) VALUES (?, ?, ?)",
                (run.id, run.repository_id, run.model_dump_json()),
            )
            self.add_maintenance_event(run.id, "run.created", run.model_dump(mode="json"), db=db)

    def update_maintenance_run(self, run: MaintenanceRun) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE maintenance_runs SET repository_id = ?, data = ? WHERE id = ?",
                (run.repository_id, run.model_dump_json(), run.id),
            )

    def get_maintenance_run(self, run_id: str) -> Optional[MaintenanceRun]:
        with self._transaction() as db:
            row = db.execute("SELECT data FROM maintenance_runs WHERE id = ?", (run_id,)).fetchone()
        return MaintenanceRun.model_validate_json(row["data"]) if row else None

    def list_maintenance_runs(self, repository_id: Optional[str] = None) -> list[MaintenanceRun]:
        with self._transaction() as db:
            if repository_id is None:
                rows = db.execute("SELECT data FROM maintenance_runs").fetchall()
            else:
                rows = db.execute(
                    "SELECT data FROM maintenance_runs WHERE repository_id = ?",
                    (repository_id,),
                ).fetchall()
        runs = [MaintenanceRun.model_validate_json(row["data"]) for row in rows]
        return sorted(runs, key=lambda item: item.created_at, reverse=True)

    def add_validation_result(self, result: ValidationResult) -> None:
        self._insert_json_row("validation_results", result.id, result.run_id, result.model_dump_json())

    def list_validation_results(self, run_id: str) -> list[ValidationResult]:
        return self._list_json_rows("validation_results", "run_id", run_id, ValidationResult)

    def add_maintenance_approval(self, approval: MaintenanceApproval) -> None:
        self._insert_json_row("maintenance_approvals", approval.id, approval.run_id, approval.model_dump_json())

    def list_maintenance_approvals(self, run_id: str) -> list[MaintenanceApproval]:
        return self._list_json_rows("maintenance_approvals", "run_id", run_id, MaintenanceApproval)

    def add_maintenance_feedback(self, feedback: MaintenanceFeedback) -> None:
        self._insert_json_row("maintenance_feedback", feedback.id, feedback.run_id, feedback.model_dump_json())

    def list_maintenance_feedback(self, run_id: str) -> list[MaintenanceFeedback]:
        return self._list_json_rows("maintenance_feedback", "run_id", run_id, MaintenanceFeedback)

    def add_memory_candidate(self, candidate: MemoryCandidate) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO memory_candidates (id, run_id, repository_id, data) VALUES (?, ?, ?, ?)",
                (candidate.id, candidate.run_id, candidate.repository_id, candidate.model_dump_json()),
            )

    def list_memory_candidates(self, repository_id: Optional[str] = None) -> list[MemoryCandidate]:
        with self._transaction() as db:
            if repository_id is None:
                rows = db.execute("SELECT data FROM memory_candidates").fetchall()
            else:
                rows = db.execute(
                    "SELECT data FROM memory_candidates WHERE repository_id = ?",
                    (repository_id,),
                ).fetchall()
        return [MemoryCandidate.model_validate_json(row["data"]) for row in rows]

    def add_evolution_batch(self, batch: EvolutionBatch) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO evolution_batches (id, repository_id, data) VALUES (?, ?, ?)",
                (batch.id, batch.repository_id, batch.model_dump_json()),
            )

    def list_evolution_batches(self) -> list[EvolutionBatch]:
        with self._transaction() as db:
            rows = db.execute("SELECT data FROM evolution_batches ORDER BY id DESC").fetchall()
        return [EvolutionBatch.model_validate_json(row["data"]) for row in rows]

    def add_maintenance_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        db: Optional[sqlite3.Connection] = None,
    ) -> str:
        if db is None:
            with self._transaction() as connection:
                return self.add_maintenance_event(run_id, event_type, payload, db=connection)
        cursor = db.execute(
            "INSERT INTO maintenance_events (run_id, event_type, payload, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (run_id, event_type, json.dumps(payload, ensure_ascii=False)),
        )
        return str(cursor.lastrowid)

    def list_maintenance_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT id, event_type, payload, created_at FROM maintenance_events "
                "WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "event_type": row["event_type"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _insert_json_row(self, table: str, row_id: str, owner_id: str, data: str) -> None:
        allowed = {"validation_results", "maintenance_approvals", "maintenance_feedback"}
        if table not in allowed:
            raise ValueError("Unsupported JSON table")
        with self._transaction() as db:
            db.execute(
                "INSERT INTO {table} (id, run_id, data) VALUES (?, ?, ?)".format(table=table),
                (row_id, owner_id, data),
            )

    def _list_json_rows(self, table: str, column: str, value: str, model):
        allowed = {"validation_results", "maintenance_approvals", "maintenance_feedback"}
        if table not in allowed or column != "run_id":
            raise ValueError("Unsupported JSON table")
        with self._transaction() as db:
            rows = db.execute(
                "SELECT data FROM {table} WHERE {column} = ? ORDER BY id".format(
                    table=table, column=column
                ),
                (value,),
            ).fetchall()
        return [model.model_validate_json(row["data"]) for row in rows]

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        with closing(self._connect()) as connection:
            with connection:
                if immediate:
                    connection.execute("BEGIN IMMEDIATE")
                self._local.connection = connection
                try:
                    yield connection
                finally:
                    self._local.connection = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection
