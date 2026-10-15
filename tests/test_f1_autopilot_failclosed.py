"""F1: autopilot fail-closed 化の失敗再現テスト (疑似PJのみ・実関数)。

app/autopilot.py の高リスク検出 (baseline で file が app/autopilot.py のもの)
それぞれについて、失敗を注入したときに成功 (advanced / 待機のまま進行扱い)
や外部増分0扱いにしないことを確認する。実関数・疑似PJ・一時ディレクトリのみ。
外部通信なし。
"""
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox


def _make_manager(tmp_path: Path, tag: str):
    name = f"f1-疑似-{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = [f"架空文書{i:02d}の作成と検証" for i in range(1, 4)]
    memory.save_mission(pid, "架空目標", "\n".join(
        f"{i}. {t}" for i, t in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        tasks.append(compile_task(i, text, {
            "title": f"架空文書工程{i}", "scope": "架空の成果物を作成し根拠を検証する。",
            "headings": ["目的", "実施内容"],
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


def _enable(manager, pid):
    import app.autopilot as auto
    return auto.set_enabled(manager, pid, True, actor="架空担当",
                            reason="架空の有効化理由")


def _force_record(manager, pid, stop_class, stop_code):
    """実経路と同じ形 (preflight ok + 現行署名) の分類を強制する。"""
    import app.autopilot as auto
    sig = _sig_of(manager, pid)
    record = {"stop_class": stop_class, "stop_code": stop_code,
              "evidence_refs": {"plan_signature": sig},
              "plan_signature": sig, "next_action": "review_feedback",
              "reason_ja": "架空の停止理由", "external_sends": 0,
              "preflight_status": "ok"}
    return auto, record, sig


def test_f1_remap_preview_all_fail_not_success(tmp_path, monkeypatch):
    """検出 app/autopilot.py:_action_remap_stored_review:P1:af10980773d7。

    (1) 進行成功の判定に使うか: 再対応付けのプレビュー。失敗を continue で
    次候補へ進め、最後は bindings 0 で成功扱いにしていた。
    (2) 失敗時現状: 全件失敗でも成功 dict を返す (fail-open)。
    修正後: 試したのに全件失敗なら例外 (呼出側がティック失敗にする)。
    """
    import app.autopilot as auto
    from app.plan_review_loop import remap_preview as _real_preview
    manager, pid = _make_manager(tmp_path, "remap")
    _enable(manager, pid)
    # human_required で止まった保存済みランを2件用意し、どちらも失敗させる。
    import app.plan_review_loop as loop
    from app.goal_review import ReviewStore
    sig = _sig_of(manager, pid)
    store = ReviewStore(manager.memory.path)
    for rid in ("架空run1", "架空run2"):
        store.put(pid, "auto_loop", rid,
                  {"id": rid, "project_id": pid, "plan_signature": sig,
                   "state": "stopped", "stop_kind": "human_required",
                   "bindings": []})
    monkeypatch.setattr(loop, "remap_preview",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            ValueError("架空のプレビュー失敗")))
    # 実関数を直接呼ぶ。修正前は成功 dict (bindings 0) を返す。
    with pytest.raises(Exception):
        auto._action_remap_stored_review(manager, pid)


def test_f1_budget_snapshot_unknown_stops_tick(tmp_path, monkeypatch):
    """検出 app/autopilot.py:_budget_snapshot:P1:2775ad19df79。

    (1) 外部増分 (予算) の計数に使うか: before/after の予算比較。
    (2) 失敗時現状: 読取不能を {} (未管理扱い) にし budget_kept=True で通す。
    修正後: 不明として停止 (ティック失敗・増分0と見なさない)。
    """
    import app.autopilot as auto
    import app.goal_review as gr
    manager, pid = _make_manager(tmp_path, "budget")
    _enable(manager, pid)
    # 予算の読取不能を注入。
    monkeypatch.setattr(gr, "review_budget",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            OSError("架空の予算読取失敗")))
    snap = auto._budget_snapshot(manager, pid)
    # 空辞書で通さないこと。
    assert snap != {}, snap
    assert snap is None or (isinstance(snap, dict)
                            and snap.get("_budget_unknown")), snap
    # ティックでも増分0と見なさず失敗すること。
    _auto, record, _sig = _force_record(manager, pid, "missing_binding",
                                        "binding:invalid_reference")
    monkeypatch.setattr(_auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    out = _auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["status"] == "failed", out
    assert out.get("budget_kept") is False, out


def test_f1_budget_unchanged_comparison_failure_stops(tmp_path):
    """検出 app/autopilot.py:_budget_unchanged:P1:7bea3bdfca89。

    (1) 外部増分 (予算維持) の判定に使うか: before/after の比較。
    (2) 失敗時現状: 比較不能を False (停止側) にするが広い except のまま。
    修正後: 狭い例外で False (停止側) を保ち、lint 非該当にする。
    不明入力 (None/unknown 印) は必ず False。
    """
    import app.autopilot as auto
    # 不明は維持扱いにしない。
    assert auto._budget_unchanged(None, {}) is False
    assert auto._budget_unchanged({}, None) is False
    assert auto._budget_unchanged(
        {"_budget_unknown": True}, {"managed": False}) is False
    assert auto._budget_unchanged(
        {"managed": False}, {"_budget_unknown": True}) is False
    # 比較不能 (int 化失敗) は False (停止側)。
    assert auto._budget_unchanged(
        {"managed": True, "period_utc": "p",
         "used_calls": "壊れた値", "remaining_calls": 0},
        {"managed": True, "period_utc": "p",
         "used_calls": 0, "remaining_calls": 0}) is False
    # 正常系は保つ (未管理は True、管理下で一致は True)。
    assert auto._budget_unchanged({"managed": False},
                                  {"managed": False}) is True
    assert auto._budget_unchanged(
        {"managed": True, "period_utc": "p",
         "used_calls": 1, "remaining_calls": 2},
        {"managed": True, "period_utc": "p",
         "used_calls": 1, "remaining_calls": 2}) is True


def _restore_tick_with_broken_manager(tmp_path, monkeypatch, break_target):
    """計数器の復元失敗を注入しティックする。成功を返さないことを確認する。

    装着 (__enter__) は通し、コンテキストを抜けた直後 (復元 after) に
    `_restore_failed` を付ける。ティック側が印を増分0と見なさず失敗に
    することが実関数で確認できる。
    """
    import contextlib as _cl
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, f"restore-{break_target}")
    _enable(manager, pid)
    _auto, record, _sig = _force_record(manager, pid, "missing_binding",
                                        "binding:invalid_reference")
    monkeypatch.setattr(_auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    manager.plan_review_runner = lambda *a, **k: []
    manager._local_complete = lambda *a, **k: None
    flags = {
        "runner": "runner:RuntimeError",
        "local": "local:RuntimeError",
        "provider": "provider:RuntimeError",
        "counts_bookkeeping": "local_bookkeeping:TypeError",
        "counts_reset": "local_reset:TypeError",
    }
    if break_target not in flags:
        raise AssertionError(break_target)
    orig_wrap = _auto._count_external_calls

    @_cl.contextmanager
    def _breaking_ctx(mgr):
        with orig_wrap(mgr) as counts:
            yield counts
        # orig の finally (復元) のあと、呼出側が counts を読む前に印を残す。
        counts["_restore_failed"] = flags[break_target]

    monkeypatch.setattr(_auto, "_count_external_calls", _breaking_ctx)
    return _auto.autopilot_tick(manager, pid, actor="架空担当")


def test_f1_restore_runner_failed_not_success(tmp_path, monkeypatch):
    """検出 app/autopilot.py:_count_external_calls:P1:a2ad58d9f50d#1。

    計数器の復元失敗を増分0と見なさずティック失敗にすること。
    """
    out = _restore_tick_with_broken_manager(tmp_path, monkeypatch, "runner")
    assert out["status"] == "failed", out
    assert out.get("enabled") is False, out


def test_f1_restore_local_failed_not_success(tmp_path, monkeypatch):
    """検出 app/autopilot.py:_count_external_calls:P1:a2ad58d9f50d#2。"""
    out = _restore_tick_with_broken_manager(tmp_path, monkeypatch, "local")
    assert out["status"] == "failed", out
    assert out.get("enabled") is False, out


def test_f1_restore_provider_failed_not_success(tmp_path, monkeypatch):
    """検出 app/autopilot.py:_count_external_calls:P1:a2ad58d9f50d#3。"""
    out = _restore_tick_with_broken_manager(tmp_path, monkeypatch, "provider")
    assert out["status"] == "failed", out
    assert out.get("enabled") is False, out


def test_f1_restore_counts_bookkeeping_failed(tmp_path, monkeypatch):
    """検出 app/autopilot.py:_count_external_calls:P1:a2ad58d9f50d#4。"""
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, "restore-book")
    _enable(manager, pid)

    class _Bad:
        def __bool__(self):
            return True

        def __int__(self):
            raise TypeError("架空の計数失敗")

    with auto._count_external_calls(manager) as counts:
        counts["local"] = _Bad()
    assert counts.get("_restore_failed"), counts


def test_f1_restore_counts_reset_failed(tmp_path, monkeypatch):
    """検出 app/autopilot.py:_count_external_calls:P1:a2ad58d9f50d#5。"""
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, "restore-reset")
    _enable(manager, pid)
    with auto._count_external_calls(manager) as counts:
        pass
    # 正常時は印が無いこと。
    assert not counts.get("_restore_failed"), counts
    out = _restore_tick_with_broken_manager(tmp_path, monkeypatch,
                                            "counts_reset")
    assert out["status"] == "failed", out
    assert out.get("enabled") is False, out


def test_f1_rounds_unknown_stops_tick(tmp_path, monkeypatch):
    """検出 app/autopilot.py:_count_review_rounds:P1:eaf9362805e8。

    (1) 外部増分 (レビュー輪数) の計数に使うか: before/after の差分。
    (2) 失敗時現状: None を返すが呼出側が None を 0 差分と同一視する。
    修正後: None は呼び出し側で必ず停止 (rounds 差分不明でティック失敗)。
    """
    import app.autopilot as auto
    from app.goal_review import ReviewStore
    manager, pid = _make_manager(tmp_path, "rounds")
    _enable(manager, pid)
    assert auto._count_review_rounds(manager, pid) == 0
    # 読取不能は None (不明) を保つこと。
    monkeypatch.setattr(auto, "_count_review_rounds", lambda *_a, **_k: None)
    assert auto._count_review_rounds(manager, pid) is None
    # ティックでも 0 と同一視せず失敗すること。
    _auto, record, _sig = _force_record(manager, pid, "missing_binding",
                                        "binding:invalid_reference")
    monkeypatch.setattr(_auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    out = _auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["status"] == "failed", out
    assert "external_rounds_unknown" in str(
        out.get("reason_ja") or "") or "round" in str(
        out.get("reason_ja") or "").lower() or "不明" in str(
        out.get("reason_ja") or ""), out


def test_f1_guard_eval_failure_stops(tmp_path, monkeypatch):
    """検出 app/autopilot.py:autopilot_tick:P1:a2ad58d9f50d#1 (guard 判定)。

    空転ガードの評価失敗を握りつぶさず停止側に倒すこと。
    """
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, "guard")
    _enable(manager, pid)
    _auto, record, _sig = _force_record(manager, pid, "missing_binding",
                                        "binding:invalid_reference")
    monkeypatch.setattr(_auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    import app.stop_classifier as sc
    monkeypatch.setattr(sc, "is_repeating", lambda *_a, **_k: False)
    # autopilot_tick は guard_auto_tick を app.stop_classifier から呼ぶ。
    monkeypatch.setattr("app.stop_classifier.guard_auto_tick",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            RuntimeError("架空のガード評価失敗")))
    out = _auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["status"] in ("stopped_guard", "failed"), out
    assert out["status"] != "advanced", out


def test_f1_repeating_history_failure_is_failed(tmp_path, monkeypatch):
    """検出 autopilot_tick#2 (repeating の履歴追記失敗)。"""
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, "rep-hist")
    _enable(manager, pid)
    _auto, record, _sig = _force_record(manager, pid, "missing_binding",
                                        "binding:invalid_reference")
    monkeypatch.setattr(_auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    import app.stop_classifier as sc
    monkeypatch.setattr(sc, "is_repeating", lambda *_a, **_k: True)
    monkeypatch.setattr(sc, "append_history",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            OSError("架空の履歴書込失敗")))
    out = _auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["status"] == "failed", out
    assert out.get("enabled") is False, out


def test_f1_human_history_failure_is_failed(tmp_path, monkeypatch):
    """検出 autopilot_tick#3 (人の判断待ちの履歴追記失敗)。"""
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, "human-hist")
    _enable(manager, pid)
    _auto, record, _sig = _force_record(manager, pid, "policy_block",
                                        "policy:denied")
    monkeypatch.setattr(_auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    import app.stop_classifier as sc
    monkeypatch.setattr(sc, "append_history",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            OSError("架空の履歴書込失敗")))
    out = _auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["status"] == "failed", out


def test_f1_human_record_failure_is_failed(tmp_path, monkeypatch):
    """検出 autopilot_tick#4 (人の判断待ちの記録動作失敗)。"""
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, "human-rec")
    _enable(manager, pid)
    _auto, record, _sig = _force_record(manager, pid, "policy_block",
                                        "policy:denied")
    monkeypatch.setattr(_auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    monkeypatch.setattr(_auto, "run_named_action",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            RuntimeError("架空の記録失敗")))
    out = _auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["status"] == "failed", out


def test_f1_needshuman_history_failure_is_failed(tmp_path, monkeypatch):
    """検出 autopilot_tick#5 (needs_human 経路の履歴追記失敗)。"""
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, "needshuman")
    _enable(manager, pid)
    # missing_coverage で指摘なし -> _NeedsHumanDecision の経路。
    _auto, record, _sig = _force_record(manager, pid, "missing_coverage",
                                        "coverage:incomplete")
    monkeypatch.setattr(_auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    import app.stop_classifier as sc
    monkeypatch.setattr(sc, "append_history",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            OSError("架空の履歴書込失敗")))
    out = _auto.autopilot_tick(manager, pid, actor="架空担当")
    # needs_human 経路自体が成立しなくても、履歴失敗時は失敗で返すこと。
    assert out["status"] == "failed", out


def test_f1_failedpath_history_double_failure_still_failed(tmp_path,
                                                           monkeypatch):
    """検出 autopilot_tick#6 (失敗経路の履歴追記失敗 + 通知の二重失敗)。

    失敗の通知 (無効化・監査) が再度失敗しても成功は返さないこと。
    """
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, "failed-hist")
    _enable(manager, pid)
    _auto, record, _sig = _force_record(manager, pid, "missing_binding",
                                        "binding:invalid_reference")
    monkeypatch.setattr(_auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    # アクション失敗 + 履歴失敗 + 無効化失敗 + 監査失敗の四重失敗。
    monkeypatch.setattr(_auto, "run_named_action",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            ValueError("架空の作業失敗")))
    import app.stop_classifier as sc
    monkeypatch.setattr(sc, "append_history",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            OSError("架空の履歴書込失敗")))
    monkeypatch.setattr(_auto, "_disable_internal",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            OSError("架空の無効化失敗")))
    monkeypatch.setattr(_auto, "_audit",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            OSError("架空の監査失敗")))
    out = _auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["status"] == "failed", out
    assert out.get("enabled") is False, out


def test_f1_capability_error_history_failure_still_failed(tmp_path,
                                                          monkeypatch):
    """検出 autopilot_tick#7 (capability error 経路の履歴追記失敗)。"""
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, "cap-err")
    _enable(manager, pid)
    _auto, record, _sig = _force_record(manager, pid, "missing_binding",
                                        "binding:invalid_reference")
    monkeypatch.setattr(_auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    # capability_preflight の error 四値を一般経路で返すよう細工する。
    monkeypatch.setitem(_auto.ACTION_FOR_STOP, "missing_binding",
                        "capability_preflight")
    monkeypatch.setattr(_auto, "run_named_action",
                        lambda *_a, **_k: {"action": "capability_preflight",
                                          "outcome": "error",
                                          "error_type": "ValueError",
                                          "reason": "架空の生成失敗"})
    import app.stop_classifier as sc
    monkeypatch.setattr(sc, "append_history",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            OSError("架空の履歴書込失敗")))
    out = _auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["status"] == "failed", out


def test_f1_advanced_history_failure_is_failed(tmp_path, monkeypatch):
    """検出 autopilot_tick#8 (成功経路の履歴追記失敗)。

    ティック記録後に履歴追記が失敗しても advanced (成功) を返さないこと。
    """
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, "adv-hist")
    _enable(manager, pid)
    _auto, record, _sig = _force_record(manager, pid, "missing_binding",
                                        "binding:invalid_reference")
    monkeypatch.setattr(_auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    import app.stop_classifier as sc
    monkeypatch.setattr(sc, "append_history",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            OSError("架空の履歴書込失敗")))
    out = _auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["status"] == "failed", out
    assert out["status"] != "advanced", out
