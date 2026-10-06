from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import httpx


def _think_value():
    value = os.getenv("OLLAMA_THINK", "medium").strip().lower()
    if value in {"false", "off", "0", "no"}:
        return False
    if value in {"true", "on", "1", "yes"}:
        return True
    return value


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: float = 300) -> None:
        self.url = base_url.rstrip("/") + "/api/chat"
        self.model = model
        self.timeout = timeout

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": _think_value(),
            "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
            "options": {
                "temperature": float(os.getenv("OLLAMA_TEMPERATURE", "0.2")),
                "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192")),
                "num_predict": int(os.getenv("OLLAMA_NUM_PREDICT", "1536")),
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", self.url, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    item = json.loads(line)
                    content = item.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if item.get("done"):
                        break

    async def health(self) -> bool:
        root = self.url.removesuffix("/api/chat")
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                return (await client.get(root + "/api/tags")).is_success
        except httpx.HTTPError:
            return False