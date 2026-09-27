from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np
import sounddevice as sd


class Microphone:
    def __init__(self, sample_rate: int, block_ms: int, device: int | None = None) -> None:
        self.sample_rate = sample_rate
        self.frames = sample_rate * block_ms // 1000
        self.device = device

    async def chunks(self) -> AsyncIterator[np.ndarray]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)

        def callback(indata, frames, time, status):
            del frames, time
            if status:
                return
            data = indata[:, 0].copy()
            loop.call_soon_threadsafe(self._put, queue, data)

        with sd.InputStream(samplerate=self.sample_rate, channels=1, dtype="float32",
                            blocksize=self.frames, device=self.device, callback=callback):
            while True:
                yield await queue.get()

    @staticmethod
    def _put(queue: asyncio.Queue[np.ndarray], data: np.ndarray) -> None:
        if not queue.full():
            queue.put_nowait(data)
