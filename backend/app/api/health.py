from fastapi import APIRouter, HTTPException, Request, Response

from backend.app.config.providers import load_cli_providers
from backend.app.settings import Settings
from backend.app.storage.repository import Repository
from backend.app import extension_points
from backend.app.build_info import BUILD_ID
from backend.app.runtime_identity import capture_runtime, runtime_view


def build_router(settings: Settings, repository: Repository) -> APIRouter:
    router = APIRouter(prefix="/api/health", tags=["health"])
    started = capture_runtime(settings)

    @router.get("")
    def get_health(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            # Exercise the database rather than only reporting that the file exists.
            repository.list_conversations()
            providers = load_cli_providers(settings.config_dir)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Unity is not ready: {error}".format(error=exc),
            ) from exc

        if not any(provider.id == "fake" for provider in providers):
            raise HTTPException(
                status_code=503,
                detail="Unity is not ready: fake provider is missing",
            )

        from backend.app.runtime_control import activity, idle_seconds
        runtime = runtime_view(started)
        runtime["activity"] = activity(repository)
        runtime["idle_seconds"] = idle_seconds(request.app)
        runtime["draining"] = request.app.state.draining or request.app.state.drain_pending
        body = {
            "status": "ok",
            "version": "1.0.0",
            "database": "ready",
            "config": "ready",
            "fake_provider": "ready",
        }
        for fields in extension_points.collect('health_fields'):
            body.update(fields)
        body["build_id"] = BUILD_ID
        body["runtime"] = runtime
        return body

    return router
