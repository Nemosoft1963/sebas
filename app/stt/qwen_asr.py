from __future__ import annotations

import asyncio

import numpy as np


class QwenASR:
    def __init__(self, model_name: str) -> None:
        try:
            import torch
            from qwen_asr import Qwen3ASRModel
        except ImportError as exc:
            raise RuntimeError("Qwen ASR is not installed. Run scripts/setup-models.ps1 -ASR") from exc
        self.model = Qwen3ASRModel.from_pretrained(
            model_name, dtype=torch.bfloat16, device_map="cuda", max_new_tokens=256,
        )

    async def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        result = await asyncio.to_thread(self.model.transcribe, audio=(audio, sample_rate), language="Japanese")
        first = result[0] if isinstance(result, list) else result
        return str(getattr(first, "text", first)).strip()
