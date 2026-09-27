from __future__ import annotations

import asyncio

import numpy as np


class QwenTTS:
    def __init__(self, model_name: str, speaker: str) -> None:
        try:
            import torch
            from qwen_tts import Qwen3TTSModel
        except ImportError as exc:
            raise RuntimeError("Qwen TTS is not installed. Run scripts/setup-models.ps1 -TTS") from exc
        self.speaker = speaker
        self.model = Qwen3TTSModel.from_pretrained(
            model_name, device_map="cuda:0", dtype=torch.bfloat16,
            attn_implementation="sdpa",
        )

    async def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        wavs, rate = await asyncio.to_thread(
            self.model.generate_custom_voice, text=text, language="Japanese", speaker=self.speaker,
            instruct="自然で落ち着いた声で、簡潔に話してください。",
        )
        return np.asarray(wavs[0], dtype=np.float32), int(rate)
