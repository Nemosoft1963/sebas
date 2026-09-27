from __future__ import annotations

import asyncio

import numpy as np


class WhisperSTT:
    def __init__(self, model_name: str = "large-v3-turbo") -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper is not installed") from exc
        self.model = WhisperModel(model_name, device="cuda", compute_type="float16")

    async def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        del sample_rate
        def run() -> str:
            segments, _ = self.model.transcribe(audio, language="ja", vad_filter=False)
            return "".join(item.text for item in segments).strip()
        return await asyncio.to_thread(run)
