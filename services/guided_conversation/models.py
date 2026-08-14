from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.fluency.models import (
    FluencyObservationResult,
    FluencySessionResult,
    FluencyWord,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CEFRLevel(str, Enum):
    A1 = "A1"
    A2 = "A2"
    B1 = "B1"
    B2 = "B2"


LEVEL_RANK = {
    CEFRLevel.A1: 1,
    CEFRLevel.A2: 2,
    CEFRLevel.B1: 3,
    CEFRLevel.B2: 4,
}


class GuidedSessionStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    STOPPED = "stopped"


class GuidedSessionState(str, Enum):
    ASSISTANT_SPEAKING = "assistant_speaking"
    USER_PROMPT_VISIBLE = "user_prompt_visible"
    AWAITING_RETRY_DECISION = "awaiting_retry_decision"
    PAUSED = "paused"
    COMPLETED = "completed"
    STOPPED = "stopped"


class PronunciationStatus(str, Enum):
    NOT_REQUESTED = "not_requested"
    NOT_CONFIGURED = "not_configured"
    QUEUED = "queued"
    COMPLETED = "completed"
    FAILED = "failed"


class ScenarioEvaluation(StrictModel):
    pronunciation: bool = True
    fluency: bool = True
    target_words: list[str] = Field(default_factory=list, max_length=30)


class GuidedScenarioTurn(StrictModel):
    id: str = Field(pattern=r"^turn_[0-9]{2}$")
    assistant_display_text: str = Field(min_length=2, max_length=500)
    assistant_spoken_text: str = Field(min_length=2, max_length=500)
    assistant_audio_asset: str | None = Field(default=None, max_length=500)
    user_display_text: str = Field(min_length=2, max_length=500)
    user_expected_text: str = Field(min_length=2, max_length=500)
    arabic_hint: str | None = Field(default=None, max_length=500)
    expected_pause_after_word_indexes: list[int] = Field(default_factory=list, max_length=20)
    evaluation: ScenarioEvaluation = Field(default_factory=ScenarioEvaluation)

    @field_validator("expected_pause_after_word_indexes")
    @classmethod
    def unique_pause_indexes(cls, value: list[int]) -> list[int]:
        if any(index < 1 for index in value):
            raise ValueError("expected pause indexes are one-based and must be positive")
        return sorted(set(value))


class GuidedScenario(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_.-]+$")
    version: int = Field(ge=1)
    status: Literal["published", "draft", "retired"] = "published"
    domain_id: str = Field(pattern=r"^[a-z0-9_.-]+$")
    theme: str = Field(min_length=2, max_length=80)
    title: str = Field(min_length=2, max_length=160)
    required_level: CEFRLevel
    estimated_minutes: int = Field(ge=1, le=20)
    learner_role: str = Field(min_length=2, max_length=120)
    system_role: str = Field(min_length=2, max_length=120)
    objective: str = Field(min_length=5, max_length=500)
    situation: str = Field(min_length=5, max_length=700)
    useful_vocabulary: list[str] = Field(default_factory=list, max_length=30)
    target_functions: list[str] = Field(default_factory=list, max_length=20)
    target_phonemes: list[str] = Field(default_factory=list, max_length=20)
    closing_display_text: str = Field(min_length=2, max_length=500)
    closing_spoken_text: str = Field(min_length=2, max_length=500)
    turns: list[GuidedScenarioTurn] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_turns_and_valid_pauses(self) -> GuidedScenario:
        turn_ids = [turn.id for turn in self.turns]
        if len(turn_ids) != len(set(turn_ids)):
            raise ValueError("scenario turn IDs must be unique")
        for turn in self.turns:
            expected_words = turn.user_expected_text.split()
            if any(
                index >= len(expected_words) for index in turn.expected_pause_after_word_indexes
            ):
                raise ValueError(f"{turn.id} has a pause boundary outside its expected sentence")
        return self


class GuidedDomainDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_.-]+$")
    title: str = Field(min_length=2, max_length=120)
    description: str = Field(min_length=5, max_length=500)
    order: int = Field(default=0, ge=0)


class ScenarioCatalog(StrictModel):
    content_version: str = Field(min_length=1, max_length=80)
    domains: list[GuidedDomainDefinition] = Field(min_length=1)
    scenarios: list[GuidedScenario] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_scenario_versions(self) -> ScenarioCatalog:
        keys = [(scenario.id, scenario.version) for scenario in self.scenarios]
        if len(keys) != len(set(keys)):
            raise ValueError("scenario ID/version pairs must be unique")
        domain_ids = [domain.id for domain in self.domains]
        if len(domain_ids) != len(set(domain_ids)):
            raise ValueError("guided domain IDs must be unique")
        unknown_domains = {
            scenario.domain_id
            for scenario in self.scenarios
            if scenario.domain_id not in set(domain_ids)
        }
        if unknown_domains:
            raise ValueError(
                "scenarios reference unknown domains: " + ", ".join(sorted(unknown_domains))
            )
        return self


class ScenarioSummary(StrictModel):
    scenario_id: str
    scenario_version: int
    domain_id: str
    domain_title: str
    theme: str
    title: str
    required_level: CEFRLevel
    estimated_minutes: int
    learner_role: str
    system_role: str
    objective: str
    turn_count: int
    is_locked: bool
    lock_reason: str | None = None


class GuidedDomainSummary(StrictModel):
    domain_id: str
    title: str
    description: str
    scenario_count: int = Field(ge=0)
    available_scenario_count: int = Field(ge=0)
    scenarios: list[ScenarioSummary]


class GuidedSessionCreateRequest(StrictModel):
    user_id: str = Field(min_length=1, max_length=128)
    scenario_id: str = Field(min_length=1, max_length=160)
    scenario_version: int | None = Field(default=None, ge=1)
    placement_completed: bool
    placement_level: CEFRLevel | None = None
    interface_language: Literal["en", "ar"] = "en"
    confidence_before: int | None = Field(default=None, ge=0, le=100)
    recording_consent: bool = False

    @model_validator(mode="after")
    def placement_level_required_when_complete(self) -> GuidedSessionCreateRequest:
        if self.placement_completed and self.placement_level is None:
            raise ValueError("placement_level is required when placement_completed is true")
        return self


class GuidedDeliveryMetrics(StrictModel):
    prompt_to_speech_seconds: float | None = Field(default=None, ge=0)
    completion_ratio: float = Field(ge=0, le=1)
    expected_word_count: int = Field(ge=0)
    recognized_word_count: int = Field(ge=0)
    mid_phrase_pause_count: int = Field(ge=0)
    boundary_pause_count: int = Field(ge=0)
    retry_number: int = Field(ge=0)


class PronunciationPattern(StrictModel):
    expected: str = Field(min_length=1, max_length=40)
    observed: str | None = Field(default=None, max_length=40)
    error_type: Literal["substitution", "deletion", "insertion", "other"]
    word: str = Field(min_length=1, max_length=100)
    confidence: float = Field(ge=0, le=1)


class GuidedAttemptRecord(StrictModel):
    attempt_id: str
    idempotency_key: str
    turn_id: str
    selected: bool
    created_at: datetime
    response_started_at_ms: int | None = None
    response_ended_at_ms: int | None = None
    transcript_sha256: str
    asr_confidence: float | None = Field(default=None, ge=0, le=1)
    audio_uri: str | None = None
    audio_was_raw: bool = False
    delivery: GuidedDeliveryMetrics
    fluency: FluencyObservationResult
    pronunciation_status: PronunciationStatus
    pronunciation_patterns: list[PronunciationPattern] = Field(default_factory=list)
    pronunciation_service_version: str | None = None


class GuidedVersionSet(StrictModel):
    scenario_content: str
    scenario_engine: Literal["guided-engine-v0.1", "guided-engine-v0.2"] = "guided-engine-v0.2"
    fluency: Literal["fluency-v0.1"] = "fluency-v0.1"
    delivery: Literal["guided-delivery-v0.1", "guided-delivery-v0.2"] = "guided-delivery-v0.2"
    pronunciation: str = "external-or-not-configured"


class GuidedSessionRecord(StrictModel):
    session_id: str
    user_id: str
    scenario_id: str
    scenario_version: int
    placement_level_at_start: CEFRLevel
    interface_language: Literal["en", "ar"]
    status: GuidedSessionStatus
    state: GuidedSessionState
    paused_from_state: GuidedSessionState | None = None
    current_turn_index: int = Field(ge=0)
    attempts: list[GuidedAttemptRecord] = Field(default_factory=list)
    confidence_before: int | None = Field(default=None, ge=0, le=100)
    confidence_after: int | None = Field(default=None, ge=0, le=100)
    recording_consent: bool = False
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    stopped_at: datetime | None = None
    versions: GuidedVersionSet
    revision: int = Field(default=0, ge=0)


class CurrentGuidedTurn(StrictModel):
    turn_id: str
    turn_number: int = Field(ge=1)
    total_turns: int = Field(ge=1)
    assistant_display_text: str
    assistant_spoken_text: str
    assistant_audio_asset: str | None = None
    learner_display_text: str
    arabic_hint: str | None = None


class GuidedSessionView(StrictModel):
    session_id: str
    status: GuidedSessionStatus
    state: GuidedSessionState
    scenario_id: str
    scenario_version: int
    domain_id: str
    domain_title: str
    scenario_title: str
    scenario_level: CEFRLevel
    current_turn: CurrentGuidedTurn | None
    completed_turns: int = Field(ge=0)
    total_turns: int = Field(ge=1)
    retries: int = Field(ge=0)
    recording_consent: bool
    allowed_actions: list[
        Literal[
            "speak",
            "retry",
            "continue",
            "replay",
            "replay_slow",
            "pause",
            "resume",
            "stop",
        ]
    ]


class GuidedAttemptRequest(StrictModel):
    attempt_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)
    turn_id: str = Field(min_length=1, max_length=80)
    transcript: str = Field(default="", max_length=20_000)
    words: list[FluencyWord] = Field(default_factory=list, max_length=5_000)
    prompt_available_at_ms: int | None = Field(default=None, ge=0)
    response_started_at_ms: int | None = Field(default=None, ge=0)
    response_ended_at_ms: int | None = Field(default=None, ge=0)
    asr_confidence: float | None = Field(default=None, ge=0, le=1)
    completed: bool = True
    explicit_audio_issue: bool = False
    audio_issue_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def valid_timestamps(self) -> GuidedAttemptRequest:
        if (
            self.response_started_at_ms is not None
            and self.response_ended_at_ms is not None
            and self.response_ended_at_ms < self.response_started_at_ms
        ):
            raise ValueError("response_ended_at_ms must be >= response_started_at_ms")
        return self


class GuidedAttemptResult(StrictModel):
    attempt_id: str
    idempotent_replay: bool = False
    session: GuidedSessionView
    delivery: GuidedDeliveryMetrics
    fluency: FluencyObservationResult
    retry_recommended: bool
    retry_reason: str | None = None
    spoken_reply: str
    live_event: dict[str, object]
    pronunciation_status: PronunciationStatus


class GuidedControlResult(StrictModel):
    session: GuidedSessionView
    spoken_reply: str
    live_event: dict[str, object]


class ConfidenceUpdateRequest(StrictModel):
    confidence_after: int = Field(ge=0, le=100)


class DeliverySummary(StrictModel):
    label: Literal["stable", "developing", "needs_more_evidence"]
    mean_prompt_to_speech_seconds: float | None = Field(default=None, ge=0)
    mean_completion_ratio: float = Field(ge=0, le=1)
    mid_phrase_pauses: int = Field(ge=0)
    total_retries: int = Field(ge=0)
    completed_without_retry: int = Field(ge=0)
    interpretation: str = (
        "Observable delivery stability for this script; it is not psychological confidence."
    )


class PronunciationSummary(StrictModel):
    status: Literal["not_configured", "pending", "completed", "partial", "failed"]
    patterns: list[PronunciationPattern] = Field(default_factory=list)
    interpretation: str = (
        "Pronunciation diagnostics come from the configured phoneme/MDD service, not ASR equality."
    )


class GuidedReplayLine(StrictModel):
    sequence: int = Field(ge=1)
    role: Literal["assistant", "learner"]
    turn_id: str | None = None
    text: str = Field(min_length=1, max_length=500)


class GuidedLineDiagnostic(StrictModel):
    line_number: int = Field(ge=1)
    turn_id: str
    expected_text: str
    attempted: bool
    eligible: bool
    fluency_status: str
    expected_word_count: int = Field(ge=0)
    recognized_word_count: int = Field(ge=0)
    timed_word_count: int = Field(ge=0)
    response_duration_seconds: float = Field(ge=0)
    speech_duration_seconds: float = Field(ge=0)
    timing_source: str
    completion_ratio: float = Field(ge=0, le=1)
    asr_confidence_percent: int | None = Field(default=None, ge=0, le=100)
    selected_attempt_number: int | None = Field(default=None, ge=1)
    insufficiency_reasons: list[str] = Field(default_factory=list)


class GuidedFluencyThresholds(StrictModel):
    minimum_timed_words_per_line: int = Field(ge=1)
    minimum_timed_seconds_per_line: float = Field(gt=0)
    minimum_eligible_lines: int = Field(ge=1)
    target_eligible_lines: int = Field(ge=1)
    minimum_speech_seconds_when_below_target: float = Field(gt=0)


class GuidedResultDebug(StrictModel):
    summary: str
    thresholds: GuidedFluencyThresholds
    lines: list[GuidedLineDiagnostic]
    excluded_line_count: int = Field(ge=0)
    guidance: list[str] = Field(default_factory=list)


class GuidedLearnerSkill(StrictModel):
    key: Literal["pace", "smoothness", "connected_speech"]
    label: str
    score: int = Field(ge=0, le=100)
    rating: Literal["strong", "good", "keep_practising"]
    message: str


class GuidedLearnerCompletion(StrictModel):
    completed_lines: int = Field(ge=0)
    total_lines: int = Field(ge=1)
    percent: int = Field(ge=0, le=100)


class GuidedLearnerResult(StrictModel):
    """Small, actionable result intended for the real learner interface."""

    session_id: str
    domain_title: str
    scenario_title: str
    scenario_level: CEFRLevel
    result_status: Literal["ready", "needs_more_speech", "incomplete"]
    headline: str
    speaking_flow_score: int | None = Field(default=None, ge=0, le=100)
    completion: GuidedLearnerCompletion
    skills: list[GuidedLearnerSkill] = Field(max_length=3)
    strength: str | None = None
    next_step: str
    pronunciation_tips: list[str] = Field(default_factory=list, max_length=3)
    can_practise_again: bool = True
    replay_audio_url: str | None = None
    practice_note: str = (
        "This feedback measures how smoothly you delivered this script. "
        "It does not change your English level."
    )


class GuidedConversationReport(StrictModel):
    session_id: str
    scenario_id: str
    scenario_version: int
    domain_id: str
    domain_title: str
    scenario_title: str
    scenario_level: CEFRLevel
    status: GuidedSessionStatus
    completed_turns: int = Field(ge=0)
    total_turns: int = Field(ge=1)
    guided_speaking_fluency: FluencySessionResult
    delivery_stability: DeliverySummary
    pronunciation: PronunciationSummary
    confidence_before: int | None = Field(default=None, ge=0, le=100)
    confidence_after: int | None = Field(default=None, ge=0, le=100)
    confidence_change: int | None = None
    result_debug: GuidedResultDebug
    replay_script: list[GuidedReplayLine]
    versions: GuidedVersionSet
    limitations: list[str]


class GuidedAudioUploadResponse(StrictModel):
    session_id: str
    attempt_id: str
    audio_uri: str


class GuidedPronunciationRequestedEvent(StrictModel):
    event_type: Literal["guided.pronunciation_requested"] = "guided.pronunciation_requested"
    event_id: str
    occurred_at: datetime
    session_id: str
    attempt_id: str
    scenario_id: str
    scenario_version: int
    turn_id: str
    expected_text: str
    target_words: list[str]
    target_phonemes: list[str]
    audio_uri: str
    target_locale: Literal["en-US"] = "en-US"
    callback_url: str | None = None


class GuidedPronunciationResultEvent(StrictModel):
    event_type: Literal["guided.pronunciation_completed", "guided.pronunciation_failed"]
    event_id: str
    occurred_at: datetime
    session_id: str
    attempt_id: str
    status: Literal["completed", "failed"]
    patterns: list[PronunciationPattern] = Field(default_factory=list)
    service_version: str | None = None
