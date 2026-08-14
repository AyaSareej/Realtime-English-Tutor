from __future__ import annotations

import logging
import math
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from services.fluency.api import router as fluency_router
from services.guided_conversation.api import (
    admin_router as guided_admin_router,
)
from services.guided_conversation.api import (
    install_exception_handlers as install_guided_exception_handlers,
)
from services.guided_conversation.api import router as guided_router
from services.guided_conversation.catalog import ScenarioCatalogRepository
from services.guided_conversation.pronunciation import GuidedPronunciationPublisher
from services.guided_conversation.service import GuidedConversationService
from services.local_tts import PiperConfigurationError, PiperSynthesizer
from services.practice_sessions.api import router as practice_sessions_router
from services.practice_sessions.tokens import LiveKitTokenIssuer

from .api import router
from .config import Settings
from .item_bank import ItemBankRepository
from .metrics import ServiceMetrics
from .middleware import SecurityAndObservabilityMiddleware
from .repository import SQLRepository
from .rubric_evaluator import (
    EvaluationUnavailable,
    UnavailableEvaluator,
    build_evaluator,
)
from .service import (
    AssessmentNotFound,
    AssessmentService,
    InvalidAssessmentState,
    SubmissionConflict,
)
from .storage import AudioStorageError, build_audio_storage


def create_app(project_root: Path | None = None) -> FastAPI:
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    load_dotenv(root / ".env.local")
    load_dotenv(root / ".env", override=False)
    settings = Settings.from_env(root)
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    repository = SQLRepository(settings.database_url)
    repository.initialize()
    item_bank = ItemBankRepository(settings.item_bank_path)
    readiness_errors = settings.readiness_errors()
    try:
        evaluator = build_evaluator(settings)
        evaluator.validate()
    except EvaluationUnavailable as exc:
        evaluator = UnavailableEvaluator(str(exc))
        if str(exc) not in readiness_errors:
            readiness_errors.append(str(exc))
    try:
        audio_storage = build_audio_storage(settings)
    except AudioStorageError as exc:
        audio_storage = None
        if settings.store_all_assessment_audio:
            detail = str(exc)
            same_setting_reported = "AUDIO_ENCRYPTION_KEY" in detail and any(
                "AUDIO_ENCRYPTION_KEY" in error for error in readiness_errors
            )
            if not same_setting_reported and detail not in readiness_errors:
                readiness_errors.append(detail)

    metrics = ServiceMetrics()
    assessment_service = AssessmentService(
        settings,
        repository,
        item_bank,
        evaluator,
    )
    scenario_catalog = ScenarioCatalogRepository(settings.guided_scenario_path)
    guided_pronunciation_publisher = GuidedPronunciationPublisher(
        settings.pronunciation_service_url,
        settings.pronunciation_service_token,
        settings.evaluator_timeout_seconds,
    )
    guided_service = GuidedConversationService(
        repository,
        scenario_catalog,
        pronunciation_configured=guided_pronunciation_publisher.configured,
        public_service_url=settings.guided_service_public_url,
    )
    piper_synthesizer = None
    try:
        piper_synthesizer = PiperSynthesizer(root)
    except PiperConfigurationError as exc:
        if settings.piper_required:
            readiness_errors.append(str(exc))
        logging.getLogger("local-tts").warning("Piper unavailable: %s", exc)
    app = FastAPI(
        title="English Tutor Assessment and Practice Service",
        version=settings.assessment_version,
        description=(
            "CEFR-aligned adaptive oral placement plus deterministic guided role-play "
            "practice. Guided practice does not issue or change CEFR placement."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.state.settings = settings
    app.state.repository = repository
    app.state.item_bank = item_bank
    app.state.assessment_service = assessment_service
    app.state.guided_service = guided_service
    app.state.guided_pronunciation_publisher = guided_pronunciation_publisher
    app.state.piper_synthesizer = piper_synthesizer
    app.state.livekit_token_issuer = LiveKitTokenIssuer(
        settings.livekit_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    )
    app.state.audio_storage = audio_storage
    app.state.metrics = metrics
    app.state.readiness_errors = readiness_errors
    app.state.observed_completions = set()
    app.state.versions = {
        "assessment": settings.assessment_version,
        "item_bank": item_bank.bank.version,
        "rubric": settings.rubric_version,
        "scorer": settings.scorer_version,
        "fluency": settings.fluency_version,
        "guided_scenarios": scenario_catalog.content_version,
        "guided_engine": "guided-engine-v0.2",
        "guided_tts": "piper-1.6.0",
    }
    repository.set_runtime_setting("active_item_bank_version", item_bank.bank.version)
    app.add_middleware(SecurityAndObservabilityMiddleware, settings=settings, metrics=metrics)
    app.include_router(router)
    app.include_router(fluency_router)
    app.include_router(guided_router)
    app.include_router(guided_admin_router)
    app.include_router(practice_sessions_router)
    install_guided_exception_handlers(app)

    @app.exception_handler(AssessmentNotFound)
    async def not_found(request: Request, exc: AssessmentNotFound):
        return JSONResponse(status_code=404, content={"detail": "Assessment not found"})

    @app.exception_handler(SubmissionConflict)
    async def conflict(request: Request, exc: SubmissionConflict):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(InvalidAssessmentState)
    async def invalid_state(request: Request, exc: InvalidAssessmentState):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(EvaluationUnavailable)
    async def evaluator_unavailable(request: Request, exc: EvaluationUnavailable):
        metrics.observe_evaluator_failure()
        retry_after_seconds = exc.retry_after_seconds
        if retry_after_seconds is None:
            retry_after_seconds = 5 if exc.retryable else 30
        retry_after = str(max(1, math.ceil(retry_after_seconds)))
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "The scoring evaluator is temporarily unavailable; the same idempotent "
                    "response can be retried without repeating the learner's answer."
                ),
                "error_code": exc.category,
                "provider": exc.provider,
                "provider_status": exc.status_code,
                "retryable": exc.retryable,
            },
            headers={"Retry-After": retry_after},
        )

    return app


app = create_app()
