"""F3b: 画面JS(app/static, 中核外の要確認)の fail-closed 化テスト (静的検査・疑似データのみ)。

対象は F3b 着手時点の review かつ app/static/ 配下の *.js 36件。
実コードを1件ずつ読み、(1) except/フォールバックの用途、(2) 失敗時の現状挙動を判断し、
A=直す(失敗を成功・0件・空・正常に見せない → ベースラインから除外)
B=許容(補助のみ・理由付きで allow 化) に分けた。

- A 31件: tests/fail_open_baseline.json の _f3b_removed_ids (現コードに検出が無いこと)
- B  5件: findings の allow (理由に「F3bで許容(B)」「実コードを読んで確認済み」を含む)
- 既存 allow 4件 (artifact_shelf 1 / presenters 2 / describe 1) は F3b 以前からの許容。

Node.js は本環境に無いため、JS の実行検査は行わず、Python 標準の静的検査のみ行う。
使い捨てスクリプトは作らない。外部通信・本番データ・実案件には触れない。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.fail_open_lint as lint

BASELINE = ROOT / "tests" / "fail_open_baseline.json"
STATIC = ROOT / "app" / "static"

# B(許容・今回新規 allow): 理由付き 5件。
B_NEW_IDS = {
    "app/static/project_mission.js:readWorkflowTab:P4:eb70835b81d5",
    "app/static/project_mission.js:setupContextFolderUpload:P4:5c1517a8ee10",
    "app/static/project_mission.js:writeWorkflowTab:P4:eb70835b81d5",
    "app/static/triz_general.js:render:P4:0f2b111a0963",
    "app/static/triz_invention.js:render:P4:0f2b111a0963",
}

# 既存 allow (F3b 以前からの表示フォールバック) の JS 側 4件。
B_EXISTING_STATIC_IDS = {
    "app/static/artifact_shelf.js:load:P4:eb70835b81d5",
    "app/static/presenters.js:rawJson:P4:2bb0cda1691a",
    "app/static/presenters.js:rawJson:P4:b76981f910d6",
    "app/static/workflow_readiness.js:describe:P4:de9ea16376f5",
}


def _js(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def test_f3b_scope_matches_baseline():
    """A 31件は一覧から外れ、B 5件は理由付き allow、画面JSのレビュー残件は0件。"""
    raw = _baseline()
    removed = raw["_f3b_removed_ids"]
    assert len(removed) == 31, len(removed)
    assert len(set(removed)) == 31
    by_id = {x["id"]: x for x in raw["findings"]}
    # A は一覧に残っていないこと(直した証跡)。
    assert not (set(removed) & set(by_id)), sorted(set(removed) & set(by_id))
    # 画面JSのレビュー残件は0件(A 31 + B 5 + 既存 allow 4 = 36)。
    static_items = [x for x in raw["findings"] if str(x.get("file", "")).startswith("app/static/")]
    assert not [x for x in static_items if x.get("classification") == "review"], static_items
    assert {x["id"] for x in static_items} == B_NEW_IDS | B_EXISTING_STATIC_IDS
    # 静的対象 40件 = A 31 + B 5 + 既存allow 4 (F3b 着手時の画面JS全体)。
    assert len(removed) + len(B_NEW_IDS) + len(B_EXISTING_STATIC_IDS) == 40
    assert raw["total"] == len(raw["findings"]) == 11


def test_f3b_a_patterns_are_failclosed():
    """A(直す): 検出パターンが消え、失敗が日本語のエラー表示になること。"""
    texts = {p.name: p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("*.js"))}
    # 検出器レベル: A の関数の変更行に P4 新規検出が無いこと。
    changed = [
        "app/static/capability_gap.js",
        "app/static/experience_import.js",
        "app/static/goal_review.js",
        "app/static/ocr_review.js",
        "app/static/plan_repair_cards.js",
        "app/static/plan_review_loop.js",
        "app/static/project_delete.js",
        "app/static/project_management.js",
        "app/static/project_mission.js",
        "app/static/project_reset.js",
        "app/static/resolution_cards.js",
        "app/static/workflow_readiness.js",
    ]
    assert lint.lint_changed_files(changed) == []
    # A 31件そのものが現コードに無いこと(検出器の全体検査)。
    current_ids = {c["id"] for c in lint.lint_project(ROOT)}
    assert not (set(_baseline()["_f3b_removed_ids"]) & current_ids)
    # 失敗の見える化: 日本語の確認できず/取得失敗の文言があること。
    must_have = {
        "capability_gap.js": "応答を取得失敗しました",
        "experience_import.js": "応答を取得失敗しました",
        "goal_review.js": "応答を取得失敗しました",
        "plan_repair_cards.js": "残件カードを取得できず",
        "plan_review_loop.js": "自動評価を",
        "project_delete.js": "画面の再読み込みに失敗しました",
        "project_management.js": "状態を取得できず",
        "project_reset.js": "初期化の応答を確認できず",
        "resolution_cards.js": "残件カードを取得できず",
        "workflow_readiness.js": "確認できず",
        "ocr_review.js": "取得できず",
    }
    for name, word in must_have.items():
        assert word in texts[name], name
    # 空の成功にしない: ocr の個別状態取得は ok 時失敗を失敗件数として表示すること。
    ocr = texts["ocr_review.js"]
    assert "OCR状態を確認できず" in ocr
    assert "OCR状態を取得できず" in ocr
    assert "確認できず" in ocr
    # resolution の参考情報の欠落は件数表示で区別すること。
    res = texts["resolution_cards.js"]
    assert "参考情報の一部を確認できず" in res
    assert "candidateErrors" in res
    # project_mission の clipboard 失敗は成功文言を出さないこと。
    pm = texts["project_mission.js"]
    assert "briefCopied" in pm
    assert "コピーに失敗したため、再試行してください" in pm


def test_f3b_no_new_dangerous_html_and_contracts():
    """制約: innerHTML 等の新規使用なし・既存テストの期待文字列を壊さない。"""
    texts = {p.name: p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("*.js"))}
    for name, text in texts.items():
        assert "insertAdjacentHTML" not in text, name
        assert "outerHTML" not in text, name
        assert "document.write" not in text, name
    assert texts["project_mission.js"].count("innerHTML") == 28
    assert texts["capability_gap.js"].count("innerHTML") == 0
    assert texts["workflow_readiness.js"].count("innerHTML") == 0
    assert texts["experience_import.js"].count("innerHTML") == 0
    # 既存テストが期待する文字列・キーが残ること。
    assert "buttonStateFor" in texts["capability_gap.js"]
    assert "confirm_rag" in texts["experience_import.js"]
    assert "experienceInputVersion" in texts["experience_import.js"]
    assert "if(!actor||!confirmed)" in texts["experience_import.js"].replace(" ", "")
    assert "OVERVIEW_FIELDS" in texts["workflow_readiness.js"]
    assert "formatNextOperation" in texts["workflow_readiness.js"]
    assert "gapCountText" in texts["workflow_readiness.js"]
    assert "不足機能：未確認" in texts["workflow_readiness.js"]
    assert "不足機能はありません" in texts["capability_gap.js"]
    assert "過去の回答:" in texts["workflow_readiness.js"]
    assert "この回答を使う" in texts["workflow_readiness.js"]
    assert "候補一覧を表示" in texts["workflow_readiness.js"]
    assert "影響額" in texts["workflow_readiness.js"]
    assert "開始月 YYYY-MM" in texts["workflow_readiness.js"]
    assert "終了月 YYYY-MM" in texts["workflow_readiness.js"]
    assert "dataset.questionId" in texts["workflow_readiness.js"]
    assert "snapshotQuestionsState" in texts["workflow_readiness.js"]
    assert "restoreQuestionState" in texts["workflow_readiness.js"]
    assert "isEditingQuestions" in texts["workflow_readiness.js"]
    assert "retry.onclick" in texts["workflow_readiness.js"]
    assert "load(true)" in texts["workflow_readiness.js"]
    assert "旧版はアーカイブ済みです" in texts["project_mission.js"]
    assert "投稿証拠あり" in texts["project_mission.js"]
    assert "data-social-regenerate" in texts["project_mission.js"]
    assert "intent=newBrowser&url=" in texts["project_mission.js"]


def test_f3b_b_allow_reasons_are_concrete():
    """B(許容): 5件は理由付き allow。理由は F3b の実コード確認の根拠を含むこと。"""
    by_id = {x["id"]: x for x in _baseline()["findings"]}
    for bid in sorted(B_NEW_IDS):
        assert bid in by_id, bid
        item = by_id[bid]
        assert item.get("classification") == "allow", bid
        reason = str(item.get("reason", ""))
        assert "F3bで許容(B)" in reason, bid
        assert "実コードを読んで確認済み" in reason, bid
    # 成功・0件・完了を表示しない根拠が理由に書かれていること(捏造防止)。
    assert "成功" in by_id["app/static/triz_general.js:render:P4:0f2b111a0963"]["reason"]
    assert "未処理Promise" in by_id[
        "app/static/project_mission.js:setupContextFolderUpload:P4:5c1517a8ee10"]["reason"]
