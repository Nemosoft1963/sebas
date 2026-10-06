"""L4 秘密混入検査の検出漏れ回帰テスト。外部AIは必ずスタブ。実通信なし。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.experience_store import canonical
from app.memory.short_term import ShortTermMemory
from app.structured_planning import compile_plan, compile_task
from app.workspace_files import WorkspaceSandbox


class Manager(SimpleNamespace):
    pass


# 各形式の架空値(実在の秘密ではない)。
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
AUTH_BASIC = "Authorization: Basic dXNlcjpwYXNzd29yZA=="
PW_EQ = "password=Sup3rS3cret!"
PW_JA = "パスワード: Sup3rS3cret"
PEM_KEY = "-----BEGIN RSA PRIVATE KEY-----"
CARD = "4111 1111 1111 1111"
NUM12 = "123456789012"
MYNUMBER = "マイナンバー 123456789012"
KOJIN = "個人番号 123456789012"
KOUZA = "口座番号 1234567"
GHP = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXabcdef"
GITHUB_PAT = "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
SLACK = "xoxb-ABCDEFGHIJLMNOPQRSTUVWX"
STRIPE_SK = "sk" + "_live_" + "ABCDEFGHIJKLMNOPQRSTUVWX"
STRIPE_RK = "rk" + "_live_" + "ABCDEFGHIJKLMNOPQRSTUVWX"
STRIPE_PK = "pk" + "_live_" + "ABCDEFGHIJKLMNOPQRSTUVWX"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"
CRED_URL = "https://user:Sup3rS3cret@example.invalid/path"
SK_ANT = "sk-ant-ABCDEFGHIJKLMNOP"
# 回帰用: 既存パターン
FAKE_SK = "sk-fake0123456789abcdef"
FAKE_MAIL = "kaku-test-001@example.invalid"
FAKE_PHONE = "090-0000-0001"

SECRET_CASES = [
    AWS_KEY, AUTH_BASIC, PW_EQ, PW_JA, PEM_KEY, CARD, NUM12, MYNUMBER, KOJIN, KOUZA,
    GHP, GITHUB_PAT, SLACK, STRIPE_SK, STRIPE_RK, STRIPE_PK, JWT, CRED_URL, SK_ANT,
    FAKE_SK, FAKE_MAIL, FAKE_PHONE,
]

HARMLESS = [
    "目標: 販売活動の成果を測定し、報告書を作成する(3工程)",
    "SC01〜SC18の達成条件を満たす",
    "期限は2026年10月末、予算の上限は設定しない",
    "ファイルは result/report.md に保存する",
    "パスワードや認証情報は扱わない(この文は禁止事項の説明であり値を含まない)",
    "承認済みの工程のみ実行する",
]


@pytest.mark.parametrize("value", SECRET_CASES)
def test_secrets_detected(value):
    import app.plan_review_loop as loop

    assert loop.contains_secret(f"計画メモ {value} 確認") != ""


@pytest.mark.parametrize("text", HARMLESS)
def test_harmless_not_detected(text):
    import app.plan_review_loop as loop

    assert loop.contains_secret(text) == ""


@pytest.mark.parametrize("value", SECRET_CASES)
def test_returns_label_not_value(value):
    import re

    import app.plan_review_loop as loop

    label = loop.contains_secret(value)
    assert label != ""
    # 検出値そのものを返さない。種別名(例: Authorizationヘッダー)と
    # 値の接頭辞が偶然重なる場合は許容し、秘密トークン部分の漏洩を検査する。
    assert value not in label
    assert len(label) <= 20
    for tok in re.split(r"\s+", value):
        t = tok.strip(":=").strip("：＝")
        if len(t) >= 8 and re.search(r"\d", t) and re.fullmatch(r"[A-Za-z0-9_\-\.=~+/]+={0,2}", t):
            assert t not in label


def _manager(tmp_path, providers=("chatgpt",)):
    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("l4secret")["id"]
    mem.save_mission(pid, "疑似目標\n1. 架空手順Aを定義する\n2. 架空手順Bを報告する",
                     "疑似目標\n1. 架空手順Aを定義する\n2. 架空手順Bを報告する",
                     "", True, list(providers))
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(), llm=None,
                  workers={}, _sync_memos=lambda pid: None,
                  provider_statuses=lambda: [{"id": p, "configured": True} for p in providers],
                  plan_review_runner=None)
    return mgr, mem, pid


def _plan(mgr, mem, pid):
    criteria = ["架空手順Aを定義する", "架空手順Bを報告する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, criteria[1], {"title": "架空資料B", "scope": "架空Bの整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    compiled = compile_plan(criteria, [t1, t2], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "l4 secret plan", compiled["tasks"])
    return mem.get_mission(pid)


def _contract(mgr, pid):
    from app.goal_contract import activate

    return activate(mgr, pid)


def _draft_body():
    return {
        "purpose": "疑似計画の候補",
        "criterion_ids": ["SC01", "SC02"],
        "constraints": ["公開・送信は人間承認後"],
        "steps": [{"id": "S1", "title": "疑似工程1"}, {"id": "S2", "title": "疑似工程2"}],
        "dependencies": [{"from": "S1", "to": "S2"}],
        "input_types": ["登録原本"],
        "artifacts": ["result/l4a.md"],
        "verification": ["見出し検査"],
        "capabilities": ["文書作成"],
        "external_actions": ["なし"],
        "human_approvals": ["公開前に人間承認"],
    }


class StubExternal:
    def __init__(self, draft=None):
        import json as _json
        self.draft = draft or _draft_body()
        self.sent_prompts = []
        self.calls = 0

    async def fake_gemini(self, provider_id, prompt, system, max_tokens=2200, reasoning_effort=None):
        import json as _json
        assert provider_id == "gemini"
        self.calls += 1
        self.sent_prompts.append(str(prompt))
        return SimpleNamespace(text=_json.dumps(self.draft), model="gemini-stub")

    def review_runner(self):
        async def _run(text, providers):
            import json as _json
            return [{"id": p, "ok": True,
                     "review": _json.dumps({"verdict": "pass", "issues": []})} for p in providers]
        return _run


def _install(monkeypatch, mgr, stub):
    import app.plan_review_loop as loop
    import app.external_ai as ext

    async def _fake(provider_id, prompt, system, max_tokens=2200, reasoning_effort=None):
        return await stub.fake_gemini(provider_id, prompt, system, max_tokens, reasoning_effort)

    monkeypatch.setattr(ext, "call_provider_with_metadata", _fake)
    mgr.plan_review_runner = stub.review_runner()
    return loop


@pytest.mark.parametrize("value", SECRET_CASES)
def test_start_blocks_each_secret_without_leak(tmp_path, monkeypatch, value):
    mgr, mem, pid = _manager(tmp_path)
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    stub = StubExternal()
    loop = _install(monkeypatch, mgr, stub)
    with pytest.raises(ValueError, match="秘密混入"):
        loop.start_run(mgr, pid, "human-a", ["chatgpt"], f"公開説明です。{value} が混入", f"idem-{abs(hash(value)) % 10**8}")
    assert stub.calls == 0
    # エラーメッセージに値そのものを出さない。
    try:
        loop.start_run(mgr, pid, "human-a", ["chatgpt"], f"公開説明です。{value} が混入", "idem-x")
    except ValueError as exc:
        assert value not in str(exc)
        assert "種別" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("must stop")


@pytest.mark.parametrize("field", ["purpose", "constraints", "steps", "capabilities"])
@pytest.mark.parametrize("value", [AWS_KEY, PW_EQ, PEM_KEY, GHP, CARD, CRED_URL, JWT])
def test_draft_fields_block_secrets_without_leak(field, value):
    import app.plan_review_loop as loop

    body = _draft_body()
    if field == "steps":
        body["steps"] = [{"id": "S1", "title": f"疑似工程 {value}"}, {"id": "S2", "title": "疑似工程2"}]
    elif field == "constraints":
        body["constraints"] = [f"制約 {value} を含む"]
    elif field == "capabilities":
        body["capabilities"] = [f"能力要約 {value} を含む"]
    else:
        body[field] = f"候補 {value} を含む"
    with pytest.raises(ValueError, match="秘密混入"):
        loop.validate_gemini_draft(body)
    # 付随チェック: 達成条件・制約などの他フィールドに仕込んでも同様に止まる。
    blob = canonical(body)
    assert loop.contains_secret(blob) != ""
    assert value not in loop.contains_secret(blob)


def test_payload_log_state_screen_api_have_no_secret(tmp_path, monkeypatch):
    mgr, mem, pid = _manager(tmp_path)
    # 原本に疑似秘密を仕込んでも送信payloadに出ない。
    mem.add_context_file(pid, "l4secret.md", f"本文 {AWS_KEY} {GHP} {CARD}",
                         10, b"x", "text/markdown", "document", "", "h")
    _plan(mgr, mem, pid)
    _contract(mgr, pid)
    stub = StubExternal()
    loop = _install(monkeypatch, mgr, stub)
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-noleak")
    out = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert out["state"] == "awaiting_human"
    blob = canonical(stub.sent_prompts)
    for v in (AWS_KEY, GHP, CARD, FAKE_SK, FAKE_MAIL):
        assert v not in blob
    stored = loop.get_run(mgr, pid, started["id"])
    state_blob = canonical(loop.public_view(stored)) + canonical(stored.get("rounds") or [])
    for v in (AWS_KEY, GHP, CARD):
        assert v not in state_blob
    # APIレスポンスに値が出ない: 秘密混入のstartは409で、値は含まない。
    import app.web as web_module
    from fastapi.testclient import TestClient

    monkeypatch.setattr(web_module, "memory", mem)
    monkeypatch.setattr(web_module, "orchestrator", mgr)
    client = TestClient(web_module.app)
    resp = client.post(f"/api/projects/{pid}/goal-review/auto-loop/start",
                       json={"actor": "human-a", "providers": ["chatgpt"],
                             "public_summary": f"公開説明 {AUTH_BASIC}",
                             "idempotency_key": "idem-api-leak"})
    assert resp.status_code == 409
    assert "秘密混入" in resp.text
