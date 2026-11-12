"""A1 要求契約の再設計の受入試験。疑似PJ・実関数・フェイクLLM。外部通信なし。

- 登録コンテキストに架空メール、達成条件に日時・問い合わせ先・準備・終了後確認を置く。
- 案内文に実値があれば問い合わせ先 PASS、消す/偽値にすると FAIL。
- チェックリストに終了後確認見出しがあれば PASS、消すと FAIL。
- 要求節全文が expected.keywords に保存されない。重複項目を作らない。
- 別成果物だけでは対象成果物は PASS にならない。
- 連絡先が無ければ input_missing になり、URL等を発明しない。
- P0-B修復試験の前提は変えない(本試験は追加のみ)。
"""
from __future__ import annotations

from app.requirement_matrix import (
    extract_requirement_items,
    resolve_contact_values,
)
from app.requirement_verification import verify_requirement_items


REGISTERED = (
    "開催日: 2026年11月14日 開始時刻10:00 会場: 架空ホール "
    "連絡先: aozora@example.invalid"
)
CRITERION = (
    "案内文1ファイルとチェックリスト1ファイル、合計2ファイルを作る。"
    "案内文には日時・架空の問い合わせ先を含め、"
    "チェックリストには準備・終了後確認を含める。"
)


def _outputs():
    return [
        {"path": "result/sc01.md", "artifact_category": "document",
         "required_headings": ["日時・場所", "問い合わせ先"]},
        {"path": "result/sc01_checklist.md", "artifact_category": "checklist",
         "required_headings": ["準備", "終了後確認"]},
    ]


def _bind_items(registered=REGISTERED):
    return extract_requirement_items(
        "SC01", CRITERION, _outputs(), [], registered_text=registered)


def _resolve(files: dict):
    def _inner(relative: str):
        import pathlib

        target = pathlib.Path(relative)
        assert str(relative) in files, relative
        path = pathlib.Path("/tmp") / "a1_dummy"
        path.write_text(files[str(relative)], encoding="utf-8")
        return path

    return _inner


def test_resolve_contact_values_single_interface():
    resolved = resolve_contact_values("pj-a1", REGISTERED)
    assert resolved == [
        {"kind": "email", "value": "aozora@example.invalid",
         "source_kind": "registered_context", "source_ref": ""},
    ]
    assert resolve_contact_values("pj-a1", "連絡先なし") == []
    assert resolve_contact_values("pj-a1", "") == []


def test_contact_item_uses_registered_value_and_target_artifact():
    items = _bind_items()
    contacts = [row for row in items
                if "aozora@example.invalid" in list(
                    (row.get("expected") or {}).get("keywords") or [])]
    assert contacts, items
    contact = contacts[0]
    assert contact["method"] == "phrase"
    assert contact["artifact_path"] == "result/sc01.md"
    expected = contact.get("expected") or {}
    assert expected.get("semantic_role") == "連絡先"
    assert expected.get("target_artifact") == "result/sc01.md"
    assert expected.get("match_policy") == "any"
    assert expected.get("match") == "any"
    assert expected.get("source_hash")
    # 要求節の全文は保存しない。
    for row in items:
        if row["method"] != "phrase":
            continue
        for keyword in list((row.get("expected") or {}).get("keywords") or []):
            assert "問い合わせ先を含め" not in str(keyword)
            assert "終了後確認を含める" not in str(keyword)


def test_contact_pass_and_fail_by_registered_value(tmp_path):
    import json

    items = _bind_items()
    contract = {"criterion_ids": ["SC01"], "requirement_matrix": items,
                "outputs": [{"path": "result/sc01.md"},
                            {"path": "result/sc01_checklist.md"}]}
    task = {"task_key": "SC01",
            "acceptance_criteria": json.dumps(contract, ensure_ascii=False)}
    good = {
        "result/sc01.md": (
            "# 案内\n2026年11月14日\n"
            "## 日時・場所\n日時と場所。\n"
            "## 問い合わせ先\naozora@example.invalid へ連絡。\n"),
        "result/sc01_checklist.md": (
            "# 準備\n準備する。\n"
            "## 準備\n準備。\n"
            "## 終了後確認\n終了後確認。\n"),
    }
    root = tmp_path / "ws"
    (root / "result").mkdir(parents=True)

    def resolve(relative: str):
        target = root / relative
        assert relative in good or "bad" in str(relative)
        return target

    for path, body in good.items():
        (root / path).write_text(body, encoding="utf-8")
    rows = verify_requirement_items(task, resolve, semantic_judge=None)
    contact_rows = [row for row in rows
                    if "aozora@example.invalid" in str(
                        (row.get("expected") or {}).get("keywords") or [])]
    assert contact_rows and all(row["result"] == "PASS" for row in contact_rows), rows
    # メールを消すと FAIL。
    (root / "result/sc01.md").write_text(
        good["result/sc01.md"].replace("aozora@example.invalid", "連絡先は未定"),
        encoding="utf-8")
    rows2 = verify_requirement_items(task, resolve, semantic_judge=None)
    contact2 = [row for row in rows2
                if "aozora@example.invalid" in str(
                    (row.get("expected") or {}).get("keywords") or [])]
    assert contact2 and all(row["result"] == "FAIL" for row in contact2), rows2
    # 偽の値に置き換えても FAIL(実値照合のため)。
    (root / "result/sc01.md").write_text(
        good["result/sc01.md"].replace("aozora@example.invalid", "fake@example.com"),
        encoding="utf-8")
    rows3 = verify_requirement_items(task, resolve, semantic_judge=None)
    contact3 = [row for row in rows3
                if "aozora@example.invalid" in str(
                    (row.get("expected") or {}).get("keywords") or [])]
    assert contact3 and all(row["result"] == "FAIL" for row in contact3), rows3
    # 別成果物だけでは対象成果物は PASS にならない。
    (root / "result/sc01.md").write_text(
        good["result/sc01.md"].replace("aozora@example.invalid", "連絡先は未定"),
        encoding="utf-8")
    (root / "result/sc01_checklist.md").write_text(
        good["result/sc01_checklist.md"] + "\naozora@example.invalid\n",
        encoding="utf-8")
    rows4 = verify_requirement_items(task, resolve, semantic_judge=None)
    contact4 = [row for row in rows4
                if "aozora@example.invalid" in str(
                    (row.get("expected") or {}).get("keywords") or [])]
    assert contact4 and all(row["result"] == "FAIL" for row in contact4), rows4


def test_heading_item_pass_and_fail(tmp_path):
    import json

    items = _bind_items()
    contract = {"criterion_ids": ["SC01"], "requirement_matrix": items,
                "outputs": [{"path": "result/sc01.md"},
                            {"path": "result/sc01_checklist.md"}]}
    task = {"task_key": "SC01",
            "acceptance_criteria": json.dumps(contract, ensure_ascii=False)}
    root = tmp_path / "ws2"
    (root / "result").mkdir(parents=True)
    (root / "result/sc01.md").write_text(
        "# 案内\n2026年11月14日\n## 日時・場所\n日時。\n## 問い合わせ先\n"
        "aozora@example.invalid\n", encoding="utf-8")
    (root / "result/sc01_checklist.md").write_text(
        "# 準備\n準備。\n## 準備\n準備。\n## 終了後確認\n確認。\n",
        encoding="utf-8")

    def resolve(relative: str):
        return root / relative

    rows = verify_requirement_items(task, resolve, semantic_judge=None)
    headings = [row for row in rows if row["method"] == "headings"
                and "終了後確認" in list(
                    (row.get("expected") or {}).get("headings") or [])]
    assert headings and all(row["result"] == "PASS" for row in headings), rows
    # 見出しを消すと FAIL。
    (root / "result/sc01_checklist.md").write_text(
        "# 準備\n準備。\n## 準備\n準備。\n", encoding="utf-8")
    rows2 = verify_requirement_items(task, resolve, semantic_judge=None)
    headings2 = [row for row in rows2 if row["method"] == "headings"
                 and "終了後確認" in list(
                     (row.get("expected") or {}).get("headings") or [])]
    assert headings2 and all(row["result"] == "FAIL" for row in headings2), rows2


def test_contact_missing_source_is_input_missing_without_invention():
    items = extract_requirement_items(
        "SC01", CRITERION, _outputs(), [], registered_text="案内を作成する")
    missing = [row for row in items if row["method"] == "input_missing"]
    assert missing and any("問い合わせ先" in str(row.get("requirement") or "")
                           for row in missing)
    for row in items:
        for keyword in list((row.get("expected") or {}).get("keywords") or []):
            assert "http" not in str(keyword)
            assert "@" not in str(keyword) or "aozora" not in str(keyword)


def test_three_step_checklist_with_heading_drops_instruction_phrase():
    """実モデルの準備・受付・終了後確認要求で命令文を必須語に残さない。"""
    from app.requirement_matrix import detect_extraction_doubts

    criterion = (
        "案内文1ファイルと当日運営チェックリスト1ファイル、合計2ファイルを作る。"
        "案内文には日時・場所・対象・持ち物・架空の問い合わせ先を含める。"
        "チェックリストには準備・受付・終了後確認を含める。"
    )
    outputs = [
        {"path": "result/sc01.md", "artifact_category": "document",
         "required_headings": ["日時・場所・対象・持ち物・問い合わせ先"]},
        {"path": "result/sc01_checklist.md", "artifact_category": "checklist",
         "required_headings": ["準備（9:30）", "受付（10:00～）", "終了後確認（15:00）",
                               "終了後確認"]},
    ]
    items = extract_requirement_items(
        "SC01", criterion, outputs, [], registered_text=REGISTERED)
    phrase_words = [str(word) for row in items if row.get("method") == "phrase"
                    for word in (row.get("expected") or {}).get("keywords", [])]
    assert "終了後確認を含める" not in phrase_words
    assert "場所" in phrase_words
    assert "対象" in phrase_words
    assert "語句の抽出漏れの疑義: 終了後確認を含める" not in detect_extraction_doubts(
        criterion, items)
