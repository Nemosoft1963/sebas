from __future__ import annotations

import asyncio

import numpy as np
import sounddevice as sd


class Speaker:
    async def play(self, audio: np.ndarray, sample_rate: int) -> None:
        await asyncio.to_thread(self._play_blocking, audio, sample_rate)

    @staticmethod
    def _play_blocking(audio: np.ndarray, sample_rate: int) -> None:
        sd.play(audio, sample_rate, blocking=True)

    def stop(self) -> None:
        sd.stop()
