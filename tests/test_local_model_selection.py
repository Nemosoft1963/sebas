from pathlib import Path

import pytest

from app.core import Ollama
from app import web


@pytest.mark.asyncio
async def test_thinking_is_disabled_for_incompatible_model(monkeypatch):
    llm = Ollama("http://ollama", "mistral-small")

    async def completion_only(refresh=False):
        del refresh
        return {"completion", "tools"}

    monkeypatch.setattr(llm, "capabilities", completion_only)

    assert await llm.effective_think("high") is False
    assert await llm.effective_think(True) is False


@pytest.mark.asyncio
async def test_thinking_level_is_kept_for_supported_model(monkeypatch):
    llm = Ollama("http://ollama", "qwen-thinking")

    async def supports_thinking(refresh=False):
        del refresh
        return {"completion", "thinking"}

    monkeypatch.setattr(llm, "capabilities", supports_thinking)

    assert await llm.effective_think("medium") == "medium"


def test_switching_model_invalidates_capability_cache():
    llm = Ollama("http://ollama", "first")
    llm._capabilities_model = "first"
    llm._capabilities = {"completion", "thinking"}

    llm.set_model("second")

    assert llm.model == "second"
    assert llm._capabilities_model is None
    assert llm._capabilities == set()


def test_local_model_selection_is_persisted(tmp_path, monkeypatch):
    settings = tmp_path / "memory" / "local_model.json"
    monkeypatch.setattr(web, "MODEL_SETTINGS_PATH", settings)

    web.save_local_model_selection("qwen3.5:9b")

    assert web.load_local_model_selection("fallback") == "qwen3.5:9b"


def test_local_model_ui_and_api_are_wired():
    root = Path(__file__).resolve().parents[1]
    html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
    script = (root / "app" / "static" / "local_models.js").read_text(encoding="utf-8")

    assert "/static/local_models.js" in html
    assert "/api/local-models" in script
    assert "/api/local-models/selected" in script
    assert "ローカルLLM" in script
