"""Per-turn budget, isolated across concurrent discussion workers."""
import time
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps


@dataclass
class TurnBudget:
    deadline: float
    attempts_left: int = 2  # primary and one fallback, shared across protocols

    def claim(self):
        if self.attempts_left <= 0 or self.remaining() <= 0:
            return False
        self.attempts_left -= 1
        return True

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    def extend_to(self, deadline):
        """Renew for verified progress: never shrink, never revive an exhausted budget."""
        if self.remaining() <= 0 or deadline <= self.deadline:
            return False
        self.deadline = deadline
        return True


_current = ContextVar('turn_budget', default=None)


def remaining_timeout(configured):
    budget = _current.get()
    return min(configured, budget.remaining()) if budget else configured


def extend_remaining(seconds):
    """Keep the current turn budget alive for at least ``seconds`` from now.

    Progress renewal (tool results seen on a host check) must move the shared
    budget too; otherwise the budget silently caps every renewal at the
    original ``cli_timeout``. Retries stay bounded because a fallback is never
    attempted once tool activity was observed (see ``fallback_policy.may_retry``).
    """
    budget = _current.get()
    if budget is None:
        return False
    return budget.extend_to(time.monotonic() + max(0.0, seconds))


def budgeted_turn(timeout):
    """Create a fresh budget per invocation, resetting it even on exceptions."""
    def decorate(method):
        @wraps(method)
        def wrapped(*args, **kwargs):
            seconds = timeout(*args, **kwargs) if callable(timeout) else timeout
            token = _current.set(TurnBudget(time.monotonic() + seconds))
            try:
                return method(*args, **kwargs)
            finally:
                _current.reset(token)
        return wrapped
    return decorate


def claim_attempt():
    budget = _current.get()
    return budget is None or budget.claim()
