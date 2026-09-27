from __future__ import annotations

import numpy as np


class EnergyVAD:
    """Low-overhead fallback VAD. Threshold is normalized RMS amplitude."""
    def __init__(self, threshold: float = 0.015) -> None:
        self.threshold = threshold

    def is_speech(self, audio: np.ndarray) -> bool:
        if audio.size == 0:
            return False
        return float(np.sqrt(np.mean(np.square(audio.astype(np.float32))))) >= self.threshold
