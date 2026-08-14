# Piper voice files

Run `scripts/setup_piper.ps1` once while connected to the internet. It downloads:

- `en_US-lessac-medium.onnx`
- `en_US-lessac-medium.onnx.json`

The files remain local and are used for guided scenario speech and full-dialogue replay.
They are intentionally excluded from the source archive because the model is about 63 MB.

The selected voice is U.S. English, single-speaker, medium quality, and 22,050 Hz. Review
the upstream [voice model card](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/lessac/medium/MODEL_CARD)
and dataset terms before redistribution. Piper 1.6.0 itself is GPL-3.0 licensed.
