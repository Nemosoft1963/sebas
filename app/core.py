import asyncio
import json
import os
import sqlite3
import time
import uuid
from enum import Enum

import httpx

from app.recovery_policy import CURRENT_ATTEMPT


class State(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"


class Memory:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS turns(id INTEGER PRIMARY KEY,session_id TEXT,created_at TEXT,user_text TEXT,assistant_text TEXT,model TEXT,latency_ms REAL,interrupted INTEGER)")

    def recent(self, sid, n=8):
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT user_text,assistant_text FROM turns WHERE session_id=? ORDER BY id DESC LIMIT ?", (sid, n)).fetchall()
        return [item for user, assistant in reversed(rows) for item in ({"role": "user", "content": user}, {"role": "assistant", "content": assistant})]

    def save(self, sid, user, assistant, model, elapsed_ms, interrupted):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO turns(session_id,created_at,user_text,assistant_text,model,latency_ms,interrupted) VALUES(?,datetime('now'),?,?,?,?,?)", (sid, user, assistant, model, elapsed_ms, int(interrupted)))


def ollama_think_value():
    value = os.getenv("OLLAMA_THINK", "medium").strip().lower()
    if value in {"false", "off", "0", "no"}:
        return False
    if value in {"true", "on", "1", "yes"}:
        return True
    return value


def compact_messages_for_retry(messages, max_chars=16000):
    """Keep task identity and source tail while reducing an oversized retry prompt."""
    copied = [
        {"role": str(item.get("role", "user")), "content": str(item.get("content", ""))}
        for item in messages
    ]
    if sum(len(item["content"]) for item in copied) <= max_chars:
        return copied
    system = next((item for item in copied if item["role"] == "system"), None)
    last = copied[-1] if copied else {"role": "user", "content": ""}
    system_budget = min(3000, max_chars // 5) if system and system is not last else 0
    system_item = (
        {"role": "system", "content": system["content"][:system_budget]}
        if system_budget else None
    )
    available = max(1000, max_chars - system_budget)
    head = available * 2 // 5
    tail = available - head
    content = last["content"]
    if len(content) > available:
        content = content[:head] + "\n[... retry context compacted ...]\n" + content[-tail:]
    result = []
    if system_item:
        result.append(system_item)
    result.append({"role": last["role"], "content": content})
    return result


class Ollama:
    def __init__(self, url, model):
        self.url = url.rstrip("/")
        self.model = model
        self._capabilities_model = None
        self._capabilities = set()

    def set_model(self, model):
        """Switch the active model and invalidate model-specific metadata."""
        self.model = model
        self._capabilities_model = None
        self._capabilities = set()

    async def capabilities(self, refresh=False):
        if not refresh and self._capabilities_model == self.model:
            return set(self._capabilities)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    self.url + "/api/show", json={"model": self.model}
                )
                response.raise_for_status()
            capabilities = {
                str(item).strip().lower()
                for item in response.json().get("capabilities", [])
                if str(item).strip()
            }
        except (httpx.HTTPError, ValueError, TypeError):
            capabilities = set()
        self._capabilities_model = self.model
        self._capabilities = capabilities
        return set(capabilities)

    async def effective_think(self, requested):
        """Disable Ollama thinking for models that do not advertise support."""
        if requested is False or requested is None:
            return False
        capabilities = await self.capabilities()
        return requested if "thinking" in capabilities else False

    async def health(self):
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                return (await client.get(self.url + "/api/tags")).is_success
        except httpx.HTTPError:
            return False

    async def _request_once(
        self, messages, think, num_ctx, num_predict, temperature,
        response_format=None,
    ):
        effective_think = await self.effective_think(think)
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": effective_think,
            "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
            "options": {
                "num_ctx": num_ctx,
                "num_predict": num_predict,
                "temperature": temperature,
            },
        }
        if response_format is not None:
            payload["format"] = response_format
        timeout = float(os.getenv("OLLAMA_TIMEOUT", "300"))
        attempt = CURRENT_ATTEMPT.get()
        call_id = attempt.before_send(payload) if attempt else None
        if attempt and attempt.mode == "enforce":
            timeout = min(timeout, max(0.1, attempt.budget.remaining()))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(self.url + "/api/chat", json=payload)
            if attempt:
                attempt.response(call_id, status=response.status_code)
            response.raise_for_status()
        data = response.json()
        if attempt:
            attempt.response(call_id, reason=str(data.get("done_reason", "")))
        message = data.get("message") or {}
        return str(message.get("content", "")).strip(), {
            "done_reason": str(data.get("done_reason", "")),
            "thinking_chars": len(str(message.get("thinking", ""))),
            "eval_count": int(data.get("eval_count") or 0),
        }

    async def _complete_once(self, messages, think, num_ctx, num_predict, temperature):
        return await self._request_once(
            messages, think, num_ctx, num_predict, temperature
        )

    async def complete(self, messages):
        """Return a complete answer, retrying thought-only or length-limited replies."""
        configured_think = ollama_think_value()
        base_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
        base_predict = int(os.getenv("OLLAMA_NUM_PREDICT", "1536"))
        base_temperature = float(os.getenv("OLLAMA_TEMPERATURE", "0.2"))
        retry_predict = max(
            base_predict, int(os.getenv("OLLAMA_EMPTY_RETRY_NUM_PREDICT", "6144"))
        )
        retry_ctx = max(
            base_ctx, int(os.getenv("OLLAMA_EMPTY_RETRY_NUM_CTX", "16384"))
        )
        retry_chars = int(os.getenv("OLLAMA_EMPTY_RETRY_MAX_CHARS", "16000"))
        attempts = [
            (messages, configured_think, base_ctx, base_predict, base_temperature),
            (messages, "low", retry_ctx, retry_predict, min(base_temperature, 0.05)),
            (
                compact_messages_for_retry(messages, retry_chars),
                "low", retry_ctx, retry_predict, min(base_temperature, 0.05),
            ),
            (
                compact_messages_for_retry(messages, retry_chars) + [{
                    "role": "user",
                    "content": "回答本文を空にせず、思考過程を除いた最終回答だけを今すぐ返してください。",
                }],
                False, retry_ctx, retry_predict, 0.0,
            ),
        ]
        diagnostics = []
        for attempt_messages, think, num_ctx, num_predict, temperature in attempts:
            content, meta = await self._complete_once(
                attempt_messages, think, num_ctx, num_predict, temperature
            )
            diagnostics.append(
                f"think={think}, ctx={num_ctx}, predict={num_predict}, "
                f"reason={meta['done_reason'] or 'unknown'}, "
                f"content={len(content)}, thinking={meta['thinking_chars']}"
            )
            if content and meta["done_reason"] != "length":
                return content
        raise RuntimeError(
            "Ollama returned no complete answer after automatic retries: "
            + " | ".join(diagnostics)
        )

    async def complete_json(self, messages, schema):
        """Generate schema-constrained JSON and return only the final content."""
        base_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
        base_predict = int(os.getenv("OLLAMA_NUM_PREDICT", "1536"))
        retry_ctx = max(
            base_ctx, int(os.getenv("OLLAMA_EMPTY_RETRY_NUM_CTX", "16384"))
        )
        retry_predict = max(
            base_predict, int(os.getenv("OLLAMA_EMPTY_RETRY_NUM_PREDICT", "6144"))
        )
        # Schema-constrained calls need final JSON, not a reasoning transcript.
        # Some thinking models can consume the entire prediction budget in the
        # hidden thinking field and return an empty JSON body.
        attempts = [
            (messages, False, base_ctx, base_predict),
            (compact_messages_for_retry(messages), False, retry_ctx, retry_predict),
        ]
        diagnostics = []
        for attempt_messages, think, num_ctx, num_predict in attempts:
            content, meta = await self._request_once(
                attempt_messages, think, num_ctx, num_predict, 0.0, schema
            )
            diagnostics.append(
                f"think={think}, ctx={num_ctx}, predict={num_predict}, "
                f"reason={meta['done_reason'] or 'unknown'}, content={len(content)}"
            )
            if content and meta["done_reason"] != "length":
                return content
        raise RuntimeError(
            "Ollama returned no complete structured answer: "
            + " | ".join(diagnostics)
        )

    async def stream(self, messages):
        effective_think = await self.effective_think(ollama_think_value())
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": effective_think,
            "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
            "options": {
                "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192")),
                "num_predict": int(os.getenv("OLLAMA_NUM_PREDICT", "1536")),
                "temperature": float(os.getenv("OLLAMA_TEMPERATURE", "0.2")),
            },
        }
        timeout = float(os.getenv("OLLAMA_TIMEOUT", "300"))
        attempt = CURRENT_ATTEMPT.get()
        call_id = attempt.before_send(payload) if attempt else None
        if attempt and attempt.mode == 'enforce':
            timeout = min(timeout, max(0.1, attempt.budget.remaining()))
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", self.url + "/api/chat", json=payload) as response:
                if attempt:
                    attempt.response(call_id, status=response.status_code)
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    item = json.loads(line)
                    content = item.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if item.get("done"):
                        if attempt:
                            attempt.response(call_id, reason=str(item.get("done_reason", "")))
                        break


class Controller:
    def __init__(self, llm, memory, prompt, tts=None, player=None):
        self.llm, self.memory, self.prompt, self.tts, self.player = llm, memory, prompt, tts, player
        self.session_id = uuid.uuid4().hex
        self.state = State.IDLE
        self.task = None

    def barge_in(self):
        if self.state in (State.THINKING, State.SPEAKING):
            self.state = State.INTERRUPTED
            if self.player:
                self.player.stop()
            if self.task:
                self.task.cancel()

    async def respond(self, user, speak=True):
        if self.task and not self.task.done():
            self.barge_in()
            await asyncio.gather(self.task, return_exceptions=True)
        output = []
        started = time.perf_counter()
        interrupted = False

        async def run():
            self.state = State.THINKING
            buffer = ""
            messages = [{"role": "system", "content": self.prompt}] + self.memory.recent(self.session_id) + [{"role": "user", "content": user}]
            async for token in self.llm.stream(messages):
                output.append(token)
                buffer += token
                if speak and self.tts and self.player and any(buffer.endswith(x) for x in "。！？!?\n"):
                    self.state = State.SPEAKING
                    audio, rate = await self.tts.synthesize(buffer)
                    buffer = ""
                    await self.player.play(audio, rate)
            if speak and buffer and self.tts and self.player:
                audio, rate = await self.tts.synthesize(buffer)
                await self.player.play(audio, rate)

        self.task = asyncio.create_task(run())
        try:
            await self.task
        except asyncio.CancelledError:
            interrupted = True
        finally:
            self.memory.save(self.session_id, user, "".join(output), self.llm.model, (time.perf_counter() - started) * 1000, interrupted)
            self.state = State.IDLE
        return "".join(output)
