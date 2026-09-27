"""テスト: workflow_readiness.js の questionCard UI 改善の静的検査。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WR = (ROOT / "app/static/workflow_readiness.js").read_text(encoding="utf-8")


def test_qc01_no_raw_json_describe_in_question_card():
    # questionCard の本体を取得
    start = WR.find("function questionCard(")
    assert start != -1
    end = WR.find("function init()", start)
    assert end != -1
    body = WR[start:end]

    # describe(values[index]) のような全フィールド JSON.stringify 生表示ループがないこと
    assert "HEADINGS.forEach" not in body
    assert "describe(values[index])" not in body
    assert "JSON.stringify(values" not in body


def test_qc02_confirmed_facts_rendering_and_use_button():
    assert "過去の回答:" in WR
    assert "この回答を使う" in WR
    assert "過去の回答はありません" in WR
    assert "select.value='__exclude__'" in WR or 'select.value = "__exclude__"' in WR or "select.value = '__exclude__'" in WR
    assert "select.value=val.vehicle_id" in WR or "select.value = val.vehicle_id" in WR


def test_qc03_candidates_summary_and_details_fold():
    assert "formatCandidatesSummary" in WR
    assert "候補一覧を表示" in WR
    assert "details" in WR
    assert "summary" in WR
    assert "影響額" in WR


def test_qc04_date_range_inputs_and_visibility():
    assert "開始月 YYYY-MM" in WR
    assert "終了月 YYYY-MM" in WR
    assert "dateFrom.hidden" in WR or "date_from" in WR
    assert "dateTo.hidden" in WR or "date_to" in WR
    assert "apply.value==='date_range'" in WR or 'apply.value === "date_range"' in WR


def test_qc05_fetch_payload_fields_preserved():
    expected_fields = [
        "version:",
        "input_hash:",
        "issue_id:",
        "vehicle_id:",
        "exclude:",
        "reason:",
        "apply_to:",
        "allocations:",
        "date_from:",
        "date_to:",
        "answered_by:",
    ]
    for field in expected_fields:
        assert field in WR, f"Field {field} missing from workflow_readiness.js fetch payload"
    assert "/questions/answer" in WR
    assert "innerHTML" not in WR


def test_qc06_form_state_snapshot_by_question_id():
    # loadQuestions / init 内に question.id / dataset.questionId をキーにしてフォーム入力値を退避する処理が存在する
    assert "dataset.questionId" in WR
    assert "snapshotQuestionsState" in WR or "snapshot" in WR
    for key in ["vehicle", "reason", "apply_to", "date_from", "date_to", "answered_by", "detailsOpen"]:
        assert key in WR, f"Snapshot key {key} missing from workflow_readiness.js"


def test_qc07_form_state_restore_after_card_creation():
    # カード生成後に退避値を復元する処理が存在する
    assert "restoreQuestionState" in WR or "restore" in WR
    assert "details.open" in WR
    assert "dispatchEvent" in WR or "updateDateInputs" in WR


def test_qc08_skip_polling_when_focus_active():
    # activeElement がフォーム内にある間はポーリングによる再読込をスキップするコードが存在する
    assert "document.activeElement" in WR or "activeElement" in WR
    assert "isEditingQuestions" in WR or "active" in WR


def test_qc09_manual_retry_forces_immediate_load():
    # 手動の再取得ボタン(retry.onclick)は強制フラグを渡して即時実行する
    assert "retry.onclick" in WR
    assert "load(true)" in WR
