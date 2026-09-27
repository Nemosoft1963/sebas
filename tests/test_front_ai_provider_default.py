"""FRONT_AI_PROVIDER のコード既定が compose / .env.example と同じ 'ollama' であることの検証。

app.web はモジュール読み込み時に環境変数から定数を決めるため、既定値の検証は
環境変数を外した子プロセスで実際に import して確認する(静的な文字列照合ではない)。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def import_web(tmp_path, front_ai_provider=None):
    """FRONT_AI_PROVIDER を制御した子プロセスで app.web を import し、結果を返す。"""
    env = dict(os.environ)
    env.pop("FRONT_AI_PROVIDER", None)
    if front_ai_provider is not None:
        env["FRONT_AI_PROVIDER"] = front_ai_provider
    env["DATA_DIR"] = str(tmp_path / "data")
    env["WORKSPACE_ROOT"] = str(tmp_path / "workspace")
    env["PYTHONPATH"] = str(ROOT)
    code = (
        "import app.web as web\n"
        "provider = web.front_provider()\n"
        "print(web.FRONT_AI_PROVIDER)\n"
        "print('None' if provider is None else provider.id)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120,
    )
    assert result.returncode == 0, result.stderr
    value, provider_id = result.stdout.strip().splitlines()[-2:]
    return value, provider_id


def test_default_front_ai_provider_is_ollama(tmp_path):
    """環境変数が未設定なら既定は 'ollama'(外部AIプロバイダは選ばれない)。"""
    value, provider_id = import_web(tmp_path)
    assert value == "ollama"
    assert provider_id == "None"


def test_explicit_provider_is_still_normalized(tmp_path):
    """明示指定時の正規化(前後空白除去・小文字化)と PROVIDERS 参照は従来どおり。"""
    value, provider_id = import_web(tmp_path, front_ai_provider="  ChatGPT  ")
    assert value == "chatgpt"
    assert provider_id == "chatgpt"


def test_ollama_is_not_an_external_provider():
    """'ollama' は PROVIDERS に無いため front_provider() は None になり、外部AIは呼ばれない。"""
    from app import web

    assert "ollama" not in web.PROVIDERS


def test_code_default_matches_compose_and_env_example():
    """コード既定と docker-compose.yml / .env.example の既定が一致していること。"""
    web_source = (ROOT / "app" / "web.py").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert 'os.getenv("FRONT_AI_PROVIDER", "ollama")' in web_source
    assert "FRONT_AI_PROVIDER: ${FRONT_AI_PROVIDER:-ollama}" in compose
    assert "FRONT_AI_PROVIDER=ollama" in env_example
