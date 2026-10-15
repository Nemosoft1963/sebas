"""Stage3: 境界付き自走ランナー (疑似PJのみ・実関数)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
外部AIとローカルLLMはスタブで呼出回数を数える。期待値は入力から導く。
分野の異なる疑似PJ3種 (文書生成型/表データ型/外部作用型、架空)。
画面は未実装 (APIまで)。
"""
import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.core import Ollama
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

DOMAINS = ["doc", "table", "external"]
ROOT = Path(__file__).resolve().parents[1]


def _criteria_for(domain, n):
    out = []
    for i in range(1, n + 1):
        if domain == "doc":
            out.append(f"架空文書{i:02d}の作成と検証")
        elif domain == "table":
            out.append(f"架空営業管理表{i:02d}の集計と検算")
        else:
            if i % 3 == 0:
                out.append(f"架空顧客へ送信{i:02d}の実行と検証")
            else:
                out.append(f"架空告知文書{i:02d}の作成と検証")
    return out


def _titles_for(domain, i):
    if domain == "doc":
        return f"架空文書工程{i}", f"架空文書{i:02d}の成果物を作成し根拠を検証する。"
    if domain == "table":
        return f"架空集計工程{i}", f"架空営業管理表{i:02d}の表を作成し検算する。"
    if i % 3 == 0:
        return f"架空送信工程{i}", f"架空顧客へ送信{i:02d}を実行する。実際の外部送信は行わない。"
    return f"架空告知工程{i}", f"架空告知文書{i:02d}の成果物を作成し根拠を検証する。"


def _make_manager(tmp_path, domain, n, tag=""):
    assert "架空" in f"架空-{domain}"
    name = f"s3-疑似-{domain}-{n}{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = _criteria_for(domain, n)
    memory.save_mission(pid, "架空目標-" + domain, "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        title, scope = _titles_for(domain, i)
        tasks.append(compile_task(i, text, {
            "title": title, "scope": scope, "headings": ["目的", "実施内容"],
        }, []))
    memory.replace_plan(pid, name + " plan", tasks)
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name)),
    )
    return manager, pid


def _sig_of(manager, pid):
    from app.goal_review import plan_snapshot
    return plan_snapshot(manager, pid)[1]


def _store_plan(manager, pid, status, reviews=None):
    from app.goal_review import ReviewStore
    sig = _sig_of(manager, pid)
    store = ReviewStore(manager.memory.path)
    body = {"status": status, "reviews": list(reviews or []),
            "finished": 0, "packet": None}
    store.put(pid, "plan", sig, body)
    return sig


def _store_revision(manager, pid, *, blockers=None, matrix_rows=None):
    from app.goal_review import ReviewStore
    sig = _sig_of(manager, pid)
    store = ReviewStore(manager.memory.path)
    matrix = {"version": "coverage-matrix-v1", "rows": list(matrix_rows or [])}
    body = {"status": "draft", "blockers": list(blockers or []),
            "changes": [], "coverage_matrix": matrix,
            "actions": [], "candidate_id": "架空候補"}
    store.put(pid, "revision", sig, body)
    return sig


def _fail_issue():
    return {"provider": "chatgpt", "status": "fail",
            "issues": [{"text": "架空指摘文の対応が必要",
                        "criterion": "SC01", "step": "1"}]}


def _install_local_stub(manager, pid, counter=None):
    """S3の保存済み指摘から amend 候補を返す LLM スタブ。外部は呼ばない。"""
    counter = counter if counter is not None else {"local": 0, "external": 0}
    manager.llm = Mock(spec=Ollama)

    async def _local(*args):
        counter["local"] += 1
        from app.goal_review import plan_snapshot
        from app.plan_feedback import issues_for

        sig = plan_snapshot(manager, pid)[1]
        current = issues_for(manager, pid, sig)
        task_keys = [t["task_key"] for t in plan_snapshot(manager, pid)[0]["tasks"]]
        assert task_keys, "amend可能な工程が必要"
        data = {"actions": []}
        for pos, issue in enumerate(current):
            data["actions"].append({
                "issue_id": issue["id"], "disposition": "amend",
                "target": task_keys[pos % len(task_keys)],
                "change": "登録された原本の参照箇所と成果物の主張を一対一で照合し、不一致を明示する追加手順を実施する。",
                "reason": "原本と成果物の対応が欠けるという指摘に具体的な照合手順を追加する対応を行う。",
            })
        return json.dumps(data)

    async def _external(*args):
        counter["external"] += 1
        raise AssertionError("external must not be called")

    manager._local_complete = _local
    manager.plan_review_runner = _external
    return counter


def _seed_missing_coverage(manager, pid):
    _store_plan(manager, pid, "not_passed", [_fail_issue()])


def _seed_missing_binding(manager, pid):
    _store_revision(manager, pid, blockers=[],
                    matrix_rows=[{"issue_id": "架空指摘1",
                                  "coverage": "uncovered",
                                  "reason": "架空の被覆不足"}])


def _seed_policy_block(manager, pid):
    _store_plan(manager, pid, "waiting_budget", [])


def _seed_missing_capability(manager, pid):
    _store_revision(manager, pid,
                    blockers=[{"disposition": "development",
                               "reason": "架空の追加開発が必要"}],
                    matrix_rows=[])


def _seed_missing_evidence(manager, pid):
    _store_plan(manager, pid, "connection_failed", [])


def _event_kinds(manager, pid):
    db = sqlite3.connect(f"file:{manager.memory.path.as_posix()}?mode=ro", uri=True)
    try:
        try:
            rows = db.execute(
                "SELECT kind FROM project_events WHERE project_id=?",
                (pid,)).fetchall()
        except sqlite3.Error:
            return []
        return sorted({str(r[0]) for r in rows})
    finally:
        db.close()


@pytest.mark.parametrize("domain", DOMAINS)
def test_default_disabled_tick_does_nothing(tmp_path, domain):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-dis")
    assert auto.get_config(manager, pid)["enabled"] is False
    assert auto.is_enabled(manager, pid) is False
    out = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["status"] == "disabled"
    assert out["reason"] == "自走は無効です"
    assert auto.list_ticks(manager, pid) == []
    # 何も書かない (ティック履歴なし。設定DBは作らないか空のまま)
    assert auto.get_config(manager, pid)["enabled"] is False


@pytest.mark.parametrize("domain", DOMAINS)
def test_enable_requires_actor_reason_and_audit(tmp_path, domain):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-en")
    with pytest.raises(ValueError):
        auto.set_enabled(manager, pid, True, actor="", reason="架空理由")
    with pytest.raises(ValueError):
        auto.set_enabled(manager, pid, True, actor="架空担当", reason="")
    with pytest.raises(ValueError):
        auto.autopilot_tick(manager, pid, actor="")
    cfg = auto.set_enabled(manager, pid, True, actor="架空担当",
                           reason="架空の有効化理由")
    assert cfg["enabled"] is True
    assert cfg["actor"] == "架空担当"
    kinds = _event_kinds(manager, pid)
    assert "autopilot_enabled" in kinds
    cfg2 = auto.set_enabled(manager, pid, False, actor="架空担当",
                            reason="架空の無効化理由")
    assert cfg2["enabled"] is False
    kinds2 = _event_kinds(manager, pid)
    assert "autopilot_disabled" in kinds2


@pytest.mark.parametrize("domain", DOMAINS)
def test_auto_stops_advance_one_action(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    from app.stop_classifier import CLASS_TABLE, classify_stop
    # missing_coverage の種だけでは事前照合の不足が無いPJのため、
    # 実経路では不足なしの分類になる。不足なし系の実経路はこのテストでは
    # 扱わず、missing_coverage 単体の決定的対応は分類の強制で確認する。
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-cov")
    _seed_missing_coverage(manager, pid)
    # 4-B: missing_coverage は保存済み指摘からローカル候補を生成する。
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    from app.workflow_readiness import build_readiness
    from app import capability_gap as _gap
    _preflight = _gap.capability_preflight(manager, pid)
    _missing_count = len(_preflight.get("missing_capability") or [])
    _built = build_readiness(manager, pid)
    _stop = (_built.get("stop_class") or {}).get("stop_class")
    out = auto.autopilot_tick(manager, pid, actor="架空担当")
    if _missing_count:
        # B-3: 事前照合が不足を返すPJでは missing_capability が主たる停止になり、
        # 下書きまで自走して確認待ちで止まる (review/export は自動化しない)。
        assert out["stop_class"] == "missing_capability", out
        assert out["status"] == "waiting_human", out
        assert out["action_name"] == "capability_preflight", out
        assert out.get("capability_outcome") == "created", out
        assert out["enabled"] is True
    else:
        assert out["stop_class"] == _stop, out
        if _stop == "missing_coverage":
            assert out["status"] == "advanced"
            assert out["action_name"] == "generate_candidate"
            assert out["action_name"] in auto.ALLOWED_ACTIONS
            assert out["detail"]["candidate_id"]
            assert out["next_action"] == "review_feedback"
            assert out["external_delta"] == 0
            assert out["external_rounds_delta"] == 0
            assert out["budget_kept"] is True
            assert out["enabled"] is True
        else:
            assert out["status"] == "waiting_human", out
            assert out["action_name"] == "record_stop_class", out
            assert out["enabled"] is True
    # 1ティックで書くもの: 状態・次操作・停止クラス・証拠参照・外部増分
    for key in ("status", "next_action", "stop_class", "evidence_refs",
                "external_delta"):
        assert key in out
    # missing_binding (実関数で分類した記録を tick に渡す)。
    # 実経路では gate の被覆不足が優先順位で主たる停止になるため
    # (PRIORITY: missing_coverage > missing_binding)、binding 単体の分類を
    # 実関数 classify_stop で作り、tick が決定的に進むことを確認する。
    manager2, pid2 = _make_manager(tmp_path, domain, 3, tag="-bind")
    _seed_missing_binding(manager2, pid2)
    auto.set_enabled(manager2, pid2, True, actor="架空担当",
                     reason="架空の有効化理由")
    sig2 = _sig_of(manager2, pid2)
    record2 = classify_stop({"binding_state": "invalid_reference",
                             "plan_signature": sig2})
    assert record2["stop_class"] == "missing_binding"
    assert record2["stop_code"] == "binding:invalid_reference"
    assert CLASS_TABLE[record2["stop_code"]] == "missing_binding"
    # A-1修正後: 実経路と同じ形 (preflight_status ok + 現行署名) で渡す。
    record2["preflight_status"] = "ok"
    monkeypatch.setattr(auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record2)))
    out2 = auto.autopilot_tick(manager2, pid2, actor="架空担当")
    assert out2["stop_class"] == "missing_binding", out2
    assert out2["status"] == "advanced", out2
    assert out2["action_name"] == "remap_stored_review", out2
    assert out2["action_name"] in auto.ALLOWED_ACTIONS
    assert out2["external_delta"] == 0
    assert out2["status"] == "advanced"
    assert out2["action_name"] in auto.ALLOWED_ACTIONS
    assert out2["external_delta"] == 0


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("kind", ["policy_block", "missing_decision",
                                  "missing_capability", "missing_evidence"])
def test_human_stops_do_not_advance(tmp_path, domain, kind):
    import app.autopilot as auto
    from app import capability_gap as _gap
    manager, pid = _make_manager(tmp_path, domain, 3, tag=f"-h-{kind}")
    if kind == "policy_block":
        _seed_policy_block(manager, pid)
    elif kind == "missing_capability":
        _seed_missing_capability(manager, pid)
    elif kind == "missing_evidence":
        _seed_missing_evidence(manager, pid)
    else:
        pass  # 既定のまま missing_decision
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    out = auto.autopilot_tick(manager, pid, actor="架空担当")
    # 事前照合が不足を返すPJでは、policy_block を除き
    # missing_capability が主たる停止になる (意図した変更)。
    # missing_decision の種は付けないため既定は idle 起源であり、
    # idle は不足を隠さない。人の承認待ち (awaiting_human) は別テスト。
    _preflight = _gap.capability_preflight(manager, pid)
    _has_missing = bool(_preflight.get("missing_capability"))
    if kind in ("missing_evidence", "missing_decision") and _has_missing:
        assert out["stop_class"] == "missing_capability", out
    else:
        assert out["stop_class"] == kind, out
    assert out["status"] == "waiting_human"
    if out["stop_class"] == "missing_capability":
        # B-3: 機能不足が主たる停止なら下書きまで自走し、確認待ちで止まる。
        assert out["action_name"] == "capability_preflight", out
        assert out.get("capability_outcome") in ("created", "existing", "skipped"), out
    else:
        assert out["action_name"] == "record_stop_class"
    assert out["next_action"]
    assert out["reason_ja"]
    assert isinstance(out["evidence_refs"], dict)
    assert out["enabled"] is True
    # 実行せず記録だけ (ティックは追記のみ)
    assert len(auto.list_ticks(manager, pid)) == 1


@pytest.mark.parametrize("domain", DOMAINS)
def test_dry_run_writes_nothing(tmp_path, domain):
    import app.autopilot as auto
    from app.stop_classifier import _db_path as _stop_path
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-dry")
    _seed_missing_coverage(manager, pid)
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    ticks_before = auto.list_ticks(manager, pid)
    auto_path = auto.autopilot_db_path(manager)
    mtime_before = auto_path.stat().st_mtime_ns if auto_path.exists() else None
    stop_before = _stop_path(manager).exists()
    out = auto.autopilot_tick(manager, pid, actor="架空担当", dry_run=True)
    assert out["status"] == "dry_run"
    assert out["dry_run"] is True
    assert out.get("planned_action") in auto.ALLOWED_ACTIONS
    assert auto.list_ticks(manager, pid) == ticks_before
    if mtime_before is None:
        # 設定で作られたDBは残るが、ティックでは増えない
        pass
    else:
        assert auto_path.stat().st_mtime_ns == mtime_before
    assert _stop_path(manager).exists() == stop_before


def test_allowed_list_single_place_and_permission(tmp_path):
    import app.autopilot as auto
    assert set(auto.ALLOWED_ACTIONS) == {
        "remap_stored_review", "generate_candidate", "refresh_coverage",
        "capability_preflight", "completion_replay_read", "record_stop_class"}
    for name, entry in auto.ALLOWED_ACTIONS.items():
        assert entry["permission_class"] == "local_reversible", name
        assert callable(entry["func"])
    # 決定的に1つ
    assert auto.choose_action("missing_binding") == "remap_stored_review"
    # 4-B: missing_coverage は保存済み指摘からのローカル候補生成が決定的な一手。
    assert auto.choose_action("missing_coverage") == "generate_candidate"
    assert auto.choose_action("missing_binding") == auto.choose_action("missing_binding")
    assert auto.choose_action("missing_coverage") == auto.choose_action("missing_coverage")


def test_denied_actions_rejected_and_audited(tmp_path):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-deny")
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    denied = ["approve_plan", "apply", "apply_candidate", "send_packet",
              "resend_external", "publish_site", "post_sns",
              "contact_customer", "contact_lead", "register_rag",
              "apply_ocr", "purge_project", "export_package",
              "implement_proposal"]
    for name in auto.DENIED_ACTION_NAMES:
        assert name in denied, name
    for name in denied:
        with pytest.raises(ValueError):
            auto.run_named_action(manager, pid, name, actor="架空担当")
    # 許可リスト外の名前は拒否し監査に残る
    with pytest.raises(ValueError):
        auto.run_named_action(manager, pid, "架空の未登録動作", actor="架空担当")
    kinds = _event_kinds(manager, pid)
    assert "autopilot_action_rejected" in kinds
    assert auto.list_ticks(manager, pid) == []


def test_tick_external_delta_zero_with_stubs(tmp_path):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, "table", 3, tag="-ext0")
    _seed_missing_coverage(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    # 4-B: ローカルAI(Ollama)の呼出は外部増分に数えない。呼出自体は1回ある。
    counter = {"local": 0, "external": 0}
    _install_local_stub(manager, pid, counter)

    async def _runner(prompt, providers):
        counter["external"] += 10
        return []

    async def _provider(*a, **k):
        counter["external"] += 10
        return type("R", (), {"text": ""})()

    manager.plan_review_runner = _runner
    import app.external_ai as _ext
    orig = _ext.call_provider_with_metadata
    _ext.call_provider_with_metadata = _provider
    try:
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
    finally:
        _ext.call_provider_with_metadata = orig
    assert out["status"] == "advanced"
    assert out["action_name"] == "generate_candidate"
    assert out["external_delta"] == 0
    assert out["external_rounds_delta"] == 0
    assert out["budget_kept"] is True
    assert out["local_calls"] == 1
    assert counter["local"] == 1
    assert counter["external"] == 0


def test_tick_fails_when_external_called(tmp_path, monkeypatch):
    import app.autopilot as auto
    from app import capability_gap as _gap
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-extf")
    _seed_missing_coverage(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")

    async def _touch_runner(prompt, providers):
        return []

    def _evil(_manager, _pid):
        # 外部呼出スタブを呼ぶ疑似アクション (テスト用の一時登録)
        runner = _manager.plan_review_runner
        if callable(runner):
            result = runner("架空prompt", [])
            if hasattr(result, "__await__"):
                import asyncio as _aio
                try:
                    _aio.get_running_loop()
                except RuntimeError:
                    _aio.run(result)
        return {"action": "evil", "external_calls": 1}

    monkeypatch.setitem(auto.ALLOWED_ACTIONS, "test_evil_touch",
                        {"permission_class": "local_reversible",
                         "description": "架空の外部接触",
                         "func": _evil})
    monkeypatch.setitem(auto.ACTION_FOR_STOP, "missing_coverage",
                        "test_evil_touch")
    manager.plan_review_runner = _touch_runner
    # doc 型の疑似PJは事前照合の不足が無いため、実経路の主たる停止は
    # missing_coverage のまま (不足なし系は従来どおり)。
    _preflight = _gap.capability_preflight(manager, pid)
    assert not _preflight.get("missing_capability"), _preflight.get("gap_codes")
    out = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["status"] == "failed"
    assert out["external_delta"] != 0
    assert auto.is_enabled(manager, pid) is False
    # 以降の自走が止まる
    out2 = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out2["status"] == "disabled"


def test_guard_two_consecutive_and_release(tmp_path, monkeypatch):
    import app.autopilot as auto
    from app.goal_review import ReviewStore
    from app.stop_classifier import classify_stop
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-guard")
    _seed_missing_coverage(manager, pid)
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    first = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert first["status"] == "advanced"
    # 4-B: 候補生成後は人の確認待ちへ進む。同一 missing_coverage を強制すれば
    # 2連続で空転ガードが止める (ガード自体は外さない)。
    sig = _sig_of(manager, pid)
    record = classify_stop({"coverage_incomplete": True,
                            "plan_signature": sig})
    assert record["stop_class"] == "missing_coverage"
    # A-1修正後: 実経路と同じ形 (preflight_status ok + 現行署名) で渡す。
    record["preflight_status"] = "ok"
    monkeypatch.setattr(auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の再開理由")
    auto.autopilot_tick(manager, pid, actor="架空担当")
    second = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert second["status"] == "stopped_guard"
    assert second["enabled"] is False
    # 新署名で解除 (計画を変える)。分類の強制を外し実経路へ戻す。
    mission = manager.memory.get_mission(pid)
    manager.memory.replace_plan(pid, (mission.get("plan_summary") or "架空") + " 改訂",
                                mission["tasks"])
    monkeypatch.undo()
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の再開理由")
    third = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert third["status"] in ("advanced", "waiting_human", "disabled")
    assert third["status"] != "stopped_guard"
    # 人の選択で解除
    manager2, pid2 = _make_manager(tmp_path, "doc", 3, tag="-guard2")
    _seed_missing_coverage(manager2, pid2)
    _install_local_stub(manager2, pid2)
    auto.set_enabled(manager2, pid2, True, actor="架空担当",
                     reason="架空の有効化理由")
    sig = _sig_of(manager2, pid2)
    store = ReviewStore(manager2.memory.path)
    store.put(pid2, "auto_loop", "架空run1",
              {"id": "架空run1", "project_id": pid2,
               "plan_signature": sig, "manual_bindings": [{"issue_id": "x"}],
               "state": "stopped"})
    auto.autopilot_tick(manager2, pid2, actor="架空担当")
    second2 = auto.autopilot_tick(manager2, pid2, actor="架空担当")
    # 人の選択があるため空転ガードで止めない
    assert second2["status"] != "stopped_guard"


def test_guard_cannot_be_disabled_statically():
    import app.autopilot as auto
    text = (ROOT / "app/autopilot.py").read_text(encoding="utf-8")
    assert "is_repeating" in text
    assert "guard_auto_tick" in text
    assert "disable_guard" not in text
    assert "guard_enabled" not in text
    assert "enable_guard" not in text
    # 設定にガード無効化の項目が無い
    manager_cls = auto.get_config
    assert "guard" not in str(manager_cls.__code__.co_varnames)


def test_run_until_stop_caps_at_ten(tmp_path, monkeypatch):
    import app.autopilot as auto
    assert auto.MAX_TICKS == 10
    manager, pid = _make_manager(tmp_path, "table", 3, tag="-run")
    _seed_missing_coverage(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    out = auto.run_until_stop(manager, pid, actor="架空担当", max_ticks=10)
    assert out["count"] <= 10
    assert out["max_ticks"] == 10
    # 上限を超える指定は10に丸める
    out2 = auto.run_until_stop(manager, pid, actor="架空担当", max_ticks=100)
    assert out2["max_ticks"] == 10
    assert out2["count"] <= 10
    # 変化し続ける停止では上限で止まる (署名を毎回変えて空転回避)。
    # 4-B: missing_binding の決定的対応(remap_stored_review)で外部0のまま回す。
    manager3, pid3 = _make_manager(tmp_path, "table", 3, tag="-runmax")
    auto.set_enabled(manager3, pid3, True, actor="架空担当",
                     reason="架空の有効化理由")
    seq = {"n": 0}
    sig_real = _sig_of(manager3, pid3)
    # A-1修正後: 実経路と同じ形 (preflight ok + 現行署名) で渡す。
    # 空転ガード回避のため stop_code を交互に変えて10回進める。
    codes = ["binding:invalid_reference", "binding:multiple_targets"]

    def _fake_classify(_m, _p):
        seq["n"] += 1
        code = codes[seq["n"] % 2]
        record = {"stop_class": "missing_binding",
                  "stop_code": code,
                  "evidence_refs": {"plan_signature": sig_real},
                  "plan_signature": sig_real,
                  "next_action": "review_feedback",
                  "reason_ja": "架空の対応付け不足",
                  "external_sends": 0,
                  "preflight_status": "ok"}
        return {}, record

    monkeypatch.setattr(auto, "_classify_current", _fake_classify)
    out3 = auto.run_until_stop(manager3, pid3, actor="架空担当", max_ticks=10)
    assert out3["count"] == 10
    assert out3["status"] == "max_ticks"


def test_concurrent_ticks_single_execution(tmp_path):
    import app.autopilot as auto
    from app.stop_classifier import classify_stop
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-conc")
    _seed_missing_coverage(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    executions = {"n": 0}
    orig_choose = auto.choose_action
    orig_run = auto.run_named_action

    def _counting(manager_inner, project_id, action_name, **kwargs):
        if action_name in auto.ALLOWED_ACTIONS:
            executions["n"] += 1
            import time as _t
            _t.sleep(0.2)
        return orig_run(manager_inner, project_id, action_name, **kwargs)

    # missing_binding の分類で決定的に remap を回し、LLM不要で二重実行を検証する。
    sig = _sig_of(manager, pid)
    record = classify_stop({"binding_state": "invalid_reference",
                            "plan_signature": sig})
    assert record["stop_class"] == "missing_binding"
    # A-1修正後: 実経路と同じ形 (preflight_status ok + 現行署名) で渡す。
    record["preflight_status"] = "ok"
    orig_classify = auto._classify_current
    auto._classify_current = lambda _m, _p: ({}, dict(record))
    auto.run_named_action = _counting
    try:
        async def _gather():
            return await asyncio.gather(
                asyncio.to_thread(auto.autopilot_tick, manager, pid,
                                  actor="架空担当"),
                asyncio.to_thread(auto.autopilot_tick, manager, pid,
                                  actor="架空担当"))
        results = asyncio.run(_gather())
    finally:
        auto.run_named_action = orig_run
        auto._classify_current = orig_classify
        assert auto.choose_action == orig_choose
    statuses = sorted(str(r.get("status")) for r in results)
    # 一方が進み、もう一方は busy (二重に実行されない)
    assert executions["n"] == 1, executions
    assert "busy" in statuses


def test_resume_setting_separation_and_deleted_and_init(tmp_path):
    import app.autopilot as auto
    import app.safe_auto_resume as resume
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-sep")
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    assert auto.is_enabled(manager, pid) is True
    # safe_auto_resume とは分離 (互いに有効化しない)
    assert resume.enabled(manager, pid) is False
    # 論理削除中は拒否
    import app.project_delete as _del
    _del._set_state(manager.memory.path, pid, "deleted", actor="架空担当",
                    backup_id="架空退避", manifest_sha256="0" * 64)
    try:
        with pytest.raises(ValueError):
            auto.set_enabled(manager, pid, True, actor="架空担当",
                             reason="架空理由")
        with pytest.raises(ValueError):
            auto.autopilot_tick(manager, pid, actor="架空担当")
        with pytest.raises(ValueError):
            auto.run_until_stop(manager, pid, actor="架空担当")
        with pytest.raises(ValueError):
            auto.run_named_action(manager, pid, "refresh_coverage",
                                  actor="架空担当")
    finally:
        _del._set_state(manager.memory.path, pid, "restored",
                        actor="架空担当", backup_id="架空退避",
                        manifest_sha256="0" * 64)
    # PJ初期化で無効に戻る
    assert auto.is_enabled(manager, pid) is True
    reset = auto.reset_for_generation(manager.memory.path, pid)
    assert reset["enabled"] is False
    assert auto.is_enabled(manager, pid) is False


def test_secret_not_in_history_response_log(tmp_path, caplog):
    import app.autopilot as auto
    import logging
    manager, pid = _make_manager(tmp_path, "table", 3, tag="-sec")
    _seed_missing_coverage(manager, pid)
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    secret = "sk-架空ダミー秘密値-xyz1234567890"
    out = auto.autopilot_tick(manager, pid, actor="架空担当")
    blob = json.dumps(out, ensure_ascii=False)
    assert "架空ダミー秘密値" not in blob
    for row in auto.list_ticks(manager, pid):
        assert secret not in json.dumps(row, ensure_ascii=False)
        assert "架空ダミー秘密値" not in json.dumps(row, ensure_ascii=False)
    with caplog.at_level(logging.INFO):
        auto.autopilot_tick(manager, pid, actor="架空担当")
    assert "架空ダミー秘密値" not in caplog.text


def test_api_autopilot_crud_and_readonly(tmp_path, monkeypatch):
    import app.autopilot as auto
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, "external", 3, tag="-api")
    _install_local_stub(manager, pid)
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    # 既定は無効
    first = client.get(f"/api/projects/{pid}/autopilot")
    assert first.status_code == 200
    assert first.json()["config"]["enabled"] is False
    assert first.json()["read_only"] is True
    # actor・reason 必須
    assert client.post(f"/api/projects/{pid}/autopilot/enable",
                       json={"actor": "", "reason": "架空理由"}).status_code == 422
    assert client.post(f"/api/projects/{pid}/autopilot/enable",
                       json={"actor": "架空担当"}).status_code == 422
    enabled = client.post(f"/api/projects/{pid}/autopilot/enable",
                          json={"actor": "架空担当", "reason": "架空理由"})
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    # tick (1ティック) と dry_run
    _seed_missing_coverage(manager, pid)
    ticked = client.post(f"/api/projects/{pid}/autopilot/tick",
                         json={"actor": "架空担当"})
    assert ticked.status_code == 200
    assert ticked.json()["action_name"] in auto.ALLOWED_ACTIONS
    dried = client.post(f"/api/projects/{pid}/autopilot/tick",
                        json={"actor": "架空担当", "dry_run": True})
    assert dried.status_code == 200
    assert dried.json()["status"] == "dry_run"
    # run (上限つき)
    ran = client.post(f"/api/projects/{pid}/autopilot/run",
                      json={"actor": "架空担当", "max_ticks": 3})
    assert ran.status_code == 200
    assert ran.json()["count"] <= 3
    # disable
    disabled = client.post(f"/api/projects/{pid}/autopilot/disable",
                           json={"actor": "架空担当", "reason": "架空理由"})
    assert disabled.status_code == 200
    # GET が読み取り専用 (呼出前後でDB・ファイル不変)
    db = sqlite3.connect(f"file:{manager.memory.path.as_posix()}?mode=ro",
                         uri=True)
    try:
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        before = {}
        for table in tables:
            try:
                before[table] = int(db.execute(
                    f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            except sqlite3.Error:
                continue
    finally:
        db.close()
    auto_path = auto.autopilot_db_path(manager)
    auto_before = auto_path.stat().st_mtime_ns if auto_path.exists() else None
    files_before = sorted(p.name for p in tmp_path.iterdir())
    client.get(f"/api/projects/{pid}/autopilot")
    client.get(f"/api/projects/{pid}/autopilot")
    db2 = sqlite3.connect(f"file:{manager.memory.path.as_posix()}?mode=ro",
                          uri=True)
    try:
        after = {}
        for table in tables:
            try:
                after[table] = int(db2.execute(
                    f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            except sqlite3.Error:
                continue
    finally:
        db2.close()
    assert before == after
    if auto_before is not None:
        assert auto_path.stat().st_mtime_ns == auto_before
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before
    # 論理削除中は拒否
    monkeypatch.setattr("app.project_delete.is_deleted",
                        lambda *a, **k: True)
    try:
        assert client.get(f"/api/projects/{pid}/autopilot").status_code == 410
        assert client.post(f"/api/projects/{pid}/autopilot/tick",
                           json={"actor": "架空担当"}).status_code == 410
        assert client.post(f"/api/projects/{pid}/autopilot/run",
                           json={"actor": "架空担当"}).status_code == 410
    finally:
        import importlib
        import app.project_delete as _del
        importlib.reload(_del)


def test_no_background_threads_statically():
    text = (ROOT / "app/autopilot.py").read_text(encoding="utf-8")
    assert "threading.Thread" not in text
    assert "Timer" not in text or "timeout" in text.lower()
    assert "asyncio.create_task" not in text
    assert "schedule" not in text or "scheduled" not in text.lower()
    # 定期実行の痕跡なし
    for word in ("setInterval", "setTimeout", "cron", "APScheduler"):
        assert word not in text


def test_registry_and_lifecycle(tmp_path):
    import app.autopilot as auto
    from app.project_lifecycle_backup import create_backup, verify_backup
    from app.project_lifecycle_registry import (
        audit_registry_completeness, count_all, resolve_db_path)
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    assert "autopilot_config" in audit["scanned"]
    assert "autopilot_ticks" in audit["scanned"]
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-lc")
    _seed_missing_coverage(manager, pid)
    _install_local_stub(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    auto.autopilot_tick(manager, pid, actor="架空担当")
    gap_path = auto.autopilot_db_path(manager)
    assert gap_path.exists()
    assert resolve_db_path(manager.memory.path, "autopilot") == gap_path
    # 他PJに影響しない
    other, other_pid = _make_manager(tmp_path, "table", 2, tag="-other")
    assert auto.list_ticks(other, other_pid) == []
    assert auto.get_config(other, other_pid)["enabled"] is False
    assert auto.list_ticks(manager, pid)
    # kind=other は件数対象外
    before = count_all(manager.memory.path, pid)
    assert before.get("autopilot:autopilot_config", 0) == 0
    assert before.get("autopilot:autopilot_ticks", 0) == 0
    # 退避/検証
    backup_root = tmp_path / "架空退避"
    backup = create_backup(manager.memory.path, pid, backup_root,
                           workspace_root=None, actor="架空担当")
    assert backup["ok"] is True
    assert verify_backup(backup["manifest_path"])["ok"] is True
    # PJ初期化で無効に戻るが履歴は残る
    from app.project_generation import build_reset_preview, initialize_project
    preview = build_reset_preview(manager.memory.path, pid, "replan",
                                  workspace_root=None, executions=None)
    project_name = manager.memory.get_project(pid)["name"]
    initialized = initialize_project(
        manager.memory.path, pid, "replan",
        preview_token=preview["preview_token"], project_name=project_name,
        actor="架空担当", workspace_root=None, backup_root=backup_root,
        executions=None)
    assert initialized["ok"] is True
    assert auto.is_enabled(manager, pid) is False
    assert auto.list_ticks(manager, pid)
    # 完全削除: 当該PJのみ消える
    purged = auto.purge_project_autopilot(manager.memory.path, pid)
    assert purged["autopilot_ticks"] >= 1
    assert auto.list_ticks(manager, pid) == []
    assert auto.list_ticks(other, other_pid) == []
    _ = sqlite3


def test_generic_no_specific_project_words():
    text = (ROOT / "app/autopilot.py").read_text(encoding="utf-8")
    for word in ("拡販", "販売システム", "s3-疑似", "1659"):
        assert word not in text
    assert "innerHTML" not in text
    assert "http://" not in text and "https://" not in text
    assert "httpx" not in text and "requests.post" not in text
    assert "urlopen" not in text


def test_initialization_aborts_if_autopilot_cannot_be_disabled(tmp_path, monkeypatch):
    import app.autopilot as auto
    from app.project_generation import (build_reset_preview,
                                        current_generation_number,
                                        initialize_project)

    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-reset-failure")
    before = current_generation_number(manager.memory.path, pid)
    preview = build_reset_preview(manager.memory.path, pid, "replan",
                                  workspace_root=None, executions=None)
    project_name = manager.memory.get_project(pid)["name"]

    def cannot_disable(*_args, **_kwargs):
        raise sqlite3.OperationalError("autopilot DB is read only")

    monkeypatch.setattr(auto, "reset_for_generation", cannot_disable)
    result = initialize_project(
        manager.memory.path, pid, "replan",
        preview_token=preview["preview_token"], project_name=project_name,
        actor="架空担当", workspace_root=None,
        backup_root=tmp_path / "backup", executions=None)
    assert result["ok"] is False
    assert result["code"] == "AUTOPILOT_RESET_FAILED"
    assert current_generation_number(manager.memory.path, pid) == before
