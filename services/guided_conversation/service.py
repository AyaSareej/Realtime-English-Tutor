from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Sequence

from services.fluency import aggregate_session, score_observation
from services.fluency.config import FluencySettings
from services.fluency.models import FluencyMode, FluencyObservationRequest, FluencyWord

from .catalog import ScenarioCatalogRepository
from .models import (
    ConfidenceUpdateRequest,
    CurrentGuidedTurn,
    DeliverySummary,
    GuidedAttemptRecord,
    GuidedAttemptRequest,
    GuidedAttemptResult,
    GuidedControlResult,
    GuidedConversationReport,
    GuidedDeliveryMetrics,
    GuidedDomainSummary,
    GuidedFluencyThresholds,
    GuidedLearnerCompletion,
    GuidedLearnerResult,
    GuidedLearnerSkill,
    GuidedLineDiagnostic,
    GuidedPronunciationRequestedEvent,
    GuidedPronunciationResultEvent,
    GuidedReplayLine,
    GuidedResultDebug,
    GuidedScenario,
    GuidedScenarioTurn,
    GuidedSessionCreateRequest,
    GuidedSessionRecord,
    GuidedSessionState,
    GuidedSessionStatus,
    GuidedSessionView,
    GuidedVersionSet,
    PronunciationStatus,
    PronunciationSummary,
    ScenarioSummary,
    utc_now,
)
from .repository import GuidedConversationRepository

TOKEN_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)?")


class GuidedConversationError(RuntimeError):
    pass


class GuidedSessionNotFound(GuidedConversationError):
    pass


class InvalidGuidedState(GuidedConversationError):
    pass


class GuidedAttemptConflict(GuidedConversationError):
    pass


def _tokens(text: str) -> list[str]:
    return [token.lower().replace("’", "'") for token in TOKEN_RE.findall(text)]


def _lcs_length(left: Sequence[str], right: Sequence[str]) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    for left_token in left:
        current = [0]
        for index, right_token in enumerate(right, start=1):
            if left_token == right_token:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(current[-1], previous[index]))
        previous = current
    return previous[-1]


def _completion_ratio(expected_text: str, transcript: str, recognized_words: list[str]) -> float:
    expected = _tokens(expected_text)
    observed = _tokens(transcript) or [
        token for word in recognized_words for token in _tokens(word)
    ]
    if not expected:
        return 1.0
    return round(min(1.0, _lcs_length(expected, observed) / len(expected)), 4)


class GuidedConversationService:
    def __init__(
        self,
        repository: GuidedConversationRepository,
        catalog: ScenarioCatalogRepository,
        *,
        pronunciation_configured: bool = False,
        public_service_url: str = "",
        fluency_settings: FluencySettings | None = None,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.pronunciation_configured = pronunciation_configured
        self.public_service_url = public_service_url.rstrip("/")
        self.fluency_settings = fluency_settings or FluencySettings.from_env()

    def catalog_view(
        self,
        placement_completed: bool,
        placement_level,
    ) -> list[ScenarioSummary]:
        return self.catalog.list_summaries(placement_completed, placement_level)

    def domain_catalog_view(
        self,
        placement_completed: bool,
        placement_level,
    ) -> list[GuidedDomainSummary]:
        return self.catalog.list_domains(placement_completed, placement_level)

    def scenario_preview(
        self,
        scenario_id: str,
        version: int | None,
        placement_completed: bool,
        placement_level,
    ) -> GuidedScenario:
        scenario = self.catalog.get(scenario_id, version)
        self.catalog.authorize(scenario, placement_completed, placement_level)
        return scenario

    def create_session(
        self,
        payload: GuidedSessionCreateRequest,
        correlation_id: str = "",
    ) -> GuidedSessionView:
        scenario = self.catalog.get(payload.scenario_id, payload.scenario_version)
        self.catalog.authorize(
            scenario,
            payload.placement_completed,
            payload.placement_level,
        )
        assert payload.placement_level is not None
        now = utc_now()
        record = GuidedSessionRecord(
            session_id=f"guided-{uuid.uuid4()}",
            user_id=payload.user_id,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            placement_level_at_start=payload.placement_level,
            interface_language=payload.interface_language,
            status=GuidedSessionStatus.IN_PROGRESS,
            state=GuidedSessionState.ASSISTANT_SPEAKING,
            current_turn_index=0,
            confidence_before=payload.confidence_before,
            recording_consent=payload.recording_consent,
            created_at=now,
            updated_at=now,
            versions=GuidedVersionSet(
                scenario_content=self.catalog.content_version,
                pronunciation=(
                    "external-configured"
                    if self.pronunciation_configured
                    else "external-not-configured"
                ),
            ),
        )
        self.repository.create_guided_session(record, correlation_id)
        return self._view(record, scenario)

    def _record(self, session_id: str) -> GuidedSessionRecord:
        record = self.repository.get_guided_session(session_id)
        if record is None:
            raise GuidedSessionNotFound(session_id)
        return record

    def get_session(self, session_id: str) -> GuidedSessionView:
        record = self._record(session_id)
        return self._view(record, self.catalog.get(record.scenario_id, record.scenario_version))

    @staticmethod
    def _selected_attempts(record: GuidedSessionRecord) -> list[GuidedAttemptRecord]:
        return [attempt for attempt in record.attempts if attempt.selected]

    @staticmethod
    def _completed_turns(record: GuidedSessionRecord) -> int:
        return len({attempt.turn_id for attempt in record.attempts if attempt.selected})

    def _view(
        self,
        record: GuidedSessionRecord,
        scenario: GuidedScenario,
    ) -> GuidedSessionView:
        current = None
        if record.status == GuidedSessionStatus.IN_PROGRESS:
            turn = scenario.turns[record.current_turn_index]
            current = CurrentGuidedTurn(
                turn_id=turn.id,
                turn_number=record.current_turn_index + 1,
                total_turns=len(scenario.turns),
                assistant_display_text=turn.assistant_display_text,
                assistant_spoken_text=turn.assistant_spoken_text,
                assistant_audio_asset=turn.assistant_audio_asset,
                learner_display_text=turn.user_display_text,
                arabic_hint=turn.arabic_hint,
            )
        actions: list[str]
        if record.state == GuidedSessionState.ASSISTANT_SPEAKING:
            actions = ["pause", "stop"]
        elif record.state == GuidedSessionState.USER_PROMPT_VISIBLE:
            actions = ["speak", "replay", "replay_slow", "pause", "stop"]
        elif record.state == GuidedSessionState.AWAITING_RETRY_DECISION:
            actions = ["retry", "continue", "replay", "replay_slow", "pause", "stop"]
        elif record.state == GuidedSessionState.PAUSED:
            actions = ["resume", "stop"]
        else:
            actions = []
        retries_by_turn: dict[str, int] = {}
        for attempt in record.attempts:
            retries_by_turn[attempt.turn_id] = max(
                retries_by_turn.get(attempt.turn_id, 0),
                attempt.delivery.retry_number,
            )
        retries = sum(retries_by_turn.values())
        domain = self.catalog.get_domain(scenario.domain_id)
        return GuidedSessionView(
            session_id=record.session_id,
            status=record.status,
            state=record.state,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            domain_id=domain.id,
            domain_title=domain.title,
            scenario_title=scenario.title,
            scenario_level=scenario.required_level,
            current_turn=current,
            completed_turns=self._completed_turns(record),
            total_turns=len(scenario.turns),
            retries=retries,
            recording_consent=record.recording_consent,
            allowed_actions=actions,  # type: ignore[arg-type]
        )

    def mark_prompt_ready(self, session_id: str) -> GuidedSessionView:
        record = self._record(session_id)
        scenario = self.catalog.get(record.scenario_id, record.scenario_version)
        if record.state == GuidedSessionState.USER_PROMPT_VISIBLE:
            return self._view(record, scenario)
        if record.state != GuidedSessionState.ASSISTANT_SPEAKING:
            raise InvalidGuidedState("The learner prompt cannot become active in this state")
        record.state = GuidedSessionState.USER_PROMPT_VISIBLE
        record.updated_at = utc_now()
        self.repository.save_guided_record(record, "guided.prompt_ready")
        return self._view(record, scenario)

    def _delivery(
        self,
        request: GuidedAttemptRequest,
        turn: GuidedScenarioTurn,
        retry_number: int,
    ) -> GuidedDeliveryMetrics:
        # Re-validate nested words defensively. Callers may construct a valid request and
        # later replace ``words`` while building a retry in tests or SDK code; Pydantic
        # does not validate assignment unless a model opts into it.
        recognized = sorted(
            (FluencyWord.model_validate(word) for word in request.words),
            key=lambda word: (word.start, word.end),
        )
        pause_indexes: list[int] = []
        for index in range(1, len(recognized)):
            gap = max(0.0, recognized[index].start - recognized[index - 1].end)
            if gap > self.fluency_settings.pause_threshold_seconds:
                pause_indexes.append(index)
        expected_boundaries = set(turn.expected_pause_after_word_indexes)
        boundary_pauses = sum(index in expected_boundaries for index in pause_indexes)
        initiation = None
        if (
            request.prompt_available_at_ms is not None
            and request.response_started_at_ms is not None
        ):
            initiation = round(
                max(0, request.response_started_at_ms - request.prompt_available_at_ms) / 1000.0,
                3,
            )
        return GuidedDeliveryMetrics(
            prompt_to_speech_seconds=initiation,
            completion_ratio=_completion_ratio(
                turn.user_expected_text,
                request.transcript,
                [word.word for word in recognized],
            ),
            expected_word_count=len(_tokens(turn.user_expected_text)),
            recognized_word_count=len(recognized) or len(_tokens(request.transcript)),
            mid_phrase_pause_count=sum(index not in expected_boundaries for index in pause_indexes),
            boundary_pause_count=boundary_pauses,
            retry_number=retry_number,
        )

    def submit_attempt(
        self,
        session_id: str,
        payload: GuidedAttemptRequest,
        correlation_id: str = "",
    ) -> GuidedAttemptResult:
        replay = self.repository.get_guided_attempt_replay(session_id, payload.idempotency_key)
        if replay is not None:
            return replay
        record = self._record(session_id)
        scenario = self.catalog.get(record.scenario_id, record.scenario_version)
        if record.status != GuidedSessionStatus.IN_PROGRESS:
            raise InvalidGuidedState("This guided conversation is not in progress")
        if record.state not in {
            GuidedSessionState.USER_PROMPT_VISIBLE,
            GuidedSessionState.AWAITING_RETRY_DECISION,
        }:
            raise InvalidGuidedState("The learner line is not ready for speech")
        turn = scenario.turns[record.current_turn_index]
        if payload.turn_id != turn.id:
            raise GuidedAttemptConflict(
                f"Expected {turn.id}, but received an attempt for {payload.turn_id}"
            )
        if any(attempt.attempt_id == payload.attempt_id for attempt in record.attempts):
            raise GuidedAttemptConflict("attempt_id has already been used")

        previous = [attempt for attempt in record.attempts if attempt.turn_id == turn.id]
        retry_number = len(previous)
        delivery = self._delivery(payload, turn, retry_number)
        fluency_request = FluencyObservationRequest(
            session_id=record.session_id,
            turn_id=f"{turn.id}:selected",
            mode=FluencyMode.GUIDED,
            transcript=payload.transcript,
            words=payload.words,
            response_started_at_ms=payload.response_started_at_ms,
            response_ended_at_ms=payload.response_ended_at_ms,
            completed=payload.completed,
            assistance_count=0,
            task_type="guided_oral_reading",
            explicit_audio_issue=payload.explicit_audio_issue,
            audio_issue_reason=payload.audio_issue_reason,
        )
        fluency = score_observation(fluency_request, self.fluency_settings)
        audio_uri = (
            self.repository.get_guided_audio(session_id, payload.attempt_id)
            if record.recording_consent
            else None
        )
        pronunciation_status = PronunciationStatus.NOT_REQUESTED
        if turn.evaluation.pronunciation and record.recording_consent:
            if audio_uri and self.pronunciation_configured:
                pronunciation_status = PronunciationStatus.QUEUED
            elif audio_uri:
                pronunciation_status = PronunciationStatus.NOT_CONFIGURED

        attempts = [
            attempt.model_copy(update={"selected": False})
            if attempt.turn_id == turn.id and attempt.selected
            else attempt
            for attempt in record.attempts
        ]
        attempt = GuidedAttemptRecord(
            attempt_id=payload.attempt_id,
            idempotency_key=payload.idempotency_key,
            turn_id=turn.id,
            selected=True,
            created_at=utc_now(),
            response_started_at_ms=payload.response_started_at_ms,
            response_ended_at_ms=payload.response_ended_at_ms,
            transcript_sha256=hashlib.sha256(payload.transcript.encode("utf-8")).hexdigest(),
            asr_confidence=payload.asr_confidence,
            audio_uri=audio_uri,
            audio_was_raw=bool(audio_uri),
            delivery=delivery,
            fluency=fluency,
            pronunciation_status=pronunciation_status,
        )
        attempts.append(attempt)
        record.attempts = attempts

        retry_recommended = (
            not payload.explicit_audio_issue
            and delivery.completion_ratio < 0.55
            and retry_number < 2
        )
        retry_reason = None
        if payload.explicit_audio_issue:
            retry_recommended = retry_number < 2
            retry_reason = payload.audio_issue_reason or "The audio could not be evaluated."
        elif retry_recommended:
            retry_reason = "Some expected words may not have been captured."

        if retry_recommended:
            record.state = GuidedSessionState.AWAITING_RETRY_DECISION
            spoken_reply = "Some words may have been missed. You can try again or continue."
        elif record.current_turn_index + 1 >= len(scenario.turns):
            record.status = GuidedSessionStatus.COMPLETED
            record.state = GuidedSessionState.COMPLETED
            record.paused_from_state = None
            record.completed_at = utc_now()
            spoken_reply = scenario.closing_spoken_text
        else:
            record.current_turn_index += 1
            record.state = GuidedSessionState.ASSISTANT_SPEAKING
            spoken_reply = scenario.turns[record.current_turn_index].assistant_spoken_text
        record.updated_at = utc_now()
        result = GuidedAttemptResult(
            attempt_id=payload.attempt_id,
            session=self._view(record, scenario),
            delivery=delivery,
            fluency=fluency,
            retry_recommended=retry_recommended,
            retry_reason=retry_reason,
            spoken_reply=spoken_reply,
            live_event=self._attempt_event(record, scenario, attempt, retry_recommended),
            pronunciation_status=pronunciation_status,
        )
        self.repository.save_guided_transition(
            record,
            payload.idempotency_key,
            result,
            correlation_id,
        )
        return result

    def _attempt_event(
        self,
        record: GuidedSessionRecord,
        scenario: GuidedScenario,
        attempt: GuidedAttemptRecord,
        retry_recommended: bool,
    ) -> dict[str, object]:
        event_type = (
            "guided.completed"
            if record.status == GuidedSessionStatus.COMPLETED
            else "guided.turn_evaluated"
        )
        return {
            "type": event_type,
            "data": {
                "session": self._view(record, scenario).model_dump(mode="json"),
                "session_id": record.session_id,
                "attempt_id": attempt.attempt_id,
                "turn_id": attempt.turn_id,
                "status": record.status.value,
                "state": record.state.value,
                "completed_turns": self._completed_turns(record),
                "total_turns": len(scenario.turns),
                "retry_recommended": retry_recommended,
                "fluency_status": attempt.fluency.status.value,
            },
        }

    def continue_after_retry(self, session_id: str) -> GuidedControlResult:
        record = self._record(session_id)
        scenario = self.catalog.get(record.scenario_id, record.scenario_version)
        if record.state != GuidedSessionState.AWAITING_RETRY_DECISION:
            raise InvalidGuidedState("There is no pending retry decision")
        if record.current_turn_index + 1 >= len(scenario.turns):
            record.status = GuidedSessionStatus.COMPLETED
            record.state = GuidedSessionState.COMPLETED
            record.paused_from_state = None
            record.completed_at = utc_now()
            spoken = scenario.closing_spoken_text
        else:
            record.current_turn_index += 1
            record.state = GuidedSessionState.ASSISTANT_SPEAKING
            spoken = scenario.turns[record.current_turn_index].assistant_spoken_text
        record.updated_at = utc_now()
        self.repository.save_guided_record(record, "guided.retry_skipped")
        return self._control_result(record, scenario, spoken, "guided.continued")

    def retry_current_turn(self, session_id: str) -> GuidedControlResult:
        record = self._record(session_id)
        scenario = self.catalog.get(record.scenario_id, record.scenario_version)
        if record.state != GuidedSessionState.AWAITING_RETRY_DECISION:
            raise InvalidGuidedState("There is no pending retry decision")
        record.state = GuidedSessionState.USER_PROMPT_VISIBLE
        record.updated_at = utc_now()
        self.repository.save_guided_record(record, "guided.retry_selected")
        return self._control_result(
            record,
            scenario,
            "Please try the same line again.",
            "guided.retry_ready",
        )

    def pause(self, session_id: str) -> GuidedControlResult:
        record = self._record(session_id)
        scenario = self.catalog.get(record.scenario_id, record.scenario_version)
        if record.status != GuidedSessionStatus.IN_PROGRESS:
            raise InvalidGuidedState("Only an in-progress conversation can be paused")
        if record.state == GuidedSessionState.PAUSED:
            return self._control_result(record, scenario, "", "guided.paused")
        if record.state not in {
            GuidedSessionState.ASSISTANT_SPEAKING,
            GuidedSessionState.USER_PROMPT_VISIBLE,
            GuidedSessionState.AWAITING_RETRY_DECISION,
        }:
            raise InvalidGuidedState("The conversation cannot be paused in this state")
        record.paused_from_state = record.state
        record.state = GuidedSessionState.PAUSED
        record.updated_at = utc_now()
        self.repository.save_guided_record(record, "guided.session_paused")
        return self._control_result(record, scenario, "", "guided.paused")

    def resume(self, session_id: str) -> GuidedControlResult:
        record = self._record(session_id)
        scenario = self.catalog.get(record.scenario_id, record.scenario_version)
        if record.status != GuidedSessionStatus.IN_PROGRESS:
            raise InvalidGuidedState("Only an in-progress conversation can be resumed")
        if record.state != GuidedSessionState.PAUSED:
            raise InvalidGuidedState("The guided conversation is not paused")
        previous = record.paused_from_state or GuidedSessionState.USER_PROMPT_VISIBLE
        if previous not in {
            GuidedSessionState.ASSISTANT_SPEAKING,
            GuidedSessionState.USER_PROMPT_VISIBLE,
            GuidedSessionState.AWAITING_RETRY_DECISION,
        }:
            previous = GuidedSessionState.USER_PROMPT_VISIBLE
        record.state = previous
        record.paused_from_state = None
        record.updated_at = utc_now()
        self.repository.save_guided_record(record, "guided.session_resumed")
        spoken = (
            scenario.turns[record.current_turn_index].assistant_spoken_text
            if previous == GuidedSessionState.ASSISTANT_SPEAKING
            else ""
        )
        return self._control_result(record, scenario, spoken, "guided.resumed")

    def stop(self, session_id: str) -> GuidedControlResult:
        record = self._record(session_id)
        scenario = self.catalog.get(record.scenario_id, record.scenario_version)
        if record.status != GuidedSessionStatus.IN_PROGRESS:
            raise InvalidGuidedState("Only an in-progress conversation can be stopped")
        record.status = GuidedSessionStatus.STOPPED
        record.state = GuidedSessionState.STOPPED
        record.paused_from_state = None
        record.stopped_at = utc_now()
        record.updated_at = record.stopped_at
        self.repository.save_guided_record(record, "guided.session_stopped")
        return self._control_result(
            record,
            scenario,
            "The conversation has been stopped. Your completed turns were saved.",
            "guided.stopped",
        )

    def update_confidence(
        self,
        session_id: str,
        payload: ConfidenceUpdateRequest,
    ) -> GuidedSessionView:
        record = self._record(session_id)
        scenario = self.catalog.get(record.scenario_id, record.scenario_version)
        record.confidence_after = payload.confidence_after
        record.updated_at = utc_now()
        self.repository.save_guided_record(record, "guided.confidence_updated")
        return self._view(record, scenario)

    def _control_result(
        self,
        record: GuidedSessionRecord,
        scenario: GuidedScenario,
        spoken_reply: str,
        event_type: str,
    ) -> GuidedControlResult:
        return GuidedControlResult(
            session=self._view(record, scenario),
            spoken_reply=spoken_reply,
            live_event={
                "type": event_type,
                "data": self._view(record, scenario).model_dump(mode="json"),
            },
        )

    def pronunciation_request(
        self,
        session_id: str,
        attempt_id: str,
    ) -> GuidedPronunciationRequestedEvent | None:
        record = self._record(session_id)
        attempt = next(
            (item for item in record.attempts if item.attempt_id == attempt_id),
            None,
        )
        if (
            attempt is None
            or attempt.pronunciation_status != PronunciationStatus.QUEUED
            or not attempt.audio_uri
        ):
            return None
        scenario = self.catalog.get(record.scenario_id, record.scenario_version)
        turn = next(item for item in scenario.turns if item.id == attempt.turn_id)
        callback = (
            f"{self.public_service_url}/v1/guided-conversations/pronunciation/callback"
            if self.public_service_url
            else None
        )
        return GuidedPronunciationRequestedEvent(
            event_id=f"guided-pron-{session_id}-{attempt_id}",
            occurred_at=utc_now(),
            session_id=session_id,
            attempt_id=attempt_id,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            turn_id=turn.id,
            expected_text=turn.user_expected_text,
            target_words=turn.evaluation.target_words,
            target_phonemes=scenario.target_phonemes,
            audio_uri=attempt.audio_uri,
            callback_url=callback,
        )

    def apply_pronunciation_result(
        self,
        payload: GuidedPronunciationResultEvent,
    ) -> None:
        record = self._record(payload.session_id)
        found = False
        attempts: list[GuidedAttemptRecord] = []
        for attempt in record.attempts:
            if attempt.attempt_id != payload.attempt_id:
                attempts.append(attempt)
                continue
            found = True
            attempts.append(
                attempt.model_copy(
                    update={
                        "pronunciation_status": (
                            PronunciationStatus.COMPLETED
                            if payload.status == "completed"
                            else PronunciationStatus.FAILED
                        ),
                        "pronunciation_patterns": payload.patterns,
                        "pronunciation_service_version": payload.service_version,
                    }
                )
            )
        if not found:
            raise GuidedAttemptConflict("Pronunciation callback attempt was not found")
        record.attempts = attempts
        record.updated_at = utc_now()
        self.repository.save_guided_record(record, "guided.pronunciation_updated")

    def mark_pronunciation_failed(self, session_id: str, attempt_id: str) -> None:
        self.apply_pronunciation_result(
            GuidedPronunciationResultEvent(
                event_type="guided.pronunciation_failed",
                event_id=f"local-failure-{uuid.uuid4()}",
                occurred_at=utc_now(),
                session_id=session_id,
                attempt_id=attempt_id,
                status="failed",
            )
        )

    def _result_debug(
        self,
        scenario: GuidedScenario,
        selected: Sequence[GuidedAttemptRecord],
    ) -> GuidedResultDebug:
        by_turn = {attempt.turn_id: attempt for attempt in selected}
        lines: list[GuidedLineDiagnostic] = []
        for index, turn in enumerate(scenario.turns, start=1):
            attempt = by_turn.get(turn.id)
            if attempt is None:
                lines.append(
                    GuidedLineDiagnostic(
                        line_number=index,
                        turn_id=turn.id,
                        expected_text=turn.user_expected_text,
                        attempted=False,
                        eligible=False,
                        fluency_status="not_attempted",
                        expected_word_count=len(_tokens(turn.user_expected_text)),
                        recognized_word_count=0,
                        timed_word_count=0,
                        response_duration_seconds=0,
                        speech_duration_seconds=0,
                        timing_source="unavailable",
                        completion_ratio=0,
                        insufficiency_reasons=["The line was not completed."],
                    )
                )
                continue
            features = attempt.fluency.features
            lines.append(
                GuidedLineDiagnostic(
                    line_number=index,
                    turn_id=turn.id,
                    expected_text=turn.user_expected_text,
                    attempted=True,
                    eligible=attempt.fluency.eligible,
                    fluency_status=attempt.fluency.status.value,
                    expected_word_count=attempt.delivery.expected_word_count,
                    recognized_word_count=attempt.delivery.recognized_word_count,
                    timed_word_count=features.word_count,
                    response_duration_seconds=features.response_duration_seconds,
                    speech_duration_seconds=features.speech_duration_seconds,
                    timing_source=features.timing_source,
                    completion_ratio=attempt.delivery.completion_ratio,
                    asr_confidence_percent=(
                        round(attempt.asr_confidence * 100)
                        if attempt.asr_confidence is not None
                        else None
                    ),
                    selected_attempt_number=attempt.delivery.retry_number + 1,
                    insufficiency_reasons=attempt.fluency.insufficiency_reasons,
                )
            )
        excluded = sum(not line.eligible for line in lines)
        if excluded:
            summary = (
                f"{excluded} of {len(lines)} selected line(s) were excluded from the fluency "
                "calculation. Inspect each line below to see whether word timings, timed words, "
                "or duration were missing."
            )
        else:
            summary = "Every completed line supplied enough timed evidence for guided scoring."
        return GuidedResultDebug(
            summary=summary,
            thresholds=GuidedFluencyThresholds(
                minimum_timed_words_per_line=self.fluency_settings.guided_minimum_turn_words,
                minimum_timed_seconds_per_line=self.fluency_settings.guided_minimum_turn_seconds,
                minimum_eligible_lines=self.fluency_settings.guided_minimum_turns,
                target_eligible_lines=self.fluency_settings.guided_target_turns,
                minimum_speech_seconds_when_below_target=(
                    self.fluency_settings.guided_minimum_speech_seconds
                ),
            ),
            lines=lines,
            excluded_line_count=excluded,
            guidance=[
                "A clear voice can still be excluded when Flux does not return usable word timestamps.",
                "Word-recognition confidence is a debugging signal, not a pronunciation grade.",
                "Only the final selected attempt for each line contributes to the session result.",
            ],
        )

    @staticmethod
    def _replay_script(scenario: GuidedScenario) -> list[GuidedReplayLine]:
        replay: list[GuidedReplayLine] = []
        sequence = 1
        for turn in scenario.turns:
            replay.append(
                GuidedReplayLine(
                    sequence=sequence,
                    role="assistant",
                    turn_id=turn.id,
                    text=turn.assistant_spoken_text,
                )
            )
            sequence += 1
            replay.append(
                GuidedReplayLine(
                    sequence=sequence,
                    role="learner",
                    turn_id=turn.id,
                    text=turn.user_expected_text,
                )
            )
            sequence += 1
        replay.append(
            GuidedReplayLine(
                sequence=sequence,
                role="assistant",
                text=scenario.closing_spoken_text,
            )
        )
        return replay

    def report(self, session_id: str) -> GuidedConversationReport:
        record = self._record(session_id)
        scenario = self.catalog.get(record.scenario_id, record.scenario_version)
        selected = self._selected_attempts(record)
        fluency = aggregate_session(
            session_id,
            FluencyMode.GUIDED,
            [attempt.fluency for attempt in selected],
            self.fluency_settings,
        ).model_copy(
            update={
                "score_interpretation": (
                    "Guided-speaking delivery index for this oral-reading scenario; "
                    "not a probability, overall English fluency score, or CEFR placement."
                )
            }
        )
        initiation_values = [
            attempt.delivery.prompt_to_speech_seconds
            for attempt in selected
            if attempt.delivery.prompt_to_speech_seconds is not None
        ]
        mean_initiation = (
            round(sum(initiation_values) / len(initiation_values), 3) if initiation_values else None
        )
        mean_completion = (
            round(
                sum(attempt.delivery.completion_ratio for attempt in selected) / len(selected),
                4,
            )
            if selected
            else 0.0
        )
        total_retries = sum(attempt.delivery.retry_number for attempt in selected)
        mid_phrase = sum(attempt.delivery.mid_phrase_pause_count for attempt in selected)
        without_retry = sum(attempt.delivery.retry_number == 0 for attempt in selected)
        if len(selected) < 3:
            delivery_label = "needs_more_evidence"
        elif mean_completion >= 0.85 and total_retries <= 1 and mid_phrase <= len(selected):
            delivery_label = "stable"
        else:
            delivery_label = "developing"

        pronunciation = self._pronunciation_summary(selected)
        confidence_change = None
        if record.confidence_before is not None and record.confidence_after is not None:
            confidence_change = record.confidence_after - record.confidence_before
        domain = self.catalog.get_domain(scenario.domain_id)
        return GuidedConversationReport(
            session_id=record.session_id,
            scenario_id=scenario.id,
            scenario_version=scenario.version,
            domain_id=domain.id,
            domain_title=domain.title,
            scenario_title=scenario.title,
            scenario_level=scenario.required_level,
            status=record.status,
            completed_turns=self._completed_turns(record),
            total_turns=len(scenario.turns),
            guided_speaking_fluency=fluency,
            delivery_stability=DeliverySummary(
                label=delivery_label,  # type: ignore[arg-type]
                mean_prompt_to_speech_seconds=mean_initiation,
                mean_completion_ratio=mean_completion,
                mid_phrase_pauses=mid_phrase,
                total_retries=total_retries,
                completed_without_retry=without_retry,
            ),
            pronunciation=pronunciation,
            confidence_before=record.confidence_before,
            confidence_after=record.confidence_after,
            confidence_change=confidence_change,
            result_debug=self._result_debug(scenario, selected),
            replay_script=self._replay_script(scenario),
            versions=record.versions,
            limitations=[
                "The learner read visible, predetermined lines, so this does not measure spontaneous language generation.",
                "Scenario performance cannot change the learner's CEFR placement.",
                "ASR completeness is an optional retry signal, not a pronunciation judgment.",
                "The fluency thresholds are an explainable MVP baseline pending human calibration.",
            ],
        )

    @staticmethod
    def _learner_rating(score: int) -> str:
        if score >= 80:
            return "strong"
        if score >= 60:
            return "good"
        return "keep_practising"

    @staticmethod
    def _learner_skill_message(key: str, score: int) -> str:
        if key == "pace":
            if score >= 80:
                return "You kept a comfortable, steady speaking pace."
            if score >= 60:
                return "Your pace was mostly clear; keep it steady through the whole line."
            return "Say each line a little more slowly and keep an even rhythm."
        if key == "smoothness":
            if score >= 80:
                return "You moved through the lines with very few disruptive pauses."
            if score >= 60:
                return "Most lines flowed well; practise the places where you stopped mid-sentence."
            return "Practise the line in short chunks, then join the chunks without long pauses."
        if score >= 80:
            return "You connected your words into complete, natural phrases."
        if score >= 60:
            return "Your words were usually connected; aim for slightly longer phrases."
        return "Try to say two or three more words together before pausing."

    def learner_result(self, session_id: str) -> GuidedLearnerResult:
        """Transform internal evidence into a small, non-diagnostic learner result."""
        report = self.report(session_id)
        completion_percent = round(report.completed_turns / report.total_turns * 100)
        completion = GuidedLearnerCompletion(
            completed_lines=report.completed_turns,
            total_lines=report.total_turns,
            percent=completion_percent,
        )
        if report.status != GuidedSessionStatus.COMPLETED:
            return GuidedLearnerResult(
                session_id=report.session_id,
                domain_title=report.domain_title,
                scenario_title=report.scenario_title,
                scenario_level=report.scenario_level,
                result_status="incomplete",
                headline="Conversation not completed",
                completion=completion,
                skills=[],
                next_step="Finish every line to receive speaking-flow feedback.",
            )

        fluency = report.guided_speaking_fluency
        if fluency.fluency_index is None or fluency.subscores is None:
            return GuidedLearnerResult(
                session_id=report.session_id,
                domain_title=report.domain_title,
                scenario_title=report.scenario_title,
                scenario_level=report.scenario_level,
                result_status="needs_more_speech",
                headline="Let’s try this scenario once more",
                completion=completion,
                skills=[],
                next_step=(
                    "Repeat the scenario and speak each complete line clearly so the app can "
                    "measure your speaking flow."
                ),
                replay_audio_url=(
                    f"/v1/guided-conversations/sessions/{report.session_id}/replay-audio"
                ),
            )

        skill_values = [
            ("pace", "Pace", fluency.subscores.speed),
            ("smoothness", "Smoothness", fluency.subscores.breakdown),
            ("connected_speech", "Connected speech", fluency.subscores.continuity),
        ]
        skills = [
            GuidedLearnerSkill(
                key=key,  # type: ignore[arg-type]
                label=label,
                score=score,
                rating=self._learner_rating(score),  # type: ignore[arg-type]
                message=self._learner_skill_message(key, score),
            )
            for key, label, score in skill_values
        ]
        strongest = max(skills, key=lambda item: item.score)
        weakest = min(skills, key=lambda item: item.score)
        index = fluency.fluency_index
        headline = (
            "Strong speaking flow"
            if index >= 80
            else "Good progress"
            if index >= 60
            else "Keep practising your flow"
        )
        pronunciation_tips: list[str] = []
        seen_words: set[str] = set()
        for pattern in report.pronunciation.patterns:
            word_key = pattern.word.lower()
            if word_key in seen_words:
                continue
            seen_words.add(word_key)
            pronunciation_tips.append(
                f"Practise “{pattern.word}” slowly, then say it again inside the full sentence."
            )
            if len(pronunciation_tips) == 3:
                break
        return GuidedLearnerResult(
            session_id=report.session_id,
            domain_title=report.domain_title,
            scenario_title=report.scenario_title,
            scenario_level=report.scenario_level,
            result_status="ready",
            headline=headline,
            speaking_flow_score=index,
            completion=completion,
            skills=skills,
            strength=f"Your strongest area was {strongest.label.lower()}.",
            next_step=weakest.message,
            pronunciation_tips=pronunciation_tips,
            replay_audio_url=(
                f"/v1/guided-conversations/sessions/{report.session_id}/replay-audio"
            ),
        )

    @staticmethod
    def _pronunciation_summary(
        selected: Sequence[GuidedAttemptRecord],
    ) -> PronunciationSummary:
        statuses = [attempt.pronunciation_status for attempt in selected]
        patterns = sorted(
            [pattern for attempt in selected for pattern in attempt.pronunciation_patterns],
            key=lambda pattern: pattern.confidence,
            reverse=True,
        )[:3]
        if not statuses or all(
            status in {PronunciationStatus.NOT_REQUESTED, PronunciationStatus.NOT_CONFIGURED}
            for status in statuses
        ):
            status = "not_configured"
        elif any(status == PronunciationStatus.QUEUED for status in statuses):
            status = "pending"
        elif all(status == PronunciationStatus.COMPLETED for status in statuses):
            status = "completed"
        elif any(status == PronunciationStatus.COMPLETED for status in statuses):
            status = "partial"
        else:
            status = "failed"
        return PronunciationSummary(status=status, patterns=patterns)  # type: ignore[arg-type]
