"""U4: 5タブ再配置とプロジェクト単位のタブ記憶（静的検査）。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PM = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")
WR = (ROOT / "app/static/workflow_readiness.js").read_text(encoding="utf-8")
GR = (ROOT / "app/static/goal_review.js").read_text(encoding="utf-8")
AS = (ROOT / "app/static/artifact_shelf.js").read_text(encoding="utf-8")
EI = (ROOT / "app/static/experience_import.js").read_text(encoding="utf-8")
OCR = (ROOT / "app/static/ocr_review.js").read_text(encoding="utf-8")
AE = (ROOT / "app/static/agent_examples.js").read_text(encoding="utf-8")
TRIZ = (ROOT / "app/static/triz_invention.js").read_text(encoding="utf-8")
INDEX = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/project_mission.css").read_text(encoding="utf-8")


def test_u4_five_tabs_exist():
    for label in ("概要", "目標と計画", "実行と確認", "成果物", "履歴"):
        assert f">{label}</button>" in PM
    for view in ("overview", "goal_plan", "execute", "artifacts", "history"):
        assert f'data-view="{view}"' in PM
    assert "panel.id='workflow-'+key" in PM
    assert "examplesHost.id='workflow-examples'" in PM
    assert "workflowPanel('goal_plan')" in PM
    assert "workflowPanel('execute')" in PM
    assert "workflowPanel('artifacts')" in PM
    assert "workflowPanel('history')" in PM
    assert "getElementById('workflow-overview')" in WR
    assert "getElementById('workflow-goal_plan')" in GR
    assert "getElementById('workflow-artifacts')" in AS


def test_u4_tab_memory_is_per_project():
    assert "localWorkflowTab:" in PM
    assert "function workflowTabStorageKey(pid)" in PM
    assert "return 'localWorkflowTab:'+String(pid||'')" in PM.replace(" ", "") or "localWorkflowTab:'+String(pid" in PM
    assert "localStorage.getItem('localWorkflowTab')" not in PM
    assert "localStorage.setItem('localWorkflowTab'," not in PM
    assert "localStorage.getItem(workflowTabStorageKey(pid))" in PM
    assert "localStorage.setItem(workflowTabStorageKey(pid)" in PM
    assert "function applyProjectTabMemory(" in PM
    assert "readWorkflowTab(currentProject)" in PM
    assert "writeWorkflowTab(currentProject,key)" in PM
    assert "canonicalWorkflowTab" in PM
    assert "if(!pid)return" in PM


def test_u4_project_switch_clears_drafts_and_restores_tab():
    assert "function clearUnsentDrafts(" in PM
    assert "applyProjectTabMemory()" in PM
    wrap_start = PM.find("renderProject=function()")
    wrap_end = PM.find("};", wrap_start)
    wrap = PM[wrap_start:wrap_end]
    assert "clearUnsentDrafts()" in wrap
    assert "applyProjectTabMemory()" in wrap
    assert "lastRenderedProject!==currentProject" in wrap or "switched" in wrap
    for field in (
        "missionGoal",
        "missionCriteria",
        "missionConstraints",
        "missionInstructionInput",
        "externalActionTarget",
        "externalActionContent",
        "premarketingTitle",
        "researchTopic",
    ):
        assert field in PM[PM.find("function clearUnsentDrafts("):PM.find("function updateWorkflowEmptyState(")]


def test_u4_unselected_project_guide_exists():
    assert 'id="workflowEmptyGuide"' in PM or "id='workflowEmptyGuide'" in PM
    assert "プロジェクトを選択してください" in PM
    assert "新規作成" in PM
    assert "function updateWorkflowEmptyState(" in PM


def test_u4_existing_features_remain_in_new_tabs():
    assert "ocrReviewRoot" in INDEX
    assert "OCRフォールバック読取" in OCR
    assert "confirm_rag" in EI
    assert "経験RAG取り込み(成功事例)" in EI
    assert "表処理のTRIZ" in TRIZ
    assert 'id="missionApprove"' in INDEX or "missionApprove" in PM
    assert "この計画を承認して実行準備へ" in PM
    assert "goalPlanSend" in GR
    assert "goalFeedbackImport" in GR
    assert "goalResultApprove" in GR
    assert "確認済みとしてRAG登録" in GR
    assert "vehicleAutoPrepare" in AS
    assert "artifactZip" in AS
    assert "workflow-examples" in AE
    assert "実行例・再利用" in AE
    assert "missionStart" in PM
    assert "missionArtifactsDownload" in PM
    assert "window.selectWorkflowTab=selectWorkflowTab" in PM


def test_u4_overview_links_and_artifact_classes():
    assert "計画内容の詳細" in WR
    assert "実行モニタリングの詳細" in WR
    assert "成果物を開く" in WR
    assert "履歴を開く" in WR
    assert "workflowOverviewPlanLink" in WR
    assert "workflowOverviewMonitorLink" in WR
    assert "openOverviewTarget('goal_plan'" in WR
    assert "openOverviewTarget('execute'" in WR
    assert "下書き" in AS and "暫定" in AS and "検証済み" in AS and "人間承認済み" in AS
    assert "RAG登録は人間承認済み" in PM
    assert "工程依存図（補助表示）" in PM
    assert "workflowResumeHint" in PM


def test_u4_readiness_and_innerhtml_contracts_remain():
    assert WR.count("innerHTML") == 0
    assert PM.count("innerHTML") == 28
    assert AS.count("innerHTML") == 2
    assert "window.applyWorkflowReadiness" in WR
    assert "window.reloadWorkflowReadiness" in WR
    assert "targetBar.insertAdjacentElement('afterend',host)" in PM
    assert PM.index("targetBar.insertAdjacentElement('afterend',host)") < PM.index("host.querySelectorAll('[role=tab]')")
    assert ".workflow-empty-guide{" in CSS
