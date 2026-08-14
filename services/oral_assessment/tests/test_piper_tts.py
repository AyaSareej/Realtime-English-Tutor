from __future__ import annotations

import sys
import tempfile
import types
import unittest
import wave
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from services.local_tts import PiperSynthesizer


class FakeSynthesisConfig:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class FakeChunk:
    sample_rate = 22_050
    sample_channels = 1
    sample_width = 2
    audio_int16_bytes = b"\x01\x00" * 2_205


class FakeVoice:
    @classmethod
    def load(cls, _model_path: str):
        return cls()

    def synthesize(self, _text: str, *, syn_config):
        assert isinstance(syn_config, FakeSynthesisConfig)
        yield FakeChunk()


class PiperSynthesizerTests(unittest.TestCase):
    def test_local_pcm_and_dialogue_wav(self) -> None:
        with tempfile.TemporaryDirectory() as temp_directory:
            root = Path(temp_directory)
            voice_directory = root / "data" / "piper"
            voice_directory.mkdir(parents=True)
            model = voice_directory / "en_US-lessac-medium.onnx"
            model.write_bytes(b"fake-onnx")
            Path(f"{model}.json").write_text("{}", encoding="utf-8")
            fake_piper = types.SimpleNamespace(
                PiperVoice=FakeVoice,
                SynthesisConfig=FakeSynthesisConfig,
            )
            with patch.dict(sys.modules, {"piper": fake_piper}):
                synthesizer = PiperSynthesizer(root)
                audio = synthesizer.synthesize_pcm("Good morning")
                self.assertEqual(22_050, audio.sample_rate)
                self.assertEqual(2, audio.sample_width)
                dialogue = synthesizer.synthesize_dialogue_wav(
                    [("Good morning", 1.0), ("Hello", 1.06)],
                    pause_seconds=0.1,
                )
            with wave.open(BytesIO(dialogue), "rb") as wav_file:
                self.assertEqual(22_050, wav_file.getframerate())
                self.assertEqual(1, wav_file.getnchannels())
                self.assertGreater(wav_file.getnframes(), 4_410)


if __name__ == "__main__":
    unittest.main()
