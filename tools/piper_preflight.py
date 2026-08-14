from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from services.local_tts import PiperConfigurationError, PiperSynthesizer


def main() -> int:
    try:
        synthesizer = PiperSynthesizer(PROJECT_ROOT)
        audio = synthesizer.synthesize_pcm("Piper guided speech is ready.")
    except (PiperConfigurationError, RuntimeError, ValueError) as exc:
        print(f"Piper preflight failed: {exc}")
        return 1
    duration = len(audio.pcm_s16le) / (
        audio.sample_rate * audio.num_channels * audio.sample_width
    )
    if duration <= 0.1:
        print("Piper preflight failed: generated audio was unexpectedly short")
        return 1
    print(
        "Piper preflight passed: "
        f"voice={synthesizer.model_name}, sample_rate={audio.sample_rate}, "
        f"duration={duration:.2f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
