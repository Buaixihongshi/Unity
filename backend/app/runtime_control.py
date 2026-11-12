"""Managed, token-protected draining; serialize it against incoming mutations."""
from __future__ import annotations

import asyncio
import hmac
import os
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.app.runtime_activity import activity_counts


def activity(repository) -> dict:
    # Counts only, never message bodies or credentials. The cold-migration
    # checker uses these same lifecycle rules.
    with repository._transaction() as db:
        return activity_counts(db)


# Environment tooling marks its own polls; a visible page's health refresh still counts as use.
PROBE_HEADER = "X-AgentClub-Probe"


def is_bookkeeping(request: Request) -> bool:
    """Lifecycle control and tooling probes are not user activity."""
    return request.url.path.startswith("/api/runtime/") or PROBE_HEADER in request.headers


def idle_seconds(app) -> int:
    return max(0, int(time.time() - app.state.last_request_at))


def install(app, repository, settings):
    app.state.draining = False
    app.state.drain_pending = False
    # Wall-clock time of the last request that was not lifecycle bookkeeping. Managed
    # development supervisors poll /api/health and stop an environment nobody uses.
    app.state.last_request_at = time.time()
    # Test clients and embedded callers may use different event loops. Admission
    # bookkeeping is thread-safe and never holds a lock across an await.
    lock = threading.Lock()
    in_flight = 0
    token = os.environ.get("YIREN_LIFECYCLE_TOKEN", "")
    router = APIRouter(prefix="/api/runtime", tags=["runtime"])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        counted = not is_bookkeeping(request)
        if counted: app.state.last_request_at = time.time()
        try:
            return await admit(request, call_next)
        finally:
            # A slow request counts as use until its response is ready.
            if counted: app.state.last_request_at = time.time()

    async def admit(request: Request, call_next):
        nonlocal in_flight
        if request.method in {"GET", "HEAD", "OPTIONS"} or request.url.path in {"/api/runtime/drain", "/api/runtime/resume"}:
            return await call_next(request)
        with lock:
            if app.state.draining or app.state.drain_pending:
                return JSONResponse(status_code=503, content={"detail": "服务正在切换版本，请稍后重试"}, headers={"Retry-After": "5"})
            path = request.url.path
            if settings.demo_only and (path.endswith("/verify") or
                    path == "/api/onboarding/first-chat" or
                    (path.startswith("/api/onboarding/") and path.endswith("/activate")) or
                    (path.endswith("/probe") and "/fake/" not in path)):
                return JSONResponse(status_code=409, content={"detail": "演示环境不调用真实 Provider；请显式启动 --real-cli 环境"})
            in_flight += 1
        try:
            return await call_next(request)
        finally:
            with lock: in_flight -= 1

    def authenticate(request):
        if not token or not hmac.compare_digest(request.headers.get("X-AgentClub-Control", ""), token):
            raise HTTPException(403, "Managed runtime control requires its private token")

    @router.post("/drain")
    async def drain(request: Request):
        authenticate(request)
        with lock:
            if app.state.drain_pending: raise HTTPException(409, "Another drain is in progress")
            app.state.drain_pending = True
        try:
            deadline = time.monotonic() + 10
            while True:
                with lock: pending = in_flight
                if not pending: break
                if time.monotonic() > deadline: raise HTTPException(409, "A write request is still in flight")
                await asyncio.sleep(0.01)
            counts = activity(repository)
            if any(counts.values()):
                raise HTTPException(409, {"message": "服务仍有未完成工作", **counts})
            with lock: app.state.draining = True
            return {"draining": True, **counts}
        finally:
            with lock: app.state.drain_pending = False

    @router.post("/resume")
    async def resume(request: Request):
        authenticate(request)
        with lock:
            if app.state.drain_pending: raise HTTPException(409, "Drain is in progress")
            app.state.draining = False
        return {"draining": False}

    app.include_router(router)
