import asyncio

from app.conversation.controller import ConversationController
from app.memory.short_term import ShortTermMemory


class FakeLLM:
    async def stream(self, messages):
        del messages
        for token in ["はい。", "承知しました。"]:
            await asyncio.sleep(0)
            yield token


async def test_controller_stream_and_store(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    controller = ConversationController(FakeLLM(), None, None, memory, "fake", "system")
    assert await controller.respond("テスト", speak=False) == "はい。承知しました。"
    assert len(memory.recent(controller.session_id)) == 2
