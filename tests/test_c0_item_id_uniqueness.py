"""C0 項目IDの一意性。疑似PJ・実関数・フェイクなし。外部通信なし。

実走の形(案内文+チェックリスト、日時・問い合わせ先・準備・終了後確認)で
extract/normalize/保存後再正規化/台帳投入の全出口を通して item_id が一意。
特定案件の語彙は試験入力にだけ置き、製品コードに入れない。
"""
from __future__ import annotations

import json

import pytest

from app.requirement_matrix import (
    assert_unique_item_ids,
    ensure_unique_item_ids,
    extract_requirement_items,
    normalize_p0a_items,
    validate_requirement_matrix,
)


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


def _ids(rows):
    return [str(r.get("item_id") or "") for r in rows if isinstance(r, dict)]


def test_c0_real_case_ids_unique():
    rows = extract_requirement_items("SC01", CRITERION, _outputs(), [], registered_text=REGISTERED)
    assert rows
    ids = _ids(rows)
    assert len(set(ids)) == len(ids)
    assert_unique_item_ids(rows, where="extract_requirement_items")


def test_c0_save_after_renormalize_stays_unique():
    rows = extract_requirement_items("SC01", CRITERION, _outputs(), [], registered_text=REGISTERED)
    fixed, _report = normalize_p0a_items("SC01", CRITERION, rows, _outputs(), REGISTERED)
    ids = _ids(fixed)
    assert len(set(ids)) == len(ids)
    # 修復後の再生成(同じ入力をもう一度正規化)でも一意。
    fixed2, _ = normalize_p0a_items("SC01", CRITERION, fixed, _outputs(), REGISTERED)
    ids2 = _ids(fixed2)
    assert len(set(ids2)) == len(ids2)


def test_c0_combinations_unique():
    variants = [
        (CRITERION, REGISTERED, False),
        (CRITERION, "案内を作成する", False),
        ("案内文には日時と日付と時刻を含める。", "開催日: 2026年11月14日 開始時刻10:00 時刻15:00", False),
        ("案内文には日時・場所・対象・持ち物を含める。チェックリストには準備・受付・終了後確認を含める。",
         REGISTERED, True),
        ("案内文1ファイルとチェックリスト1ファイル、合計2ファイルを作る。案内文には日時を含める。",
         REGISTERED, False),
    ]
    for criterion, registered, semantic in variants:
        rows = extract_requirement_items(
            "SC01", criterion, _outputs(), [], registered_text=registered,
            require_semantic=semantic)
        ids = _ids(rows)
        assert len(set(ids)) == len(ids), (criterion, ids)
        assert_unique_item_ids(rows, where="extract_requirement_items")


def test_c0_deterministic_same_input_same_ids():
    first = extract_requirement_items("SC01", CRITERION, _outputs(), [], registered_text=REGISTERED)
    second = extract_requirement_items("SC01", CRITERION, _outputs(), [], registered_text=REGISTERED)
    assert _ids(first) == _ids(second)


def test_c0_existing_unique_ids_unchanged():
    rows = [
        {"item_id": "SC01-R01", "criterion_id": "SC01", "artifact_path": "result/sc01.md",
         "requirement": "a", "method": "phrase", "required": True, "expected": {}},
        {"item_id": "SC01-R02", "criterion_id": "SC01", "artifact_path": "result/sc01.md",
         "requirement": "b", "method": "phrase", "required": True, "expected": {}},
    ]
    fixed = ensure_unique_item_ids("SC01", rows)
    assert _ids(fixed) == ["SC01-R01", "SC01-R02"]


def test_c0_late_duplicate_gets_next_free_number():
    rows = [
        {"item_id": "SC01-R01", "criterion_id": "SC01", "artifact_path": "result/sc01.md",
         "requirement": "a", "method": "phrase", "required": True, "expected": {}},
        {"item_id": "SC01-R01", "criterion_id": "SC01", "artifact_path": "result/sc01_checklist.md",
         "requirement": "b", "method": "phrase", "required": True, "expected": {}},
    ]
    fixed = ensure_unique_item_ids("SC01", rows)
    ids = _ids(fixed)
    assert ids[0] == "SC01-R01"
    assert len(set(ids)) == len(ids)
    assert ids[1] == "SC01-R02"
    # 決定的: もう一度同じ入力なら同じ結果。
    assert _ids(ensure_unique_item_ids("SC01", rows)) == ids


def test_c0_human_duplicate_is_rejected(tmp_path):
    dup = [
        {"item_id": "SC01-R01", "criterion_id": "SC01", "artifact_path": "result/sc01.md",
         "requirement": "a", "method": "phrase", "required": True, "expected": {}},
        {"item_id": "SC01-R01", "criterion_id": "SC01", "artifact_path": "result/sc01.md",
         "requirement": "b", "method": "phrase", "required": True, "expected": {}},
    ]
    with pytest.raises(ValueError, match="duplicate item_id"):
        assert_unique_item_ids(dup, where="test")
    # 台帳投入口は同一検証結果の二重投入を黙って上書きせず失敗側に倒す。
    from app.requirement_item_ledger import record_ledger

    same = [dict(dup[0]), dict(dup[0])]
    with pytest.raises(ValueError, match="duplicate item_id"):
        record_ledger(tmp_path / "m.db", {
            "project_id": "pj-c0", "plan_signature": "s", "source_hash": "h",
            "artifact_hashes": {}, "rows": same, "criterion_verdicts": {},
            "ledger_digest": "d"})
    # 保存前検査は重複を不合格にする。
    task = {"task_key": "SC01", "acceptance_criteria": json.dumps({
        "schema": "local-cowork-plan/v1", "criterion_ids": ["SC01"], "criterion": CRITERION,
        "outputs": [{"path": "result/sc01.md"}, {"path": "result/sc01_checklist.md"}],
        "requirement_matrix": dup}, ensure_ascii=False)}
    checked = validate_requirement_matrix({"tasks": [task]})
    assert checked["passed"] is False
    assert any("duplicate item id" in i for i in checked["issues"])
