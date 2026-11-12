"""G2-1 DLP決定的検査の受入試験。実通信なし。偽ランナーのみ。"""
import base64
import json
import time

import pytest

from app.memory.short_term import ShortTermMemory
from app.planning_privacy import check_outbound_text
from app.project_manager import ProjectOrchestrator


def _mission(memory, pid, goal="通常の目標", success="通常の達成条件",
             constraints="通常の制約"):
    return memory.save_mission(pid, goal, success, constraints, True, ["chatgpt"])


def _manager(memory, **runners):
    return ProjectOrchestrator(
        memory, None, lambda project: ("", []), runners.get("research"),
        lambda: [{"id": "chatgpt", "configured": True}],
        runners.get("plan"), None, runners.get("capability"),
    )


def _blocked(memory, pid):
    return [e for e in memory.get_mission(pid, event_limit=500)["events"]
            if e.get("kind") == "external_send_blocked_privacy"]


def _audited(memory, pid):
    return [e for e in memory.get_mission(pid, event_limit=500)["events"]
            if e.get("kind") == "external_send_audited"]


def _no_leak(memory, pid, *values):
    blob = "\n".join(
        str(e.get("message") or "") + "\n" + str(e.get("detail") or "")
        for e in memory.get_mission(pid, event_limit=500)["events"]
    )
    for value in values:
        assert value not in blob


# ---- 1: 各コードの検出/非検出 ----
def _assert_block(text, code, **kwargs):
    result = check_outbound_text(text, **kwargs)
    assert result["ok"] is False, text
    assert code in result["codes"], result
    return result


def _assert_ok(text, **kwargs):
    result = check_outbound_text(text, **kwargs)
    assert result == {"ok": True, "codes": [], "details": {}}, (text, result)
    return result


def test_phone_detect():
    _assert_block("連絡先 090-1234-5678 まで", "phone")
    _assert_block("０９０－１２３４－５６７８", "phone")
    _assert_block("0 9 0 - 1 2 3 4 - 5 6 7 8", "phone")
    _assert_block("電話+81-90-1234-5678へ", "phone")
    _assert_block("連絡先 090\u200b1234\u200b5678 まで", "phone")


def test_phone_not_detect():
    _assert_ok("SC01工程を3件対応し、C02の条件を満たす通常の計画文です")
    _assert_ok("内線090の断片だけのメモです")
    _assert_ok("2026年3月5日にresult/x.mdを確認する通常文です")


def test_postal_detect():
    _assert_block("〒100-0001 東京都千代田区千代田1-1", "postal_code")
    _assert_block("東京都渋谷区 150-0001 近辺の計画", "postal_code")
    _assert_block("1 5 0 - 0 0 0 1 東京都渋谷区の資料", "postal_code")


def test_postal_not_detect():
    _assert_ok("管理番号1234567を記録する通常の計画文です")
    _assert_ok("SC01の達成に向けて工程を進めます")


def test_address_detect():
    _assert_block("東京都渋谷区渋谷2-11-3で作業", "address")
    _assert_block("大阪府大阪市北区梅田1丁目3-1を確認", "address")
    _assert_block("東京都渋谷区渋谷２－１１－３の全角番地", "address")


def test_address_not_detect():
    _assert_ok("東京都の計画を検討する通常文です")
    _assert_ok("渋谷区の検討を進める通常の計画文です")


def test_person_name_detect():
    _assert_block("氏名:山田太郎を確認", "person_name")
    _assert_block("担当者:鈴木花子へ連絡", "person_name")
    _assert_block("山田太郎様への案内", "person_name")
    _assert_block("鈴木さんへの対応", "person_name")


def test_person_name_not_detect():
    _assert_ok("お客様のご案内をする通常の計画文です")
    _assert_ok("各位への連絡をまとめた通常文です")
    _assert_ok("担当者様への一般案内の通常文です")


def test_bank_detect():
    _assert_block("口座番号 1234567 を確認", "bank_account")
    _assert_block("口座番号：１２３４５６７の全角", "bank_account")
    _assert_block("○○銀行渋谷支店 口座番号1234567", "bank_account")
    _assert_block("送金先 DE89370400440532013000", "bank_account")


def test_bank_not_detect():
    _assert_ok("口座の開設を検討する通常の計画文です")
    _assert_ok("銀行の制度を調査する通常文です")


def test_amount_detect():
    _assert_block("費用は¥1,200,000です", "amount_detail")
    _assert_block("予算120万円を計上", "amount_detail")
    _assert_block("投資3億円の計画", "amount_detail")
    _assert_block("cost $12,000 total", "amount_detail")


def test_amount_not_detect():
    _assert_ok("3件の対応をする通常の計画文です")
    _assert_ok("割合20%の通常文です")
    _assert_ok("2026年の計画を進めます")
    _assert_ok("500円の少額メモを含む通常文です")
    _assert_ok("result/x.mdを参照する通常文です")


def test_long_number_detect():
    _assert_block("番号1234567890を確認", "long_number")
    _assert_block("番号1234-5678-9012を確認", "long_number")
    _assert_block("登録T1234567890123を確認", "long_number")


def test_long_number_not_detect():
    h = "0123456789abcdef" * 4
    result = check_outbound_text("値は " + h + " です")
    assert "hash_like" in result["codes"]
    assert "long_number" not in result["codes"], result
    _assert_ok("番号123456789の9桁メモです")
    _assert_ok("2026年3月5日の通常文です")


def test_encoded_detect():
    raw = base64.b64encode(bytes(range(1, 100))).decode()
    assert len(raw) >= 48
    _assert_block("添付 " + raw + " を確認", "encoded_blob")
    pct = "%E3%81%82" * 8
    _assert_block("符号 " + pct + " を含む", "encoded_blob")
    esc = "\\u3042" * 6
    _assert_block("符号 " + esc + " を含む", "encoded_blob")
    ent = "&#12354;" * 6
    _assert_block("符号 " + ent + " を含む", "encoded_blob")


def test_encoded_not_detect():
    _assert_ok("supercalifragilisticexpialidocious という長い英単語の通常文です")
    _assert_ok("成果物は result/report.md を参照する通常文です")


# ---- 2は上記に含む ----

# ---- 3: source_quote ----
SOURCE_DOC = "これは登録原本の本文です。重要な計画方針と実施手順を記載します。追加の背景説明と検証観点を十分な長さで含めます。"


def test_source_quote_detect():
    quote = SOURCE_DOC[:30]
    _assert_block("計画メモ " + quote + " を含む", "source_quote",
                  source_texts=[SOURCE_DOC])
    # 空白・記号・全角半角を変えた形
    variant = quote.replace("、", " , ").replace("。", " 。 ")
    _assert_block("計画メモ " + variant + " を含む", "source_quote",
                  source_texts=[SOURCE_DOC])


def test_source_quote_not_detect():
    _assert_ok("全く関係ない通常の計画文です", source_texts=[SOURCE_DOC])


def test_source_quote_truncated():
    big = "あ" * 200_005
    result = check_outbound_text("通常の計画文です", source_texts=[big])
    assert result["details"].get("source_truncated") == 1, result


def test_source_quote_invalid_type():
    for bad in ({"a": 1}, 123, ["ok", 123]):
        result = check_outbound_text("通常文", source_texts=bad)
        assert result == {"ok": False, "codes": ["check_error"],
                          "details": {"check_error": True}}, bad


def test_source_quote_perf():
    sources = [("原本本文の反復。" * 30000)[:200_000] for _ in range(3)]
    body = ("通常の計画文です。" * 1000)[:10000]
    start = time.monotonic()
    result = check_outbound_text(body, source_texts=sources)
    elapsed = time.monotonic() - start
    assert elapsed < 10, elapsed
    assert result["ok"] is True, result


# ---- 4: details漏洩なし・ok定義・既存不変 ----
def test_details_has_no_values():
    phone = "090-1234-5678"
    result = check_outbound_text("連絡 " + phone, source_texts=[SOURCE_DOC])
    blob = json.dumps(result["details"], ensure_ascii=False)
    assert phone not in blob
    assert SOURCE_DOC not in blob
    assert SOURCE_DOC[:30] not in blob
    for code in result["codes"]:
        assert result["details"].get(code) is True
    assert result["ok"] is False


def test_ok_only_when_no_codes():
    assert check_outbound_text("通常の計画文です")["ok"] is True
    assert check_outbound_text("連絡 090-1234-5678")["ok"] is False


# ---- 5: 配線 ----
class _FakeLlm:
    def __init__(self, *responses):
        self.responses = list(responses) or ["{}"]

    async def stream(self, messages):
        yield self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _plan_llm():
    return _FakeLlm(
        '{"summary":"draft","tasks":[{"task_key":"draft","depends_on":[],'
        '"title":"Draft","description":"d","acceptance_criteria":"a"}]}')


@pytest.mark.asyncio
async def test_wiring_plan_review_phone_blocked(tmp_path):
    calls = []

    async def fake_runner(text, providers):
        calls.append((text, providers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G2-1 plan phone")
    pid = project["id"]
    _mission(memory, pid, goal="計画を作る 090-1234-5678")
    manager = ProjectOrchestrator(
        memory, _plan_llm(), lambda project: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}], fake_runner,
    )
    await manager.generate_plan(pid)
    assert calls == []
    blocked = _blocked(memory, pid)
    assert len(blocked) == 1
    detail = json.loads(blocked[0]["detail"])
    assert "phone" in detail["codes"]
    assert set(detail.keys()) == {"purpose", "codes", "sha256"}
    _no_leak(memory, pid, "090-1234-5678")
    await manager.shutdown()


@pytest.mark.asyncio
async def test_wiring_research_source_quote_blocked(tmp_path):
    calls = []

    async def fake_research(prompt, providers, synthesizer, style, context):
        calls.append((prompt, providers, synthesizer, style, context))
        return {"synthesis": "公開の知見"}

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G2-1 research quote")
    pid = project["id"]
    _mission(memory, pid)
    memory.add_context_file(pid, "src.md", SOURCE_DOC, len(SOURCE_DOC.encode()),
                            SOURCE_DOC.encode(), "text/markdown", "markdown",
                            "note", "hash-src")
    mission = memory.replace_plan(pid, "plan", [{
        "task_key": "r1", "depends_on": [], "title": "公開調査",
        "description": "公開情報を調査する " + SOURCE_DOC[:30],
        "acceptance_criteria": "調査メモ", "mode": "research",
    }])
    manager = ProjectOrchestrator(
        memory,
        _FakeLlm('{"result":"調査した","operations":[],"table_operations":[],'
                 '"source_references":[],"capability_gaps":[]}'),
        lambda project: ("", []), fake_research,
        lambda: [{"id": "chatgpt", "configured": True}],
    )
    await manager._execute_legacy_task(pid, mission["tasks"][0]["id"])
    assert calls == []
    blocked = _blocked(memory, pid)
    assert len(blocked) == 1
    assert "source_quote" in json.loads(blocked[0]["detail"])["codes"]
    _no_leak(memory, pid, SOURCE_DOC[:30])
    await manager.shutdown()


@pytest.mark.asyncio
async def test_wiring_pass_identical_and_list_failure(tmp_path):
    calls = []

    async def fake_runner(text, providers):
        calls.append((text, providers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("G2-1 pass")
    pid = project["id"]
    _mission(memory, pid, goal="計画を作る", success="検証を含める", constraints="削除なし")
    manager = ProjectOrchestrator(
        memory,
        _FakeLlm(
            '{"summary":"draft","tasks":[{"task_key":"draft","depends_on":[],'
            '"title":"Draft","description":"d","acceptance_criteria":"a"}]}',
            '{"summary":"refined","tasks":[{"task_key":"research","depends_on":[],'
            '"title":"Research","description":"d","acceptance_criteria":"a"},'
            '{"task_key":"verify","depends_on":["research"],'
            '"title":"Verify","description":"d","acceptance_criteria":"a"}]}'),
        lambda project: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}], fake_runner,
    )
    await manager.generate_plan(pid)
    assert len(calls) == 1
    audited = _audited(memory, pid)
    assert len(audited) == 1
    assert not _blocked(memory, pid)
    await manager.shutdown()

    # 一覧取得が想定内の例外のとき送らない
    calls2 = []

    async def fake_runner2(text, providers):
        calls2.append((text, providers))
        return [{"id": "x", "label": "X", "model": "m", "ok": True, "review": "よい"}]

    memory2 = ShortTermMemory(tmp_path / "memory2.db")
    project2 = memory2.create_project("G2-1 undecidable")
    pid2 = project2["id"]
    _mission(memory2, pid2)
    manager2 = ProjectOrchestrator(
        memory2, _plan_llm(), lambda project: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}], fake_runner2,
    )

    def boom(project_id, include_content=False):
        if not include_content:
            raise OSError("list unavailable")
        return ShortTermMemory.list_context_files(memory2, project_id,
                                                  include_content=True)

    memory2.list_context_files = boom
    await manager2.generate_plan(pid2)
    assert calls2 == []
    assert json.loads(_blocked(memory2, pid2)[0]["detail"])["codes"] == ["check_error"]
    await manager2.shutdown()


def _assert_no_person_name(text, **kwargs):
    result = check_outbound_text(text, **kwargs)
    assert "person_name" not in result["codes"], (text, result)
    return result


def test_person_name_honorific_not_detect():
    # G2-1: 敬称の誤検出を狭める(ひらがなだけの語・別語の一部・連結形を見ない)
    _assert_ok("目標: 月次の経理資料をたくさん整理し、result/report.md に出力する。")
    _assert_ok("担当者は承認する。氏名は含めない。")
    _assert_ok("さんまの缶詰やお客様対応の手順を整理する。")
    _assert_ok("ありがとうございます")
    _assert_ok("たくさんの資料")
    _assert_ok("ご担当者は確認する")
    _assert_ok("氏名欄は空欄にする")
    _assert_ok("お客様向けの案内")
    _assert_ok("各氏の意見")
    _assert_ok("同氏の資料")


def test_person_name_existing_normal_no_person_name():
    # 本ファイルの既存の通常文は person_name なし
    for text in [
        "SC01工程を3件対応し、C02の条件を満たす通常の計画文です",
        "内線090の断片だけのメモです",
        "2026年3月5日にresult/x.mdを確認する通常文です",
        "管理番号1234567を記録する通常の計画文です",
        "SC01の達成に向けて工程を進めます",
        "東京都の計画を検討する通常文です",
        "渋谷区の検討を進める通常の計画文です",
        "お客様のご案内をする通常の計画文です",
        "各位への連絡をまとめた通常文です",
        "担当者様への一般案内の通常文です",
        "口座の開設を検討する通常の計画文です",
        "銀行の制度を調査する通常文です",
        "3件の対応をする通常の計画文です",
        "割合20%の通常文です",
        "2026年の計画を進めます",
        "500円の少額メモを含む通常文です",
        "result/x.mdを参照する通常文です",
        "番号123456789の9桁メモです",
        "2026年3月5日の通常文です",
        "supercalifragilisticexpialidocious という長い英単語の通常文です",
        "成果物は result/report.md を参照する通常文です",
        "全く関係ない通常の計画文です",
        "通常の計画文です",
    ]:
        _assert_no_person_name(text)


def test_person_name_honorific_detect():
    _assert_block("田中様に連絡する", "person_name")
    _assert_block("山田太郎さんが承認", "person_name")
    _assert_block("佐藤殿", "person_name")
    _assert_block("鈴木先生に相談", "person_name")
    _assert_block("ヤマダ氏が", "person_name")
    _assert_block("担当者: 佐藤", "person_name")
    _assert_block("氏名:山田太郎", "person_name")
    _assert_block("田中 様", "person_name")
