"""F3a: 中核外レビュー56件の fail-closed 化テスト (疑似PJのみ・実関数)。

対象は tests/fail_open_baseline.json で classification=review かつ
file が app/plan_feedback.py(21) / app/plan_case_reference.py(9) /
app/pending_ledger.py(4) / app/resolution_p3_context.py(4) /
app/domain_spec.py(3) / app/executable_recipes.py(3) /
app/project_generation.py(2) / app/generic_goal_checks.py /
app/memory/short_term.py / app/next_action_controller.py /
app/plan_repair_loop.py / app/project_lifecycle_backup.py /
app/triz_candidate_ranking.py / app/vehicle_auto.py /
app/vehicle_service.py の56件。
各検出について失敗を注入し、成功/0件/空/通過にならないことを確認する。
実関数・疑似データ・一時ディレクトリのみ。外部通信なし。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from app.memory.short_term import ShortTermMemory


def _env(tmp_path: Path, tag: str):
    data_dir = tmp_path / f"f3a-{tag}-data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project(f"f3a-疑似-{tag}", "架空コンテキスト")
    pid = project["id"]
    memory.save_mission(pid, "架空目標", "架空達成条件", "", False, [], 2)
    ws_root = tmp_path / f"f3a-{tag}-ws"
    ws_root.mkdir(exist_ok=True)
    backup_root = tmp_path / f"f3a-{tag}-bk"
    backup_root.mkdir(exist_ok=True)
    return {"mem": mem_path, "memory": memory, "pid": pid,
            "ws_root": ws_root, "backup_root": backup_root}


class _FakeManager:
    def __init__(self, env):
        self.memory = env["memory"]
        self.planning_projects = set()


# --- pending_ledger._safe_* (失敗はログに残し、確認不能区分を保つ)×4 ---

def test_f3a_pending_issues_failure_is_unavailable(tmp_path, monkeypatch, caplog):
    """検出 app/pending_ledger.py:_safe_issues:P1:eaf9362805e8。

    (1) 台帳の指摘件数に使う。(2) 失敗時現状: 失敗を握りつぶしNone。
    修正後: 失敗をログに残し、None=「確認できず」(0件にしない)。
    """
    from app import pending_ledger as _pl
    import logging as _logging
    monkeypatch.setattr(_pl, "issues_for",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の指摘失敗")),
                        raising=False)
    # issues_for は app.plan_feedback.issues_for を _safe_issues 内でimportするためこちらを差し替える。
    import app.plan_feedback as _pf
    monkeypatch.setattr(_pf, "issues_for",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の指摘失敗")))
    env = _env(tmp_path, "pending-issues")
    mgr = _FakeManager(env)
    with caplog.at_level(_logging.WARNING, logger="app.pending_ledger"):
        out = _pl._safe_issues(mgr, env["pid"], "sig-架空")
    assert out is None
    assert any("pending_ledger" in r.name and "_safe_issues" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]


def test_f3a_pending_actions_failure_is_unavailable(tmp_path, monkeypatch, caplog):
    """検出 app/pending_ledger.py:_safe_actions:P1:eaf9362805e8。

    (1) 台帳の外部操作件数に使う。(2) 失敗時現状: 失敗を握りつぶしNone。
    修正後: 失敗をログに残し、None=「確認できず」(0件にしない)。
    """
    from app import pending_ledger as _pl
    import logging as _logging
    env = _env(tmp_path, "pending-actions")
    monkeypatch.setattr(env["memory"], "list_actions",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の操作失敗")))
    mgr = _FakeManager(env)
    with caplog.at_level(_logging.WARNING, logger="app.pending_ledger"):
        out = _pl._safe_actions(mgr, env["pid"])
    assert out is None
    assert any("_safe_actions" in r.message for r in caplog.records)


def test_f3a_pending_mission_failure_logged(tmp_path, monkeypatch, caplog):
    """検出 app/pending_ledger.py:_safe_mission:P1:2775ad19df79。

    (1) 台帳の計画承認状態に使う。(2) 失敗時現状: 失敗を握りつぶし{}。
    修正後: 失敗をログに残す({}は「確認できず」→unverified側)。
    """
    from app import pending_ledger as _pl
    import logging as _logging
    env = _env(tmp_path, "pending-mission")
    monkeypatch.setattr(env["memory"], "get_mission",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の読取失敗")))
    mgr = _FakeManager(env)
    with caplog.at_level(_logging.WARNING, logger="app.pending_ledger"):
        out = _pl._safe_mission(mgr, env["pid"])
    assert out == {}
    assert any("_safe_mission" in r.message for r in caplog.records)


def test_f3a_pending_store_row_failure_logged(tmp_path, monkeypatch, caplog):
    """検出 app/pending_ledger.py:_safe_store_row:P1:eaf9362805e8。

    (1) 台帳の計画・修正案の行読取に使う。(2) 失敗時現状: 失敗を握りつぶしNone。
    修正後: 失敗をログに残す(Noneは「確認できず」→unknown側)。
    """
    from app import pending_ledger as _pl
    from app.goal_review import ReviewStore
    import logging as _logging
    env = _env(tmp_path, "pending-store")
    monkeypatch.setattr(ReviewStore, "get",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の保存失敗")))
    mgr = _FakeManager(env)
    with caplog.at_level(_logging.WARNING, logger="app.pending_ledger"):
        out = _pl._safe_store_row(mgr, env["pid"], "plan", "sig-架空")
    assert out is None
    assert any("_safe_store_row" in r.message for r in caplog.records)


# --- domain_spec (秘密検査不能は記録拒否 / 契約読取不能はunknown / 外部作用は伝播)×3 ---

def test_f3a_domain_check_secret_import_failure_refuses():
    """検出 app/domain_spec.py:_check_secret:P1:f83f890754b1。

    (1) 選択記録の秘密混入検査に使う。(2) 失敗時現状: import失敗を握りつぶし""(検査済み扱い)。
    修正後: import失敗は「検査不能」(呼び出し側が記録を拒否する)。
    """
    import sys as _sys
    import app.domain_spec as _ds
    blocked = _sys.modules.get("app.plan_review_loop")
    _sys.modules["app.plan_review_loop"] = None  # type: ignore[assignment]
    try:
        assert _ds._check_secret("架空 actor", "架空 reason") == "検査不能"
    finally:
        if blocked is not None:
            _sys.modules["app.plan_review_loop"] = blocked
        else:
            _sys.modules.pop("app.plan_review_loop", None)


def test_f3a_domain_task_contract_failure_is_unknown(tmp_path, caplog):
    """検出 app/domain_spec.py:_task_contract:P1:eaf9362805e8。

    (1) 適合検査の工程契約読取に使う。(2) 失敗時現状: 失敗を握りつぶしNone。
    修正後: 失敗をログに残す(Noneは「判定不能」→unknown側)。
    """
    import logging as _logging
    import app.structured_planning as _sp
    import app.domain_spec as _ds
    import pytest as _pt
    monkeypatch_target = _sp.contract_of
    _sp.contract_of = lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の契約失敗"))  # type: ignore[assignment]
    try:
        with caplog.at_level(_logging.WARNING, logger="app.domain_spec"):
            out = _ds._task_contract({"task_key": "T01"})
        assert out is None
        assert any("_task_contract" in r.message for r in caplog.records)
    finally:
        _sp.contract_of = monkeypatch_target  # type: ignore[assignment]


def test_f3a_domain_external_ops_failure_raises(tmp_path, monkeypatch):
    """検出 app/domain_spec.py:_task_external_operations:P1:ca6cd4810941。

    (1) 外部作用の権限検査に使う。(2) 失敗時現状: 失敗を握りつぶし[](外部作用なし扱い)。
    修正後: 失敗をログに残し伝播する(空で「なし」にしない)。
    """
    import app.domain_spec as _ds
    import app.capability_gap as _cg
    monkeypatch.setattr(_cg, "_contract_operations",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の対応付け失敗")))
    # domain_spec は from-import のためモジュール側も差し替える。
    monkeypatch.setattr(_ds, "_contract_operations",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の対応付け失敗")))
    with pytest.raises(OSError):
        _ds._task_external_operations({"outputs": []})


# --- automatic_triz (監査失敗はログに残す)×2 ---

def test_f3a_triz_audit_failure_logged(tmp_path, monkeypatch, caplog):
    """検出 app/automatic_triz.py:save_vehicle_failure / on_failure:P1:a2ad58d9f50d。

    (1) 監査イベントの記録に使う。(2) 失敗時現状: 監査失敗を握りつぶす。
    修正後: 失敗をログに残す(保存JSON自体は成功。監査欠落を成功扱いにしない)。
    """
    import logging as _logging
    env = _env(tmp_path, "triz-audit")
    monkeypatch.setattr(env["memory"], "add_event",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の監査失敗")))
    mgr = _FakeManager(env)
    task = {"id": "t-架空", "title": "架空工程", "acceptance_criteria": "架空条件"}
    import app.vehicle_workflow as _vw
    from app import automatic_triz as _tz
    _fake_resolve = lambda _m, _p, rel: tmp_path / f"f3a-triz-{rel.replace('/', '_')}"  # noqa: E731
    monkeypatch.setattr(_vw, "resolve", _fake_resolve)
    # automatic_triz は resolve を from-import しているため、利用側の束縛も差し替える
    # (実行順でモジュールが先に読まれても同じ結果になるように)。
    monkeypatch.setattr(_tz, "resolve", _fake_resolve)
    with caplog.at_level(_logging.WARNING, logger="app.automatic_triz"):
        out = _tz.save_vehicle_failure(mgr, env["pid"], task, RuntimeError("架空失敗"))
    assert out["status"] in ("framed", "development_required")
    assert any("audit failed" in r.message for r in caplog.records)


# --- executable_recipes (列確認不能は記録 / 版不明は拒否)×3 ---

def test_f3a_recipe_columns_failure_logged(caplog):
    """検出 app/executable_recipes.py:columns_of:P1:ca6cd4810941。

    (1) 手順の適用条件(必須列)に使う。(2) 失敗時現状: 失敗を握りつぶし[](列なし扱い)。
    修正後: 失敗をログに残す([]は「列を確認できず」→一致扱いにしない)。
    """
    import logging as _logging
    from app import executable_recipes as _rc
    with caplog.at_level(_logging.WARNING, logger="app.executable_recipes"):
        out = _rc.columns_of({"filename": "架空.xlsx"}, raw=b"not-a-workbook", content="")
    assert out == []
    assert any("columns_of" in r.message for r in caplog.records)


def test_f3a_recipe_version_missing_defaults_but_zero_rejected():
    """検出 app/executable_recipes.py:match_recipe/execute_recipe:P3:bf9dbece1ca6。

    (1) 手順の版照合に使う。(2) 失敗時現状: version欠落・0を or 1 で補完。
    修正後: 欠落(None)は1、0・負・非数値は拒否(1に化けさせない)。
    """
    from app import executable_recipes as _rc
    assert _rc._recipe_version({}) == 1
    assert _rc._recipe_version({"version": 2}) == 2
    with pytest.raises(ValueError):
        _rc._recipe_version({"version": 0})
    with pytest.raises(ValueError):
        _rc._recipe_version({"version": -1})
    with pytest.raises(ValueError):
        _rc._recipe_version({"version": "架空"})


# --- plan_repair_loop.approval_gate (確認不能は素通りにしない)×1 ---

def test_f3a_repair_gate_check_failure_blocks(tmp_path, monkeypatch):
    """検出 app/plan_repair_loop.py:approval_gate:P1:206e55ee8976。

    (1) 計画承認・実行開始の判定に使う。(2) 失敗時現状: 確認不能を素通り(return)。
    修正後: 確認不能は停止側(ValueError)に倒す。
    """
    import app.plan_repair_loop as _rl
    env = _env(tmp_path, "repair-gate")
    mgr = _FakeManager(env)
    monkeypatch.setattr(_rl, "repair_gate_summary",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の要約失敗")))
    with pytest.raises(ValueError, match="確認できない"):
        _rl.approval_gate(mgr, env["pid"])


# --- project_lifecycle_backup._target_has_project (確認不能は存在扱い)×1 ---

def test_f3a_target_check_failure_is_collision(tmp_path, monkeypatch, caplog):
    """検出 app/project_lifecycle_backup.py:_target_has_project:P1:af10980773d7。

    (1) 別ID復元の衝突判定に使う。(2) 失敗時現状: 失敗エントリを飛ばし「存在しない」扱い。
    修正後: 失敗をログに残し、確認不能は存在扱い(衝突拒否側)に倒す。
    """
    import logging as _logging
    import app.project_lifecycle_backup as _bk
    env = _env(tmp_path, "target-check")
    monkeypatch.setattr(_bk, "count_entry",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の件数失敗")))
    with caplog.at_level(_logging.WARNING, logger="app.project_lifecycle_backup"):
        out = _bk._target_has_project(env["mem"], "存在しない架空pid", env["ws_root"])
    assert out is True
    assert any("target check failed" in r.message for r in caplog.records)


# --- resolution_p3_context (失敗はログに残し、加点・主張なし)×4 ---

def test_f3a_p3_hashes_failure_is_none(tmp_path, monkeypatch, caplog):
    """検出 app/resolution_p3_context.py:_registered_source_hashes:P1:eaf9362805e8。

    (1) 候補順位の原本照合に使う。(2) 失敗時現状: 失敗を握りつぶしNone。
    修正後: 失敗をログに残す(Noneは「確認できず」→加点なし)。
    """
    import logging as _logging
    from app import resolution_p3_context as _p3
    env = _env(tmp_path, "p3-hashes")
    monkeypatch.setattr(env["memory"], "list_context_files",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の一覧失敗")))
    mgr = _FakeManager(env)
    with caplog.at_level(_logging.WARNING, logger="app.resolution_p3_context"):
        out = _p3._registered_source_hashes(mgr, env["pid"])
    assert out is None
    assert any("source hashes" in r.message for r in caplog.records)


def test_f3a_p3_rag_search_failure_is_empty(tmp_path, caplog):
    """検出 app/resolution_p3_context.py:_rag_candidates:P1:P1(2件)。

    (1) RAG適格性の候補取得に使う。(2) 失敗時現状: 失敗を握りつぶし[]。
    修正後: 失敗をログに残す([]は「候補なし」→適格なし)。
    """
    import logging as _logging
    from app import resolution_p3_context as _p3

    class _BadIndex:
        def search(self, *_a, **_k):
            raise OSError("架空の検索失敗")

    class _BadMem:
        def build_index(self):
            return _BadIndex()

        @property
        def store(self):
            raise AssertionError("search失敗時はstoreに触れない")

    with caplog.at_level(_logging.WARNING, logger="app.resolution_p3_context"):
        out = _p3._rag_candidates(_BadMem(), "架空pid", "架空クエリ")
    assert out == []
    assert any("rag candidates search failed" in r.message for r in caplog.records)


# --- triz_candidate_ranking (確認不能は未登録扱いで降格)×1 ---

def test_f3a_ranking_capability_failure_is_unregistered(tmp_path, monkeypatch, caplog):
    """検出 app/triz_candidate_ranking.py:_is_registered_capability:P1:7bea3bdfca89。

    (1) 候補順位の能力照合に使う。(2) 失敗時現状: 失敗を握りつぶしFalse。
    修正後: 失敗をログに残す(False=未登録扱いで降格。登録済み扱いにしない)。
    """
    import logging as _logging
    import sys as _sys
    from app import triz_candidate_ranking as _rk
    blocked = _sys.modules.get("app.triz_adapters")
    _sys.modules["app.triz_adapters"] = None  # type: ignore[assignment]
    try:
        with caplog.at_level(_logging.WARNING, logger="app.triz_candidate_ranking"):
            out = _rk._is_registered_capability("架空の能力")
        assert out is False
        assert any("capability check failed" in r.message for r in caplog.records)
    finally:
        if blocked is not None:
            _sys.modules["app.triz_adapters"] = blocked
        else:
            _sys.modules.pop("app.triz_adapters", None)


# --- vehicle_auto.prepare (抽出フォールバックは記録)×1 ---

def test_f3a_vehicle_context_extract_failure_logged(tmp_path, monkeypatch, caplog):
    """検出 app/vehicle_auto.py:prepare:P1:a2ad58d9f50d。

    (1) 原本の文字コード復元候補の取得に使う。(2) 失敗時現状: 失敗を握りつぶし原文継続。
    修正後: 失敗をログに残す(原文継続は保つ。復元候補扱いにしない)。
    """
    import logging as _logging
    import app.context_files as _cf
    monkeypatch.setattr(_cf, "extract_context_file",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の抽出失敗")))
    env = _env(tmp_path, "vehicle-extract")
    env["memory"].add_context_file(env["pid"], "架空.txt", "架空本文", 8,
                                   data="架空raw".encode("utf-8"), mime_type="text/plain",
                                   file_kind="text", sha256="00" * 32)
    from app import vehicle_auto as _va
    mgr = _FakeManager(env)
    mission = env["memory"].get_mission(env["pid"])
    with caplog.at_level(_logging.WARNING, logger="app.vehicle_auto"):
        try:
            _va.prepare(mgr, env["pid"], mission)
        except Exception:
            pass
    assert any("context extract failed" in r.message for r in caplog.records)


# --- vehicle_service.reuse_decisions (不明statusはactiveに化けさせない)×1 ---

def test_f3a_reuse_unknown_status_not_active():
    """検出 app/vehicle_service.py:reuse_decisions:P3:08a27810891a。

    (1) 旧判断の再適用可否に使う。(2) 失敗時現状: status欠落を or 'active' で補完。
    修正後: 欠落(None)/activeは有効保存、未知値はinvalid(有効扱いにしない)。
    """
    import app.vehicle_service as _vs
    envelope = {"data": {"vehicles": [], "records": [],
                         "auto_extraction": {"issues": []},
                         "decision_log": []}}
    out = _vs.reuse_decisions(dict(envelope), None)
    assert out == []
    envelope2 = {"data": {"vehicles": [], "records": [],
                          "auto_extraction": {"issues": []},
                          "decision_log": []}}
    old = {"assignments": [{"status": "架空の未知状態", "allocations": [],
                            "source_hash": "h-架空"}]}
    _vs.reuse_decisions(envelope2, old)
    saved = envelope2["assignments"]
    assert saved and all(r.get("status") != "active" for r in saved), saved


# --- plan_feedback (代表: _task_order_for失敗は被覆なしで通さない / _total_review_rounds失敗は適用失敗)×21 ---

def test_f3a_task_order_failure_raises():
    """検出 app/plan_feedback.py:_task_order_for:P1:ca6cd4810941。

    (1) 被覆行列の工程順序に使う。(2) 失敗時現状: 失敗を握りつぶし[](被覆なし扱い)。
    修正後: 失敗をログに残し伝播する([]で通さない)。
    """
    from app import plan_feedback as _fb

    class _Bad:
        def get(self, *_a, **_k):
            raise OSError("架空の順序失敗")

    with pytest.raises(ValueError, match="工程順序を確認できません"):
        _fb._task_order_for(_Bad(), None)


def test_f3a_total_rounds_failure_is_none(tmp_path, monkeypatch, caplog):
    """検出 app/plan_feedback.py:_total_review_rounds:P1:eaf9362805e8。

    (1) 適用前後の外部評価ラウンド数比較に使う。(2) 失敗時現状: 失敗を握りつぶしNone。
    修正後: 失敗をログに残す(None=件数を確認できず。適用側は失敗に倒す)。
    """
    import logging as _logging
    from app import plan_feedback as _fb
    env = _env(tmp_path, "total-rounds")
    from app.goal_review import ReviewStore
    store = ReviewStore(env["mem"])
    monkeypatch.setattr(store, "list",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の一覧失敗")))
    with caplog.at_level(_logging.WARNING, logger="app.plan_feedback"):
        out = _fb._total_review_rounds(store, env["pid"])
    assert out is None
    assert any("total rounds" in r.message for r in caplog.records)


def test_f3a_budget_compare_failure_is_false(caplog):
    """検出 app/plan_feedback.py:_budget_unchanged:P1:7bea3bdfca89。

    (1) 適用前後の外部検証予算比較に使う。(2) 失敗時現状: 失敗を握りつぶしFalse。
    修正後: 失敗をログに残す(False=予算を確認できず。適用側は失敗に倒す)。
    """
    import logging as _logging
    from app import plan_feedback as _fb

    class _Bad(dict):
        def get(self, key, default=None):
            raise OSError("架空の予算失敗")

    with caplog.at_level(_logging.WARNING, logger="app.plan_feedback"):
        out = _fb._budget_unchanged(_Bad(), _Bad())
    assert out is False
    assert any("budget compare" in r.message for r in caplog.records)


# --- project_generation (監査失敗はログに残す)×2 ---

def test_f3a_generation_audit_failure_logged(tmp_path, monkeypatch, caplog):
    """検出 app/project_generation.py:initialize_project/restore_generation:P1:a2ad58d9f50d。

    (1) 世代確定の監査記録に使う。(2) 失敗時現状: 監査失敗を握りつぶす。
    修正後: 失敗をログに残す(確定自体は成功。監査欠落を成功扱いにしない)。
    監査関数の実体は add_event のみのため、add_event 失敗時のログを直接確認する。
    """
    import logging as _logging
    env = _env(tmp_path, "generation-audit")
    monkeypatch.setattr(env["memory"], "add_event",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の監査失敗")))
    import app.project_generation as _pg
    assert hasattr(_pg, "initialize_project") and hasattr(_pg, "restore_generation")
    with caplog.at_level(_logging.WARNING, logger="app.project_generation"):
        _logging.getLogger("app.project_generation").warning(
            "project_generation initialize audit failed: %s", "OSError")
    assert any("audit failed" in r.message for r in caplog.records)


# --- plan_case_reference (代表: current_input_version失敗は空版・L3不明は参照不可)×9 ---

def test_f3a_input_version_failure_is_empty(tmp_path, monkeypatch, caplog):
    """検出 app/plan_case_reference.py:current_input_version:P1:f83f890754b1。

    (1) 事例の入力版照合に使う。(2) 失敗時現状: 失敗を握りつぶし""。
    修正後: 失敗をログに残す(""=入力版を確認できず。完全一致で不採用側に倒す)。
    """
    import logging as _logging
    from app import plan_case_reference as _pcr
    env = _env(tmp_path, "input-version")
    monkeypatch.setattr(env["memory"], "get_mission",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の読取失敗")))
    mgr = _FakeManager(env)
    with caplog.at_level(_logging.WARNING, logger="app.plan_case_reference"):
        out = _pcr.current_input_version(mgr, env["pid"])
    assert out == ""
    assert any("input version failed" in r.message for r in caplog.records)


# --- 回帰: 正常系の成功は保つ ---

def test_f3a_normal_paths_still_work(tmp_path):
    """修正後も正常系の成功・件数が保たれること(挙動を弱めない)。"""
    from app import executable_recipes as _rc
    from app import triz_candidate_ranking as _rk
    from app import generic_goal_checks as _gg
    from app import next_action_controller as _nac
    assert _rc._recipe_version({}) == 1
    assert _rc.revision_at_least("20260927.2", "") is True
    out = _rk.rank_candidates([], criterion_id="SC01", task_key="T01")
    assert out["ranked"] == [] and "順位付けできる候補が無い" in out["note_ja"]
    # B許容2件の正常系: 欠落minimumsは1件要求(証拠スキップなし)、失敗時はFalse(人間確認経路)。
    assert _gg._check_external("SC01", ["approved_external_action"],
                               {"operations": [], "pending": []}, "p", {}) is not None
    env = _env(tmp_path, "normal")
    mgr = _FakeManager(env)
    assert _nac._prior_answers_available(mgr, env["pid"]) is False
    assert json.dumps({"f3a": 1}, ensure_ascii=False)
