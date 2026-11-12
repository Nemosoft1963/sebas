"""P1(修正C)/P2(修正D)の受入試験。疑似PJ・実関数・外部通信なし。

P1(修正C: 外部AI評価パケットを正確かつ安全にする):
- 公開パケットの項目数と公開コード数の意味が一致する(同じ出所・非公開は「非公開N件」)。
- 原本・秘密情報を送らずに3デモ(イベント/カフェ/備品申請)の構造を説明できる。
- unverifiableが合格に変換されない。
- 最終検証の状態モデルが矛盾しない(未承認では最終完了しない)。

P2(修正D: 指摘対応先の案内を改善する):
- ambiguous/invalid_referenceでは自動反映を止める(現行動作を維持)。
- UI相当の純粋関数で元の指摘・候補工程・候補条件・候補理由・差分プレビューを示す。
- 準備工程SC00や最終検証への指摘は実行SCへ無理に割り当てず種別を区別する。
- 人間の選択は計画署名と指摘IDを照合してから適用する。
- 計画が変わったら選択を失効させ、修正後の構造検査と外部再評価を必須にする。
- 未選択なら停止し、選択・差分確認後だけ再評価へ進む。
- 画面の静的検査(innerHTML系なし)、JS参照キーとAPI応答キーの一致、取得失敗が成功表示にならない。
- 既存の外部送信の安全(public_structureは内部原本を送らない)が回帰で維持される。
"""
import json
from pathlib import Path

from app.goal_review import evaluate_external_review_status, public_structure
from app.structured_planning import compile_plan, compile_task, contract_of


def _proposal(title, scope, headings, extra=None):
    row = {"title": title, "scope": scope, "headings": headings, "depends_on": []}
    if extra:
        row.update(extra)
    return row


def _demo_plans():
    event_task = compile_task(1, "架空イベントの案内文とチェックリストを作成する", _proposal(
        "架空案内", "架空イベントの案内内容を整理する。", ["案内文", "注意事項"],
        {"outputs": [
            {"kind": "document", "required_headings": ["案内文", "注意事項"]},
            {"kind": "checklist", "required_headings": ["準備", "当日対応"]},
        ]},
    ), [])
    event = compile_plan(
        ["架空イベントの案内文とチェックリストを作成する"], [event_task],
        goal="完全架空のイベントデモ")
    cafe_task = compile_task(1, "完全架空のカフェ新メニューを作成する。価格は仮の例示であり要確認とする", _proposal(
        "架空メニュー",
        "完全架空の商品説明を作成する。価格は仮の例示であり、食品安全の仮定は要確認とする。",
        ["商品説明", "仮価格の扱い"],
    ), [])
    cafe = compile_plan(
        ["完全架空のカフェ新メニューを作成する。価格は仮の例示であり要確認とする"],
        [cafe_task], goal="完全架空のカフェデモ")
    supply_criteria = [
        "完全架空の備品申請4段階(申請・上長承認・総務確認・払出)を手順書として作成する",
        "完全架空の備品申請FAQ5件を作成し、各質問を個別に検証する",
    ]
    supply_tasks = [
        compile_task(1, supply_criteria[0], _proposal(
            "架空備品手順書",
            "完全架空の4段階を手順書として整理する。要確認は未確認とする。",
            ["申請手順", "承認手順", "総務確認", "払出手順"],
            {"outputs": [
                {"kind": "procedure", "required_headings": ["申請手順", "承認手順"]},
            ]},
        ), []),
        compile_task(2, supply_criteria[1], _proposal(
            "架空FAQ文書",
            "完全架空のFAQ5件を整理する。各質問の回答根拠と要確認を区別する。",
            ["FAQ1", "FAQ2", "FAQ3"],
            {"outputs": [
                {"kind": "faq", "required_headings": ["FAQ1", "FAQ2", "FAQ3"]},
            ]},
        ), []),
    ]
    supply = compile_plan(supply_criteria, supply_tasks, goal="完全架空の備品申請デモ")
    return [("event", event), ("cafe", cafe), ("supply", supply)]


def _twenty_step_snapshot():
    tasks = []
    criteria = []
    for i in range(20):
        if i == 0:
            key, cids = "SC00", []
        elif i == 19:
            key, cids = "final_verification", [f"SC{j:02d}" for j in range(1, 19)]
        else:
            key, cids = f"SC{i:02d}", [f"SC{i:02d}"]
        tasks.append({
            "id": f"t{i}", "task_key": key, "title": key, "description": "疑似工程",
            "acceptance_criteria": json.dumps({
                "schema": "local-cowork-plan/v1", "criterion_ids": cids,
            }, ensure_ascii=False),
            "depends_on": [],
        })
        if 1 <= i <= 18:
            criteria.append({
                "criterion_id": f"SC{i:02d}",
                "exec_task_keys": [f"SC{i:02d}"],
                "verify_task_keys": ["final_verification"],
            })
    return {"tasks": tasks}, {"criteria": criteria, "content_hash": "hash-20"}


def test_p1_packet_counts_match_and_nonpublic_marked():
    import app.goal_review as gr

    for _name, plan in _demo_plans():
        packet = public_structure({"tasks": plan["tasks"]})
        assert packet["structure_version"] == gr.PUBLIC_STRUCTURE_VERSION
        disclosure = packet["exit_check_disclosure"]
        assert disclosure["total_count"] == disclosure["public_code_count"] + disclosure["withheld_count"]
        assert disclosure["withheld_note"] == ("非公開%d件" % disclosure["withheld_count"])
        for row in packet["tasks"]:
            assert row["exit_check_count"] == len(row["exit_check_codes"]) + row["exit_check_withheld_count"]
            assert row["exit_check_withheld_note"] == ("非公開%d件" % row["exit_check_withheld_count"])
        vdisc = packet["verification_disclosure"]
        assert vdisc["withheld_note"] == ("非公開%d件" % vdisc["withheld_item_count"])
        assert vdisc["aggregation"] == gr.VERIFICATION_AGGREGATION_RULE


def test_p1_three_demos_explained_without_originals_or_secrets():
    for _name, plan in _demo_plans():
        packet = public_structure({"tasks": plan["tasks"]})
        blob = json.dumps(packet, ensure_ascii=False)
        assert "架空イベントの案内文とチェックリストを作成する" not in blob
        assert "完全架空のカフェ新メニューを作成する" not in blob
        assert "完全架空の備品申請4段階" not in blob
        assert "src-" not in blob
        # 成果物ごとに項目ID・検査方法分類・集約条件を公開範囲で示す。
        assert packet["verification_disclosure"]["public_item_count"] > 0
        kinds = set()
        for row in packet["tasks"]:
            for item in row["verification_items"]:
                assert {"item_id", "method", "required"} <= set(item)
                assert "artifact_index" in item or "artifact_indices" in item
                assert item["method"] in (
                    "count", "artifact_count", "section_char_count",
                    "approval_stage_matrix", "headings", "phrase", "llm_semantic",
                )
                assert isinstance(item["required"], bool)
                kinds.add(row.get("issue_kind", ""))
            assert row["verification_aggregation"] == "all_required_items_must_pass_for_task_pass"
        # 原本本文・個人情報・内部ファイル名は既定で送らない。
        assert "result/sc01.md" not in blob
        assert ".md" in blob  # 拡張子だけの構造情報は残す。


def test_p1_unverifiable_never_passes_and_final_model_consistent():
    parsed = [{"provider": "chatgpt", "status": "unverifiable", "issues": []}]
    judged = evaluate_external_review_status(parsed, ["chatgpt"], {"min_success_count": 1})
    assert judged.get("status") != "passed"
    judged2 = evaluate_external_review_status(parsed, ["chatgpt"], {"min_success_count": 1})
    assert judged2.get("status") != "passed"
    for _name, plan in _demo_plans():
        packet = public_structure({"tasks": plan["tasks"]})
        model = packet["final_verification_model"]
        assert model["report_generated_by"] == "system"
        assert model["human_approval_gate"] == "separate_human_acceptance"
        assert model["completes_without_human_approval"] is False
        assert model["unverifiable_policy"] == "never_pass_without_evidence"
        assert packet["external_evaluation_policy"]["unverifiable_is_not_pass"] is True
        finals = [row for row in packet["tasks"] if row["role"] == "final_verification"]
        assert finals
        for row in finals:
            assert row["approval_model"]["completes_without_human_approval"] is False
            assert row["approval_model"]["separate_human_approval_gate"] is True


def test_p2_ambiguous_and_invalid_stop_auto_apply_and_show_candidates():
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    parsed = [{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "3", "unmet_goal": "SC01の検証不足",
         "reason": "SC01の検証手順が不足している", "remedy": "追記する"},
        {"step": "99", "unmet_goal": "存在しない工程",
         "reason": "工程99は無い", "remedy": "見直す"},
    ]}]
    table = loop.build_issue_bindings(parsed, contract, snap, plan_signature="sig",
                                      goal_contract_hash="hash-20", packet_hash="pkt")
    assert table["bindings"][0]["state"] == "ambiguous"
    assert table["bindings"][1]["state"] == "invalid_reference"
    for binding in table["bindings"]:
        assert binding["auto_apply_allowed"] is False
        assert binding["requires_human_selection"] is True
        assert binding["selection_status"] == "unselected"
        assert binding["issue_kind"] in loop.ISSUE_KINDS
    # 自動反映は止まる(現行動作を維持)。
    try:
        loop._map_issues_local(parsed, contract, snap)
    except ValueError as exc:
        assert "対応先" in str(exc)
    else:
        raise AssertionError("ambiguous/invalid_reference must stop auto apply")
    # UI相当の表示項目(元の指摘・候補工程・候補条件・候補理由)を持つ。
    first = table["bindings"][0]
    assert first["issue_text"]
    assert first["candidate_task_keys"]
    assert first["candidate_criterion_ids"]
    assert first["candidate_reasons"]
    options = loop._binding_selection_options(first)
    assert options
    preview = loop.preview_binding_selection(first, task_key="SC01")
    assert preview["before"] and preview["after"] and preview["diff"]
    assert "未選択なら停止" in preview["note"]


def test_p2_preparation_final_plan_kinds_are_distinguished():
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    prep = loop.build_issue_bindings([{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "1", "unmet_goal": "準備の不足",
         "reason": "準備工程の確認が不足", "remedy": "追記する"}]}], contract, snap)
    assert prep["bindings"][0]["issue_kind"] == "preparation"
    final = loop.build_issue_bindings([{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "20", "unmet_goal": "最終検証の不足",
         "reason": "最終検証の確認が不足", "remedy": "追記する"}]}], contract, snap)
    assert final["bindings"][0]["issue_kind"] == "final_verification"
    level = loop.build_issue_bindings([{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "99", "unmet_goal": "計画全体の不足",
         "reason": "計画全体の見直しが必要", "remedy": "見直す"}]}], contract, snap)
    assert level["bindings"][0]["issue_kind"] == "plan_level"
    exec_issue = loop.build_issue_bindings([{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "16-18", "unmet_goal": "後半の検証不足",
         "reason": "工程16-18の検証が不足", "remedy": "検証を追加"}]}], contract, snap)
    assert exec_issue["bindings"][0]["issue_kind"] == "requirement_item"


def test_p2_selection_requires_signature_and_preview_then_expires():
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    parsed = [{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "3", "unmet_goal": "SC01の検証不足",
         "reason": "SC01の検証手順が不足している", "remedy": "追記する"}]}]
    table = loop.build_issue_bindings(parsed, contract, snap, plan_signature="sig-a",
                                      goal_contract_hash="hash-a", packet_hash="pkt-a")
    binding = table["bindings"][0]
    run = {"issue_bindings": table["bindings"], "manual_bindings": [],
           "plan_signature": "sig-a", "goal_contract_hash": "hash-a"}
    # 指摘IDが一致しない選択は拒否。
    try:
        loop.confirm_binding_candidate(run, "different-id", actor="human-a",
                                       task_key="SC01", plan_signature="sig-a",
                                       goal_contract_hash="hash-a", packet_hash="pkt-a",
                                       confirmed_preview=True)
    except ValueError as exc:
        assert "対応表にありません" in str(exc) or "一致しない" in str(exc)
    else:
        raise AssertionError("mismatched issue id must be rejected")
    # 差分プレビューの確認なしでは確定できない。
    try:
        loop.confirm_binding_candidate(run, binding["issue_id"], actor="human-a",
                                       task_key="SC01", plan_signature="sig-a",
                                       goal_contract_hash="hash-a", packet_hash="pkt-a",
                                       confirmed_preview=False)
    except ValueError as exc:
        assert "プレビュー" in str(exc)
    else:
        raise AssertionError("selection without preview must stop")
    # 計画署名が一致しない選択は拒否。
    try:
        loop.confirm_binding_candidate(run, binding["issue_id"], actor="human-a",
                                       task_key="SC01", plan_signature="sig-b",
                                       goal_contract_hash="hash-a", packet_hash="pkt-a",
                                       confirmed_preview=True)
    except ValueError as exc:
        assert "409" in str(exc)
    else:
        raise AssertionError("mismatched signature must be rejected")
    # 署名一致・差分確認後だけ確定できる(検証合格にはならない)。
    preview = loop.binding_selection_preview_for_run(run, binding["issue_id"], task_key="SC01")
    assert preview["external_calls"] == 0
    assert preview["unselected_stops"] is True
    assert preview["selection_options"]
    entry = loop.confirm_binding_candidate(run, binding["issue_id"], actor="human-a",
                                           task_key="SC01", plan_signature="sig-a",
                                           goal_contract_hash="hash-a", packet_hash="pkt-a",
                                           confirmed_preview=True)
    assert entry["actor"] == "human-a"
    assert entry["plan_signature"] == "sig-a"
    assert binding["selection_status"] == "confirmed"
    assert binding["state"] == "ambiguous"
    # 計画が変わったら選択が失効する。
    expired = loop._invalidate_stale_selections(run, signature="sig-b",
                                                goal_contract_hash="hash-a")
    assert expired >= 1
    assert binding["selection_status"] == "expired"
    assert run["manual_bindings"] == []


def test_p2_unselected_stops_and_js_keys_match_api():
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    table = loop.build_issue_bindings([{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "3", "unmet_goal": "SC01の検証不足",
         "reason": "SC01の検証手順が不足している", "remedy": "追記する"}]}], contract, snap)
    binding = table["bindings"][0]
    assert binding["selection_status"] == "unselected"
    assert binding["requires_human_selection"] is True
    run = {"id": "run-1", "project_id": "pid-1", "plan_signature": "",
           "goal_contract_hash": "", "packet_hash": "",
           "issue_bindings": [binding]}
    preview = loop.binding_selection_preview_for_run(run, binding["issue_id"], task_key="SC01")
    assert preview["run_id"] == "run-1"
    assert preview["plan_signature"] == ""
    assert preview["requires_preview_confirmation"] is True
    # JSの参照キーとAPIの応答キーが一致する。
    for key in ("issue_id", "issue_kind", "issue_kind_ja", "issue_text", "step_raw",
                "candidate_task_keys", "candidate_criterion_ids", "candidate_reasons",
                "selected_task_key", "selected_criterion_id", "before", "after"):
        assert key in preview, key
    js = Path("app/static/plan_review_loop.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js
    assert "createElement" in js and "textContent" in js
    for key in ("issue_kind_ja", "candidate_task_keys", "candidate_criterion_ids",
                "binding-preview", "binding-confirm", "issue_id",
                "no_candidate_options", "decision_option", "awaiting_decision",
                "判断待ち", "確定済み", "判断の記録のみ"):
        assert key in js, key
    # 取得失敗が成功表示にならない。
    assert "対応先を確認できず" in js
    assert "選択を確定できず" in js
    assert "差分を確認できません" in js


def test_existing_external_send_safety_regression():
    plan = compile_plan(["架空文書を作成する"], [
        compile_task(1, "架空文書を作成する", _proposal(
            "架空文書", "架空文書の内容を整理する。", ["目的", "実施内容"]), ["src-1"]),
    ])
    packet = public_structure({"tasks": plan["tasks"]})
    blob = json.dumps(packet, ensure_ascii=False)
    assert "架空文書を作成する" not in blob
    assert "src-1" not in blob
    assert "result/sc01.md" not in blob
