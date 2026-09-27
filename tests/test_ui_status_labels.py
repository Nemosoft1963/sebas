"""UI-11〜15: 旧 completed 表示の縮小と指標パネル（静的検査）。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PM = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")
AS = (ROOT / "app/static/artifact_shelf.js").read_text(encoding="utf-8")
WR = (ROOT / "app/static/workflow_readiness.js").read_text(encoding="utf-8")

# innerHTML 基準件数（第5段階-B 実装前のソースを目視カウント）。
# project_mission.js: escapeHtml の d.innerHTML、外部アクション/プレマーケ/プロバイダ、
# 計画評価・タスク・イベント・指示チャット、workspace、会計プレビュー、コンテキスト一覧、
# 同時実行・外部実行・プレマーケ箱、workflow タブ host/instruction/tools、
# 計画タスク一覧、フロー node、工程詳細。今回の変更は statusLabel と missionState 描画のみ。
INNERHTML_PROJECT_MISSION = 28
# artifact_shelf.js: 棚パネルと車両入力箱の初期マークアップのみ。今回は completed 文言と再評価関数のみ。
INNERHTML_ARTIFACT_SHELF = 2
# workflow_readiness.js は当初から textContent / createElement のみ。
INNERHTML_WORKFLOW_READINESS = 0


def test_ui11_completed_default_is_not_goal_achieved():
    assert 'completed:"目標達成"' not in PM
    assert "completed:'目標達成'" not in PM
    assert 'completed:"全工程完了・目標達成は未確定"' in PM
    assert "未確定" in PM
    assert "mission.status==='completed'?'目標達成'" not in AS
    assert 'mission.status==="completed"?"目標達成"' not in AS
    assert "全工程完了・目標達成は未確定" in AS


def test_ui12_confirmed_label_only_with_final_completed():
    for source in (PM, AS):
        assert "目標達成(確定)" in source
        for index in range(len(source)):
            if source.startswith("目標達成(確定)", index):
                window = source[max(0, index - 400): index + 80]
                assert "final_completed" in window
    assert "window.workflowReadinessSnapshot" in WR
    assert "return state" in WR
    assert "state=null" in WR


def test_ui13_no_new_innerhtml():
    assert WR.count("innerHTML") == INNERHTML_WORKFLOW_READINESS
    assert PM.count("innerHTML") == INNERHTML_PROJECT_MISSION
    assert AS.count("innerHTML") == INNERHTML_ARTIFACT_SHELF


def test_ui14_metrics_manual_only_and_null_not_zero():
    assert "/goal-metrics" in WR
    assert "指標を更新" in WR
    assert "不明" in WR and "未計測" in WR
    assert "value==null?label" in WR or "value==null" in WR
    load_start = WR.find("async function load()")
    load_end = WR.find("async function loadMetrics()")
    assert load_start != -1 and load_end > load_start
    load_body = WR[load_start:load_end]
    assert "goal-metrics" not in load_body
    assert "loadMetrics" not in load_body
    schedule_start = WR.find("function schedule(")
    schedule_body = WR[schedule_start:load_start]
    assert "goal-metrics" not in schedule_body
    assert "loadMetrics" not in schedule_body
    assert "mretry.onclick=loadMetrics" in WR
    assert "addEventListener('change',loadMetrics)" in WR


def test_ui15_existing_readiness_ui_strings_remain():
    assert "innerHTML" not in WR
    assert all(x in WR for x in ["不明点", "止まる条件", "確認済み事実", "候補と影響", "再実行範囲"])
    assert "if(!answeredBy.value.trim())" in WR
    assert "window.applyWorkflowReadiness" in WR
    assert "window.reloadWorkflowReadiness" in WR
    assert "workflow-readiness" in WR
    assert "missionArtifactsDownload" in PM
    assert 'el("missionArtifactsDownload").disabled=mission.status!=="completed"' in PM


def test_refresh_artifact_goal_labels_does_not_clobber_messages():
    start = AS.find("window.refreshArtifactGoalLabels=")
    assert start != -1
    end = AS.find(";", start)
    while end != -1 and AS[start:end + 1].count("{") != AS[start:end + 1].count("}"):
        end = AS.find(";", end + 1)
    body = AS[start:end + 1]
    assert "render()" not in body
    assert "indexOf('計画版 ')===0" in body
    assert "textContent=" in body
    unconditional = [part for part in body.split(";") if "textContent=" in part and "indexOf('計画版 ')" not in part]
    assert unconditional == []
