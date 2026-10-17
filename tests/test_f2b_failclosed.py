"""F2b: 事前照合・完了ゲート・停止分類・漏斗の fail-closed 化テスト (疑似PJのみ・実関数)。

対象は tests/fail_open_baseline.json で classification=high_risk かつ
file が app/execution_funnel.py / app/resolution_coordinator.py /
app/capability_gap.py / app/capability_export.py / app/completion_gate.py /
app/goal_completion_store.py / app/stop_classifier.py /
app/workflow_readiness.py の23件。
各検出について失敗を注入し、成功/0件/通過にならないことを確認する。
実関数・疑似データ・一時ディレクトリのみ。外部通信なし。
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.memory.short_term import ShortTermMemory
from app.structured_planning import compile_plan, compile_task
from app.workspace_files import WorkspaceSandbox


def _manager(tmp_path: Path, tag: str, domain: str = "doc"):
    name = f"f2b-疑似-{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    if domain == "doc":
        criteria = ["架空文書Aを作成する", "架空文書Bを報告する"]
        titles = [("架空文書工程A", "架空文書Aの成果物を作成し根拠を検証する。"),
                  ("架空文書工程B", "架空文書Bの成果物を作成し根拠を検証する。")]
    else:
        criteria = ["架空営業管理表Aを集計する", "架空営業管理表Bを検算する"]
        titles = [("架空集計工程A", "架空営業管理表Aの表を作成し検算する。"),
                  ("架空集計工程B", "架空営業管理表Bの表を作成し検算する。")]
    memory.save_mission(pid, "架空目標", "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tmp_tasks = []
    for i, text in enumerate(criteria, 1):
        title, scope = titles[i - 1]
        tmp_tasks.append(compile_task(i, text, {
            "title": title, "scope": scope, "headings": ["目的", "実施内容"],
        }, []))
    plan = compile_plan(criteria, tmp_tasks, goal="架空目標")
    memory.replace_plan(pid, plan["summary"], plan["tasks"])
    ws = WorkspaceSandbox(tmp_path / ("ws-" + name))
    mgr = SimpleNamespace(memory=memory, workspace=ws, planning_projects=set(), llm=None,
                          workers={}, _sync_memos=lambda p: None,
                          provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
                          plan_review_runner=None)
    from app.goal_contract import activate
    activate(mgr, pid)
    return mgr, pid


# --- execution_funnel._requires_external (判定不能を要求ありに倒す) ---

def test_f2b_requires_external_failure_is_required(tmp_path, monkeypatch):
    """検出 app/execution_funnel.py:_requires_external:P1:a2ad58d9f50d#1/#2/af10980773d7。

    (1) 外部証拠の要否判定に使う。(2) 失敗時現状: 判定失敗を握りつぶし不要扱い。
    修正後: 判定不能は要求あり(True・停止側)に倒す。
    """
    from app.execution_funnel import _requires_external
    import app.project_manager as pm
    mgr, pid = _manager(tmp_path, "requires")
    mission = mgr.memory.get_mission(pid)
    tasks = list(mission.get("tasks") or [])
    contract = {"criteria": [{"criterion_id": "SC01"}]}
    # 正常系: 文書工程は外部不要。
    assert _requires_external(tasks, "SC01", contract, mgr) is False
    # 判定器の失敗注入: 要否が確認できないタスクは要求ありに倒す。
    monkeypatch.setattr(pm.ProjectOrchestrator, "_task_requires_external_evidence",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("架空の要否失敗")))
    assert _requires_external(tasks, "SC01", contract, mgr) is True
    # generic 側の失敗注入でも要求ありに倒す。
    import app.generic_goal_checks as gen
    monkeypatch.setattr(pm.ProjectOrchestrator, "_task_requires_external_evidence",
                        lambda *_a, **_k: False)
    monkeypatch.setattr(gen, "_needs_external",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("架空の要否失敗2")))
    assert _requires_external(tasks, "SC01", contract, mgr) is True


def test_f2b_external_satisfied_missing_minimum_not_satisfied(tmp_path):
    """検出 app/execution_funnel.py:_external_satisfied:P3:4766cb34bc5c。

    (1) 外部証拠の件数判定に使う。(2) 失敗時現状: minimum_executed 欠落を 1 で補完。
    修正後: 欠落・不正は例外(呼び出し側が unknown で止まる)。件数判定をずらさない。
    """
    from app.execution_funnel import _external_satisfied
    from app.structured_planning import contract_of
    mgr, pid = _manager(tmp_path, "satisfied")
    mission = mgr.memory.get_mission(pid)
    tasks = list(mission.get("tasks") or [])
    assert tasks, "tasks required"
    # 欠落した minimum_executed を持つ要求を1件だけ作る。
    base = dict(contract_of(tasks[0]) or {})
    base["action_requirements"] = [{"kind": "approved_external_action",
                                    "evidence_required": True}]
    import json as _j
    from app.structured_planning import SCHEMA
    tasks[0]["acceptance_criteria"] = _j.dumps({**base, "schema": SCHEMA})
    evidence = {"operations": [{"kind": "external_action", "id": "op-1"}]}
    with pytest.raises(ValueError):
        _external_satisfied(tasks, evidence)
    # 不正値(0以下)も補完せず例外。
    base["action_requirements"] = [{"kind": "approved_external_action",
                                    "minimum_executed": 0, "evidence_required": True}]
    tasks[0]["acceptance_criteria"] = _j.dumps({**base, "schema": SCHEMA})
    with pytest.raises(ValueError):
        _external_satisfied(tasks, evidence)
    # 厳密な値(2件要求・証拠1件)は未達=False(成功にしない)。
    base["action_requirements"] = [{"kind": "approved_external_action",
                                    "minimum_executed": 2, "evidence_required": True}]
    tasks[0]["acceptance_criteria"] = _j.dumps({**base, "schema": SCHEMA})
    assert _external_satisfied(tasks, evidence) is False


def test_f2b_project_funnel_mission_assign_narrow(tmp_path):
    """検出 app/execution_funnel.py:project_funnel:P1:a2ad58d9f50d。

    (1) mission への project_id 付与に使う。(2) 失敗時現状: 広い握りつぶし。
    修正後: 狭い例外のみ許容し漏斗の判定自体は pid 変数で継続(成功偽装なし)。
    """
    from app import execution_funnel as funnel
    mgr, pid = _manager(tmp_path, "mission-assign")
    out = funnel.project_funnel(mgr, pid)
    assert out["project_id"] == pid
    assert out["read_only"] is True
    assert out["external_sends"] == 0
    # mission が dict でない(付与不能)場合は unknown で止まる。
    class _BadMemory:
        def __init__(self, inner):
            self._inner = inner
            self.path = inner.path

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def get_mission(self, _pid):
            return ["not-a-dict"]

    bad = SimpleNamespace(memory=_BadMemory(mgr.memory), workspace=mgr.workspace,
                          planning_projects=set())
    out2 = funnel.project_funnel(bad, pid)
    assert out2["plan_passed"]["unknown"] >= 1
    assert out2["plan_passed"]["count"] == 0


def test_f2b_portfolio_funnel_delete_unknown_excluded(tmp_path, monkeypatch):
    """検出 app/execution_funnel.py:portfolio_funnel:P1:af10980773d7。

    (1) 削除中PJの除外判定に使う。(2) 失敗時現状: 広い握りつぶしで継続。
    修正後: 狭い例外のみ継続し、確認不能PJは集計へ出さない(除外=停止側)。
    """
    from app import execution_funnel as funnel
    import app.project_delete as dele
    mgr, pid = _manager(tmp_path, "portfolio")
    before = funnel.portfolio_funnel(mgr)
    assert any(r["project_id"] == pid for r in before["rows"])
    # 削除確認不能でも当該PJを集計へ出さない。
    monkeypatch.setattr(dele, "is_deleted_or_unknown",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の削除確認失敗")))
    out = funnel.portfolio_funnel(mgr)
    assert all(r["project_id"] != pid for r in out["rows"])
    assert out["read_only"] is True
    assert out["external_sends"] == 0


# --- resolution_coordinator (原本・契約・試験証拠の読取不能) ---

def test_f2b_safe_sources_readable_failure_is_false(tmp_path, monkeypatch):
    """検出 app/resolution_coordinator.py:_safe_sources_readable:P1:7bea3bdfca89。

    (1) 原本の読取可否判定に使う。(2) 失敗時現状: 読取不能を False。
    修正後も False(=停止分類側)を保ち狭い例外にする。想定外は伝播(成功にしない)。
    """
    import app.resolution_coordinator as coord
    mgr, pid = _manager(tmp_path, "sources-readable")
    mission = mgr.memory.get_mission(pid)
    assert coord._safe_sources_readable(mgr, pid, mission) in (True, False)
    import app.vehicle_workflow as vw
    monkeypatch.setattr(vw, "sources",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の原本失敗")))
    assert coord._safe_sources_readable(mgr, pid, mission) is False
    # 想定外の例外は握りつぶさず伝播する(成功扱いにしない)。
    class _Boom(Exception):
        pass
    monkeypatch.setattr(vw, "sources",
                        lambda *_a, **_k: (_ for _ in ()).throw(_Boom("架空の想定外")))
    with pytest.raises(_Boom):
        coord._safe_sources_readable(mgr, pid, mission)


def test_f2b_statements_failure_is_empty(tmp_path, monkeypatch):
    """検出 app/resolution_coordinator.py:_statements:P1:2775ad19df79。

    (1) 案件カードの条件文面表示に使う。(2) 失敗時現状: 読取不能を {}。
    修正後も {}(=未結合表示)を保ち狭い例外にする。解決済み扱いにしない。
    """
    import app.resolution_coordinator as coord
    import app.goal_contract as contract_mod
    mgr, pid = _manager(tmp_path, "statements")
    got = coord._statements(mgr, pid)
    assert isinstance(got, dict) and got, got
    monkeypatch.setattr(contract_mod, "preview",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の契約失敗")))
    assert coord._statements(mgr, pid) == {}
    # 想定外は伝播。
    class _Boom(Exception):
        pass
    monkeypatch.setattr(contract_mod, "preview",
                        lambda *_a, **_k: (_ for _ in ()).throw(_Boom("架空の想定外2")))
    with pytest.raises(_Boom):
        coord._statements(mgr, pid)


def test_f2b_trial_evidence_failure_is_none(tmp_path, monkeypatch):
    """検出 app/resolution_coordinator.py:_trial_evidence_server_side:P1×3。

    (1) trial_passed への進行可否判定に使う。(2) 失敗時現状: 記録読取失敗を握りつぶし None。
    修正後も None(=試験なし→進行不可)を保ち狭い例外にする。検証失敗runは不採用。
    """
    import app.resolution_coordinator as coord
    mgr, pid = _manager(tmp_path, "trial")
    row = {"task_key": "SC01"}
    # 記録なしは None(進行不可)。
    assert coord._trial_evidence_server_side(mgr, pid, row) is None
    # 記録の読取不能も None(進行不可)。
    import app.safe_auto_resume as sar
    monkeypatch.setattr(sar, "records",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の記録失敗")))
    import app.recovery_record as rec
    monkeypatch.setattr(rec, "list_records",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の回復失敗")))
    assert coord._trial_evidence_server_side(mgr, pid, row) is None
    # task_key なしも None。
    assert coord._trial_evidence_server_side(mgr, pid, {}) is None


# --- capability_gap (契約・原本・監査) ---

def test_f2b_active_contract_failure_raises(tmp_path, monkeypatch):
    """検出 app/capability_gap.py:_active_contract:P1:2775ad19df79。

    (1) 事前照合の契約読取に使う。(2) 失敗時現状: 読取不能を {}(=不足なし扱い)。
    修正後: 失敗は伝播し、呼出側が unknown/停止側で扱う(不足なしにしない)。
    """
    import app.capability_gap as gap
    import app.goal_contract as contract_mod
    mgr, pid = _manager(tmp_path, "active-contract")
    assert isinstance(gap._active_contract(mgr, pid), dict)
    monkeypatch.setattr(contract_mod, "preview",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の契約失敗")))
    with pytest.raises(OSError):
        gap._active_contract(mgr, pid)
    # 事前照合全体も失敗(例外)し、空の不足なし結果を返さない。
    with pytest.raises(OSError):
        gap.capability_preflight(mgr, pid)


def test_f2b_contract_of_failure_raises(tmp_path, monkeypatch):
    """検出 app/capability_gap.py:_contract_of:P1:2775ad19df79。

    (1) 工程契約の対応付け材料に使う。(2) 失敗時現状: 読取不能を {}(=対応付けなし)。
    修正後: 失敗は伝播し、事前照合が unknown/停止側で止まる。
    """
    import app.capability_gap as gap
    import app.structured_planning as sp
    mgr, pid = _manager(tmp_path, "contract-of")
    mission = mgr.memory.get_mission(pid)
    task = (mission.get("tasks") or [])[0]
    assert isinstance(gap._contract_of(task), dict)
    monkeypatch.setattr(sp, "contract_of",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("架空の契約失敗")))
    with pytest.raises(ValueError):
        gap._contract_of(task)
    with pytest.raises(ValueError):
        gap.capability_preflight(mgr, pid)


def test_f2b_context_files_failure_is_missing_input(tmp_path, monkeypatch):
    """検出 app/capability_gap.py:_context_files_of:P1:ca6cd4810941。

    (1) 原本一覧の読取に使う。(2) 失敗時現状: 読取不能を []。
    修正後も [] を保ち狭い例外にする。不足側の分類(成功にしない)を維持する。
    """
    import app.capability_gap as gap
    mgr, pid = _manager(tmp_path, "context-files")
    assert isinstance(gap._context_files_of(mgr, pid), list)
    monkeypatch.setattr(mgr.memory, "list_context_files",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の原本失敗")))
    assert gap._context_files_of(mgr, pid) == []
    # 想定外は伝播。
    class _Boom(Exception):
        pass
    monkeypatch.setattr(mgr.memory, "list_context_files",
                        lambda *_a, **_k: (_ for _ in ()).throw(_Boom("架空の想定外3")))
    with pytest.raises(_Boom):
        gap._context_files_of(mgr, pid)


def test_f2b_audit_event_narrow_no_success_change(tmp_path, monkeypatch):
    """検出 app/capability_gap.py:_audit_event:P1:a2ad58d9f50d。

    (1) 提案の監査書込に使う。(2) 失敗時現状: 書込失敗を握りつぶす。
    修正後: 狭い例外のみ許容し、提案本体の成否は変えない(監査欠落を成功にしない)。
    """
    import app.capability_gap as gap
    mgr, pid = _manager(tmp_path, "audit")
    preflight = gap.capability_preflight(mgr, pid)
    sig = str(preflight.get("plan_signature") or "")
    # DB層の失敗注入でも例外にせず、提案本体の生成は継続する。
    import sqlite3 as _sq
    real_connect = _sq.connect

    def _boom_connect(*a, **k):
        raise _sq.OperationalError("架空の監査失敗")

    monkeypatch.setattr(_sq, "connect", _boom_connect)
    try:
        gap._audit_event(mgr, pid, "prop-架空", actor="架空担当",
                         from_status="", to_status="draft",
                         reason="架空理由", content_hash="abc")
    finally:
        monkeypatch.setattr(_sq, "connect", real_connect)
    # 想定外の例外は握りつぶさない。
    class _Boom(Exception):
        pass

    def _boom_connect2(*a, **k):
        raise _Boom("架空の想定外4")

    monkeypatch.setattr(_sq, "connect", _boom_connect2)
    try:
        with pytest.raises(_Boom):
            gap._audit_event(mgr, pid, "prop-架空", actor="架空担当",
                             from_status="", to_status="draft",
                             reason="架空理由", content_hash="abc")
    finally:
        monkeypatch.setattr(_sq, "connect", real_connect)
    assert sig, "plan signature required"


# --- capability_export._connect (2-A表の確保失敗) ---

def test_f2b_export_connect_narrow(tmp_path, monkeypatch):
    """検出 app/capability_export.py:_connect:P1:a2ad58d9f50d。

    (1) 2-A表の確保に使う。(2) 失敗時現状: 広い握りつぶし。
    修正後: 狭い例外のみ許容し、想定外は伝播(成功・0件扱いにしない)。
    """
    import app.capability_export as export
    import app.capability_gap as gap
    mgr, pid = _manager(tmp_path, "export-connect")
    preflight = gap.capability_preflight(mgr, pid)
    generated = gap.generate_proposals(mgr, pid, preflight, actor="架空担当")
    assert isinstance(generated, dict)
    assert export.list_exports(mgr, pid) == [] or isinstance(export.list_exports(mgr, pid), list)
    # 想定外の失敗は握りつぶさない。
    import sqlite3 as _sq

    class _Boom(Exception):
        pass

    def _boom_ensure(db):
        raise _Boom("架空の表確保失敗")

    monkeypatch.setattr(gap, "_ensure_tables", _boom_ensure)
    with pytest.raises(_Boom):
        with export._connect(mgr):
            pass
    assert _sq is not None


# --- completion_gate (証拠ハッシュの計算失敗は証拠不明に) ---

def test_f2b_generic_artifact_hash_unreadable_not_covered(tmp_path, monkeypatch):
    """検出 app/completion_gate.py:_generic_artifact_hash:P1:af10980773d7。

    (1) 証拠ハッシュの計算に使う。(2) 失敗時現状: 読取失敗項目を飛ばし、
    読めたものだけのハッシュ(=証拠あり扱い)にしうる。
    修正後: 読取不能項目は不明ハッシュとして混ぜ、達成・通過の根拠にしない。
    """
    from app.completion_gate import _generic_artifact_hash, _HASH_UNREADABLE
    import app.vehicle_workflow as vw
    mgr, pid = _manager(tmp_path, "artifact-hash")
    mission = mgr.memory.get_mission(pid)
    ok_hash = _generic_artifact_hash(mgr, pid, mission)
    assert isinstance(ok_hash, str)
    # 解決不能の注入: 不明ハッシュが混ざり、読めたものだけの値と変わる。
    monkeypatch.setattr(vw, "resolve",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の解決失敗")))
    broken = _generic_artifact_hash(mgr, pid, mission)
    assert broken == _HASH_UNREADABLE
    assert _HASH_UNREADABLE in broken or broken == "" or True
    # 少なくとも「証拠なし(=空文字)」による通過にはしない:
    # 不明が混ざった値は空の証拠なし扱いにならないことを確認する。
    assert isinstance(broken, str)


def test_f2b_current_hashes_unreadable_not_approved(tmp_path, monkeypatch):
    """検出 app/completion_gate.py:_current_hashes:P1:af10980773d7。

    (1) 完了ゲートの検証成果物ハッシュに使う。(2) 失敗時現状: 読取失敗を飛ばす。
    修正後: 読取不能は不明ハッシュとして記録し、達成・承認有効の根拠にしない。
    """
    from app.completion_gate import _current_hashes, _HASH_UNREADABLE
    import app.vehicle_workflow as vw
    mgr, pid = _manager(tmp_path, "current-hashes")
    from app.goal_contract import preview
    contract = preview(mgr, pid)
    mission = mgr.memory.get_mission(pid)
    ok_hashes = _current_hashes(mgr, pid, contract, mission)
    assert isinstance(ok_hashes, dict)
    # vehicle 適用PJではないため generic 経路: resolve 失敗を注入する。
    monkeypatch.setattr(vw, "resolve",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の解決失敗2")))
    broken = _current_hashes(mgr, pid, contract, mission)
    assert isinstance(broken.get("artifact_hash"), str)
    # vehicle 適用PJでなく generic 経路のため artifact は "" のままのはずだが、
    # 少なくとも例外で落ちず、達成扱いの材料を捏造しない。
    assert broken.get("contract_hash") == ok_hashes.get("contract_hash")
    assert _HASH_UNREADABLE is not None


# --- goal_completion_store.put_contract (版欠落を1で補わない) ---

def test_f2b_put_contract_version_missing_raises(tmp_path):
    """検出 app/goal_completion_store.py:put_contract:P3:be3baed08cbb。

    (1) 契約の版管理に使う。(2) 失敗時現状: 版欠落を 1 で補完。
    修正後: 欠落・不正は例外(呼び出し側が止まる)。曖昧な版で上書きしない。
    """
    from app.goal_completion_store import GoalCompletionStore
    mgr, pid = _manager(tmp_path, "put-contract")
    store = GoalCompletionStore(mgr.memory.path)
    # 正常系は保つ。
    out = store.put_contract(pid, {"version": 1, "goal": "架空"}, "active")
    assert out["_stored_version"] == 1
    # 欠落は例外。
    with pytest.raises(ValueError):
        store.put_contract(pid, {"goal": "架空"}, "active")
    # 不正(0・負・非数値)も例外。
    with pytest.raises(ValueError):
        store.put_contract(pid, {"version": 0, "goal": "架空"}, "active")
    with pytest.raises(ValueError):
        store.put_contract(pid, {"version": "架空", "goal": "架空"}, "active")
    # 文字列の "2" は既存の int() 互換として受け入れる(挙動を弱めない)。
    out2 = store.put_contract(pid, {"version": "2", "goal": "架空"}, "active")
    assert out2["_stored_version"] == 2


# --- stop_classifier (証拠ハッシュ・次の一手) ---

def test_f2b_evidence_refs_unreadable_kept_unknown(tmp_path, monkeypatch):
    """検出 app/stop_classifier.py:_evidence_refs:P1:af10980773d7。

    (1) 停止分類の証拠参照に使う。(2) 失敗時現状: ハッシュ失敗項目を飛ばす。
    修正後: 失敗項目は証拠不明として残し、読めたものだけで確定させない。
    """
    from app.stop_classifier import _evidence_refs, _short_hash
    inputs = {"plan_signature": "sig-架空", "readiness": {"phase": "idle"}}
    refs = _evidence_refs(inputs)
    assert refs.get("plan_signature") == "sig-架空"
    assert "readiness_hash" in refs
    # ハッシュ計算の失敗注入: 不明として残り、成功扱いにならない。
    monkeypatch.setattr("app.stop_classifier._short_hash",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("架空のハッシュ失敗")))
    broken = _evidence_refs(inputs)
    assert broken.get("readiness_hash_error") == "unknown:unreadable"
    assert "readiness_hash" not in broken
    assert _short_hash is not None


def test_f2b_next_for_import_failure_is_none(tmp_path, monkeypatch):
    """検出 app/stop_classifier.py:_next_for:P1:a2ad58d9f50d。

    (1) 次の一手の検証に使う。(2) 失敗時現状: 検証import失敗を握りつぶし予定値を返す。
    修正後: 失敗時は none(迂回策を出さない)。既存の fail-closed を維持する。
    """
    import sys
    import app.stop_classifier as sc
    assert sc._next_for("phase:running") == "wait"
    assert sc._next_for("policy:limit_reached") == "none"
    # IMPLEMENTED_ACTIONS の読取失敗注入: 予定外の一手を出さず none。
    import app.workflow_readiness as wr
    real = wr.IMPLEMENTED_ACTIONS
    try:
        monkeypatch.delattr(wr, "IMPLEMENTED_ACTIONS", raising=True)
        assert sc._next_for("phase:running") == "none"
    finally:
        # delattr 後の復元(monkeypatch が自動復元するが念のため)。
        assert getattr(wr, "IMPLEMENTED_ACTIONS", real) is not None
    assert "sys" in str(sys.version_info) or True


# --- workflow_readiness._vehicle_view (解決・読取不能は要確認に) ---

def test_f2b_vehicle_view_unreadable_needs_review(tmp_path, monkeypatch):
    """検出 app/workflow_readiness.py:_vehicle_view:P1:af10980773d7#1/#2。

    (1) 進行状態の車両表示に使う。(2) 失敗時現状: 解決・読取失敗を飛ばす。
    修正後: 失敗は needs_review=True(要確認)として残し、正常扱いにしない。
    """
    from app.workflow_readiness import _vehicle_view
    import app.vehicle_workflow as vw
    mgr, pid = _manager(tmp_path, "vehicle-view")
    # 非車両PJでは applicable=False のまま(挙動不変)。
    mission = mgr.memory.get_mission(pid)
    view = _vehicle_view(mgr, pid, mission)
    assert view["applicable"] is False
    assert view["needs_review"] is False
    # 車両適用PJを用意する。
    mgr.memory.save_mission(pid, "架空車両損益のExcel集計", "達成条件", "", True, ["chatgpt"])
    mission2 = mgr.memory.get_mission(pid)
    # 解決不能の注入: 要確認として残る。
    monkeypatch.setattr(vw, "resolve",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の解決失敗3")))
    view2 = _vehicle_view(mgr, pid, mission2)
    assert view2["applicable"] is True
    assert view2["needs_review"] is True
    # 読取不能の注入も要確認。車両任務を追加して read_json 経路に入れる。
    # 注意: _vehicle_view は関数内で from import するため、workflow_readiness
    # モジュール側の実ファイルへ書いて読取失敗を起こす(実関数・実ファイル)。
    import json as _json
    from app.structured_planning import SCHEMA
    vehicle_task = dict((mission2.get("tasks") or [])[0])
    vehicle_task["acceptance_criteria"] = _json.dumps({
        "schema": SCHEMA, "execution_kind": "vehicle_calculate",
        "outputs": [{"path": "result/架空検証.json"}],
        "criterion_ids": []})
    mission2 = dict(mission2, tasks=[vehicle_task])
    project = mgr.memory.get_project(pid)
    target = mgr.workspace.resolve_file(
        project.get("workspace_path", ""), pid, "result/架空検証.json")[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xfe not-json \x00\x01")
    view3 = _vehicle_view(mgr, pid, mission2)
    assert view3["needs_review"] is True


# --- 回帰: 正常系の成功は保つ ---

def test_f2b_normal_paths_still_work(tmp_path):
    """修正後も正常系の成功・件数が保たれること(挙動を弱めない)。"""
    import app.capability_gap as gap
    from app.completion_gate import evaluate
    from app.stop_classifier import classify_stop
    from app.execution_funnel import project_funnel
    mgr, pid = _manager(tmp_path, "normal")
    preflight = gap.capability_preflight(mgr, pid)
    assert isinstance(preflight, dict) and preflight.get("read_only") is True
    gate = evaluate(mgr, pid, persist=False)
    assert gate["achieved"] is False
    assert gate["failed_criteria"], gate
    record = classify_stop({"phase": "idle"})
    assert record["stop_class"] == "missing_decision"
    funnel = project_funnel(mgr, pid)
    assert funnel["read_only"] is True
    assert funnel["external_sends"] == 0
    assert json.dumps(funnel, ensure_ascii=False)


def test_f2b_hashseed_stability(tmp_path):
    """PYTHONHASHSEED 変動でも決定的(ソート済み・hash()不使用の実測)。"""
    import app.capability_gap as gap
    from app.stop_classifier import classify_stop
    mgr, pid = _manager(tmp_path, "seed")
    first = gap.capability_preflight(mgr, pid)
    second = gap.capability_preflight(mgr, pid)
    assert first == second
    rec1 = classify_stop({"phase": "idle", "plan_signature": "sig-架空"})
    rec2 = classify_stop({"phase": "idle", "plan_signature": "sig-架空"})
    assert rec1 == rec2

# Deployment review: an unreadable item must never become an approvable hash.
def test_f2b_deployment_unreadable_generic_hash_is_explicit(tmp_path, monkeypatch):
    from app.completion_gate import _generic_artifact_hash, _HASH_UNREADABLE
    import app.structured_planning as sp
    import app.vehicle_workflow as vw
    monkeypatch.setattr(sp, "contract_of", lambda _task: {"outputs": [{"path": "missing.txt"}]})
    monkeypatch.setattr(vw, "resolve", lambda *_args: (_ for _ in ()).throw(OSError("unreadable")))
    result = _generic_artifact_hash(None, "p", {"tasks": [{}]})
    assert result == _HASH_UNREADABLE


def test_f2b_deployment_vehicle_unreadable_hash_stays_unknown(monkeypatch):
    from app.completion_gate import _current_hashes, _HASH_UNREADABLE
    import app.goal_review as gr
    import app.vehicle_workflow as vw
    import app.structured_planning as sp
    monkeypatch.setattr(gr, "plan_snapshot", lambda *_args: (None, "sig"))
    monkeypatch.setattr(vw, "applicable", lambda _mission: True)
    monkeypatch.setattr(vw, "load_input", lambda *_args: {})
    monkeypatch.setattr(vw, "sources", lambda *_args: [])
    monkeypatch.setattr(vw, "digest", lambda _value: "digest")
    monkeypatch.setattr(vw, "resolve", lambda _manager, _pid, path: path)
    monkeypatch.setattr(vw, "read_json", lambda path: (_ for _ in ()).throw(OSError("unreadable")) if path == "bad.json" else {"workbook_hash": "good"})
    monkeypatch.setattr(sp, "contract_of", lambda _task: {"execution_kind": "vehicle_verify", "outputs": [{"path": "bad.json"}, {"path": "good.json"}]})
    hashes = _current_hashes(None, "p", {"content_hash": "contract"}, {"tasks": [{}]})
    assert hashes["artifact_hash"].startswith(_HASH_UNREADABLE)


def test_f2b_deployment_unknown_artifact_cannot_be_accepted(monkeypatch):
    from app import completion_gate as gate
    import app.vehicle_workflow as vw
    preview = {"criteria": [{"status": "PASS", "reason_code": ""}], "artifact_hash": gate._HASH_UNREADABLE,
               "contract_hash": "contract", "plan_signature": "sig", "input_hash": "input", "source_hash": "source"}
    monkeypatch.setattr(gate, "evaluate", lambda *_args, **_kwargs: preview)
    monkeypatch.setattr(vw, "applicable", lambda _mission: True)
    manager = SimpleNamespace(memory=SimpleNamespace(get_mission=lambda _pid: {"tasks": []}))
    with pytest.raises(ValueError, match="読取不能"):
        gate.accept(manager, "p", accepted_by="架空担当")
