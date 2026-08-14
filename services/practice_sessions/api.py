from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status

from services.fluency import PracticeMode, aggregate_session
from services.fluency.models import FluencyMode
from services.guided_conversation.models import GuidedSessionCreateRequest

from .models import (
    PracticeSessionCreateRequest,
    PracticeSessionCreateResponse,
    PracticeSessionResult,
)
from .tokens import LiveKitConfigurationError

router = APIRouter(prefix="/v1/practice-sessions", tags=["practice-sessions"])


@router.post(
    "",
    response_model=PracticeSessionCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_practice_session(
    payload: PracticeSessionCreateRequest,
    request: Request,
) -> PracticeSessionCreateResponse:
    issuer = request.app.state.livekit_token_issuer
    try:
        issuer.validate_configuration()
    except LiveKitConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    guided_view = None
    if payload.mode == PracticeMode.GUIDED:
        guided_view = request.app.state.guided_service.create_session(
            GuidedSessionCreateRequest(
                user_id=payload.user_id,
                scenario_id=payload.scenario_id,
                scenario_version=payload.scenario_version,
                placement_completed=payload.placement_completed,
                placement_level=payload.placement_level,
                interface_language=payload.interface_language,
                confidence_before=payload.confidence_before,
                recording_consent=payload.recording_consent,
            ),
            request.state.correlation_id,
        )
        practice_session_id = guided_view.session_id
    else:
        practice_session_id = f"free-{uuid.uuid4()}"

    room_name = practice_session_id
    participant_identity = f"{payload.user_id}-{uuid.uuid4().hex[:10]}"
    dispatch_metadata: dict[str, object] = {
        "conversation_mode": payload.mode.value,
        "practice_session_id": practice_session_id,
        "user_id": payload.user_id,
    }
    if guided_view is not None:
        dispatch_metadata["guided_session_id"] = guided_view.session_id

    try:
        issued = issuer.issue(
            room_name=room_name,
            participant_identity=participant_identity,
            participant_name=payload.participant_name or payload.user_id,
            dispatch_metadata=dispatch_metadata,
        )
    except LiveKitConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    request.app.state.repository.audit(
        "practice.session_created",
        {
            "mode": payload.mode.value,
            "room_name": room_name,
            "agent_name": "english-tutor",
        },
        practice_session_id,
        request.state.correlation_id,
    )
    return PracticeSessionCreateResponse(
        practice_session_id=practice_session_id,
        mode=payload.mode,
        room_name=room_name,
        server_url=issuer.server_url,
        participant_token=issued.token,
        participant_identity=participant_identity,
        token_expires_at=issued.expires_at,
        result_url=(
            f"/v1/practice-sessions/{practice_session_id}/result?mode={payload.mode.value}"
        ),
        events_topic="guided.events" if guided_view is not None else None,
        commands_topic="guided.command" if guided_view is not None else None,
        guided_session=guided_view,
    )


@router.get(
    "/{practice_session_id}/result",
    response_model=PracticeSessionResult,
)
def get_practice_result(
    practice_session_id: str,
    mode: PracticeMode,
    request: Request,
) -> PracticeSessionResult:
    if mode == PracticeMode.GUIDED:
        result = request.app.state.guided_service.learner_result(practice_session_id)
    else:
        observations = request.app.state.repository.list_fluency_observations(
            practice_session_id
        )
        result = aggregate_session(practice_session_id, FluencyMode.FREE, observations)
    return PracticeSessionResult(
        practice_session_id=practice_session_id,
        mode=mode,
        result=result,
    )
