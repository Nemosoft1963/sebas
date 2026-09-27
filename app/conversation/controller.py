from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator

from app.conversation.interrupt import InterruptController
from app.conversation.state import ConversationState
from app.interfaces import LLM, Player, TTS
from app.memory.short_term import ShortTermMemory


LOG = logging.getLogger(__name__)
SENTENCE_END = re.compile(r"(?<=[。！？!?\n])")


class ConversationController:
    def __init__(self, llm: LLM, tts: TTS | None, player: Player | None,
                 memory: ShortTermMemory, model: str, system_prompt: str) -> None:
        self.llm, self.tts, self.player, self.memory = llm, tts, player, memory
        self.model, self.system_prompt = model, system_prompt
        self.session_id = uuid.uuid4().hex
        self.state = ConversationState.IDLE
        self.interrupt = InterruptController()
        self._active: asyncio.Task[None] | None = None

    def barge_in(self) -> None:
        if self.state in (ConversationState.THINKING, ConversationState.SPEAKING):
            self.state = ConversationState.INTERRUPTED
            self.interrupt.request()
            if self.player:
                self.player.stop()
            if self._active:
                self._active.cancel()

    async def respond(self, user_text: str, speak: bool = True) -> str:
        if self._active and not self._active.done():
            self.barge_in()
            await asyncio.gather(self._active, return_exceptions=True)
        self.interrupt.reset()
        result: list[str] = []
        started = time.perf_counter()
        first_token_at: float | None = None
        interrupted = False

        async def run() -> None:
            nonlocal first_token_at
            self.state = ConversationState.THINKING
            messages = [{"role": "system", "content": self.system_prompt}]
            messages += self.memory.recent(self.session_id)
            messages.append({"role": "user", "content": user_text})
            buffer = ""
            async for token in self.llm.stream(messages):
                self.interrupt.raise_if_requested()
                first_token_at = first_token_at or time.perf_counter()
                result.append(token)
                buffer += token
                if speak and self.tts and self.player:
                    parts = SENTENCE_END.split(buffer)
                    if len(parts) > 1:
                        for sentence in parts[:-1]:
                            if sentence.strip():
                                self.state = ConversationState.SPEAKING
                                audio, rate = await self.tts.synthesize(sentence)
                                self.interrupt.raise_if_requested()
                                await self.player.play(audio, rate)
                        buffer = parts[-1]
            if speak and buffer.strip() and self.tts and self.player:
                self.state = ConversationState.SPEAKING
                audio, rate = await self.tts.synthesize(buffer)
                await self.player.play(audio, rate)

        self._active = asyncio.create_task(run())
        try:
            await self._active
        except asyncio.CancelledError:
            interrupted = True
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            answer = "".join(result).strip()
            self.memory.save(self.session_id, user_text, answer, self.model, elapsed, interrupted)
            LOG.info("turn latency_ms=%.1f first_token_ms=%s interrupted=%s", elapsed,
                     f"{(first_token_at-started)*1000:.1f}" if first_token_at else "none", interrupted)
            self.state = ConversationState.IDLE
        return "".join(result).strip()
