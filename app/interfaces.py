from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

import numpy as np


class STT(Protocol):
    async def transcribe(self, audio: np.ndarray, sample_rate: int) -> str: ...


class LLM(Protocol):
    def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]: ...


class TTS(Protocol):
    async def synthesize(self, text: str) -> tuple[np.ndarray, int]: ...


class Player(Protocol):
    async def play(self, audio: np.ndarray, sample_rate: int) -> None: ...
    def stop(self) -> None: ...
