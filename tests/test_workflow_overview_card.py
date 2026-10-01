"""U2: 概要カード(状態・停止理由・次操作・担当・達成の証拠)の静的検査と構造検証。"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WR = (ROOT / "app/static/workflow_readiness.js").read_text(encoding="utf-8")
PM = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/project_mission.css").read_text(encoding="utf-8")


def _js_object(name):
    match = re.search(rf"const {name}=\{{([^}}]+)\}}", WR)
    assert match, f"{name} が見つかりません"
    items = {}
    for key, value in re.findall(r"([A-Za-z0-9_]+):'([^']*)'", match.group(1)):
        items[key] = value
    return items


def _function_body(name):
    start = WR.find(f"function {name}(")
    assert start != -1, f"{name} が見つかりません"
    depth = 0
    begun = False
    for index in range(start, len(WR)):
        char = WR[index]
        if char == "{":
            depth += 1
            begun = True
        elif char == "}":
            depth -= 1
            if begun and depth == 0:
                return WR[start:index + 1]
    raise AssertionError(f"{name} の本体を切り出せません")


def status_code_ja(code):
    table = _js_object("STATUS_CODE_JA")
    if code is None or code == "":
        return "不明"
    return table[code] if code in table else "不明"


def format_count(value):
    body = _function_body("formatCount")
    assert "value==null" in body and "未取得" in body and "+'件'" in body
    if value is None:
        return "未取得"
    if isinstance(value, (int, float)):
        return str(value) + "件"
    return "不明"


def collect_stop_reason_cards(data):
    body = _function_body("collectStopReasonCards")
    assert "category:'計画内容'" in body
    assert "category:'接続・認証'" in body
    assert "category:'人間確認'" in body
    assert "cards.push" in body
    plan, connection, human, budget = [], [], [], []

    def add_item(bucket, item):
        if item and item not in bucket:
            bucket.append(item)

    if data is None:
        return []
    phase = data.get("phase")
    stop = data.get("stop_reason") or ""
    blocking = data.get("blocking_error") or ""
    gate = data.get("gate") or {}
    failed = gate.get("failed_criteria")
    extra = " ".join(x for x in [blocking, gate.get("error"), gate.get("reason"), stop, phase] if x)
    http_match = re.search(r"\b(401|429|503)\b", extra)
    http = http_match.group(1) if http_match else None
    replan = data.get("replan_failure") or {}
    has_plan = (
        phase in {"issues_open", "dev_blocked"}
        or (isinstance(failed, list) and len(failed) > 0)
        or ("指摘" in stop)
        or replan.get("code") in {"conflict_unresolved", "development_required"}
    )
    has_connection = (
        phase == "connection_failed"
        or bool(http)
        or ("再認証" in extra)
        or ("connection_failed" in extra)
    )
    has_budget = phase == "waiting_budget" or ("利用枠" in stop) or ("予算回復" in stop)
    has_human = phase in {"unverified", "provisional", "fact_confirm", "accuracy_blocked"} or (
        phase == "complete" and data.get("final_completed") is not True
    )
    if has_plan:
        if isinstance(failed, list) and failed:
            add_item(plan, "計画内容の指摘が" + str(len(failed)) + "件")
        if stop and ("指摘" in stop or phase in {"issues_open", "dev_blocked"}):
            add_item(plan, stop)
        if not plan:
            add_item(plan, status_code_ja("not_passed"))
    if has_connection:
        if "再認証" in extra:
            add_item(connection, stop if "再認証" in stop else "再認証が必要")
        if http:
            add_item(connection, "外部AIに接続できない（HTTP " + http + "）")
        if not connection:
            add_item(
                connection,
                stop if phase == "connection_failed" and stop else status_code_ja("connection_failed"),
            )
    if has_budget:
        add_item(budget, stop or status_code_ja("waiting_budget"))
    if has_human:
        if phase == "unverified":
            add_item(human, stop or status_code_ja("unverified"))
        else:
            add_item(human, stop or "人間の確認待ちです")
    cards = []
    if plan:
        cards.append({"category": "計画内容", "items": plan})
    if connection:
        cards.append({"category": "接続・認証", "items": connection})
    if human:
        cards.append({"category": "人間確認", "items": human})
    if budget:
        cards.append({"category": "利用枠", "items": budget})
    return cards


def test_u2_status_code_dictionary_maps_required_codes():
    table = _js_object("STATUS_CODE_JA")
    assert table["not_passed"] == "計画内容の指摘あり"
    assert table["connection_failed"] == "外部AIに接続できない"
    assert table["waiting_budget"] == "利用枠の回復待ち"
    assert table["unverified"] == "未検証"
    body = _function_body("statusCodeJa")
    assert "STATUS_CODE_JA" in body
    assert "return '不明'" in body
    assert status_code_ja("not_passed") == "計画内容の指摘あり"
    assert status_code_ja("connection_failed") == "外部AIに接続できない"
    assert status_code_ja("waiting_budget") == "利用枠の回復待ち"
    assert status_code_ja("unverified") == "未検証"


def test_u2_unknown_code_is_unknown_not_guessed():
    table = _js_object("STATUS_CODE_JA")
    known = set(table.values())
    body = _function_body("statusCodeJa")
    assert "hasOwnProperty.call(STATUS_CODE_JA,code)" in body
    assert body.count("return '不明'") >= 2
    for code in ("passed", "complete", "idle", "http_401", "needs_review", ""):
        label = status_code_ja(code)
        assert label == "不明"
        assert label not in known
    assert status_code_ja(None) == "不明"
    assert "推測" not in WR


def test_u2_overview_five_fields_exist():
    fields = re.search(r"const OVERVIEW_FIELDS=\[([^\]]+)\]", WR)
    assert fields, "OVERVIEW_FIELDS がありません"
    parsed = re.findall(r"'([^']+)'", fields.group(1))
    assert parsed == ["現在", "止まっている理由", "次の操作", "担当", "達成の証拠"]
    for ident in (
        "workflowOverview",
        "workflowOverviewCurrent",
        "workflowOverviewStop",
        "workflowOverviewNext",
        "workflowOverviewNextButton",
        "workflowOverviewOwner",
        "workflowOverviewEvidence",
        "workflowOverviewPlanLink",
        "workflowOverviewMonitorLink",
    ):
        assert ident in WR
    for fn in (
        "formatCurrent",
        "collectStopReasonCards",
        "formatNextOperation",
        "classifyOwner",
        "formatEvidence",
        "buildOverviewModel",
        "renderOverview",
    ):
        assert f"function {fn}(" in WR
    current = WR.find("add(currentBlock,'h3','現在')")
    stop = WR.find("add(stopBlock,'h3','止まっている理由')")
    nxt = WR.find("add(nextBlock,'h3','次の操作')")
    owner = WR.find("add(ownerBlock,'h3','担当')")
    evidence = WR.find("add(evidenceBlock,'h3','達成の証拠')")
    assert -1 not in (current, stop, nxt, owner, evidence)
    assert current < stop < nxt < owner < evidence
    assert "セバスが自動処理" in WR
    assert "人間が確認・承認" in WR
    assert "外部サービスの復旧待ち" in WR
    assert "この画面を開く" in WR
    assert "計画内容の詳細" in WR
    assert "実行モニタリングの詳細" in WR
    assert "window.selectWorkflowTab=selectWorkflowTab" in PM
    assert ".workflow-overview{" in CSS


def test_u2_multiple_stop_reasons_split_into_cards():
    cards = collect_stop_reason_cards({
        "phase": "issues_open",
        "stop_reason": "計画への指摘が未対応です。修正案を作成してください。",
        "blocking_error": "Googleの再認証が必要 HTTP 401",
        "gate": {"failed_criteria": ["a", "b", "c"]},
    })
    categories = [row["category"] for row in cards]
    assert categories == ["計画内容", "接続・認証"]
    plan_items = cards[0]["items"]
    conn_items = cards[1]["items"]
    assert any("計画内容の指摘が3件" in item for item in plan_items)
    assert any("再認証" in item for item in conn_items)
    assert all("再認証" not in item for item in plan_items)
    assert all("指摘が3件" not in item for item in conn_items)

    mixed = collect_stop_reason_cards({
        "phase": "unverified",
        "stop_reason": "現行版の外部検証が未完了または未合格です。",
        "blocking_error": "connection_failed HTTP 503",
        "gate": {"failed_criteria": ["plan"]},
    })
    mixed_cats = [row["category"] for row in mixed]
    assert mixed_cats.count("計画内容") == 1
    assert mixed_cats.count("接続・認証") == 1
    assert mixed_cats.count("人間確認") == 1
    assert "計画内容" in mixed_cats
    assert "接続・認証" in mixed_cats
    assert "人間確認" in mixed_cats


def test_u2_zero_count_is_not_unknown_and_missing_is_not_zero():
    assert format_count(0) == "0件"
    assert format_count(None) == "未取得"
    assert format_count(0) != "不明"
    assert format_count(None) != "0件"
    evidence_body = _function_body("formatEvidence")
    current_body = _function_body("formatCurrent")
    assert "failed_criteria==null" in evidence_body or "failed_criteria" in evidence_body
    assert "未取得" in evidence_body
    assert "0件" in evidence_body
    assert "final_completed===true" in evidence_body
    assert "未確定" in evidence_body
    assert "unverified:'計画を作成済み。外部検証は未検証です'" in current_body
    assert "未検証" in current_body
    assert "合格" not in current_body
    assert "目標達成(確定)" in current_body
    idle_gate = "el('workflowReadinessGate').textContent=failed.length?failed.join(', '):'なし'"
    assert idle_gate in WR
    assert "unknownMetric(value,label){return value==null?label:String(value);}" in WR or "value==null?label" in WR


def test_u2_disabled_button_shows_visible_reason():
    body = _function_body("showActionBlockReason")
    hint = _function_body("actionUnlockHint")
    assert "action-block-reason" in body
    assert "利用できない理由:" in body
    assert "解除方法:" in hint
    assert "insertAdjacentElement('afterend',hint)" in body
    assert "applyActions" in WR
    assert "showActionBlockReason(button,rule)" in WR
    assert "button.title=rule.allowed?'':(rule.reason||'')" in WR
    assert ".action-block-reason{" in CSS


def test_u2_existing_readiness_contracts_remain():
    assert WR.count("innerHTML") == 0
    assert "/api/projects/'+encodeURIComponent(pid)+'/workflow-readiness'" in WR
    assert "window.applyWorkflowReadiness" in WR
    assert "window.reloadWorkflowReadiness" in WR
    assert "window.workflowReadinessSnapshot" in WR
    assert "if(!answeredBy.value.trim())" in WR
    assert all(x in WR for x in ["不明点", "止まる条件", "確認済み事実", "候補と影響", "再実行範囲"])
    assert "targetBar.insertAdjacentElement('afterend',host)" in PM
    load_start = WR.find("async function load()")
    load_end = WR.find("async function loadMetrics()")
    load_body = WR[load_start:load_end]
    assert "goal-metrics" not in load_body
    assert "loadMetrics" not in load_body
    assert "mretry.onclick=loadMetrics" in WR
