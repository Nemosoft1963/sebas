"""P0-B 失敗理由別の修正ループの受入試験。疑似PJ・実関数・外部通信なし。

- 各種類の失敗が期待どおりに分類される。
- 『元パス・失敗項目が特定でき、必要な値が原本にある』phrase_missing は
  元成果物が版管理(修正前の保全)されて再生成され、台帳が無効化されて再検証される。
  修正後に値が入れば PASS、入らなければ FAIL のまま。
- 追補ファイルが残っていても、項目の合否は変わらない(追補で PASS にならない)。
- semantic_quote_mismatch は再試行が1回だけ行われ、なお不一致なら
  UNTESTABLE のまま人に提示される(PASS にならない)。
- human/外部/原本外/不明/システム異常は自動修復されず、理由と人の操作が記録される。
- 同じ失敗署名での再試行は2回で止まり、判断待ちになる。追補が無限に増えない。
  修正前後の項目結果と成果物ハッシュが保存される。
- 特定案件の語彙は試験入力にだけ置き、製品コードに入れない。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.requirement_item_ledger import (
    latest_ledger,
    list_repair_runs,
    record_ledger,
    record_repair_run,
)
from app.requirement_verification import (
    P0B_MAX_REPAIR_ATTEMPTS,
    classify_item_failure,
    failure_operator_hint,
    failure_signature,
    verify_requirement_items,
)
from app.structured_planning import compile_plan, compile_task, contract_of
from app.workspace_files import WorkspaceSandbox


def test_item_ledger_index_creation_failure_is_not_silenced():
    import app.requirement_item_ledger as ledger

    class EmptyResult:
        def fetchall(self):
            return []

    class FailingConnection:
        def execute(self, sql, *_args):
            if "CREATE INDEX" in sql:
                raise sqlite3.OperationalError("injected index failure")
            return EmptyResult()

    with pytest.raises(sqlite3.OperationalError, match="injected index failure"):
        ledger._ensure_tables(FailingConnection())


REGISTERED = "開催日: 2026年11月14日 10:00 会場: 架空ホール 準備 受付 終了後確認"
CRITERION = (
    "案内文1ファイルとチェックリスト1ファイル、合計2ファイルを作る。"
    "案内文には日時を含める。チェックリストには準備・終了後確認を含める。"
)


def _proposal():
    return {"title": "疑似案内", "scope": "疑似の案内と手順を整理する。",
            "headings": ["概要", "詳細"], "depends_on": [],
            "outputs": [
                {"kind": "document",
                 "required_headings": ["案内文：イベント概要と参加方法",
                                       "案内文：持ち物と問い合わせ先"]},
                {"kind": "checklist",
                 "required_headings": ["チェックリスト：準備・受付・終了後の手順",
                                       "根拠と未確認事項"]},
            ]}


def _manager(tmp_path, registered=REGISTERED):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("疑似P0-B")
    memory.save_mission(project["id"], "## 達成条件\n1. " + CRITERION, "", "", False, [])
    data = registered.encode("utf-8")
    memory.add_context_file(project["id"], "registered.md", registered, len(data),
                            data, "text/markdown", "markdown", "疑似原本",
                            "", source="registered")
    workspace = WorkspaceSandbox(tmp_path / "workspace")

    def _static(_project):
        return (registered, [{"id": "ctx1", "filename": "registered.md",
                              "content": registered, "file_kind": "markdown",
                              "size_bytes": len(registered.encode()), "source": "registered",
                              "char_count": len(registered)}])

    class FakeLlm:
        async def stream(self, messages):  # pragma: no cover - 使わない
            yield "{}"

    manager = ProjectOrchestrator(
        memory, FakeLlm(), _static, None, lambda: [], workspace=workspace)
    return manager, project["id"]


def _bind(manager, pid):
    task = compile_task(1, CRITERION, _proposal(), [], registered_text=REGISTERED)
    plan = compile_plan([CRITERION], [task])
    manager.memory.replace_plan(pid, plan["summary"], plan["tasks"])
    return next(t for t in manager.memory.get_mission(pid)["tasks"]
                if t["task_key"] == "SC01")


def _write(manager, pid, path, body):
    project = manager.memory.get_project(pid)
    target = manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def _resolve(manager, pid):
    project = manager.memory.get_project(pid)

    def _inner(relative):
        return manager.workspace.resolve_file(
            project.get("workspace_path", ""), pid, relative, must_exist=True)[2]

    return _inner


_GOOD = (
    "開催日", "架空ホール",
    "## 案内文：イベント概要と参加方法\n日時と場所。\n",
    "## 案内文：持ち物と問い合わせ先\n持ち物と架空の問い合わせ先。\n",
    "## 根拠と未確認事項\n架空と要確認と想定。\n",
    "## 実施状態と次の行動\n実施状態と次の行動。\n",
    "## 準備・受付・終了後の手順\n準備と受付と終了後確認。\n",
)


def _write_fixtures(manager, pid, missing=("準備",)):
    _write(manager, pid, "result/sc01.md",
           "# 案内\n開催日: 2026年11月14日 10:00 会場: 架空ホール\n"
           "## 案内文：イベント概要と参加方法\n日時と場所。\n"
           "## 案内文：持ち物と問い合わせ先\n持ち物と架空の問い合わせ先。\n"
           "## 根拠と未確認事項\n架空と要確認と想定。\n"
           "## 実施状態と次の行動\n実施状態と次の行動。\n")
    # 欠落させる語は汎用抽出の必須語(終了後確認)に合わせる。本文だけ変え、
    # 見出しは壊さない(見出し欠落と語句欠落を混ぜない)。
    keep = [w for w in ("準備", "受付", "終了後確認") if w not in missing]
    if "受付" not in keep:
        keep.append("受付")
    body_line = "と".join(keep) + "を記載する。"
    checklist = ("# チェックリスト\n" + body_line + "\n"
                 "## チェックリスト：準備・受付・終了後の手順\n"
                 + body_line + "\n"
                 "## 根拠と未確認事項\n架空と要確認と想定。\n"
                 "## 実施状態と次の行動\n実施状態と次の行動。\n")
    _write(manager, pid, "result/sc01_checklist.md", checklist)


_MISSING_PHRASE = "終了後確認"


def _save_ledger(manager, pid, task, rows):
    from app.goal_review import plan_snapshot
    _, signature = plan_snapshot(manager, pid)
    payload_rows = list(rows)
    record_ledger(manager.memory.path, {
        "project_id": pid, "plan_signature": signature, "source_hash": "src-p0b",
        "artifact_hashes": {"result/sc01.md": "h1", "result/sc01_checklist.md": "h2"},
        "rows": payload_rows,
        "criterion_verdicts": {},
        "ledger_digest": "d",
    })
    from app.requirement_verification import ledger_payload
    record_ledger(manager.memory.path, ledger_payload(
        pid, signature, "src-p0b", payload_rows,
        {"result/sc01.md": "h1", "result/sc01_checklist.md": "h2"}))


def test_classify_each_failure_kind():
    base = {"item_id": "SC01-R01", "required": True}
    assert classify_item_failure(
        {**base, "result": "FAIL", "method": "phrase",
         "artifact_path": "result/sc01.md", "reason": "必須語句なし: 日時"}) == "phrase_missing"
    assert classify_item_failure(
        {**base, "result": "UNTESTABLE", "method": "phrase", "artifact_path": "",
         "reason": "対象成果物不明のため判定不能"}) == "artifact_missing"
    assert classify_item_failure(
        {**base, "result": "FAIL", "method": "phrase", "artifact_path": "result/sc01.md",
         "reason": "読み取り失敗: OSError"}) == "artifact_missing"
    assert classify_item_failure(
        {**base, "result": "FAIL", "method": "phrase", "artifact_path": "result/a.md",
         "reason": "wrong_target: SC01-R01 points at result/a.md"}) == "wrong_target"
    assert classify_item_failure(
        {**base, "result": "UNTESTABLE", "method": "llm_semantic",
         "artifact_path": "result/sc01.md",
         "reason": "人の確認待ち: 引用が成果物と一致しない"}) == "semantic_quote_mismatch"
    assert classify_item_failure(
        {**base, "result": "FAIL", "method": "phrase", "artifact_path": "result/sc01.md",
         "reason": "原本外の断定を検出"}) == "unsupported_fact"
    assert classify_item_failure(
        {**base, "result": "UNTESTABLE", "method": "llm_semantic",
         "artifact_path": "result/sc01.md", "reason": "人の確認待ち: ローカルLLMの失敗"}) == "human_decision"
    assert classify_item_failure(
        {**base, "result": "FAIL", "method": "count", "artifact_path": "result/sc01.md",
         "reason": "承認済み外部アクションの実行証拠が不足しています"}) == "external_evidence"
    assert classify_item_failure(
        {**base, "result": "UNTESTABLE", "method": "", "artifact_path": "result/sc01.md",
         "reason": "台帳の読取に失敗"}) == "system_error"
    assert classify_item_failure(
        {**base, "result": "UNTESTABLE", "method": "count", "artifact_path": "result/sc01.md",
         "reason": "数量の期待値が未定義のため判定不能"}) == "unclassified"
    # 不明は system_error ではなく unclassified(推測で自動修復しない)。
    assert classify_item_failure(
        {**base, "result": "UNTESTABLE", "method": "count", "artifact_path": "result/sc01.md",
         "reason": "よく分からない理由"}) == "unclassified"
    # PASS は分類しない。
    assert classify_item_failure(
        {**base, "result": "PASS", "method": "phrase",
         "artifact_path": "result/sc01.md", "reason": ""}) is None
    # 壊れた行は system_error として記録を残す。
    assert classify_item_failure(None) == "system_error"
    # 人の操作の文面は分類ごとに固定。
    assert failure_operator_hint("phrase_missing")
    assert failure_operator_hint("human_decision")
    assert failure_operator_hint("no-such-kind")


def test_phrase_missing_repairs_artifact_with_versioning_and_reverify(tmp_path):
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    _write_fixtures(manager, pid, missing=(_MISSING_PHRASE,))
    rows = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    phrase_fails = [r for r in rows
                    if r["method"] == "phrase" and r["result"] == "FAIL"]
    assert phrase_fails, rows
    by_reason = {r["item_id"]: r.get("reason") for r in phrase_fails}
    assert any(_MISSING_PHRASE in str(reason or "") for reason in by_reason.values()), by_reason
    target = next(r for r in phrase_fails if _MISSING_PHRASE in str(r.get("reason") or ""))
    before_text = (manager.workspace.resolve_file(
        manager.memory.get_project(pid).get("workspace_path", ""), pid,
        "result/sc01_checklist.md", must_exist=True)[2].read_text(encoding="utf-8"))
    _save_ledger(manager, pid, task, rows)
    saved = latest_ledger(manager.memory.path, pid)
    debug_rows = [(r.get("item_id"), r.get("method"), r.get("result"),
                   classify_item_failure(r)) for r in saved["rows"]]
    assert any(k == "phrase_missing" for _, _, _, k in debug_rows), debug_rows
    # フェイクLLM: 不足値を原本の値で補った全文を返す(自己申告の合否は使わない)。
    required_value = "終了後確認を含める"
    mended = before_text.replace("を記載する。", f"と{required_value}。")
    if required_value not in mended:
        mended = before_text + f"\n{required_value}\n"

    async def _fake_complete(_system, _prompt):
        return mended

    manager._local_complete = _fake_complete  # type: ignore[method-assign]
    runs_before = list_repair_runs(manager.memory.path, pid)
    mission_before = manager.memory.get_mission(pid, event_limit=500)
    ok = manager._append_goal_supplement(pid, ["SC01: 準備が無い"], "# report")
    runs_after = list_repair_runs(manager.memory.path, pid)
    events_after = manager.memory.get_mission(pid, event_limit=500)["events"]
    kinds_after = [e["kind"] for e in events_after[:12]]
    assert ok is True, {"runs_before": runs_before, "runs_after": runs_after,
                        "kinds": kinds_after}
    # 版管理: 修正前が .v1 として保全されている。
    project = manager.memory.get_project(pid)
    preserved = manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid,
        "result/sc01_checklist.v1.md", must_exist=True)[2]
    assert preserved.read_text(encoding="utf-8") == before_text
    # 台帳が無効化されて再検証され、修正後に PASS になる。
    saved = latest_ledger(manager.memory.path, pid)
    assert saved is not None and saved["rows"]
    by_id = {r["item_id"]: r for r in saved["rows"]}
    assert by_id[target["item_id"]]["result"] == "PASS", by_id[target["item_id"]]
    # 追補ファイルは作らない(版管理修正が中心)。
    project = manager.memory.get_project(pid)
    try:
        manager.workspace.resolve_file(
            project.get("workspace_path", ""), pid,
            "result/goal_supplement_1_1.md", must_exist=True)
        created_supplement = True
    except (OSError, ValueError, FileNotFoundError):
        created_supplement = False
    assert created_supplement is False
    runs = list_repair_runs(manager.memory.path, pid)
    assert any(r["decision"] == "reverified" for r in runs)
    assert any(r.get("artifact_hash_after") for r in runs)


def test_supplement_file_does_not_change_item_verdict(tmp_path):
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    _write_fixtures(manager, pid, missing=(_MISSING_PHRASE,))
    rows = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    fails_before = sorted(r["item_id"] for r in rows
                          if r["method"] == "phrase" and r["result"] == "FAIL")
    assert fails_before
    # 追補ファイルが残っていても、項目の合否は変わらない(追補で PASS にならない)。
    _write(manager, pid, "result/goal_supplement_1_1.md",
           "# 追補\n準備について補足する。\n")
    rows2 = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    # 比較対象は前段と同じ決定的 phrase 項目。文意項目はローカル判定器の有無で
    # FAIL/UNTESTABLE が変わり得るため、追補不変条件の証明には混ぜない。
    fails_after = sorted(r["item_id"] for r in rows2
                         if r["method"] == "phrase" and r["result"] == "FAIL")
    assert fails_after == fails_before
    # より強い検査: 追補の前後で全行の item_id → result が完全に一致する。
    before_map = {str(r.get("item_id") or ""): str(r.get("result") or "") for r in rows}
    after_map = {str(r.get("item_id") or ""): str(r.get("result") or "") for r in rows2}
    assert after_map == before_map


def test_semantic_requote_once_then_human(tmp_path):
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    _write_fixtures(manager, pid, missing=())
    # 文意の失敗行だけの台帳を作る(引用不一致)。
    fake_row = {"item_id": "SC01-R14", "criterion_id": "SC01",
                "artifact_path": "result/sc01.md", "requirement": "文意の適合",
                "method": "llm_semantic", "required": True, "result": "UNTESTABLE",
                "line": -1, "quote": "", "reason": "人の確認待ち: 引用が成果物と一致しない"}
    _save_ledger(manager, pid, task, [fake_row])
    async def _boom(_system, _prompt):
        raise RuntimeError("no llm")
    manager._local_complete = _boom  # type: ignore[method-assign]
    ok = manager._append_goal_supplement(pid, ["SC01: 引用不一致"], "# report")
    assert ok is True
    runs = list_repair_runs(manager.memory.path, pid)
    assert any(r["decision"] == "requote_failed" for r in runs)
    saved = latest_ledger(manager.memory.path, pid)
    row = next(r for r in saved["rows"] if r["item_id"] == "SC01-R14")
    assert row["result"] == "UNTESTABLE"
    mission = manager.memory.get_mission(pid)
    assert mission["status"] == "paused"
    assert any(e["kind"] == "p0b_awaiting_decision" for e in mission["events"])


def test_non_repairable_kinds_are_not_auto_repaired(tmp_path):
    kinds = {
        "human_decision": {"result": "UNTESTABLE", "method": "llm_semantic",
                           "artifact_path": "result/sc01.md",
                           "reason": "人の確認待ち: 判断が未実施"},
        "external_evidence": {"result": "FAIL", "method": "count",
                              "artifact_path": "result/sc01.md",
                              "reason": "承認済み外部アクションの実行証拠が不足しています"},
        "unsupported_fact": {"result": "FAIL", "method": "phrase",
                             "artifact_path": "result/sc01.md", "reason": "原本外の断定"},
        "unclassified": {"result": "UNTESTABLE", "method": "count",
                         "artifact_path": "result/sc01.md", "reason": "数量の期待値が未定義"},
        "system_error": {"result": "UNTESTABLE", "method": "",
                         "artifact_path": "result/sc01.md", "reason": "台帳の読取に失敗"},
    }
    for kind, extra in kinds.items():
        manager, pid = _manager(tmp_path / kind)
        task = _bind(manager, pid)
        _write_fixtures(manager, pid, missing=())
        row = {"item_id": "SC01-R01", "criterion_id": "SC01",
               "requirement": "疑似項目", "required": True, **extra}
        _save_ledger(manager, pid, task, [row])
        called = []
        orig = manager._repair_phrase_missing_once
        manager._repair_phrase_missing_once = lambda *a, **k: called.append(True) or "repaired"  # type: ignore[method-assign]
        try:
            ok = manager._append_goal_supplement(pid, ["SC01: 疑似未達"], "# report")
        finally:
            manager._repair_phrase_missing_once = orig  # type: ignore[method-assign]
        assert ok is True, kind
        assert called == [], kind
        runs = list_repair_runs(manager.memory.path, pid)
        awaiting = [r for r in runs if r["decision"] == "awaiting_decision"]
        assert awaiting and awaiting[0]["failure_kind"] == kind, (kind, runs)
        assert awaiting[0]["operator_action"], kind
        project = manager.memory.get_project(pid)
        try:
            manager.workspace.resolve_file(
                project.get("workspace_path", ""), pid,
                "result/sc01.v1.md", must_exist=True)
            versioned = True
        except (OSError, ValueError, FileNotFoundError):
            versioned = False
        assert versioned is False, kind


def test_same_signature_stops_after_two_and_awaits(tmp_path):
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    _write_fixtures(manager, pid, missing=(_MISSING_PHRASE,))
    rows = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    target = next(r for r in rows
                  if r["method"] == "phrase" and r["result"] == "FAIL")
    _save_ledger(manager, pid, task, rows)
    from app.vehicle_workflow import digest as _digest, sources as _sources
    artifact_hash = _digest(b"h")
    signature = failure_signature(target["item_id"], "phrase_missing",
                                  artifact_hash, "src-p0b")
    for _ in range(P0B_MAX_REPAIR_ATTEMPTS):
        record_repair_run(manager.memory.path, project_id=pid,
                          item_id=target["item_id"], failure_kind="phrase_missing",
                          failure_signature=signature, plan_signature="sig",
                          source_hash="src-p0b", artifact_path=target["artifact_path"],
                          artifact_hash_before=artifact_hash,
                          artifact_hash_after=artifact_hash,
                          item_result_before="FAIL", item_result_after="FAIL",
                          decision="repair_failed", operator_action="人の操作",
                          reason="疑似の失敗")
    # 同じ失敗署名では2回で止まり、判断待ちになる。追補が無限に増えない。
    def _sig(*_a, **_k):
        return signature
    import app.requirement_verification as _rv
    orig_sig = _rv.failure_signature
    _rv.failure_signature = _sig  # type: ignore[assignment]
    try:
        ok = manager._append_goal_supplement(pid, ["SC01: 準備が無い"], "# report")
    finally:
        _rv.failure_signature = orig_sig  # type: ignore[assignment]
    assert ok is True
    mission = manager.memory.get_mission(pid)
    supplements = [t for t in mission["tasks"] if t["task_key"].startswith("goal_supplement_")]
    assert supplements == []
    runs = list_repair_runs(manager.memory.path, pid)
    awaiting = [r for r in runs if r["decision"] == "awaiting_decision"]
    assert awaiting
    saved = latest_ledger(manager.memory.path, pid)
    row = next(r for r in saved["rows"] if r["item_id"] == target["item_id"])
    assert row["result"] == "FAIL"
    assert any(e["kind"] == "p0b_awaiting_decision" for e in mission["events"])


@pytest.mark.asyncio
async def test_p0b_async_path_awaits_llm_without_runtime_warning(tmp_path):
    import gc
    import warnings

    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    _write_fixtures(manager, pid, missing=(_MISSING_PHRASE,))
    rows = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    _save_ledger(manager, pid, task, rows)
    project = manager.memory.get_project(pid)
    before = manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid,
        "result/sc01_checklist.md", must_exist=True)[2].read_text(encoding="utf-8")
    mended = before + "\n終了後確認を含める\n"
    calls = []

    async def fake_complete(_system, _prompt):
        calls.append("called")
        return mended

    manager._local_complete = fake_complete  # type: ignore[method-assign]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await asyncio.wait_for(
            manager._append_goal_supplement_async(
                pid, ["SC01: 終了後確認が無い"], "# report"),
            timeout=5.0,
        )
        gc.collect()
    assert result is True
    assert calls == ["called"]
    assert "終了後確認を含める" in manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid,
        "result/sc01_checklist.md", must_exist=True)[2].read_text(encoding="utf-8")
    assert not [w for w in caught if issubclass(w.category, RuntimeWarning)
                and "was never awaited" in str(w.message)]


def test_full_loop_repair_then_reverify_with_fake_llm(tmp_path):
    # フェイクのLLMで『失敗→分類→修復→再検証』と『上限到達→判断待ち』を保存結果まで検査。
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    _write_fixtures(manager, pid, missing=(_MISSING_PHRASE,))
    rows = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    assert any(r["method"] == "phrase" and r["result"] == "FAIL" for r in rows)
    _save_ledger(manager, pid, task, rows)
    project = manager.memory.get_project(pid)
    before = manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid,
        "result/sc01_checklist.md", must_exist=True)[2].read_text(encoding="utf-8")
    mended = before.replace("を記載する。", f"と{_MISSING_PHRASE}を記載する。")
    if _MISSING_PHRASE not in mended:
        mended = before + f"\n{_MISSING_PHRASE}\n"

    async def _fake_complete(_system, _prompt):
        return mended

    manager._local_complete = _fake_complete  # type: ignore[method-assign]
    assert manager._append_goal_supplement(pid, ["SC01: 準備が無い"], "# report") is True
    saved = latest_ledger(manager.memory.path, pid)
    assert any(r["result"] == "PASS" for r in saved["rows"])
    runs = list_repair_runs(manager.memory.path, pid)
    assert runs and all(r.get("failure_signature") for r in runs)
    assert any(r.get("artifact_hash_before") and r.get("artifact_hash_after") for r in runs)
    # API応答・最終検証書まで届く: 台帳の行が最終検証の再検査で読める。
    rows2 = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    assert any(r["result"] == "PASS" for r in rows2)


def test_existing_refusal_reasons_are_not_hidden(tmp_path):
    # 新しい処理が既存の具体的な拒否理由を隠していないか(検査の順序)。
    from app.requirement_matrix import validate_requirement_matrix
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    broken = json.loads(task["acceptance_criteria"])
    broken["requirement_matrix"][0]["artifact_path"] = ""
    checked = validate_requirement_matrix({"tasks": [{
        "task_key": "SC01",
        "acceptance_criteria": json.dumps(broken, ensure_ascii=False)}]})
    assert checked["passed"] is False
    assert any("requirement target unknown" in i for i in checked["issues"])
    asyncio.get_event_loop_policy().new_event_loop()
