from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict[str, Any]:
    with (ROOT / "config" / name).open(encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


@dataclass(slots=True)
class Settings:
    llm_url: str
    llm_model: str
    system_prompt: str
    asr_backend: str
    asr_model: str
    tts_backend: str
    tts_model: str
    tts_speaker: str
    sample_rate: int
    block_ms: int
    speech_threshold: float
    end_silence_ms: int
    min_speech_ms: int
    database: Path
    log_dir: Path

    @classmethod
    def load(cls) -> "Settings":
        models, audio, system = _load("models.yaml"), _load("audio.yaml"), _load("system.yaml")
        return cls(
            llm_url=models["llm"]["url"], llm_model=models["llm"]["model"],
            system_prompt=system["conversation"]["system_prompt"],
            asr_backend=models["asr"]["backend"], asr_model=models["asr"]["model"],
            tts_backend=models["tts"]["backend"], tts_model=models["tts"]["model"],
            tts_speaker=models["tts"]["speaker"], sample_rate=audio["sample_rate"],
            block_ms=audio["block_ms"], speech_threshold=audio["vad"]["threshold"],
            end_silence_ms=audio["vad"]["end_silence_ms"],
            min_speech_ms=audio["vad"]["min_speech_ms"],
            database=ROOT / system["storage"]["database"], log_dir=ROOT / system["storage"]["log_dir"],
        )
