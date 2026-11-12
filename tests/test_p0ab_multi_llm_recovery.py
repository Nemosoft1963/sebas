"""P0-A/P0-B受入試験。疑似PJ・実関数・外部通信なし。特定案件の語彙を製品コードに入れない。"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.goal_review import ReviewStore, plan_snapshot, public_structure
from app.structured_planning import (
    build_p0a_planning_input,
    check_p0a_fact_consistency,
    check_p0a_separate_artifacts,
    compile_plan,
    compile_task,
    count_p0a_required_files,
    extract_p0a_fact_values,
)


class Manager(SimpleNamespace):
    pass


def _proposal(title, scope, headings, extra=None):
    row = {"title": title, "scope": scope, "headings": headings, "depends_on": []}
    if extra:
        row.update(extra)
    return row


def test_p0a_registered_body_included_with_source_and_limits():
    bundle = build_p0a_planning_input(
        goal="架空目標",
        success_criteria="架空条件",
        constraints_text="架空制約",
        static_body="日時2026年10月20日 電話03-1234-5678 " + ("x" * 5000),
        source_files=[{"filename": "f.md", "content": "y" * 5000}],
        per_item_limit=100,
        total_limit=250,
    )
    assert bundle["version"] == "p0a-planning-input-v1"
    assert bundle["truncated"] is True
    assert "[出典:registered_context]" in bundle["text"]
    assert "[出典:registered_file:f.md]" in bundle["text"]
    assert len(bundle["text"]) <= 250
    assert any(item["truncated"] for item in bundle["items"])
    # 上限を超えた場合は切り、切ったことを記録する。
    clipped = [item for item in bundle["items"] if item["truncated"]]
    assert clipped and all(item["kept"] < item["chars"] for item in clipped)


def test_p0a_fact_conflict_stops_and_unconfirmed_recorded():
    registered = "日時2026年10月20日 電話03-1234-5678"
    same = check_p0a_fact_consistency(
        "日時2026年10月20日 電話03-1234-5678 の作業", registered)
    assert same["passed"] is True
    assert same["conflicts"] == []
    # 異なる値を含む計画案は保存前に停止する。
    bad = check_p0a_fact_consistency(
        "日時2026年10月21日 電話09-9999-9999 の作業", registered)
    assert bad["passed"] is False
    assert bad["conflicts"]
    # 出典のない値は「未確認」と記録される(拒否されない)。
    assert bad["unconfirmed"] == [] or isinstance(bad["unconfirmed"], list)
    unknown = check_p0a_fact_consistency("日時2026年11月1日 の作業", "登録なし")
    assert unknown["passed"] is True
    assert unknown["conflicts"] == []
    assert any(row["status"] == "unconfirmed" for row in unknown["unconfirmed"])
    # 登録コンテキストに同種の値が無ければ矛盾と判定しない。
    empty_kind = check_p0a_fact_consistency("金額なし作業", "日時2026年10月20日")
    assert empty_kind["passed"] is True
    assert extract_p0a_fact_values("日時2026年10月20日")["date"]


def test_p0a_two_files_merged_is_rejected_and_split_passes():
    criteria = ["架空資料2点を別ファイルで作成する"]
    merged_task = compile_task(1, criteria[0], _proposal(
        "架空統合", "架空の2資料を整理する。", ["概要", "詳細"]), [])
    merged = compile_plan(criteria, [merged_task], goal="架空P0A")
    bad = check_p0a_separate_artifacts(criteria, merged["tasks"])
    assert bad["passed"] is False
    assert bad["issues"]
    assert count_p0a_required_files(criteria[0]) == 2
    # 別ファイル・別の検証手順を持つ計画案は合格。
    split_task = compile_task(1, criteria[0], _proposal(
        "架空分離", "架空の2資料を整理する。", ["概要", "詳細"],
        {"outputs": [
            {"kind": "document", "required_headings": ["概要A", "詳細A"]},
            {"kind": "document", "required_headings": ["概要B", "詳細B"]},
        ]}), [])
    split = compile_plan(criteria, [split_task], goal="架空P0A")
    good = check_p0a_separate_artifacts(criteria, split["tasks"])
    assert good["passed"] is True


def test_p0a_content_item_counts_are_not_file_counts():
    faq = ("申請、上長確認、購入、受け取りまでの4段階を説明する。"
           "新人が迷いやすい質問5件と回答を作る。"
           "承認が必要な箇所を明示した1つの日本語Markdownを作る。")
    assert count_p0a_required_files(faq) == 1
    cafe = ("架空の新メニューを2品設定し、各品の魅力を80字程度で説明する。"
            "商品説明文とスタッフ向け手順書の日本語Markdownを各1点、合計2点作る。")
    assert count_p0a_required_files(cafe) == 2
    assert count_p0a_required_files("資料2点を別ファイルで作成する") == 2
    assert count_p0a_required_files("質問5件を一つの文書にまとめる") == 1


def test_p0a_public_packet_has_no_original_body(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p0a-pseudo")["id"]
    mem.save_mission(pid, "架空目標", "架空条件", "", False, [])
    mgr = Manager(memory=mem, workspace=WorkspaceSandbox(tmp_path / "workspace"),
                  planning_projects=set(), llm=None, workers={},
                  _sync_memos=lambda pid: None,
                  provider_statuses=lambda: [],
                  plan_review_runner=None,
                  _outbound_guard=lambda pid, purpose, text, providers=(): {"ok": True, "codes": []},
                  static_context=lambda project: ("日時2026年10月20日", []))
    assert "日時2026年10月20日" in mgr.static_context({})[0]
    plan = compile_plan(["架空文書を作成する"], [
        compile_task(1, "架空文書を作成する", _proposal(
            "架空文書", "架空文書の内容を整理する。", ["目的", "実施内容"]), ["src-1"]),
    ])
    packet = public_structure({"tasks": plan["tasks"]})
    blob = json.dumps(packet, ensure_ascii=False)
    assert "日時2026年10月20日" not in blob
    assert "src-1" not in blob
    assert "result/sc01.md" not in blob


def test_p0b_single_attach_and_snapshot_roundtrip():
    import app.plan_feedback as fb

    assert fb.attach_issue_bindings.__code__.co_firstlineno
    import inspect as _inspect

    found = [name for name, _ in _inspect.getmembers(fb, _inspect.isfunction)
             if name == "attach_issue_bindings"]
    assert found == ["attach_issue_bindings"]
    issues = [{"id": "i1", "text": "不足", "provider": "chatgpt"}]
    bindings = [{"issue_id": "i1", "state": "resolved",
                 "steps": [1], "candidate_task_keys": ["SC01"],
                 "candidate_criterion_ids": ["SC01"], "basis": "根拠",
                 "target_type": "goal_criterion",
                 "target_task_keys": ["SC01"], "target_ids": ["SC01"]}]
    bound = fb.attach_issue_bindings(issues, bindings)
    assert bound[0]["binding"]["state"] == "resolved"
    assert bound[0]["binding"]["target_type"] == "goal_criterion"
    snapshot = fb.build_binding_snapshot(
        bindings=bindings, plan_signature="sig",
        goal_contract_hash="hash", packet_hash="pkt",
        review_fingerprint="fp",
        confirmed=[{"issue_id": "i1", "task_key": "SC00",
                    "criterion_id": "", "actor": "human-a",
                    "plan_signature": "sig",
                    "goal_contract_hash": "hash", "packet_hash": "pkt"}])
    assert snapshot["version"] == "binding-snapshot-v1"
    assert fb.snapshot_target_for(snapshot, "i1")["target_type"] == "goal_criterion"
    assert fb.confirmed_target_for(snapshot, "i1")["task_key"] == "SC00"
    fb.check_binding_snapshot_current(snapshot, plan_signature="sig",
                                      goal_contract_hash="hash",
                                      packet_hash="pkt", review_fingerprint="fp")
    for changed in ({"packet_hash": "different"}, {"review_fingerprint": "different"}):
        try:
            fb.check_binding_snapshot_current(
                snapshot, plan_signature="sig", goal_contract_hash="hash",
                packet_hash=changed.get("packet_hash", "pkt"),
                review_fingerprint=changed.get("review_fingerprint", "fp"))
        except ValueError:
            pass
        else:
            raise AssertionError("stale packet or review must be rejected")
    try:
        fb.check_binding_snapshot_current(snapshot, plan_signature="other",
                                          goal_contract_hash="hash",
                                          packet_hash="pkt")
    except ValueError as exc:
        assert "計画版" in str(exc)
    else:
        raise AssertionError("stale snapshot must be rejected")


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
                "role": "preparation" if key == "SC00" else "execution",
                "internal_preparation_gate_ids": ["PREP00"] if key == "SC00" else [],
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


def test_p0b_confirmed_sc00_propagates_and_stale_rejected(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p0b-pseudo")["id"]
    mem.save_mission(pid, "疑似目標\n1. 架空手順Aを定義する",
                     "疑似目標\n1. 架空手順Aを定義する", "", True, ["chatgpt"])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(), llm=None,
                  workers={}, _sync_memos=lambda pid: None,
                  provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
                  plan_review_runner=None,
                  _outbound_guard=lambda pid, purpose, text, providers=(): {"ok": True, "codes": []})
    criteria = ["架空手順Aを定義する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    compiled = compile_plan(criteria, [t1], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p0b pseudo plan", compiled["tasks"])
    from app.goal_contract import activate as _activate
    _activate(mgr, pid)
    import app.plan_feedback as fb
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    parsed = [{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "1", "unmet_goal": "準備工程の検査方法が不足しています",
         "reason": "原本確認の検査方法と停止条件が不足しています",
         "remedy": "検査方法と停止条件を具体化する", "severity": "blocking"}]}]
    table = loop.build_issue_bindings(parsed, contract, snap,
                                      plan_signature="sig", goal_contract_hash="hash-20",
                                      packet_hash="pkt")
    binding = table["bindings"][0]
    assert binding["target_type"] == "preparation_gate"
    snapshot = fb.build_binding_snapshot(
        bindings=table["bindings"], plan_signature="sig",
        goal_contract_hash="hash-20", packet_hash="pkt",
        review_fingerprint="fp",
        confirmed=[{"issue_id": binding["issue_id"], "task_key": "SC00",
                    "criterion_id": "", "actor": "human-a",
                    "plan_signature": "sig",
                    "goal_contract_hash": "hash-20", "packet_hash": "pkt"}])
    # 指摘の取込 → ローカル修正案 → coverage_matrix → 適用判定で同じ値になる。
    assert fb.snapshot_target_for(snapshot, binding["issue_id"])["target_type"] == "preparation_gate"
    assert fb.confirmed_target_for(snapshot, binding["issue_id"])["task_key"] == "SC00"
    issues = [{"id": binding["issue_id"], "text": "準備不足", "provider": "chatgpt"}]
    bound = fb.attach_issue_bindings(issues, table["bindings"])
    assert bound[0]["binding"]["target_task_keys"] == ["SC00"]
    from app.plan_coverage_matrix import build_coverage_matrix, coverage_gate_error
    actions = [{"issue_id": binding["issue_id"], "disposition": "amend",
                "target": "SC00", "change": "SC00の検査方法と停止条件を具体化する手順",
                "reason": "準備指摘への対応手順を具体化した。"}]
    changes = [{"target": "SC00", "before": "旧", "after": "新"}]
    matrix = build_coverage_matrix(issues=bound, actions=actions, changes=changes,
                                   task_order=["SC00"])
    assert coverage_gate_error(matrix) is None
    # SC00がSC01へ自動で移らない。
    assert "SC01" not in (fb.snapshot_target_for(snapshot, binding["issue_id"]) or {}).get("target_task_keys", [])
    # 計画・契約・packetが変わった後の古い確定は拒否される。
    try:
        fb.check_binding_snapshot_current(snapshot, plan_signature="sig2",
                                          goal_contract_hash="hash-20",
                                          packet_hash="pkt")
    except ValueError as exc:
        assert "計画版" in str(exc)
    else:
        raise AssertionError("stale confirmation must be rejected")
    # attachは1つの定義になり、既存の挙動が保たれる。
    assert fb.binding_disposition({"target_type": "preparation_gate",
                                   "resolved_preparation": {"passed": True}}) == "amend"


def test_p0_existing_safety_regression():
    from app.goal_review import evaluate_external_review_status

    judged = evaluate_external_review_status(
        [{"provider": "chatgpt", "status": "unverifiable", "issues": []}],
        ["chatgpt"], {"min_success_count": 1})
    assert judged.get("status") != "passed"
    js = Path("app/static/plan_review_loop.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js


def test_p0b_snapshot_save_failure_records_flag_and_blocks_apply(tmp_path, monkeypatch):
    """(1) スナップショットの保存失敗(putが例外)を注入すると、binding_snapshot_save_failedが記録され、
    以後の適用判定が「確定していない」になる(ReviewStoreからの再構成で確定扱いにならない)。
    """
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox
    import app.plan_review_loop as loop
    import app.plan_feedback as fb

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p0b-save-fail")["id"]
    mem.save_mission(pid, "疑似目標\n1. 架空手順Aを定義する",
                     "疑似目標\n1. 架空手順Aを定義する", "", True, ["chatgpt"])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(), llm=None,
                  workers={}, _sync_memos=lambda pid: None,
                  provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
                  plan_review_runner=None,
                  _outbound_guard=lambda pid, purpose, text, providers=(): {"ok": True, "codes": []})
    criteria = ["架空手順Aを定義する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    compiled = compile_plan(criteria, [t1], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p0b save fail plan", compiled["tasks"])
    from app.goal_contract import activate as _activate
    _activate(mgr, pid)

    async def _runner(_text, providers):
        return [{"id": p, "ok": True, "review": json.dumps({
            "verdict": "fail",
            "issues": [{"severity": "blocking", "step": "1",
                        "unmet_goal": "準備工程の検査方法が不足しています",
                        "reason": "原本確認の検査方法と停止条件が不足しています",
                        "remedy": "検査方法と停止条件を具体化する"}]})} for p in providers]

    mgr.plan_review_runner = _runner
    summary = "架空手順Aの定義資料を作成し、達成条件SC01と検証記録を確認する公開用の具体的な計画説明です。"
    preview = loop.preview_run(mgr, pid, ["chatgpt"], explicit_summary=summary)
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], summary, "idem-save-fail-1",
                             expected_packet_hash=preview["packet_hash"])
    asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    run = loop.get_run(mgr, pid, started["id"])
    run["state"] = "stopped"
    run["stop_kind"] = "human_required"
    loop._save(mgr, run)
    # 一度正常に対応付け直して issue_bindings を載せる。
    asyncio.new_event_loop().run_until_complete(
        loop.remap_run(mgr, pid, started["id"], "human-a", "remap-initial"))
    run = loop.get_run(mgr, pid, started["id"])
    # confirm_binding で候補確定できるよう state を ambiguous にし、candidate_task_keys を持たせる。
    run["issue_bindings"][0]["state"] = "ambiguous"
    run["issue_bindings"][0]["candidate_task_keys"] = ["SC00", "SC01"]
    run["issue_bindings"][0]["candidate_criterion_ids"] = ["SC01"]
    loop._save(mgr, run)
    issue_id = run["issue_bindings"][0]["issue_id"]

    # binding_snapshot の保存時に例外を注入する(旧スナップショットも事前に消去)。
    orig_put = ReviewStore.put
    store = ReviewStore(mem.path)
    with store.connect() as db:
        db.execute("DELETE FROM reviews WHERE project=? AND kind='binding_snapshot'", (pid,))

    def _fail_snapshot_put(self, project, kind, signature, payload):
        if kind == "binding_snapshot":
            raise OSError("simulated binding_snapshot save failure")
        return orig_put(self, project, kind, signature, payload)

    monkeypatch.setattr(ReviewStore, "put", _fail_snapshot_put)

    # 1-a. confirm_binding でのスナップショット保存失敗
    entry = asyncio.new_event_loop().run_until_complete(
        loop.confirm_binding(mgr, pid, started["id"], issue_id,
                             actor="human-a", task_key="SC00", confirmed_preview=True))
    assert entry.get("binding_snapshot_save_failed") is True
    assert entry.get("binding_snapshot_save_error") == "OSError"
    saved_run = loop.get_run(mgr, pid, started["id"])
    assert saved_run.get("binding_snapshot_save_failed") is True
    assert saved_run.get("binding_snapshot_save_error") == "OSError"
    view = loop.public_view(saved_run)
    assert view["binding_snapshot_save_failed"] is True
    assert view["binding_snapshot_save_error"] == "OSError"

    # 以後の修正案生成・網羅表・適用判定は、正本が無い状態を「確定していない」として扱い拒否する
    # (ReviewStoreからの再構成で resolved 扱いに戻さない)。
    snap, sig = plan_snapshot(mgr, pid)
    raw_issues = fb.issues_for(mgr, pid, sig)
    actions = [{"issue_id": issue_id, "disposition": "amend",
                "target": "SC00", "change": "SC00の検査方法と停止条件を具体化する手順",
                "reason": "準備指摘への対応手順を具体化した。"}]
    changes = [{"target": "SC00", "before": "旧", "after": "新"}]
    try:
        fb._coverage_matrix_for(mgr, pid, sig, raw_issues, actions, changes, snap, None)
    except ValueError as exc:
        assert "被覆行列" in str(exc) or "対応付け" in str(exc) or "確定していません" in str(exc)
    else:
        raise AssertionError("coverage matrix / apply check must reject when binding_snapshot save failed")

    # 1-b. remap_run (2箇所目) でも binding_snapshot 保存失敗時に記録され停止することを確認。
    run2 = loop.get_run(mgr, pid, started["id"])
    run2["binding_version"] = "issue-binding-v1"
    run2["issue_binding_version"] = "issue-binding-v1"
    loop._save(mgr, run2)
    remapped = asyncio.new_event_loop().run_until_complete(
        loop.remap_run(mgr, pid, started["id"], "human-a", "remap-after-fail",
                       binding_version="issue-binding-v2"))
    assert remapped["binding_snapshot_save_failed"] is True
    assert remapped["binding_snapshot_save_error"] == "OSError"
    assert remapped["state"] == "stopped"
    assert not remapped.get("revision_candidate_id")


def test_p0a_prefacts_audit_failure_records_flag_and_keeps_fact_check_result(tmp_path):
    """(2) p0a_plan_prefacts 監査イベントの書込失敗(add_eventが例外)を注入すると、
    prefacts_audit_failed と型名が残り、事実の矛盾の検査結果(停止/通過)は変わらない。
    """
    from app.memory.short_term import ShortTermMemory
    from app.project_manager import ProjectOrchestrator
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    project = mem.create_project("p0a-audit-fail", workspace_path="projects/p0a_audit")
    pid = project["id"]
    registered_body = "日時2026年10月20日 電話03-1234-5678"
    mem.save_mission(pid, "## 達成条件\n1. 案内文を作成する", "1. 案内文を作成する", "", False, [])

    orig_add_event = mem.add_event

    def _fail_prefacts_event(project_id, kind, message, *args, **kwargs):
        if kind == "p0a_plan_prefacts":
            raise OSError("simulated p0a_plan_prefacts audit write failure")
        return orig_add_event(project_id, kind, message, *args, **kwargs)

    mem.add_event = _fail_prefacts_event

    # ケースA: 事実の矛盾がある計画 → 監査書込が失敗しても矛盾検出は変わらず停止し、
    # エラー文およびメタ情報に prefacts_audit_failed と型名(OSError)が残る。
    class ConflictLlm:
        async def complete_json(self, _messages, _schema):
            return json.dumps({
                "title": "案内文作成",
                "scope": "日時2026年10月21日 電話09-9999-9999 の案内を作成する。",
                "headings": ["日時", "連絡先"],
                "depends_on": [],
            }, ensure_ascii=False)

    mgr_bad = ProjectOrchestrator(
        mem, ConflictLlm(), lambda _p: (registered_body, []), None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / "ws_bad"),
    )
    try:
        asyncio.new_event_loop().run_until_complete(mgr_bad.generate_plan(pid))
    except ValueError as exc:
        assert "登録事実との矛盾" in str(exc)
        assert "prefacts_audit_failed=true:OSError" in str(exc)
    else:
        raise AssertionError("fact conflict must still stop even when audit write fails")
    assert mgr_bad._last_prefacts_audit_meta == {
        "prefacts_audit_failed": True,
        "prefacts_audit_error": "OSError",
    }

    # ケースB: 事実が整合する計画 → 計画生成自体は通過し、戻り値とmanifestに
    # prefacts_audit_failed=True と prefacts_audit_error='OSError' が残る。
    class ConsistentLlm:
        async def complete_json(self, _messages, _schema):
            return json.dumps({
                "title": "案内文作成",
                "scope": "日時2026年10月20日 電話03-1234-5678 の案内を作成する。",
                "headings": ["日時", "連絡先"],
                "depends_on": [],
            }, ensure_ascii=False)

    mgr_ok = ProjectOrchestrator(
        mem, ConsistentLlm(), lambda _p: (registered_body, []), None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / "ws_ok"),
    )
    out = asyncio.new_event_loop().run_until_complete(mgr_ok.generate_plan(pid))
    assert out.get("prefacts_audit_failed") is True
    assert out.get("prefacts_audit_error") == "OSError"
    assert '"prefacts_audit_failed": true' in str(out.get("plan_summary") or "")
    assert '"prefacts_audit_error": "OSError"' in str(out.get("plan_summary") or "")


def test_p0b_corrupted_binding_snapshot_in_bind_issues_for_matrix_never_succeeds(tmp_path):
    """(3) _bind_issues_for_matrix 内のスナップショット統合で AttributeError/TypeError/KeyError 相当の
    破損データがあった場合、黙って握りつぶして成功扱いにせず失敗(ValueError)になる。
    """
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox
    import app.plan_feedback as fb

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p0b-corrupt-snap")["id"]
    mem.save_mission(pid, "疑似目標\n1. 架空手順Aを定義する",
                     "疑似目標\n1. 架空手順Aを定義する", "", True, ["chatgpt"])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(), llm=None,
                  workers={}, _sync_memos=lambda pid: None,
                  provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
                  plan_review_runner=None,
                  _outbound_guard=lambda pid, purpose, text, providers=(): {"ok": True, "codes": []})
    criteria = ["架空手順Aを定義する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    compiled = compile_plan(criteria, [t1], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p0b corrupt snap plan", compiled["tasks"])
    from app.goal_contract import activate as _activate
    contract = _activate(mgr, pid)
    snap, sig = plan_snapshot(mgr, pid)

    store = ReviewStore(mem.path)
    store.put(pid, "plan", sig, {
        "status": "not_passed",
        "reviews": [{"provider": "chatgpt", "status": "fail",
                     "issues": [{"step": "2", "unmet_goal": "SC01の不足",
                                 "reason": "SC01の検証不足", "remedy": "追記する"}]}],
    })
    raw_issues = fb.issues_for(mgr, pid, sig)
    assert raw_issues

    # 同じ計画署名のままpacketまたは評価結果が変わった場合、確定の再利用を拒否する。
    from app.experience_store import canonical, fingerprint
    import app.plan_review_loop as loop
    packet = {"tasks": [{"task_key": "SC01", "role": "execution"}]}
    stored_reviews = [{"provider": "chatgpt", "status": "fail",
                       "issues": [{"step": "2", "unmet_goal": "SC01の不足",
                                   "reason": "SC01の検証不足", "remedy": "追記する"}]}]
    store.put(pid, "plan", sig, {"status": "not_passed", "packet": packet,
                                 "reviews": stored_reviews})
    parsed = [{"provider": row["provider"], "status": row["status"],
               "issues": row["issues"]} for row in stored_reviews]
    table = loop.build_issue_bindings(parsed, contract, snap,
                                      plan_signature=sig,
                                      goal_contract_hash=str(contract.get("content_hash") or ""),
                                      packet_hash=fingerprint(packet))
    valid_snapshot = fb.build_binding_snapshot(
        bindings=table["bindings"], plan_signature=sig,
        goal_contract_hash=str(contract.get("content_hash") or ""),
        packet_hash=fingerprint(packet),
        review_fingerprint=fingerprint([sig, canonical(stored_reviews)]))
    store.put(pid, "binding_snapshot", sig, {"snapshot": valid_snapshot})
    assert fb._bind_issues_for_matrix(mgr, pid, sig, raw_issues, snap)
    changed_packet = {"tasks": [{"task_key": "SC00", "role": "preparation"}]}
    store.put(pid, "plan", sig, {"status": "not_passed", "packet": changed_packet,
                                 "reviews": stored_reviews})
    try:
        fb._bind_issues_for_matrix(mgr, pid, sig, raw_issues, snap)
    except ValueError:
        pass
    else:
        raise AssertionError("changed packet must invalidate confirmed binding")
    changed_reviews = [{**stored_reviews[0], "issues": [
        {"step": "2", "unmet_goal": "別の指摘", "reason": "別の不足", "remedy": "別対応"}]}]
    store.put(pid, "plan", sig, {"status": "not_passed", "packet": packet,
                                 "reviews": changed_reviews})
    try:
        fb._bind_issues_for_matrix(mgr, pid, sig, raw_issues, snap)
    except ValueError:
        pass
    else:
        raise AssertionError("changed review must invalidate confirmed binding")
    store.put(pid, "plan", sig, {"status": "not_passed", "packet": packet,
                                 "reviews": stored_reviews})

    # 破損した binding_snapshot (bindings の要素が dict ではなく文字列等) を保存する。
    store.put(pid, "binding_snapshot", sig, {
        "snapshot": {
            "version": "binding-snapshot-v1",
            "plan_signature": sig,
            "goal_contract_hash": str(contract.get("content_hash") or ""),
            "packet_hash": "",
            "bindings": ["not-a-dict-row"],
        },
    })
    try:
        fb._bind_issues_for_matrix(mgr, pid, sig, raw_issues, snap)
    except ValueError as exc:
        assert "対応付け" in str(exc) or "スナップショット" in str(exc)
    else:
        raise AssertionError("corrupted binding_snapshot must not be silently ignored")

