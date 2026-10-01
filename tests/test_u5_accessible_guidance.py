"""U5: 次操作導線・無効理由・空状態・モバイル・キーボード対応の静的検査。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PM = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")
WR = (ROOT / "app/static/workflow_readiness.js").read_text(encoding="utf-8")
AS = (ROOT / "app/static/artifact_shelf.js").read_text(encoding="utf-8")
RQ = (ROOT / "app/static/review_queue.js").read_text(encoding="utf-8")
GR = (ROOT / "app/static/goal_review.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/project_mission.css").read_text(encoding="utf-8")
INDEX = (ROOT / "app/static/index.html").read_text(encoding="utf-8")


def test_u5_next_action_button_opens_corresponding_tab():
    assert "NEXT_ACTION_TAB" in WR
    for tab in ("goal_plan", "execute", "artifacts", "history"):
        assert f"'{tab}'" in WR
    assert "workflowOverviewNextButton" in WR
    assert "nextBtn.onclick=function(){openOverviewTarget(nextBtn.dataset.tab,nextBtn.dataset.actionId);}" in WR
    assert "window.selectWorkflowTab" in WR
    assert "window.selectWorkflowTab=selectWorkflowTab" in PM


def test_u5_disabled_reason_is_visible_and_has_unlock_guidance():
    assert "function showActionBlockReason(" in WR
    assert "利用できない理由:" in WR
    assert "解除方法:" in WR
    assert "insertAdjacentElement('afterend',hint)" in WR
    assert "missionGateNotice" in PM
    assert "解除方法: 外部検証または必要な確認を完了してください" in PM
    assert ".action-block-reason{" in CSS


def test_u5_major_empty_states_explain_next_step():
    assert "まだ成果物がありません。計画を実行すると生成されます" in AS
    assert "履歴はまだありません。計画を実行すると判断・操作の記録が表示されます" in PM
    assert "確認事項はまだありません。計画を生成・実行すると" in WR
    assert "確認事項はまだありません。計画を実行し" in RQ
    assert "履歴はまだありません。外部AIの指摘を取り込み" in GR
    assert "プロジェクトを選択してください" in PM


def test_u5_mobile_375_layout_avoids_page_overflow():
    assert "html,body{max-width:100%;overflow-x:hidden}" in CSS
    assert "@media(max-width:400px)" in CSS
    assert "overflow-x:auto" in CSS
    assert "flex-wrap:nowrap" in CSS
    assert "-webkit-overflow-scrolling:touch" in CSS
    assert ".project-target-main>*{min-width:0}" in CSS
    assert "grid-template-columns:minmax(0,1fr)" in CSS


def test_u5_tabs_have_aria_live_and_keyboard_handlers():
    assert 'role="tablist"' in PM
    assert PM.count('role="tab"') >= 5
    assert 'aria-selected="false"' in PM
    assert "button.setAttribute('aria-selected',String(active))" in PM
    assert "button.onkeydown=function(event)" in PM
    for key in ("ArrowRight", "ArrowLeft", "Enter", "event.key===' '"):
        assert key in PM
    assert "panel.setAttribute('role','tabpanel')" in PM
    assert "aria-live=\"polite\"" in PM
    assert "overview.setAttribute('aria-live','polite')" in WR
    assert 'id="projectSelect" aria-label="プロジェクト名の選択"' in INDEX
