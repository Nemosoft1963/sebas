"""U3: JSON表示 presenter の静的構造検査（既存静的JSテスト流儀）。"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "app/static/presenters.js").read_text(encoding="utf-8")
PM = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")
GR = (ROOT / "app/static/goal_review.js").read_text(encoding="utf-8")
INDEX = (ROOT / "app/static/index.html").read_text(encoding="utf-8")


def function_body(name):
    start = SOURCE.find("function " + name + "(")
    assert start >= 0, name
    depth = 0
    begun = False
    for index in range(start, len(SOURCE)):
        if SOURCE[index] == "{":
            depth += 1
            begun = True
        elif SOURCE[index] == "}":
            depth -= 1
            if begun and depth == 0:
                return SOURCE[start:index + 1]
    raise AssertionError(name)


def test_u3_presenter_functions_render_required_headings():
    for name in ("statusPresenter", "planContractPresenter", "reviewPresenter"):
        assert function_body(name)
    contract = function_body("planContractPresenter")
    review = function_body("reviewPresenter")
    for heading in ("この工程で作るもの", "必要な入力", "先に終える工程", "人間確認", "完了の証拠", "未達ならどうなるか"):
        assert heading in contract
    for heading in ("全体判定", "合格 n/m", "内容の指摘", "接続できなかったAI"):
        assert heading in review
    assert all(x in SOURCE for x in ["verdict", "severity", "issue", "proposal"])


def test_u3_unknown_and_null_are_not_zero_or_empty():
    present = function_body("present")
    status = function_body("statusPresenter")
    assert "var UNKNOWN='不明'" in SOURCE
    assert "value===null||value===undefined||value===''" in present
    assert "return UNKNOWN" in present
    assert "return UNKNOWN" in status
    assert "||0" not in present and "||''" not in present
    assert "hasOwnProperty.call(STATUS,key)" in status


def test_u3_technical_toggle_preserves_original_json():
    assert "function rawJson(" in SOURCE
    assert "技術情報を表示" in PM
    assert "技術情報を表示" in GR
    assert "technicalInfo(model.raw)" in PM
    assert "technicalInfo(model.raw)" in GR
    assert "JSON.stringify(value,null,2)" in SOURCE
    assert '<script src="/static/presenters.js' in INDEX


def test_u3_http_errors_are_user_facing_and_raw_is_technical_only():
    body = function_body("httpErrorPresenter")
    assert "code===401||code===403" in body and "認証" in body
    assert "code===503||code===502||code===504" in body and "一時的" in body
    assert "raw:{status:status,detail:detail}" in body
    messages = re.findall(r"message=([^;]+)", body)
    assert messages
    assert "401 Unauthorized" not in body
    assert "503 Service Unavailable" not in body


def test_u3_major_screens_no_longer_render_whole_json_directly():
    assert "<pre id=\"goalPlanResult\">" not in GR
    assert "<pre id=\"goalFeedbackHistory\">" not in GR
    assert "JSON.stringify(review()" not in GR
    assert "diff.before+'\\n【変更後】" not in GR
    assert "<pre>'+escapeHtml(task.error||task.result" not in PM
    assert "完了判定: '+escapeHtml(t.acceptance_criteria" not in PM
    assert "planContractPresenter" in PM
    assert "taskResultPresenter" in PM
    assert "reviewPresenter" in GR
    assert "revisionHistoryPresenter" in GR
    assert "diffPresenter" in GR
