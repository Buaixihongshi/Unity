from backend.app.api.artifacts import build_router as build_artifacts_router
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from backend.app import extensions
from backend.app.domain.errors import DiscussionError

from backend.app.api.agents import build_router as build_agents_router
from backend.app.api.conversations import build_router as build_conversations_router
from backend.app.api.hub_settings import build_router as build_hub_settings_router
from backend.app.api.health import build_router as build_health_router
from backend.app.api.missions import build_router as build_missions_router
from backend.app.api.observability import build_router as build_observability_router
from backend.app.api.onboarding import build_router as build_onboarding_router
from backend.app.api.providers import build_router as build_providers_router
from backend.app.adapters.base import CLIAdapter
from backend.app.settings import Settings, get_settings
from backend.app.storage.repository import Repository


def create_app(
    settings: Optional[Settings] = None,
    *,
    onboarding_verification_adapter: Optional[CLIAdapter] = None,
    mission_adapter: Optional[CLIAdapter] = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    extensions.install()
    from backend.app.domain.agent_registry import configure_custom_agents
    from backend.app.storage import hub_settings
    configure_custom_agents(app_settings.config_dir, hub_settings.default_path(app_settings))
    repository = Repository(app_settings.database_path)
    repository.initialize()
    from backend.app.storage.execution import ExecutionLedger
    ExecutionLedger(repository).recover()
    extensions.recover(repository, app_settings)
    from backend.app.orchestration import interruption
    interruption.recover(repository)
    app_settings.artifact_root.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(_app):
        import threading
        stopped = threading.Event()
        loops = [threading.Thread(target=loop, args=(repository, app_settings, stopped, mission_adapter), daemon=True)
                 for loop in extensions.background_loops()]
        for loop in loops:
            loop.start()
        try:
            yield
        finally:
            stopped.set()
            for loop in loops:
                loop.join(timeout=.2)
            if app_settings.antigravity_persistent:
                from backend.app.adapters.antigravity_session import SESSION_POOL
                SESSION_POOL.close(app_settings.database_path)

    app = FastAPI(title="Unity", lifespan=lifespan)
    from backend.app.runtime_control import install as install_runtime_control
    install_runtime_control(app, repository, app_settings)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request, exc):
        # Validation details can include nested request bodies containing keys.
        if request.url.path == "/api/hub/settings":
            return JSONResponse(status_code=422, content={"detail": "invalid settings request"})
        from fastapi.exception_handlers import request_validation_exception_handler
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(DiscussionError)
    async def discussion_error_handler(request, exc):
        return JSONResponse(status_code=exc.status_code, content={'detail': str(exc)})

    app.include_router(build_health_router(app_settings, repository))
    app.include_router(extensions.maintenance_router(app_settings, repository))
    app.include_router(build_agents_router(app_settings))
    app.include_router(build_artifacts_router(repository))
    app.include_router(build_conversations_router(app_settings, repository, adapter_override=mission_adapter))
    app.include_router(build_hub_settings_router(app_settings))
    app.include_router(build_missions_router(app_settings, repository, adapter_override=mission_adapter))
    for router in extensions.mission_routers(app_settings, repository, mission_adapter):
        app.include_router(router)
    from backend.app.api.memory import build_router as build_memory_router
    app.include_router(build_memory_router(app_settings, repository))
    app.include_router(
        build_onboarding_router(
            app_settings,
            repository,
            verification_adapter=onboarding_verification_adapter,
        )
    )
    app.include_router(build_providers_router(app_settings))
    app.include_router(build_observability_router(repository))
    if app_settings.frontend_dist is not None:
        # Mounted last so every /api route above keeps precedence.
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=app_settings.frontend_dist, html=True), name="frontend")
    # A persisted human choice may have committed just before a crash and
    # before its worker started. Reservations make rescheduling idempotent.
    extensions.resume(repository, app_settings, mission_adapter)
    return app


class LazyASGIApp:
    def __init__(self):
        self._app = None

    async def __call__(self, scope, receive, send):
        if self._app is None:
            self._app = create_app()
        await self._app(scope, receive, send)


app = LazyASGIApp()
