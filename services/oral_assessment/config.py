from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None or not value.strip() else int(value)


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None or not value.strip() else float(value)


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    database_url: str
    service_token: str
    admin_token: str
    log_level: str
    rate_limit_per_minute: int
    max_body_bytes: int
    evaluator_provider: str
    gemini_api_key: str
    gemini_model: str
    gemini_api_version: str
    openai_api_key: str
    openai_model: str
    evaluator_timeout_seconds: float
    evaluator_max_retries: int
    evaluator_max_retry_wait_seconds: float
    allow_heuristic_evaluator: bool
    assessment_version: str
    item_bank_version: str
    rubric_version: str
    scorer_version: str
    fluency_version: str
    store_all_assessment_audio: bool
    audio_storage_backend: str
    audio_storage_root: Path
    audio_encryption_key: str
    audio_retention_days: int
    s3_bucket: str
    s3_prefix: str
    s3_region: str
    s3_kms_key_id: str
    pronunciation_service_url: str
    pronunciation_service_token: str
    guided_service_public_url: str
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    piper_required: bool

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> Settings:
        root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        database_url = os.getenv(
            "ASSESSMENT_DATABASE_URL",
            f"sqlite:///{root / 'data' / 'assessment.db'}",
        )
        storage_root = Path(os.getenv("AUDIO_STORAGE_ROOT", str(root / "data" / "audio")))
        if not storage_root.is_absolute():
            storage_root = (root / storage_root).resolve()
        return cls(
            project_root=root,
            database_url=database_url,
            service_token=os.getenv("ASSESSMENT_SERVICE_TOKEN", "dev-service-token"),
            admin_token=os.getenv("ASSESSMENT_ADMIN_TOKEN", "dev-admin-token"),
            log_level=os.getenv("ASSESSMENT_LOG_LEVEL", "INFO").upper(),
            rate_limit_per_minute=_int("ASSESSMENT_RATE_LIMIT_PER_MINUTE", 120),
            max_body_bytes=_int("ASSESSMENT_MAX_BODY_BYTES", 5 * 1024 * 1024),
            evaluator_provider=os.getenv("EVALUATOR_PROVIDER", "gemini").strip().lower(),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
            gemini_api_version=os.getenv("GEMINI_API_VERSION", "v1beta").strip().lower(),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6"),
            evaluator_timeout_seconds=_float("EVALUATOR_TIMEOUT_SECONDS", 15.0),
            evaluator_max_retries=_int("EVALUATOR_MAX_RETRIES", 3),
            evaluator_max_retry_wait_seconds=_float("EVALUATOR_MAX_RETRY_WAIT_SECONDS", 60.0),
            allow_heuristic_evaluator=_bool("ALLOW_HEURISTIC_EVALUATOR", False),
            assessment_version=os.getenv("ASSESSMENT_VERSION", "0.7.0"),
            item_bank_version=os.getenv("ITEM_BANK_VERSION", "0.2.0"),
            rubric_version=os.getenv("RUBRIC_VERSION", "0.3.0"),
            scorer_version=os.getenv("SCORER_VERSION", "0.3.0"),
            fluency_version=os.getenv("FLUENCY_SCORER_VERSION", "fluency-v0.1"),
            store_all_assessment_audio=_bool("STORE_ALL_ASSESSMENT_AUDIO", False),
            audio_storage_backend=os.getenv("AUDIO_STORAGE_BACKEND", "local").lower(),
            audio_storage_root=storage_root,
            audio_encryption_key=os.getenv("AUDIO_ENCRYPTION_KEY", ""),
            audio_retention_days=_int("AUDIO_RETENTION_DAYS", 30),
            s3_bucket=os.getenv("S3_BUCKET", ""),
            s3_prefix=os.getenv("S3_PREFIX", "oral-assessment"),
            s3_region=os.getenv("S3_REGION", ""),
            s3_kms_key_id=os.getenv("S3_KMS_KEY_ID", ""),
            pronunciation_service_url=os.getenv("PRONUNCIATION_SERVICE_URL", ""),
            pronunciation_service_token=os.getenv("PRONUNCIATION_SERVICE_TOKEN", ""),
            guided_service_public_url=os.getenv("GUIDED_SERVICE_PUBLIC_URL", ""),
            livekit_url=os.getenv("LIVEKIT_URL", ""),
            livekit_api_key=os.getenv("LIVEKIT_API_KEY", ""),
            livekit_api_secret=os.getenv("LIVEKIT_API_SECRET", ""),
            piper_required=_bool("PIPER_REQUIRED", False),
        )

    @property
    def item_bank_path(self) -> Path:
        return (
            self.project_root
            / "services"
            / "oral_assessment"
            / "data"
            / (f"item_bank_v{self.item_bank_version}.json")
        )

    @property
    def guided_scenario_path(self) -> Path:
        return self.project_root / "services" / "guided_conversation" / "content"

    def readiness_errors(self) -> list[str]:
        errors: list[str] = []
        if self.evaluator_provider == "gemini" and not self.gemini_api_key:
            errors.append("GEMINI_API_KEY is required for EVALUATOR_PROVIDER=gemini")
        elif self.evaluator_provider == "openai" and not self.openai_api_key:
            errors.append("OPENAI_API_KEY is required for EVALUATOR_PROVIDER=openai")
        elif self.evaluator_provider == "heuristic" and not self.allow_heuristic_evaluator:
            errors.append(
                "Heuristic evaluation is disabled; set ALLOW_HEURISTIC_EVALUATOR=true only for demos"
            )
        elif self.evaluator_provider not in {"gemini", "openai", "heuristic"}:
            errors.append(f"Unsupported evaluator provider: {self.evaluator_provider}")
        if self.service_token in {"", "dev-service-token"}:
            errors.append("Replace ASSESSMENT_SERVICE_TOKEN before deployment")
        if self.evaluator_timeout_seconds <= 0:
            errors.append("EVALUATOR_TIMEOUT_SECONDS must be greater than zero")
        if not 0 <= self.evaluator_max_retries <= 6:
            errors.append("EVALUATOR_MAX_RETRIES must be between 0 and 6")
        if not 0 <= self.evaluator_max_retry_wait_seconds <= 120:
            errors.append("EVALUATOR_MAX_RETRY_WAIT_SECONDS must be between 0 and 120")
        if self.gemini_api_version not in {"v1", "v1beta"}:
            errors.append("GEMINI_API_VERSION must be v1 or v1beta")
        if self.audio_storage_backend not in {"local", "s3"}:
            errors.append("AUDIO_STORAGE_BACKEND must be local or s3")
        if self.audio_storage_backend == "s3" and not self.s3_bucket:
            errors.append("S3_BUCKET is required for AUDIO_STORAGE_BACKEND=s3")
        if self.store_all_assessment_audio:
            if not self.audio_encryption_key:
                errors.append(
                    "AUDIO_ENCRYPTION_KEY is required when all assessment audio is stored"
                )
            else:
                try:
                    Fernet(self.audio_encryption_key.encode("ascii"))
                except (UnicodeEncodeError, ValueError):
                    errors.append("AUDIO_ENCRYPTION_KEY must be a valid 44-character Fernet key")
        if not self.item_bank_path.exists():
            errors.append(f"Item bank not found: {self.item_bank_path}")
        if not self.guided_scenario_path.exists():
            errors.append(f"Guided scenario catalog not found: {self.guided_scenario_path}")
        return errors
