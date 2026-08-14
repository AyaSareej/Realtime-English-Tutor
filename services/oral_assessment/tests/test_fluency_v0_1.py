from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.fluency import (
    FluencyMode,
    FluencyObservationRequest,
    FluencyScoreStatus,
    aggregate_session,
    extract_features,
    score_observation,
)
from services.oral_assessment.main import create_app
from services.oral_assessment.models import CEFRLevel
from services.oral_assessment.scoring_engine import score_evaluator_output

from .helpers import PROJECT_ROOT, evaluator_output


def timed_words(count: int, *, step: float = 0.5, duration: float = 0.24) -> list[dict]:
    return [
        {
            "word": f"word{index}",
            "start": round(index * step, 3),
            "end": round(index * step + duration, 3),
            "confidence": 0.95,
        }
        for index in range(count)
    ]


def request(
    turn_id: str,
    *,
    mode: FluencyMode = FluencyMode.FREE,
    count: int = 12,
    step: float = 0.5,
    target_level: str | None = None,
) -> FluencyObservationRequest:
    return FluencyObservationRequest(
        session_id="session-1",
        turn_id=turn_id,
        mode=mode,
        transcript=" ".join(["I", "can", "continue", "my", "idea"] * 4),
        words=timed_words(count, step=step),
        completed=True,
        target_level=target_level,
    )


class FluencyFeatureTests(unittest.TestCase):
    def test_extracts_speed_breakdown_continuity_and_repair_evidence(self) -> None:
        payload = FluencyObservationRequest(
            session_id="session-1",
            turn_id="turn-features",
            mode=FluencyMode.FREE,
            transcript="I um need need a moment. I mean I can continue now.",
            words=[
                {"word": "I", "start": 0.0, "end": 0.2},
                {"word": "um", "start": 0.3, "end": 0.5},
                {"word": "need", "start": 0.6, "end": 0.9},
                {"word": "need", "start": 1.0, "end": 1.3},
                {"word": "a", "start": 3.0, "end": 3.1},
                {"word": "moment", "start": 3.2, "end": 3.6},
                {"word": "I", "start": 3.7, "end": 3.9},
                {"word": "mean", "start": 4.0, "end": 4.3},
                {"word": "continue", "start": 4.4, "end": 4.8},
            ],
        )
        features = extract_features(payload)
        self.assertEqual("word_timestamps", features.timing_source)
        self.assertEqual(1, features.long_pause_count)
        self.assertEqual(1, features.filler_count)
        self.assertEqual(1, features.immediate_repeat_count)
        self.assertEqual(1, features.self_correction_count)
        self.assertGreater(features.speech_rate_wpm, 0)
        self.assertGreater(features.mean_length_of_run_words, 0)

    def test_timestamp_absence_never_invents_a_score(self) -> None:
        result = score_observation(
            FluencyObservationRequest(
                session_id="session-1",
                turn_id="turn-no-timing",
                mode=FluencyMode.GUIDED,
                transcript="This transcript has enough words but no timing evidence at all.",
                response_started_at_ms=1_000,
                response_ended_at_ms=8_000,
            )
        )
        self.assertEqual(FluencyScoreStatus.INSUFFICIENT_EVIDENCE, result.status)
        self.assertIsNone(result.fluency_index)
        self.assertIn("Word-level timestamps were unavailable.", result.insufficiency_reasons)

    def test_extreme_speed_is_not_rewarded_indefinitely(self) -> None:
        functional = score_observation(request("functional", count=30, step=0.48))
        extreme = score_observation(request("extreme", count=30, step=0.10))
        self.assertIsNotNone(functional.fluency_index)
        self.assertIsNotNone(extreme.fluency_index)
        self.assertGreater(functional.subscores.speed, extreme.subscores.speed)


class FluencyAggregationTests(unittest.TestCase):
    def test_free_conversation_returns_index_without_cefr_label(self) -> None:
        observations = [score_observation(request(f"turn-{index}")) for index in range(5)]
        result = aggregate_session("session-1", FluencyMode.FREE, observations)
        self.assertEqual(FluencyScoreStatus.SCORED, result.status)
        self.assertIsNotNone(result.fluency_index)
        self.assertIsNone(result.cefr_fluency_estimate)
        self.assertEqual(5, result.evidence_count.eligible_turns)

    def test_short_free_session_reports_insufficient_evidence(self) -> None:
        observations = [score_observation(request(f"short-{index}")) for index in range(2)]
        result = aggregate_session("session-1", FluencyMode.FREE, observations)
        self.assertEqual(FluencyScoreStatus.INSUFFICIENT_EVIDENCE, result.status)
        self.assertIsNone(result.fluency_index)

    def test_controlled_assessment_is_the_only_mode_with_cefr_fluency(self) -> None:
        observations = [
            score_observation(
                request(
                    f"assessment-{index}",
                    mode=FluencyMode.ASSESSMENT,
                    count=16,
                    target_level="B1",
                )
            )
            for index in range(3)
        ]
        result = aggregate_session("session-1", FluencyMode.ASSESSMENT, observations)
        self.assertEqual(FluencyScoreStatus.SCORED, result.status)
        self.assertIn(result.cefr_fluency_estimate, {"Pre-A1", "A1", "A2", "B1", "B2"})

    def test_rule_scorer_overrides_evaluator_fluency_dimension(self) -> None:
        observation = score_observation(
            request(
                "slow-turn",
                mode=FluencyMode.ASSESSMENT,
                count=8,
                step=2.0,
                target_level="B2",
            )
        )
        scored = score_evaluator_output(
            evaluator_output(CEFRLevel.B2, 4),
            provider="test",
            model="test",
            fluency_observation=observation,
            target_level="B2",
        )
        self.assertEqual("rule_scorer", scored.fluency_source)
        self.assertLess(scored.scores.fluency, 4)
        self.assertIn("fluency-v0.1", scored.evidence.fluency)


class FluencyAPITests(unittest.TestCase):
    def test_turn_endpoint_is_idempotent_and_stores_no_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "fluency.db"
            environment = {
                "ASSESSMENT_DATABASE_URL": f"sqlite:///{database_path}",
                "ASSESSMENT_SERVICE_TOKEN": "fluency-service-token",
                "ASSESSMENT_ADMIN_TOKEN": "fluency-admin-token",
                "EVALUATOR_PROVIDER": "heuristic",
                "ALLOW_HEURISTIC_EVALUATOR": "true",
                "STORE_ALL_ASSESSMENT_AUDIO": "false",
            }
            with patch.dict(os.environ, environment, clear=False):
                client = TestClient(create_app(PROJECT_ROOT))
                headers = {"Authorization": "Bearer fluency-service-token"}
                payload = request("api-turn").model_dump(mode="json")
                first = client.post(
                    "/v1/fluency/sessions/session-1/turns",
                    headers=headers,
                    json=payload,
                )
                second = client.post(
                    "/v1/fluency/sessions/session-1/turns",
                    headers=headers,
                    json=payload,
                )
                self.assertEqual(200, first.status_code, first.text)
                self.assertEqual(first.json(), second.json())

                with closing(sqlite3.connect(database_path)) as connection:
                    rows = connection.execute(
                        "SELECT result_json FROM fluency_observations"
                    ).fetchall()
                self.assertEqual(1, len(rows))
                self.assertNotIn("I can continue", rows[0][0])


if __name__ == "__main__":
    unittest.main()
