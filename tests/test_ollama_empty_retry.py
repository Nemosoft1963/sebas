import pytest

from app.core import Ollama, compact_messages_for_retry


def test_retry_compaction_preserves_system_task_head_and_source_tail():
    messages = [
        {"role": "system", "content": "SYSTEM_RULES"},
        {"role": "user", "content": "TASK_HEAD " + ("middle " * 5000) + " SOURCE_TAIL"},
    ]

    compacted = compact_messages_for_retry(messages, max_chars=1200)

    assert len(compacted) == 2
    assert "SYSTEM_RULES" in compacted[0]["content"]
    assert "TASK_HEAD" in compacted[1]["content"]
    assert "SOURCE_TAIL" in compacted[1]["content"]
    assert sum(len(item["content"]) for item in compacted) <= 1300


@pytest.mark.asyncio
async def test_complete_retries_empty_and_length_limited_responses(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "1536")
    monkeypatch.setenv("OLLAMA_EMPTY_RETRY_NUM_CTX", "16384")
    monkeypatch.setenv("OLLAMA_EMPTY_RETRY_NUM_PREDICT", "6144")
    responses = [
        ("", {"done_reason": "length", "thinking_chars": 4000, "eval_count": 1536}),
        ("partial json", {"done_reason": "length", "thinking_chars": 2500, "eval_count": 3072}),
        ('{"result":"complete"}', {"done_reason": "stop", "thinking_chars": 900, "eval_count": 700}),
    ]

    class FakeOllama(Ollama):
        def __init__(self):
            super().__init__("http://local", "fake")
            self.calls = []

        async def _complete_once(self, messages, think, num_ctx, num_predict, temperature):
            self.calls.append((messages, think, num_ctx, num_predict, temperature))
            return responses.pop(0)

    llm = FakeOllama()
    result = await llm.complete([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task " + ("data " * 5000) + " end"},
    ])

    assert result == '{"result":"complete"}'
    assert [call[1] for call in llm.calls] == ["medium", "low", "low"]
    assert [call[2] for call in llm.calls] == [8192, 16384, 16384]
    assert [call[3] for call in llm.calls] == [1536, 6144, 6144]
    assert len(llm.calls[2][0][-1]["content"]) < len(llm.calls[1][0][-1]["content"])


@pytest.mark.asyncio
async def test_complete_reports_diagnostics_when_all_retries_are_empty():
    class AlwaysEmpty(Ollama):
        async def _complete_once(self, messages, think, num_ctx, num_predict, temperature):
            return "", {"done_reason": "length", "thinking_chars": num_predict, "eval_count": num_predict}

    with pytest.raises(RuntimeError, match="automatic retries") as error:
        await AlwaysEmpty("http://local", "fake").complete([
            {"role": "user", "content": "task"},
        ])

    assert "content=0" in str(error.value)
    assert "thinking=" in str(error.value)


@pytest.mark.asyncio
async def test_complete_disables_thinking_as_final_empty_response_fallback():
    responses = [
        ("", {"done_reason": "length", "thinking_chars": 1536, "eval_count": 1536}),
        ("", {"done_reason": "stop", "thinking_chars": 300, "eval_count": 300}),
        ("", {"done_reason": "stop", "thinking_chars": 200, "eval_count": 200}),
        ('{"result":"visible"}', {"done_reason": "stop", "thinking_chars": 0, "eval_count": 40}),
    ]

    class FakeOllama(Ollama):
        def __init__(self):
            super().__init__("http://local", "fake")
            self.calls = []

        async def _complete_once(self, messages, think, num_ctx, num_predict, temperature):
            self.calls.append((messages, think, temperature))
            return responses.pop(0)

    llm = FakeOllama()
    result = await llm.complete([{"role": "user", "content": "return final answer"}])

    assert result == '{"result":"visible"}'
    assert [call[1] for call in llm.calls] == ["medium", "low", "low", False]
    assert llm.calls[-1][2] == 0.0
    assert "最終回答" in llm.calls[-1][0][-1]["content"]


@pytest.mark.asyncio
async def test_complete_json_passes_schema_and_uses_low_thinking_for_gpt_oss_compatibility():
    class StructuredOllama(Ollama):
        def __init__(self):
            super().__init__("http://local", "fake")
            self.request = None

        async def _request_once(
            self, messages, think, num_ctx, num_predict, temperature,
            response_format=None,
        ):
            self.request = {
                "messages": messages,
                "think": think,
                "temperature": temperature,
                "format": response_format,
            }
            return '{"title":"ok"}', {
                "done_reason": "stop", "thinking_chars": 0, "eval_count": 1,
            }

    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }
    llm = StructuredOllama()

    result = await llm.complete_json(
        [{"role": "user", "content": "json"}], schema
    )

    assert result == '{"title":"ok"}'
    assert llm.request["format"] == schema
    assert llm.request["think"] == "low"
    assert llm.request["temperature"] == 0.0
