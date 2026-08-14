from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.fluency.models import FluencySessionResult, PracticeMode
from services.guided_conversation.models import (
    CEFRLevel,
    GuidedLearnerResult,
    GuidedSessionView,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PracticeSessionCreateRequest(StrictModel):
    """Trusted backend request for either free or guided practice."""

    user_id: str = Field(min_length=1, max_length=128)
    participant_name: str | None = Field(default=None, max_length=128)
    mode: PracticeMode
    scenario_id: str | None = Field(default=None, min_length=1, max_length=160)
    scenario_version: int | None = Field(default=None, ge=1)
    placement_completed: bool = False
    placement_level: CEFRLevel | None = None
    interface_language: str = Field(default="en", pattern=r"^(en|ar)$")
    confidence_before: int | None = Field(default=None, ge=0, le=100)
    recording_consent: bool = False

    @model_validator(mode="after")
    def enforce_mode_contract(self) -> PracticeSessionCreateRequest:
        if self.mode == PracticeMode.GUIDED:
            if not self.scenario_id:
                raise ValueError("scenario_id is required for guided practice")
            if not self.placement_completed or self.placement_level is None:
                raise ValueError(
                    "guided practice requires a completed placement and placement_level"
                )
            return self
        if self.scenario_id is not None or self.scenario_version is not None:
            raise ValueError("scenario fields are allowed only for guided practice")
        if self.recording_consent:
            raise ValueError("recording_consent is currently supported only for guided practice")
        return self


class PracticeSessionCreateResponse(StrictModel):
    practice_session_id: str
    mode: PracticeMode
    room_name: str
    server_url: str
    participant_token: str
    participant_identity: str
    token_expires_at: datetime
    agent_name: str = "english-tutor"
    result_url: str
    events_topic: str | None = None
    commands_topic: str | None = None
    guided_session: GuidedSessionView | None = None


class PracticeSessionResult(StrictModel):
    practice_session_id: str
    mode: PracticeMode
    result: FluencySessionResult | GuidedLearnerResult
