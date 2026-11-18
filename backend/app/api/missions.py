import asyncio
import json
import logging
import threading
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.app.adapters.base import CLIAdapter
from backend.app.adapters.fake import FakeCLIAdapter
from backend.app.adapters.provider_router import ProviderRouterAdapter
from backend.app.api.mission_detail import (
    TERMINAL_STATUSES,
    _artifact_records,
    _mission_payload,
    build_mission_detail,
    read_artifact_content,
    artifact_file_path,
)
from backend.app.domain.models import MissionStatus, utc_now
from backend.app.orchestration import interruption
from backend.app.orchestration.runner import MissionRunner
from backend.app.settings import Settings, snapshot_settings
from backend.app.storage.repository import Repository

from backend.app.api.mission_requests import CreateMissionRequest, create_requested_mission
from backend.app import extension_points

logger = logging.getLogger(__name__)


class InterruptRequest(BaseModel):
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    content: str = Field(min_length=1)
    target_agent_id: Optional[str] = None


def _resolve_target_ids(
    target_agent_ids: Optional[List[str]],
    target_agent_id: Optional[str],
) -> List[str]:
    """Normalize the two equivalent request fields. Plural wins if both given,
    so a multi-mention payload like `@A @B` is never silently downgraded."""
    if target_agent_ids:
        return list(target_agent_ids)
    if target_agent_id:
        return [target_agent_id]
    return []


_snapshot_settings = snapshot_settings  # backward-compatible alias for existing tests


def _build_mission_adapter(settings: Settings) -> CLIAdapter:
    if settings.use_fake_cli:
        return FakeCLIAdapter()
    return ProviderRouterAdapter(
        settings.config_dir,
        cli_timeout=settings.cli_timeout,
        a2a_idle_timeout=settings.a2a_idle_timeout,
        antigravity_persistent=settings.antigravity_persistent,
        database_path=settings.database_path,
        artifact_root=settings.artifact_root,
        memory_root=settings.memory_root,
        provider_overrides=settings.provider_overrides,
        agent_overrides=settings.agent_overrides,
        api_settings=settings,
    )


def _run_mission_in_background(
    repo: Repository,
    settings: Settings,
    mission_id: str,
    adapter: Optional[CLIAdapter] = None,
) -> None:
    try:
        MissionRunner(repo, settings, adapter or _build_mission_adapter(settings)).run(mission_id)
    except Exception as exc:
        logger.exception("Mission runner failed for %s", mission_id)
        mission = repo.get_mission(mission_id)
        if mission is None:
            return
        if extension_points.any('mission_failed', repo, mission):
            return
        failed = mission.model_copy(
            update={
                "status": MissionStatus.FAILED,
                "updated_at": utc_now(),
            }
        )
        repo.update_mission(failed)
        repo.add_event(
            mission_id,
            "status",
            {
                "mission_id": mission_id,
                "status": MissionStatus.FAILED.value,
                "summary": "Mission runner failed: {error}".format(error=exc),
            },
        )
    finally:
        extension_points.each('mission_finished', repo, mission_id)


def build_router(
    settings: Settings,
    repository: Repository,
    adapter_override: Optional[CLIAdapter] = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/missions", tags=["missions"])

    def get_repo() -> Repository:
        return repository

    @router.post("", status_code=201)
    def create_mission(payload: CreateMissionRequest, repo: Repository = Depends(get_repo)):
        mission_settings = snapshot_settings(settings)
        mission, conversation, created = create_requested_mission(repo, mission_settings, payload, payload.conversation_id)
        detail = build_mission_detail(repo, settings, mission.id)
        if created:
            threading.Thread(target=_run_mission_in_background,
                             args=(repo, mission_settings, mission.id, adapter_override), daemon=True).start()
        return detail

    @router.get("")
    def list_missions(repo: Repository = Depends(get_repo)):
        return [_mission_payload(mission) for mission in repo.list_missions()]

    @router.get("/{mission_id}")
    def get_mission(mission_id: str, repo: Repository = Depends(get_repo)):
        mission = repo.get_mission(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail="Mission not found")
        return build_mission_detail(repo, settings, mission.id)

    @router.post("/{mission_id}/interruptions", status_code=202)
    def add_interruption(
        mission_id: str,
        payload: InterruptRequest,
        repo: Repository = Depends(get_repo),
    ):
        mission = repo.get_mission(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail="Mission not found")
        if not extension_points.any('mission_interrupt', repo, mission, payload):
            # A finished run never reads its queue again; the message would sit unanswered.
            if mission.status not in {MissionStatus.PENDING, MissionStatus.RUNNING}:
                raise HTTPException(status_code=409, detail="本轮已结束，请直接发送新消息")
            repo.add_interruption(mission_id, payload.content, payload.target_agent_id)
        return build_mission_detail(repo, settings, mission_id)

    @router.post("/{mission_id}/pause", status_code=202)
    def pause_mission(mission_id: str, repo: Repository = Depends(get_repo)):
        mission = repo.get_mission(mission_id)
        if mission is None:
            raise HTTPException(status_code=404, detail="Mission not found")
        if extension_points.any('mission_owned', repo, mission):
            raise HTTPException(status_code=409, detail="讨论和任务请使用各自的控制")
        if mission.status in {MissionStatus.PENDING, MissionStatus.RUNNING}:
            interruption.request_pause(repo, mission)
        return build_mission_detail(repo, settings, mission_id)

    @router.get("/{mission_id}/artifacts")
    def list_artifacts(mission_id: str, repo: Repository = Depends(get_repo)):
        if repo.get_mission(mission_id) is None:
            raise HTTPException(status_code=404, detail="Mission not found")
        db_events = repo.list_events_after(mission_id, 0)
        return _artifact_records(settings, mission_id, db_events)

    @router.get("/{mission_id}/artifacts/content")
    def get_artifact_content(
        mission_id: str,
        path: str,
        repo: Repository = Depends(get_repo),
    ):
        if repo.get_mission(mission_id) is None:
            raise HTTPException(status_code=404, detail="Mission not found")
        return read_artifact_content(settings, mission_id, path)

    @router.get("/{mission_id}/artifacts/file")
    def download_artifact(mission_id: str, path: str, repo: Repository = Depends(get_repo)):
        if repo.get_mission(mission_id) is None:
            raise HTTPException(status_code=404, detail="Mission not found")
        target = artifact_file_path(settings, mission_id, path)
        return FileResponse(target, filename=target.name, media_type="application/octet-stream",
                            headers={"X-Content-Type-Options": "nosniff"})

    @router.get("/{mission_id}/stream")
    def stream_mission(mission_id: str, repo: Repository = Depends(get_repo)):
        if repo.get_mission(mission_id) is None:
            raise HTTPException(status_code=404, detail="Mission not found")

        async def events():
            while True:
                detail = build_mission_detail(repo, settings, mission_id)
                snapshot = {"mission_id": mission_id, **detail}
                yield "event: snapshot\n"
                yield "data: {data}\n\n".format(
                    data=json.dumps(snapshot, ensure_ascii=False)
                )
                if detail["mission"]["status"] in TERMINAL_STATUSES:
                    break
                await asyncio.sleep(0.05)

        return StreamingResponse(events(), media_type="text/event-stream")

    return router
