import pytest

from app import external_ai
from app.external_ai import ProviderHTTPError, ProviderResponse


@pytest.mark.asyncio
async def test_openai_uses_next_model_when_primary_is_unavailable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-sol,gpt-5.2")
    called_models = []

    async def fake_post(url, *, headers, payload):
        del url, headers
        called_models.append(payload["model"])
        if payload["model"] == "gpt-5.6-sol":
            raise ProviderHTTPError(404, "model is not available for this project")
        return {"output_text": "OK"}

    monkeypatch.setattr(external_ai, "_post_json", fake_post)
    response = await external_ai.call_provider_with_metadata("chatgpt", "test", "system")
    assert response == ProviderResponse(text="OK", model="gpt-5.2")
    assert called_models == ["gpt-5.6-sol", "gpt-5.2"]


@pytest.mark.asyncio
async def test_research_and_synthesis_receive_same_front_context(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-sol")
    prompts = []

    async def fake_call(provider_id, prompt, system, max_tokens=2200, reasoning_effort=None):
        del provider_id, system, max_tokens, reasoning_effort
        prompts.append(prompt)
        return ProviderResponse(text="調査結果", model="gpt-5.6-sol")

    monkeypatch.setattr(external_ai, "call_provider_with_metadata", fake_call)
    result = await external_ai.run_research(
        "続きの調査",
        ["chatgpt"],
        "chatgpt",
        "report",
        "ユーザー: 共有すべき前提\n\nフロントAI: 承知しました",
    )

    assert result["synthesis"] == "調査結果"
    assert len(prompts) == 2
    assert all("共有すべき前提" in prompt for prompt in prompts)
