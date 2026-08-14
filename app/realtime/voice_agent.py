from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    AgentStateChangedEvent,
    ConversationItemAddedEvent,
    ErrorEvent,
    TurnHandlingOptions,
    inference,
    room_io,
)
from livekit.agents.llm import ChatMessage

# Keep ALL plugin imports at module scope.
# ai_coustics registers itself during import, and LiveKit requires plugin
# registration to happen on Python's MainThread.
from livekit.plugins import ai_coustics, deepgram, silero

from app.realtime.conversation_fluency import (
    ConversationFluencyTracker,
    conversation_mode,
)
from app.realtime.guided_conversation import run_guided_conversation_session
from app.realtime.piper_tts import PiperTTS
from services.fluency.models import PracticeMode

# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv(".env.local")
load_dotenv(".env", override=False)


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    try:
        return float(raw)

    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be a number; received {raw!r}."
        ) from exc


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    try:
        return int(raw)

    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be an integer; received {raw!r}."
        ) from exc


def require_environment() -> None:
    required = (
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "DEEPGRAM_API_KEY",
    )

    missing = [
        variable
        for variable in required
        if not os.getenv(variable)
    ]

    if missing:
        raise RuntimeError(
            "Missing required values in .env.local: "
            + ", ".join(missing)
        )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
)

logger = logging.getLogger("english-tutor")

logger.info(
    "[BOOT] plugins imported on thread=%s | "
    "deepgram=yes | silero=yes | ai_coustics=yes",
    threading.current_thread().name,
)


# ============================================================
# TUTOR BEHAVIOR
# ============================================================

TUTOR_INSTRUCTIONS = """
You are a friendly English tutor in a live spoken conversation.

Conversation behavior:
- Listen carefully and answer what the learner actually asks.
- Maintain context across turns.
- Use natural spoken English.
- Keep greetings and casual replies brief, usually one or two sentences.
- For grammar, vocabulary, or deeper questions, explain the central idea
  clearly first.
- Give explanations in conversational chunks. Ask whether the learner wants
  an example or more detail instead of giving a long lecture by default.
- Give a long answer only when the learner explicitly asks for detail.
- Do not begin every answer with filler praise. Start with the useful answer.
- If the transcript is semantically strange and may contain an ASR error, ask
  one brief clarification question instead of inventing a confident meaning.
- When the learner corrects a misunderstood word, acknowledge the correction
  briefly and continue with the corrected meaning.
- Allow the learner to interrupt, correct the topic, or change direction.
- Correct grammar gently and only when useful.
- Do not correct every sentence.
- Never use ASR confidence as a pronunciation score.

Speech-output rules:
- Produce plain spoken text only.
- Do not use Markdown, asterisks, headings, bullet symbols, emojis, code
  fences, URLs, or decorative formatting.
- Write numbers and abbreviations in forms that sound natural when spoken.
"""


class EnglishTutor(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=TUTOR_INSTRUCTIONS,
        )


class FluencyTrackingTutor(EnglishTutor):
    """Preserve the tutor behavior while capturing Flux timing evidence."""

    def __init__(self, tracker: ConversationFluencyTracker) -> None:
        super().__init__()
        self.tracker = tracker

    async def stt_node(self, audio, model_settings) -> AsyncIterable[Any]:
        async for event in Agent.default.stt_node(self, audio, model_settings):
            self.tracker.observe_stt_event(event)
            yield event


# ============================================================
# PHASE 1: STREAMING ASR AND TURN DETECTION
# ============================================================

def build_stt() -> deepgram.STTv2:
    """
    Continuous Deepgram Flux ASR.

    Flux supplies partial/final transcripts and semantic/acoustic turn ending.
    No fixed-duration recording, WAV file, or static keyterm list is used.
    """

    return deepgram.STTv2(
        model="flux-general-en",

        # Let Flux start a speculative LLM request before the final turn event.
        eager_eot_threshold=env_float(
            "FLUX_EAGER_EOT_THRESHOLD",
            0.60,
        ),

        # Keep committed turn detection conservative for English learners
        # who may pause while thinking.
        eot_threshold=env_float(
            "FLUX_EOT_THRESHOLD",
            0.80,
        ),

        # Maximum forced timeout; not a mandatory delay for every turn.
        eot_timeout_ms=env_int(
            "FLUX_EOT_TIMEOUT_MS",
            7_000,
        ),
    )


def build_vad():
    """
    Silero detects overlapping learner speech for barge-in.

    Flux—not Silero—decides ordinary user-turn completion.
    """

    return silero.VAD.load(
        min_speech_duration=0.08,
        min_silence_duration=0.55,
        prefix_padding_duration=0.50,
        activation_threshold=0.50,
    )


# ============================================================
# PHASE 2: STREAMING CONVERSATIONAL LLM
# ============================================================

def build_llm() -> inference.LLM:
    """
    Restore the preferred Gemini Flash-Lite tutor.

    This uses LiveKit Inference, so it does not consume the learner's personal
    Google AI Studio free-tier key.
    """

    model = os.getenv(
        "LIVEKIT_LLM_MODEL",
        "google/gemini-2.5-flash-lite",
    )

    logger.info(
        "[LLM] model=%s",
        model,
    )

    return inference.LLM(
        model=model,
        extra_kwargs={
            "temperature": env_float(
                "LLM_TEMPERATURE",
                0.30,
            ),
            "max_completion_tokens": env_int(
                "LLM_MAX_COMPLETION_TOKENS",
                220,
            ),
        },
    )


# ============================================================
# PHASE 3: STREAMING TTS, AUDIO ENHANCEMENT, AND BARGE-IN
# ============================================================

def build_free_tts() -> deepgram.TTS:
    """
    Restore the preferred Deepgram Aura-2 Andromeda voice.

    This is a streaming TTS component. It does not create response WAV files
    and does not use pygame.
    """

    model = os.getenv(
        "DEEPGRAM_TTS_MODEL",
        "aura-2-andromeda-en",
    )

    logger.info(
        "[TTS] model=%s",
        model,
    )

    return deepgram.TTS(
        model=model,
        sample_rate=24_000,
    )


def build_tts() -> deepgram.TTS:
    """Keep assessment/free-mode callers on the existing online streaming voice."""
    return build_free_tts()


def build_guided_tts() -> PiperTTS:
    """Use a preinstalled local Piper voice for deterministic guided lines."""
    project_root = Path(__file__).resolve().parents[2]
    logger.info(
        "[TTS] provider=piper-local | voice=%s | network=no",
        os.getenv("PIPER_VOICE", "en_US-lessac-medium"),
    )
    return PiperTTS(project_root)


def build_audio_input_options() -> room_io.AudioInputOptions:
    """
    Apply ai-coustics QUAIL-L, selected by the controlled WER experiment.

    Set AUDIO_ENHANCEMENT=none only for an unprocessed debugging comparison.
    """

    mode = os.getenv(
        "AUDIO_ENHANCEMENT",
        "quail_l",
    ).strip().lower()

    if mode in {
        "",
        "none",
        "off",
        "false",
        "disabled",
    }:
        logger.info(
            "[AUDIO] ai-coustics enhancement disabled"
        )

        return room_io.AudioInputOptions()

    if mode != "quail_l":
        raise RuntimeError(
            "AUDIO_ENHANCEMENT must be 'quail_l' or 'none'; "
            f"received {mode!r}."
        )

    level_text = os.getenv(
        "AUDIO_ENHANCEMENT_LEVEL",
        "",
    ).strip()

    if not level_text:
        enhancer = ai_coustics.audio_enhancement(
            model=ai_coustics.EnhancerModel.QUAIL_L,
        )

        logger.info(
            "[AUDIO] ai-coustics QUAIL-L enabled | "
            "enhancement-level=plugin-default"
        )

    else:
        level = env_float(
            "AUDIO_ENHANCEMENT_LEVEL",
            0.80,
        )

        if not 0.0 <= level <= 1.0:
            raise RuntimeError(
                "AUDIO_ENHANCEMENT_LEVEL must be between 0.0 and 1.0."
            )

        enhancer = ai_coustics.audio_enhancement(
            model=ai_coustics.EnhancerModel.QUAIL_L,
            model_parameters=ai_coustics.ModelParameters(
                enhancement_level=level,
            ),
        )

        logger.info(
            "[AUDIO] ai-coustics QUAIL-L enabled | "
            "enhancement-level=%.2f",
            level,
        )

    return room_io.AudioInputOptions(
        noise_cancellation=enhancer,
    )


# ============================================================
# OBSERVABILITY
# ============================================================

def metric_value(
    metrics: Any,
    key: str,
) -> str:

    if not metrics:
        return "n/a"

    try:
        value = metrics.get(key)

    except (AttributeError, TypeError):
        return "n/a"

    if value is None:
        return "n/a"

    try:
        return f"{float(value):.3f}s"

    except (TypeError, ValueError):
        return str(value)


def add_observability(
    session: AgentSession,
    tracker: ConversationFluencyTracker | None = None,
) -> None:

    @session.on("conversation_item_added")
    def on_conversation_item_added(
        event: ConversationItemAddedEvent,
    ) -> None:

        item = event.item

        if not isinstance(
            item,
            ChatMessage,
        ):
            return

        metrics = getattr(
            item,
            "metrics",
            None,
        )

        if item.role == "user":
            content = getattr(item, "text_content", "")
            if callable(content):
                content = content()
            transcript = str(content or "").strip()
            logger.info(
                "[USER TURN] text=%r | "
                "transcription=%s | end-of-turn=%s",
                    transcript,
                metric_value(
                    metrics,
                    "transcription_delay",
                ),
                metric_value(
                    metrics,
                    "end_of_turn_delay",
                ),
            )
            if tracker is not None and transcript:
                turn_id = str(
                    getattr(item, "id", None)
                    or getattr(item, "item_id", None)
                    or f"turn-{threading.get_ident()}-{id(item)}"
                )
                asyncio.create_task(tracker.submit_turn(transcript, turn_id))

        elif item.role == "assistant":
            logger.info(
                "[ASSISTANT TURN] "
                "LLM-first-token=%s | "
                "TTS-first-audio=%s | "
                "E2E-first-response=%s | "
                "interrupted=%s",
                metric_value(
                    metrics,
                    "llm_node_ttft",
                ),
                metric_value(
                    metrics,
                    "tts_node_ttfb",
                ),
                metric_value(
                    metrics,
                    "e2e_latency",
                ),
                getattr(
                    item,
                    "interrupted",
                    False,
                ),
            )

    @session.on("agent_state_changed")
    def on_agent_state_changed(
        event: AgentStateChangedEvent,
    ) -> None:

        logger.info(
            "[STATE] %s -> %s",
            event.old_state,
            event.new_state,
        )

    @session.on("error")
    def on_error(
        event: ErrorEvent,
    ) -> None:

        logger.error(
            "[SESSION ERROR] "
            "source=%s | recoverable=%s | error=%s",
            type(
                getattr(
                    event,
                    "source",
                    object(),
                )
            ).__name__,
            getattr(
                event,
                "recoverable",
                "unknown",
            ),
            getattr(
                event,
                "error",
                event,
            ),
        )


# ============================================================
# REAL-TIME AGENT
# ============================================================

server = AgentServer()


@server.rtc_session(
    agent_name="english-tutor",
)
async def english_tutor_session(
    ctx: agents.JobContext,
) -> None:

    require_environment()

    logger.info(
        "[SESSION] job received | room=%s | thread=%s",
        ctx.room.name,
        threading.current_thread().name,
    )

    mode = conversation_mode(ctx)
    stt = build_stt()
    vad = build_vad()
    if mode == PracticeMode.GUIDED:
        await run_guided_conversation_session(
            ctx,
            stt=stt,
            vad=vad,
            tts=build_guided_tts(),
            audio_input_options=build_audio_input_options(),
            aec_warmup_duration=env_float("AEC_WARMUP_SECONDS", 1.0),
        )
        return

    llm = build_llm()
    tts = build_free_tts()
    fluency_tracker = ConversationFluencyTracker(
        session_id=ctx.room.name,
        mode=PracticeMode.FREE,
    )

    session = AgentSession(
        stt=stt,
        vad=vad,
        llm=llm,
        tts=tts,

        # Prevent visual formatting symbols from being spoken.
        tts_text_transforms=[
            "filter_markdown",
            "filter_emoji",
        ],

        turn_handling=TurnHandlingOptions(
            # Deepgram Flux owns ordinary turn completion.
            turn_detection="stt",

            # Do not add a large second delay after Flux has finalized.
            endpointing={
                "mode": "fixed",
                "min_delay": 0.0,
                "max_delay": 1.0,
            },

            # True barge-in: learner speech stops assistant playback.
            interruption={
                "enabled": True,
                "mode": "vad",
                "min_duration": 0.40,
                "min_words": 0,
                "false_interruption_timeout": 1.50,
                "resume_false_interruption": True,
            },

            # Flux EagerEndOfTurn starts only the LLM early.
            # TTS waits for the committed turn, avoiding speculative audio.
            preemptive_generation={
                "enabled": True,
                "preemptive_tts": False,
                "max_speech_duration": 12.0,
                "max_retries": 1,
            },
        ),

        # During this brief period assistant echo cannot trigger an
        # interruption. One second matches the agreed headphone/WebRTC setup.
        aec_warmup_duration=env_float(
            "AEC_WARMUP_SECONDS",
            1.0,
        ),

        # Keep a quiet learner in the conversation indefinitely.
        user_away_timeout=None,
    )

    add_observability(session, fluency_tracker)

    @session.on("user_state_changed")
    def on_user_state_changed(event) -> None:
        if str(event.new_state).lower().endswith("speaking"):
            fluency_tracker.mark_user_speaking()

    await session.start(
        room=ctx.room,
        agent=FluencyTrackingTutor(fluency_tracker),
        room_options=room_io.RoomOptions(
            audio_input=build_audio_input_options(),
        ),
    )

    logger.info(
        "[SESSION] ready and listening | "
        "no generated startup greeting"
    )

    # AgentSession now owns:
    # - continuous WebRTC/console input;
    # - Flux streaming ASR;
    # - conversation history;
    # - Gemini streaming generation;
    # - Aura-2 streaming output;
    # - speaking/listening handoff;
    # - barge-in cancellation and interruption retention.


if __name__ == "__main__":
    agents.cli.run_app(
        server
    )
