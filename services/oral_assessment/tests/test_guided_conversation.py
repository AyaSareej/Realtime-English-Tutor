from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from services.guided_conversation.catalog import (
    ScenarioCatalogRepository,
    ScenarioLocked,
)
from services.guided_conversation.models import (
    CEFRLevel,
    GuidedAttemptRequest,
    GuidedSessionCreateRequest,
)
from services.guided_conversation.service import GuidedConversationService
from services.oral_assessment.main import create_app
from services.oral_assessment.repository import SQLRepository

from .helpers import PROJECT_ROOT


def timed_words(text: str) -> list[dict[str, object]]:
    words = text.replace(",", "").replace(".", "").replace("!", "").split()
    return [
        {
            "word": word,
            "start": index * 0.52,
            "end": index * 0.52 + 0.30,
            "confidence": 0.95,
        }
        for index, word in enumerate(words)
    ]


class GuidedConversationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "guided.db"
        self.repository = SQLRepository(f"sqlite:///{self.database_path}")
        self.repository.initialize()
        self.catalog = ScenarioCatalogRepository(
            PROJECT_ROOT / "services" / "guided_conversation" / "content"
        )
        self.service = GuidedConversationService(self.repository, self.catalog)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_a1_session(self, scenario_id: str = "restaurant.order_drink.a1"):
        return self.service.create_session(
            GuidedSessionCreateRequest(
                user_id="learner-1",
                scenario_id=scenario_id,
                placement_completed=True,
                placement_level=CEFRLevel.A1,
                confidence_before=45,
            )
        )

    def attempt(self, view, index: int, transcript: str | None = None):
        current = view.current_turn
        assert current is not None
        expected = transcript or current.learner_display_text
        return GuidedAttemptRequest(
            attempt_id=f"attempt-{index}",
            idempotency_key=f"guided-idempotency-{index}",
            turn_id=current.turn_id,
            transcript=expected,
            words=timed_words(expected),
            prompt_available_at_ms=1_000,
            response_started_at_ms=1_500,
            response_ended_at_ms=8_000,
            asr_confidence=0.95,
        )

    def test_catalog_is_level_gated_and_higher_scenario_is_visible(self) -> None:
        summaries = self.service.catalog_view(True, CEFRLevel.A1)
        self.assertEqual(4, len(summaries))
        unlocked = {item.scenario_id for item in summaries if not item.is_locked}
        locked = {item.scenario_id for item in summaries if item.is_locked}
        self.assertEqual(
            {"restaurant.order_drink.a1", "restaurant.order_meal.a1"},
            unlocked,
        )
        self.assertEqual({"restaurant.wrong_order.b1", "airport.check_in.a2"}, locked)

        domains = self.service.domain_catalog_view(True, CEFRLevel.A1)
        self.assertEqual(2, len(domains))
        self.assertEqual("restaurant", domains[0].domain_id)
        self.assertEqual(3, domains[0].scenario_count)
        self.assertEqual(2, domains[0].available_scenario_count)
        self.assertEqual(
            {"restaurant.order_drink.a1", "restaurant.order_meal.a1"},
            {item.scenario_id for item in domains[0].scenarios if not item.is_locked},
        )
        self.assertEqual("airport", domains[1].domain_id)
        self.assertEqual(1, domains[1].scenario_count)
        self.assertEqual(0, domains[1].available_scenario_count)

        with self.assertRaises(ScenarioLocked):
            self.service.create_session(
                GuidedSessionCreateRequest(
                    user_id="learner-1",
                    scenario_id="restaurant.wrong_order.b1",
                    placement_completed=True,
                    placement_level=CEFRLevel.A1,
                )
            )

        with self.assertRaises(ScenarioLocked):
            self.service.scenario_preview(
                "restaurant.wrong_order.b1",
                None,
                True,
                CEFRLevel.A1,
            )

    def test_complete_scenario_returns_guided_fluency_without_cefr(self) -> None:
        view = self.create_a1_session()
        scenario = self.catalog.get(view.scenario_id, view.scenario_version)
        for index in range(len(scenario.turns)):
            view = self.service.mark_prompt_ready(view.session_id)
            result = self.service.submit_attempt(
                view.session_id,
                self.attempt(view, index),
            )
            view = result.session

        self.assertEqual("completed", view.status.value)
        self.assertEqual("completed", result.live_event["data"]["session"]["status"])
        report = self.service.report(view.session_id)
        self.assertEqual("scored", report.guided_speaking_fluency.status.value)
        self.assertIsNone(report.guided_speaking_fluency.cefr_fluency_estimate)
        self.assertIn("oral-reading scenario", report.guided_speaking_fluency.score_interpretation)
        self.assertEqual(6, report.completed_turns)
        self.assertEqual(45, report.confidence_before)
        self.assertEqual("restaurant", report.domain_id)
        self.assertEqual(0, report.result_debug.excluded_line_count)
        self.assertEqual(13, len(report.replay_script))
        learner = self.service.learner_result(view.session_id)
        self.assertEqual("ready", learner.result_status)
        self.assertIsNotNone(learner.speaking_flow_score)
        self.assertEqual(
            {"pace", "smoothness", "connected_speech"},
            {skill.key for skill in learner.skills},
        )
        public_payload = learner.model_dump(mode="json")
        self.assertNotIn("result_debug", public_payload)
        self.assertNotIn("confidence", public_payload)
        self.assertNotIn("delivery_stability", public_payload)

    def test_airport_scenario_is_unlocked_at_a2(self) -> None:
        summaries = self.service.domain_catalog_view(True, CEFRLevel.A2)
        airport = next(domain for domain in summaries if domain.domain_id == "airport")
        self.assertEqual(1, airport.available_scenario_count)
        view = self.service.create_session(
            GuidedSessionCreateRequest(
                user_id="traveller-1",
                scenario_id="airport.check_in.a2",
                placement_completed=True,
                placement_level=CEFRLevel.A2,
            )
        )
        self.assertEqual("Airport", view.domain_title)
        self.assertEqual("Checking In for a Flight", view.scenario_title)

    def test_guided_short_lines_use_guided_specific_evidence_thresholds(self) -> None:
        view = self.service.mark_prompt_ready(self.create_a1_session().session_id)
        payload = self.attempt(view, 30, transcript="Tea please")
        payload = payload.model_copy(
            update={
                "words": [
                    {"word": "Tea", "start": 0.0, "end": 0.35, "confidence": 0.95},
                    {"word": "please", "start": 0.55, "end": 1.05, "confidence": 0.95},
                ],
                "response_started_at_ms": 1_000,
                "response_ended_at_ms": 2_100,
            }
        )
        result = self.service.submit_attempt(view.session_id, payload)
        self.assertTrue(result.fluency.eligible)
        self.assertEqual("scored", result.fluency.status.value)

    def test_pause_and_resume_restore_the_previous_state(self) -> None:
        view = self.service.mark_prompt_ready(self.create_a1_session().session_id)
        paused = self.service.pause(view.session_id)
        self.assertEqual("paused", paused.session.state.value)
        self.assertEqual(["resume", "stop"], paused.session.allowed_actions)
        resumed = self.service.resume(view.session_id)
        self.assertEqual("user_prompt_visible", resumed.session.state.value)
        self.assertIn("speak", resumed.session.allowed_actions)

    def test_asr_mismatch_offers_retry_but_never_locks_progression(self) -> None:
        view = self.service.mark_prompt_ready(self.create_a1_session().session_id)
        poor = self.attempt(view, 1, transcript="tea")
        poor = poor.model_copy(update={"words": timed_words("tea")})
        first = self.service.submit_attempt(view.session_id, poor)
        self.assertTrue(first.retry_recommended)
        self.assertEqual("awaiting_retry_decision", first.session.state.value)

        continued = self.service.continue_after_retry(view.session_id)
        self.assertEqual("assistant_speaking", continued.session.state.value)
        self.assertEqual(2, continued.session.current_turn.turn_number)

    def test_retry_keeps_history_but_selects_only_the_latest_attempt(self) -> None:
        view = self.service.mark_prompt_ready(self.create_a1_session().session_id)
        poor = self.attempt(view, 10, transcript="tea")
        poor = poor.model_copy(update={"words": timed_words("tea")})
        self.service.submit_attempt(view.session_id, poor)
        retry_ready = self.service.retry_current_turn(view.session_id).session
        second = self.service.submit_attempt(
            view.session_id,
            self.attempt(retry_ready, 11),
        )
        record = self.repository.get_guided_session(view.session_id)
        assert record is not None
        self.assertEqual(2, len(record.attempts))
        self.assertFalse(record.attempts[0].selected)
        self.assertTrue(record.attempts[1].selected)
        self.assertEqual(2, second.session.current_turn.turn_number)

    def test_attempt_submission_is_idempotent_and_transcript_is_not_persisted(self) -> None:
        view = self.service.mark_prompt_ready(self.create_a1_session().session_id)
        payload = self.attempt(view, 20)
        first = self.service.submit_attempt(view.session_id, payload)
        replay = self.service.submit_attempt(view.session_id, payload)
        self.assertFalse(first.idempotent_replay)
        self.assertTrue(replay.idempotent_replay)
        record = self.repository.get_guided_session(view.session_id)
        assert record is not None
        self.assertEqual(1, len(record.attempts))

        with closing(sqlite3.connect(self.database_path)) as connection:
            stored_json = connection.execute(
                "SELECT record_json FROM guided_sessions WHERE session_id=?",
                (view.session_id,),
            ).fetchone()[0]
        self.assertNotIn(payload.transcript, stored_json)
        self.assertNotIn('"transcript":', stored_json)
        self.assertNotIn('"words":', stored_json)


class GuidedConversationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        database_url = f"sqlite:///{Path(self.temp.name) / 'api.db'}"
        environment = {
            "ASSESSMENT_DATABASE_URL": database_url,
            "ASSESSMENT_SERVICE_TOKEN": "guided-service-token",
            "ASSESSMENT_ADMIN_TOKEN": "guided-admin-token",
            "EVALUATOR_PROVIDER": "heuristic",
            "ALLOW_HEURISTIC_EVALUATOR": "true",
            "STORE_ALL_ASSESSMENT_AUDIO": "false",
            "AUDIO_ENCRYPTION_KEY": "",
            "AUDIO_STORAGE_ROOT": str(Path(self.temp.name) / "audio"),
        }
        self.environment = patch.dict("os.environ", environment, clear=False)
        self.environment.start()
        self.client = TestClient(create_app(PROJECT_ROOT))
        self.headers = {"Authorization": "Bearer guided-service-token"}

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def test_direct_start_of_locked_b1_scenario_is_rejected(self) -> None:
        response = self.client.post(
            "/v1/guided-conversations/sessions",
            headers=self.headers,
            json={
                "user_id": "api-learner",
                "scenario_id": "restaurant.wrong_order.b1",
                "placement_completed": True,
                "placement_level": "A1",
                "recording_consent": False,
            },
        )
        self.assertEqual(403, response.status_code)
        self.assertEqual("This scenario requires B1", response.json()["detail"])

    def test_create_and_retrieve_guided_session_contract(self) -> None:
        created = self.client.post(
            "/v1/guided-conversations/sessions",
            headers=self.headers,
            json={
                "user_id": "api-learner",
                "scenario_id": "restaurant.order_drink.a1",
                "placement_completed": True,
                "placement_level": "A1",
                "recording_consent": False,
            },
        )
        self.assertEqual(201, created.status_code)
        session = created.json()
        self.assertEqual("assistant_speaking", session["state"])
        self.assertEqual("turn_01", session["current_turn"]["turn_id"])
        self.assertFalse(session["recording_consent"])

        retrieved = self.client.get(
            f"/v1/guided-conversations/sessions/{session['session_id']}",
            headers=self.headers,
        )
        self.assertEqual(200, retrieved.status_code)
        self.assertEqual(session["session_id"], retrieved.json()["session_id"])

    def test_recording_session_requires_encrypted_audio_storage(self) -> None:
        response = self.client.post(
            "/v1/guided-conversations/sessions",
            headers=self.headers,
            json={
                "user_id": "api-learner",
                "scenario_id": "restaurant.order_drink.a1",
                "placement_completed": True,
                "placement_level": "A1",
                "recording_consent": True,
            },
        )
        self.assertEqual(503, response.status_code)

    def test_consented_original_audio_is_encrypted_and_retrievable(self) -> None:
        with patch.dict(
            "os.environ",
            {"AUDIO_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii")},
        ):
            client = TestClient(create_app(PROJECT_ROOT))
            created = client.post(
                "/v1/guided-conversations/sessions",
                headers=self.headers,
                json={
                    "user_id": "api-learner",
                    "scenario_id": "restaurant.order_drink.a1",
                    "placement_completed": True,
                    "placement_level": "A1",
                    "recording_consent": True,
                },
            )
            self.assertEqual(201, created.status_code)
            session_id = created.json()["session_id"]
            uploaded = client.post(
                f"/v1/guided-conversations/sessions/{session_id}/audio/attempt-audio",
                headers=self.headers,
                files={"audio": ("turn.raw", b"original-audio", "audio/raw")},
            )
            self.assertEqual(200, uploaded.status_code)
            uri = uploaded.json()["audio_uri"]
            self.assertEqual(b"original-audio", client.app.state.audio_storage.get(uri))

    def test_locked_scenario_preview_is_rejected_by_the_service(self) -> None:
        response = self.client.get(
            "/v1/guided-conversations/scenarios/restaurant.wrong_order.b1",
            headers=self.headers,
            params={"placement_completed": "true", "placement_level": "A1"},
        )
        self.assertEqual(403, response.status_code)

        unlocked = self.client.get(
            "/v1/guided-conversations/scenarios/restaurant.wrong_order.b1",
            headers=self.headers,
            params={"placement_completed": "true", "placement_level": "B1"},
        )
        self.assertEqual(200, unlocked.status_code)

    def test_debug_report_requires_admin_token_and_public_report_hides_diagnostics(self) -> None:
        created = self.client.post(
            "/v1/guided-conversations/sessions",
            headers=self.headers,
            json={
                "user_id": "api-learner",
                "scenario_id": "restaurant.order_drink.a1",
                "placement_completed": True,
                "placement_level": "A1",
                "recording_consent": False,
            },
        ).json()
        session_id = created["session_id"]
        public = self.client.get(
            f"/v1/guided-conversations/sessions/{session_id}/report",
            headers=self.headers,
        )
        self.assertEqual(200, public.status_code)
        self.assertEqual("incomplete", public.json()["result_status"])
        self.assertNotIn("result_debug", public.json())

        rejected = self.client.get(
            f"/v1/admin/guided-conversations/sessions/{session_id}/debug-report",
            headers=self.headers,
        )
        self.assertEqual(401, rejected.status_code)
        accepted = self.client.get(
            f"/v1/admin/guided-conversations/sessions/{session_id}/debug-report",
            headers={"Authorization": "Bearer guided-admin-token"},
        )
        self.assertEqual(200, accepted.status_code)
        self.assertIn("result_debug", accepted.json())

    def test_completed_replay_is_rendered_by_the_local_piper_service(self) -> None:
        service = self.client.app.state.guided_service
        view = service.create_session(
            GuidedSessionCreateRequest(
                user_id="replay-learner",
                scenario_id="restaurant.order_drink.a1",
                placement_completed=True,
                placement_level=CEFRLevel.A1,
            )
        )
        scenario = service.catalog.get(view.scenario_id, view.scenario_version)
        for index in range(len(scenario.turns)):
            view = service.mark_prompt_ready(view.session_id)
            current = view.current_turn
            assert current is not None
            service.submit_attempt(
                view.session_id,
                GuidedAttemptRequest(
                    attempt_id=f"replay-attempt-{index}",
                    idempotency_key=f"replay-idempotency-{index}",
                    turn_id=current.turn_id,
                    transcript=current.learner_display_text,
                    words=timed_words(current.learner_display_text),
                ),
            )

        class FakeReplaySynthesizer:
            def __init__(self) -> None:
                self.lines: list[tuple[str, float]] = []
                self.pause_seconds = 0.0

            def synthesize_dialogue_wav(self, lines, *, pause_seconds):
                self.lines = list(lines)
                self.pause_seconds = pause_seconds
                return b"RIFF-piper-test"

        synthesizer = FakeReplaySynthesizer()
        self.client.app.state.piper_synthesizer = synthesizer
        replay = self.client.get(
            f"/v1/guided-conversations/sessions/{view.session_id}/replay-audio",
            headers=self.headers,
        )
        self.assertEqual(200, replay.status_code, replay.text)
        self.assertEqual("audio/wav", replay.headers["content-type"])
        self.assertEqual(b"RIFF-piper-test", replay.content)
        self.assertEqual(13, len(synthesizer.lines))
        self.assertGreater(synthesizer.pause_seconds, 0)


if __name__ == "__main__":
    unittest.main()
