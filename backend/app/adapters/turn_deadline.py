"""Renew a turn only for returned tools, on host-controlled five-minute checks."""
import threading
import time
from collections import deque
from typing import Callable, Optional

from backend.app.adapters import antigravity_config
from backend.app.adapters.stream_events import StreamEvent, StreamEventType
from backend.app.adapters.turn_budget import remaining_timeout


PROGRESS_CHECK_SECONDS = 300
HUB_SERVER_NAMES = {"yiren_hub", antigravity_config.SERVER_NAME}
HUB_WORK_TOOLS = {"read_context", "read_artifact", "write_artifact", "read_discussion_result"}


def is_tool_return(event: StreamEvent) -> bool:
    """An empty/failed result is still real work; generated tool arguments are not."""
    if event.type is not StreamEventType.TOOL_ATTEMPT:
        return False
    if event.tool_name and event.tool_name.startswith(("mcp__yiren_hub__", "mcp:yiren_hub:")):
        return False  # Hub work results are correlated through persisted tool events.
    if event.tool_name in {"call_mcp_tool", "mcp_tool_call"}:
        server = (event.tool_args or {}).get('ServerName')
        if not isinstance(server, str) or server in HUB_SERVER_NAMES:
            return False
    return event.tool_phase in {"result", "completed", "failed"} and event.tool_ok is not None


def is_hub_work_result(record, turn_id: str) -> bool:
    return (record.event_type in {"tool.result", "tool.error"}
            and record.payload.get("turn_id") == turn_id
            and record.payload.get("tool_name") in HUB_WORK_TOOLS)


class ToolResultDeadline:
    def __init__(self, timeout: float, *, check_interval: float = PROGRESS_CHECK_SECONDS,
                 clock: Optional[Callable[[], float]] = None):
        if timeout < 0 or check_interval <= 0:
            raise ValueError("Invalid turn deadline settings")
        self.clock = clock or time.time
        self.timeout, self.check_interval = timeout, check_interval
        self.started_at = self.clock()
        self.deadline = self.started_at + timeout
        self.next_check = self.started_at + check_interval
        self.extensions = 0
        self._lock = threading.Lock()
        self._seen = set()
        self._returns = deque()

    def record_result(self, identity=None):
        with self._lock:
            now = self.clock()
            if now > self.deadline or (identity is not None and identity in self._seen):
                return
            if identity is not None:
                self._seen.add(identity)
            self._returns.append(now)

    def check(self):
        """Inspect only new returns; never resurrect an expired deadline or replay old work."""
        with self._lock:
            now = self.clock()
            extension = None
            # Evaluate scheduled windows, even if a poll arrives slightly late.
            # Returns after the old deadline are rejected by record_result().
            while self.next_check <= now and self.next_check <= self.deadline:
                checked_at = self.next_check
                self.next_check += self.check_interval
                new_results = 0
                while self._returns and self._returns[0] <= checked_at:
                    self._returns.popleft()
                    new_results += 1
                if not new_results:
                    continue
                previous = self.deadline
                self.deadline = min(max(previous, checked_at + self.timeout),
                                    now + remaining_timeout(float("inf")))
                if self.deadline == previous:
                    continue
                self.extensions += 1
                extension = {"extension": self.extensions, "new_tool_results": new_results,
                             "timeout_seconds": self.timeout, "check_interval_seconds": self.check_interval,
                             "previous_deadline_at": previous, "deadline_at": self.deadline}
            return extension

    def remaining(self):
        return remaining_timeout(max(0, self.deadline - self.clock()))

    def diagnostic(self):
        return {"deadline_extensions": self.extensions,
                "progress_check_interval_seconds": self.check_interval,
                "effective_timeout_seconds": round(self.deadline - self.started_at, 3)}
