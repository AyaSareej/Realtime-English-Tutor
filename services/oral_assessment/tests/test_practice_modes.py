from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.realtime.conversation_fluency import conversation_mode
from services.fluency import PracticeMode
from services.oral_assessment.main import create_app
from services.practice_sessions.tokens import IssuedParticipantToken

from .helpers import PROJECT_ROOT


class FakeTokenIssuer:
    server_url = "wss://example.livekit.cloud"

    def __init__(self) -> None:
        self.last_metadata: dict[str, object] | None = None

    def validate_configuration(self) -> None:
        return None

    def issue(self, **kwargs) -> IssuedParticipantToken:
        self.last_metadata = kwargs["dispatch_metadata"]
        return IssuedParticipantToken(
            token="short-lived-test-token",
            expires_at=datetime.now(UTC) + timedelta(minutes=20),
        )


class PracticeModeRoutingTests(unittest.TestCase):
    def test_dispatch_metadata_accepts_only_free_and_guided(self) -> None:
        guided = SimpleNamespace(
            job=SimpleNamespace(metadata=json.dumps({"conversation_mode": "guided"})),
            room=SimpleNamespace(metadata=json.dumps({"conversation_mode": "free"})),
        )
        self.assertEqual(PracticeMode.GUIDED, conversation_mode(guided))

        free = SimpleNamespace(
            job=SimpleNamespace(metadata=""),
            room=SimpleNamespace(metadata=json.dumps({"conversation_mode": "free"})),
        )
        self.assertEqual(PracticeMode.FREE, conversation_mode(free))

        old_third_mode = SimpleNamespace(
            job=SimpleNamespace(metadata=json.dumps({"conversation_mode": "scripted"})),
            room=SimpleNamespace(metadata=""),
        )
        with self.assertRaisesRegex(RuntimeError, "only 'free' or 'guided'"):
            conversation_mode(old_third_mode)


class PracticeSessionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        environment = {
            "ASSESSMENT_DATABASE_URL": f"sqlite:///{Path(self.temp.name) / 'practice.db'}",
            "ASSESSMENT_SERVICE_TOKEN": "practice-service-token",
            "ASSESSMENT_ADMIN_TOKEN": "practice-admin-token",
            "EVALUATOR_PROVIDER": "heuristic",
            "ALLOW_HEURISTIC_EVALUATOR": "true",
            "STORE_ALL_ASSESSMENT_AUDIO": "false",
        }
        self.environment = patch.dict("os.environ", environment, clear=False)
        self.environment.start()
        self.client = TestClient(create_app(PROJECT_ROOT))
        self.issuer = FakeTokenIssuer()
        self.client.app.state.livekit_token_issuer = self.issuer
        self.headers = {"Authorization": "Bearer practice-service-token"}

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def test_old_scripted_mode_is_rejected_by_public_contract(self) -> None:
        response = self.client.post(
            "/v1/practice-sessions",
            headers=self.headers,
            json={"user_id": "learner", "mode": "scripted"},
        )
        self.assertEqual(422, response.status_code)

    def test_free_session_dispatch_and_result(self) -> None:
        created = self.client.post(
            "/v1/practice-sessions",
            headers=self.headers,
            json={"user_id": "learner", "mode": "free"},
        )
        self.assertEqual(201, created.status_code, created.text)
        body = created.json()
        self.assertEqual("free", body["mode"])
        self.assertTrue(body["practice_session_id"].startswith("free-"))
        self.assertIsNone(body["guided_session"])
        self.assertEqual("free", self.issuer.last_metadata["conversation_mode"])

        result = self.client.get(body["result_url"], headers=self.headers)
        self.assertEqual(200, result.status_code)
        self.assertEqual("insufficient_evidence", result.json()["result"]["status"])
        self.assertIsNone(result.json()["result"]["cefr_fluency_estimate"])

    def test_guided_session_dispatch_and_report(self) -> None:
        created = self.client.post(
            "/v1/practice-sessions",
            headers=self.headers,
            json={
                "user_id": "learner",
                "mode": "guided",
                "scenario_id": "restaurant.order_drink.a1",
                "placement_completed": True,
                "placement_level": "A1",
                "recording_consent": False,
            },
        )
        self.assertEqual(201, created.status_code, created.text)
        body = created.json()
        self.assertEqual("guided", body["mode"])
        self.assertTrue(body["practice_session_id"].startswith("guided-"))
        self.assertEqual("guided.events", body["events_topic"])
        self.assertEqual("guided.command", body["commands_topic"])
        self.assertEqual(body["practice_session_id"], body["guided_session"]["session_id"])
        self.assertEqual("guided", self.issuer.last_metadata["conversation_mode"])
        self.assertEqual(
            body["practice_session_id"],
            self.issuer.last_metadata["guided_session_id"],
        )

        result = self.client.get(body["result_url"], headers=self.headers)
        self.assertEqual(200, result.status_code, result.text)
        learner_result = result.json()["result"]
        self.assertEqual("incomplete", learner_result["result_status"])
        self.assertEqual("Conversation not completed", learner_result["headline"])
        self.assertNotIn("guided_speaking_fluency", learner_result)
        self.assertNotIn("diagnostics", learner_result)


if __name__ == "__main__":
    unittest.main()
