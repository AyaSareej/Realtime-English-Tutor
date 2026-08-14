from __future__ import annotations

import io
import os
import threading
import wave
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


class PiperConfigurationError(RuntimeError):
    """Raised when the local Piper runtime or voice model is unavailable."""


@dataclass(frozen=True, slots=True)
class PiperAudio:
    pcm_s16le: bytes
    sample_rate: int
    num_channels: int
    sample_width: int

    def wav_bytes(self) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setframerate(self.sample_rate)
            wav_file.setnchannels(self.num_channels)
            wav_file.setsampwidth(self.sample_width)
            wav_file.writeframes(self.pcm_s16le)
        return output.getvalue()


def _resolve_model_path(project_root: Path, configured: str | None = None) -> Path:
    raw = configured or os.getenv(
        "PIPER_MODEL_PATH",
        "./data/piper/en_US-lessac-medium.onnx",
    )
    path = Path(raw)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


class PiperSynthesizer:
    """Load one Piper voice once and serialize CPU inference safely."""

    def __init__(
        self,
        project_root: Path,
        *,
        model_path: str | None = None,
        model_name: str | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.model_path = _resolve_model_path(self.project_root, model_path)
        self.config_path = Path(f"{self.model_path}.json")
        self.model_name = model_name or os.getenv(
            "PIPER_VOICE",
            "en_US-lessac-medium",
        )
        if not self.model_path.is_file():
            raise PiperConfigurationError(
                f"Piper voice model not found: {self.model_path}. "
                "Run scripts\\setup_piper.ps1 once while online."
            )
        if not self.config_path.is_file():
            raise PiperConfigurationError(
                f"Piper voice config not found: {self.config_path}. "
                "Run scripts\\setup_piper.ps1 once while online."
            )
        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise PiperConfigurationError(
                "piper-tts is not installed; rerun scripts\\setup.ps1"
            ) from exc
        try:
            self._voice = PiperVoice.load(str(self.model_path))
        except Exception as exc:
            raise PiperConfigurationError(
                f"Piper could not load {self.model_path.name}: {exc}"
            ) from exc
        self._lock = threading.Lock()

    @classmethod
    def availability_error(
        cls,
        project_root: Path,
        *,
        model_path: str | None = None,
    ) -> str | None:
        path = _resolve_model_path(project_root.resolve(), model_path)
        config_path = Path(f"{path}.json")
        try:
            import piper  # noqa: F401
        except ImportError:
            return "piper-tts is not installed; rerun scripts\\setup.ps1"
        if not path.is_file():
            return f"Piper voice model not found: {path}"
        if not config_path.is_file():
            return f"Piper voice config not found: {config_path}"
        return None

    @staticmethod
    def _config(length_scale: float):
        try:
            from piper import SynthesisConfig
        except ImportError as exc:
            raise PiperConfigurationError("piper-tts is not installed") from exc
        return SynthesisConfig(
            length_scale=length_scale,
            volume=float(os.getenv("PIPER_VOLUME", "1.0")),
            normalize_audio=True,
        )

    def synthesize_pcm(self, text: str, *, length_scale: float = 1.0) -> PiperAudio:
        cleaned = " ".join(text.split())
        if not cleaned:
            raise ValueError("Piper cannot synthesize empty text")
        if not 0.5 <= length_scale <= 2.0:
            raise ValueError("Piper length_scale must be between 0.5 and 2.0")

        chunks = []
        sample_rate: int | None = None
        num_channels: int | None = None
        sample_width: int | None = None
        with self._lock:
            for chunk in self._voice.synthesize(
                cleaned,
                syn_config=self._config(length_scale),
            ):
                chunk_rate = int(chunk.sample_rate)
                chunk_channels = int(chunk.sample_channels)
                chunk_width = int(chunk.sample_width)
                if sample_rate is None:
                    sample_rate = chunk_rate
                    num_channels = chunk_channels
                    sample_width = chunk_width
                elif (sample_rate, num_channels, sample_width) != (
                    chunk_rate,
                    chunk_channels,
                    chunk_width,
                ):
                    raise RuntimeError("Piper changed audio format within one synthesis")
                chunks.append(bytes(chunk.audio_int16_bytes))

        if not chunks or sample_rate is None or num_channels is None or sample_width is None:
            raise RuntimeError("Piper returned no audio")
        if sample_width != 2:
            raise RuntimeError(f"Piper returned unsupported {sample_width * 8}-bit PCM")
        return PiperAudio(
            pcm_s16le=b"".join(chunks),
            sample_rate=sample_rate,
            num_channels=num_channels,
            sample_width=sample_width,
        )

    def synthesize_dialogue_wav(
        self,
        lines: Iterable[tuple[str, float]],
        *,
        pause_seconds: float = 0.32,
    ) -> bytes:
        rendered: list[PiperAudio] = []
        for text, length_scale in lines:
            rendered.append(self.synthesize_pcm(text, length_scale=length_scale))
        if not rendered:
            raise ValueError("The replay dialogue is empty")
        first = rendered[0]
        silence = b"\x00" * int(
            first.sample_rate
            * first.num_channels
            * first.sample_width
            * max(0.0, pause_seconds)
        )
        pcm_parts: list[bytes] = []
        for index, audio in enumerate(rendered):
            if (
                audio.sample_rate,
                audio.num_channels,
                audio.sample_width,
            ) != (
                first.sample_rate,
                first.num_channels,
                first.sample_width,
            ):
                raise RuntimeError("Piper replay lines used incompatible audio formats")
            if index:
                pcm_parts.append(silence)
            pcm_parts.append(audio.pcm_s16le)
        return PiperAudio(
            pcm_s16le=b"".join(pcm_parts),
            sample_rate=first.sample_rate,
            num_channels=first.num_channels,
            sample_width=first.sample_width,
        ).wav_bytes()
