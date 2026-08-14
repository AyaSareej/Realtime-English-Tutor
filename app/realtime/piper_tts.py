from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from livekit.agents import APIConnectionError, APIConnectOptions, tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from services.local_tts import PiperSynthesizer


class PiperTTS(tts.TTS):
    """Non-networked Piper adapter for LiveKit's TTS pipeline."""

    def __init__(self, project_root: Path) -> None:
        self.engine = PiperSynthesizer(project_root)
        # Piper voices declare their sample rate in the model. Lessac medium is
        # 22,050 Hz; the first synthesis is still authoritative at emission time.
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=22_050,
            num_channels=1,
        )

    @property
    def model(self) -> str:
        return self.engine.model_name

    @property
    def provider(self) -> str:
        return "piper-local"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> PiperChunkedStream:
        return PiperChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        return None


class PiperChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: PiperTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._piper_tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        try:
            audio = await asyncio.to_thread(
                self._piper_tts.engine.synthesize_pcm,
                self._input_text,
            )
        except Exception as exc:
            raise APIConnectionError(f"Local Piper synthesis failed: {exc}") from exc
        output_emitter.initialize(
            request_id=f"piper-{uuid.uuid4()}",
            sample_rate=audio.sample_rate,
            num_channels=audio.num_channels,
            mime_type="audio/pcm",
        )
        output_emitter.push(audio.pcm_s16le)
        output_emitter.flush()
