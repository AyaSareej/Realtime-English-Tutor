from __future__ import annotations

import logging
import os
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .catalog import ScenarioLocked, ScenarioNotFound
from .models import (
    CEFRLevel,
    ConfidenceUpdateRequest,
    GuidedAttemptRequest,
    GuidedAttemptResult,
    GuidedAudioUploadResponse,
    GuidedControlResult,
    GuidedConversationReport,
    GuidedDomainSummary,
    GuidedLearnerResult,
    GuidedPronunciationResultEvent,
    GuidedScenario,
    GuidedSessionCreateRequest,
    GuidedSessionView,
    ScenarioSummary,
)
from .service import (
    GuidedAttemptConflict,
    GuidedConversationService,
    GuidedSessionNotFound,
    InvalidGuidedState,
)

logger = logging.getLogger("guided-conversation")
router = APIRouter(prefix="/v1/guided-conversations", tags=["guided-conversations"])
admin_router = APIRouter(
    prefix="/v1/admin/guided-conversations",
    tags=["guided-conversation-debug"],
)


def _service(request: Request) -> GuidedConversationService:
    return request.app.state.guided_service


def _publish_pronunciation(request: Request, session_id: str, attempt_id: str) -> None:
    service = _service(request)
    event = service.pronunciation_request(session_id, attempt_id)
    if event is None:
        return
    try:
        request.app.state.guided_pronunciation_publisher.publish(event)
    except Exception:
        logger.exception("Guided pronunciation job could not be queued")
        try:
            service.mark_pronunciation_failed(session_id, attempt_id)
        except Exception:
            logger.exception("Could not persist the pronunciation queue failure")


@router.get("/domains", response_model=list[GuidedDomainSummary])
def list_domains(
    request: Request,
    placement_completed: Annotated[bool, Query()],
    placement_level: Annotated[CEFRLevel | None, Query()] = None,
) -> list[GuidedDomainSummary]:
    return _service(request).domain_catalog_view(placement_completed, placement_level)


@router.get("/scenarios", response_model=list[ScenarioSummary])
def list_scenarios(
    request: Request,
    placement_completed: Annotated[bool, Query()],
    placement_level: Annotated[CEFRLevel | None, Query()] = None,
) -> list[ScenarioSummary]:
    return _service(request).catalog_view(placement_completed, placement_level)


@router.get("/scenarios/{scenario_id}", response_model=GuidedScenario)
def scenario_preview(
    scenario_id: str,
    request: Request,
    placement_completed: Annotated[bool, Query()],
    version: Annotated[int | None, Query(ge=1)] = None,
    placement_level: Annotated[CEFRLevel | None, Query()] = None,
) -> GuidedScenario:
    return _service(request).scenario_preview(
        scenario_id,
        version,
        placement_completed,
        placement_level,
    )


@router.post("/sessions", response_model=GuidedSessionView, status_code=status.HTTP_201_CREATED)
def create_session(payload: GuidedSessionCreateRequest, request: Request) -> GuidedSessionView:
    if payload.recording_consent and request.app.state.audio_storage is None:
        raise HTTPException(
            status_code=503,
            detail="Recording consent requires configured encrypted audio storage",
        )
    return _service(request).create_session(payload, request.state.correlation_id)


@router.get("/sessions/{session_id}", response_model=GuidedSessionView)
def get_session(session_id: str, request: Request) -> GuidedSessionView:
    return _service(request).get_session(session_id)


@router.post("/sessions/{session_id}/prompt-ready", response_model=GuidedSessionView)
def mark_prompt_ready(session_id: str, request: Request) -> GuidedSessionView:
    return _service(request).mark_prompt_ready(session_id)


@router.post("/sessions/{session_id}/attempts", response_model=GuidedAttemptResult)
def submit_attempt(
    session_id: str,
    payload: GuidedAttemptRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> GuidedAttemptResult:
    result = _service(request).submit_attempt(
        session_id,
        payload,
        request.state.correlation_id,
    )
    if result.pronunciation_status.value == "queued" and not result.idempotent_replay:
        background_tasks.add_task(_publish_pronunciation, request, session_id, result.attempt_id)
    return result


@router.post("/sessions/{session_id}/continue", response_model=GuidedControlResult)
def continue_after_retry(session_id: str, request: Request) -> GuidedControlResult:
    return _service(request).continue_after_retry(session_id)


@router.post("/sessions/{session_id}/retry", response_model=GuidedControlResult)
def retry_current_turn(session_id: str, request: Request) -> GuidedControlResult:
    return _service(request).retry_current_turn(session_id)


@router.post("/sessions/{session_id}/pause", response_model=GuidedControlResult)
def pause_session(session_id: str, request: Request) -> GuidedControlResult:
    return _service(request).pause(session_id)


@router.post("/sessions/{session_id}/resume", response_model=GuidedControlResult)
def resume_session(session_id: str, request: Request) -> GuidedControlResult:
    return _service(request).resume(session_id)


@router.post("/sessions/{session_id}/stop", response_model=GuidedControlResult)
def stop_session(session_id: str, request: Request) -> GuidedControlResult:
    return _service(request).stop(session_id)


@router.post("/sessions/{session_id}/confidence", response_model=GuidedSessionView)
def update_confidence(
    session_id: str,
    payload: ConfidenceUpdateRequest,
    request: Request,
) -> GuidedSessionView:
    return _service(request).update_confidence(session_id, payload)


@router.get("/sessions/{session_id}/report", response_model=GuidedLearnerResult)
def get_report(session_id: str, request: Request) -> GuidedLearnerResult:
    """Return only the small result intended for the learner interface."""
    return _service(request).learner_result(session_id)


@admin_router.get(
    "/sessions/{session_id}/debug-report",
    response_model=GuidedConversationReport,
)
def get_debug_report(session_id: str, request: Request) -> GuidedConversationReport:
    """Return evidence diagnostics under the admin credential only."""
    return _service(request).report(session_id)


@router.get("/sessions/{session_id}/replay-audio")
async def replay_audio(session_id: str, request: Request) -> Response:
    report = _service(request).report(session_id)
    if report.status.value != "completed":
        raise HTTPException(
            status_code=409,
            detail="Full-conversation replay is available after the scenario is completed",
        )
    synthesizer = request.app.state.piper_synthesizer
    if synthesizer is None:
        raise HTTPException(
            status_code=503,
            detail="Local Piper TTS is unavailable; run scripts\\setup_piper.ps1",
        )
    normal_scale = float(os.getenv("PIPER_LENGTH_SCALE", "1.0"))
    learner_scale = float(os.getenv("PIPER_REPLAY_LEARNER_LENGTH_SCALE", "1.06"))
    pause_seconds = float(os.getenv("PIPER_REPLAY_PAUSE_SECONDS", "0.32"))
    lines = [
        (line.text, normal_scale if line.role == "assistant" else learner_scale)
        for line in report.replay_script
    ]
    try:
        payload = await run_in_threadpool(
            synthesizer.synthesize_dialogue_wav,
            lines,
            pause_seconds=pause_seconds,
        )
    except Exception as exc:
        logger.exception("Piper could not render the guided replay")
        raise HTTPException(status_code=503, detail="Local replay audio could not be generated") from exc
    return Response(
        content=payload,
        media_type="audio/wav",
        headers={"Content-Disposition": f'inline; filename="{session_id}-replay.wav"'},
    )


@router.post(
    "/sessions/{session_id}/audio/{attempt_id}",
    response_model=GuidedAudioUploadResponse,
)
async def upload_original_audio(
    session_id: str,
    attempt_id: str,
    request: Request,
    audio: UploadFile = File(...),  # noqa: B008
) -> GuidedAudioUploadResponse:
    record = _service(request)._record(session_id)
    if not record.recording_consent:
        raise HTTPException(status_code=403, detail="Recording consent was not granted")
    if request.app.state.audio_storage is None:
        raise HTTPException(status_code=503, detail="Encrypted audio storage is not configured")
    payload = await audio.read(request.app.state.settings.max_body_bytes + 1)
    if len(payload) > request.app.state.settings.max_body_bytes:
        raise HTTPException(status_code=413, detail="Audio payload is too large")
    uri = request.app.state.audio_storage.put(
        session_id,
        attempt_id,
        payload,
        audio.content_type or "application/octet-stream",
    )
    request.app.state.repository.save_guided_audio(session_id, attempt_id, uri)
    request.app.state.repository.audit(
        "guided.audio_stored",
        {"attempt_id": attempt_id, "encrypted": True, "size_bytes": len(payload)},
        session_id,
        request.state.correlation_id,
    )
    return GuidedAudioUploadResponse(
        session_id=session_id,
        attempt_id=attempt_id,
        audio_uri=uri,
    )


@router.post("/pronunciation/callback", status_code=status.HTTP_204_NO_CONTENT)
def pronunciation_callback(
    payload: GuidedPronunciationResultEvent,
    request: Request,
) -> Response:
    _service(request).apply_pronunciation_result(payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def install_exception_handlers(app) -> None:
    @app.exception_handler(GuidedSessionNotFound)
    async def session_not_found(request: Request, exc: GuidedSessionNotFound):
        return JSONResponse(
            status_code=404,
            content={"detail": "Guided conversation session not found"},
        )

    @app.exception_handler(ScenarioNotFound)
    async def scenario_not_found(request: Request, exc: ScenarioNotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ScenarioLocked)
    async def scenario_locked(request: Request, exc: ScenarioLocked):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(InvalidGuidedState)
    async def invalid_state(request: Request, exc: InvalidGuidedState):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(GuidedAttemptConflict)
    async def attempt_conflict(request: Request, exc: GuidedAttemptConflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})
