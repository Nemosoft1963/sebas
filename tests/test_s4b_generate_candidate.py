"""Stage 4-B: 自走 generate_candidate の実体化 (疑似PJのみ・実関数)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
ローカルLLM(Ollama)はスタブで呼出回数を数える。期待値は入力から導く。
分野の異なる疑似PJ3種 (文書生成型/表データ型/外部作用型、架空)。
実際のOllamaでの生成は確認していない (スタブ)。
"""
import asyncio
import json
import sqlite3
from unittest.mock import Mock

import pytest

from app.core import Ollama
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

DOMAINS = ["doc", "table", "external"]

SAFE_CHANGE = ("登録された原本の参照箇所と成果物の主張を一対一で照合し、"
               "不一致を明示する追加手順を実施する。")
SAFE_REASON = ("原本と成果物の対応が欠けるという指摘に具体的な照合手順を"
               "追加する対応を行う。")


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
    name = f"s4b-疑似-{domain}-{n}{tag}"
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


def _install_local_stub(manager, pid, counter, mode="ok"):
    """保存済み指摘から amend 候補JSONを返す LLM スタブ。外部は呼ばない。"""
    manager.llm = Mock(spec=Ollama)

    async def _local(*args):
        counter["local"] += 1
        if mode == "timeout":
            raise asyncio.TimeoutError("架空タイムアウト")
        if mode == "bad_json":
            return "架空の不正なJSON{{{"
        if mode == "invalid":
            # 検査不合格: 指摘IDが一致しない対応表を返す。
            return json.dumps({"actions": [{
                "issue_id": "架空の別指摘ID", "disposition": "amend",
                "target": "SC01", "change": SAFE_CHANGE, "reason": SAFE_REASON,
            }]})
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
                "change": SAFE_CHANGE, "reason": SAFE_REASON,
            })
        if mode == "no_llm":
            raise AssertionError("LLMを呼んではならない")
        return json.dumps(data)

    async def _external(*args):
        counter["external"] += 1
        raise AssertionError("external must not be called")

    manager._local_complete = _local
    manager.plan_review_runner = _external
    return counter


def _seed_missing_coverage(manager, pid, text="架空指摘文の対応が必要"):
    from app.goal_review import ReviewStore
    sig = _sig_of(manager, pid)
    store = ReviewStore(manager.memory.path)
    body = {"status": "not_passed",
            "reviews": [{"provider": "chatgpt", "status": "fail",
                         "issues": [{"text": text, "criterion": "SC01",
                                     "step": "1"}]}], "finished": 0, "packet": None}
    store.put(pid, "plan", sig, body)
    return sig


def _fail_text_of(manager, pid):
    from app.plan_feedback import issues_for
    return [x.get("text") or "" for x in issues_for(manager, pid, _sig_of(manager, pid))]


def _external_counters(manager, pid):
    import app.autopilot as auto
    from app.goal_review import review_budget
    from app.stop_classifier import _EXTERNAL_SENDS_COUNTER as counter
    return {
        "sends": int(counter.get("calls") or 0),
        "rounds": auto._count_review_rounds(manager, pid),
        "budget": review_budget(manager, pid),
    }


def _version_of(manager, pid):
    return manager.memory.get_mission(pid)["plan_version"]


@pytest.mark.parametrize("domain", DOMAINS)
def test_tick_generates_candidate_via_propose(tmp_path, domain):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-gen")
    sig = _seed_missing_coverage(manager, pid)
    counter = _install_local_stub(manager, pid, {"local": 0, "external": 0})
    version_before = _version_of(manager, pid)
    ext_before = _external_counters(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    out = auto.autopilot_tick(manager, pid, actor="架空担当")
    if domain == "external":
        # B-3: 事前照合で機能不足が確定するため下書きまで自走し、確認待ちで止まる。
        assert out["stop_class"] == "missing_capability", out
        assert out["status"] == "waiting_human", out
        assert out["action_name"] == "capability_preflight", out
        assert out.get("capability_outcome") == "created", out
        assert out["enabled"] is True
        assert counter["local"] == 0, counter
        assert out.get("local_calls", 0) == 0, out
        assert counter["external"] == 0
        assert out["external_delta"] == 0
        assert out["external_rounds_delta"] == 0
        assert out["budget_kept"] is True
        assert _version_of(manager, pid) == version_before
        ext_after = _external_counters(manager, pid)
        assert ext_after["sends"] == ext_before["sends"]
        assert ext_after["rounds"] == ext_before["rounds"]
        assert ext_after["budget"] == ext_before["budget"]
        from app.goal_review import ReviewStore
        stored_ext = ReviewStore(manager.memory.path).get(pid, "revision", sig) or {}
        assert not stored_ext.get("candidate_id"), stored_ext
        assert "機能" in out.get("reason_ja", ""), out
        assert out["next_action"] == "resolve_development", out
        # 主たる停止は機能不足だが被覆不足も併発として残る。
        from app.workflow_readiness import build_readiness, build_stop_class
        record = build_stop_class(manager, pid, build_readiness(manager, pid))
        assert record["stop_class"] == "missing_capability", record
        assert any(a.get("stop_class") == "missing_coverage"
                   for a in (record.get("also") or [])), record
        return
    assert out["stop_class"] == "missing_coverage", out
    assert out["status"] == "advanced", out
    assert out["action_name"] == "generate_candidate", out
    detail = out["detail"]
    candidate_id = detail.get("candidate_id")
    assert candidate_id, out
    # (b) LLMスタブの呼出が1回
    assert counter["local"] == 1, counter
    assert out.get("local_calls") == 1, out
    # (c) apply が呼ばれていない・計画版が増えていない
    assert _version_of(manager, pid) == version_before
    from app.goal_review import ReviewStore
    stored = ReviewStore(manager.memory.path).get(pid, "revision", sig) or {}
    assert stored.get("candidate_id") == candidate_id
    assert stored.get("status") == "draft"
    assert stored.get("coverage_matrix"), out
    # (a) 候補ID・被覆行列が付く
    assert stored["candidate_id"] == candidate_id
    assert detail.get("coverage_rows") == len(
        (stored.get("coverage_matrix") or {}).get("rows") or [])
    # (d) 外部AI呼出0・外部評価ラウンド/予算が増えていない
    ext_after = _external_counters(manager, pid)
    assert counter["external"] == 0
    assert out["external_delta"] == 0
    assert out["external_rounds_delta"] == 0
    assert out["budget_kept"] is True
    assert ext_after["sends"] == ext_before["sends"]
    assert ext_after["rounds"] == ext_before["rounds"]
    assert ext_after["budget"] == ext_before["budget"]
    # (e) ティック記録に候補IDと被覆の要約があり、本文全文が入っていない
    blob = json.dumps(out, ensure_ascii=False)
    assert candidate_id in blob
    assert "被覆" in out.get("reason_ja") or "修正案" in out.get("reason_ja")
    for secret_text in _fail_text_of(manager, pid):
        assert secret_text not in blob
    assert SAFE_CHANGE not in blob
    # 「解消」とは記録しない。次の一手は人の確認(review_feedback)。
    assert "解消" not in out.get("reason_ja", "")
    assert out["next_action"] == "review_feedback", out
    assert out["enabled"] is True


@pytest.mark.parametrize("domain", DOMAINS)
def test_idempotent_second_tick_no_new_llm_call(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-idem")
    sig = _seed_missing_coverage(manager, pid)
    counter = _install_local_stub(manager, pid, {"local": 0, "external": 0})
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    first = auto.autopilot_tick(manager, pid, actor="架空担当")
    if domain == "external":
        # B-3: 機能不足は下書きまで自走し、確認待ちで止まる。LLM呼出は増えない。
        assert first["status"] == "waiting_human", first
        assert first["stop_class"] == "missing_capability", first
        assert first["action_name"] == "capability_preflight", first
        assert first.get("capability_outcome") == "created", first
        assert counter["local"] == 0, counter
        # 2回目は既存提案を確認して止まり、3回目で空転ガードが止める。
        second = auto.run_until_stop(manager, pid, actor="架空担当", max_ticks=5)
        assert counter["local"] == 0, counter
        assert counter["external"] == 0
        assert second["status"] in ("waiting_human", "stopped_guard"), second
        if second["status"] == "stopped_guard":
            assert auto.is_enabled(manager, pid) is False
        else:
            third = auto.run_until_stop(manager, pid, actor="架空担当", max_ticks=5)
            assert third["status"] == "stopped_guard", third
            assert auto.is_enabled(manager, pid) is False
        from app.goal_review import ReviewStore
        stored = ReviewStore(manager.memory.path).get(pid, "revision", sig) or {}
        assert not stored.get("candidate_id"), stored
        # 空転ガードの従来動作(同一停止の強制で止まる)は doc/table 側で確認する。
        manager2, pid2 = _make_manager(tmp_path, "doc", 3, tag="-idem2")
        _seed_missing_coverage(manager2, pid2)
        counter2 = _install_local_stub(manager2, pid2, {"local": 0, "external": 0})
        auto.set_enabled(manager2, pid2, True, actor="架空担当",
                         reason="架空の有効化理由")
        sig2 = _sig_of(manager2, pid2)
        from app.stop_classifier import classify_stop
        record = classify_stop({"binding_state": "invalid_reference",
                                "plan_signature": sig2})
        assert record["stop_class"] == "missing_binding"
        # A-1修正後: 実経路と同じ形 (preflight_status ok + 現行署名) で渡す。
        record["preflight_status"] = "ok"
        monkeypatch.setattr(auto, "_classify_current",
                            lambda _m, _p: ({}, dict(record)))
        auto.autopilot_tick(manager2, pid2, actor="架空担当")
        guarded = auto.autopilot_tick(manager2, pid2, actor="架空担当")
        assert guarded["status"] == "stopped_guard", guarded
        assert auto.is_enabled(manager2, pid2) is False
        assert counter2["external"] == 0
        return
    assert first["status"] == "advanced", first
    first_id = first["detail"]["candidate_id"]
    assert counter["local"] == 1
    # 実経路では候補生成後に分類が missing_decision(人の確認待ち)へ進む。
    # 2回目は新しい進展として数えず、LLM呼出も増えない。
    second = auto.run_until_stop(manager, pid, actor="架空担当", max_ticks=5)
    assert counter["local"] == 1, counter
    assert counter["external"] == 0
    assert second["status"] == "waiting_human", second
    from app.goal_review import ReviewStore
    stored = ReviewStore(manager.memory.path).get(pid, "revision", sig) or {}
    assert stored.get("candidate_id") == first_id
    # 同一結果の2連続は空転ガードに任せる: 同じ停止分類を強制すれば止まる。
    manager2, pid2 = _make_manager(tmp_path, domain, 3, tag="-idem2")
    _seed_missing_coverage(manager2, pid2)
    counter2 = _install_local_stub(manager2, pid2, {"local": 0, "external": 0})
    auto.set_enabled(manager2, pid2, True, actor="架空担当",
                     reason="架空の有効化理由")
    sig2 = _sig_of(manager2, pid2)
    from app.stop_classifier import classify_stop
    record = classify_stop({"binding_state": "invalid_reference",
                            "plan_signature": sig2})
    assert record["stop_class"] == "missing_binding"
    # A-1修正後: 実経路と同じ形 (preflight_status ok + 現行署名) で渡す。
    record["preflight_status"] = "ok"
    monkeypatch.setattr(auto, "_classify_current",
                        lambda _m, _p: ({}, dict(record)))
    auto.autopilot_tick(manager2, pid2, actor="架空担当")
    guarded = auto.autopilot_tick(manager2, pid2, actor="架空担当")
    assert guarded["status"] == "stopped_guard", guarded
    assert auto.is_enabled(manager2, pid2) is False
    assert counter2["external"] == 0


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("mode", ["missing_llm", "timeout", "bad_json", "invalid"])
def test_generation_failures_recorded_as_failed(tmp_path, domain, mode):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, domain, 3, tag=f"-fail-{mode}")
    _seed_missing_coverage(manager, pid)
    if mode == "missing_llm":
        counter = {"local": 0, "external": 0}
        manager.llm = None

        async def _no_local(*args):
            counter["local"] += 1
            raise AssertionError("local must not be called")

        async def _no_external(*args):
            counter["external"] += 1
            raise AssertionError("external must not be called")

        manager._local_complete = _no_local
        manager.plan_review_runner = _no_external
    else:
        counter = _install_local_stub(manager, pid, {"local": 0, "external": 0},
                                      mode=mode)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    out = auto.autopilot_tick(manager, pid, actor="架空担当")
    if domain == "external":
        # B-3: 機能不足は下書きまで自走し、確認待ちで止まる(失敗ではない)。
        assert out["status"] == "waiting_human", out
        assert out["stop_class"] == "missing_capability", out
        assert out["action_name"] == "capability_preflight", out
        assert out.get("capability_outcome") == "created", out
        assert out["reason_ja"], out
        assert out["enabled"] is True
        assert auto.is_enabled(manager, pid) is True
        assert out["external_delta"] == 0
        assert out["external_rounds_delta"] == 0
        assert counter["external"] == 0
        assert counter["local"] == 0, counter
        # 2回目は既存提案の確認。3回目で空転ガードが止める。
        again = auto.autopilot_tick(manager, pid, actor="架空担当")
        assert again["status"] in ("waiting_human", "stopped_guard"), again
        if again["status"] == "waiting_human":
            third = auto.autopilot_tick(manager, pid, actor="架空担当")
            assert third["status"] == "stopped_guard", third
        return
    # 失敗は成功扱いにしない。理由つきで記録し、自走を止める。
    assert out["status"] == "failed", out
    assert out["reason_ja"], out
    assert out["enabled"] is False
    assert auto.is_enabled(manager, pid) is False
    assert out["external_delta"] == 0
    assert out["external_rounds_delta"] == 0
    assert counter["external"] == 0
    if mode == "missing_llm":
        # 生成を試みていない (LLM呼出0)。
        assert counter["local"] == 0, counter
    # 連続する同一失敗で自走が止まる (2回目は disabled)。
    again = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert again["status"] == "disabled", again


@pytest.mark.parametrize("domain", DOMAINS)
def test_attempt_limit_stops_without_fourth_call(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    from app.goal_review import ReviewStore
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-limit")
    sig = _seed_missing_coverage(manager, pid)
    counter = _install_local_stub(manager, pid, {"local": 0, "external": 0},
                                  mode="invalid")
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    # 各ティックの分類を署名違いで回し、検査不合格の同一失敗を
    # 3回連続させる(空転ガードは署名違いでは発動しない)。
    seq = {"n": 0}

    def _fake_classify(_m, _p):
        seq["n"] += 1
        sig_current = _sig_of(manager, pid)
        # A-1修正後: 実経路と同じ形 (preflight ok + 現行署名) で渡す。
        # 空転ガード回避のため stop_code を交互に変えて3回失敗させる。
        codes = ["coverage:incomplete", "coverage:partial"]
        return {}, {"stop_class": "missing_coverage",
                    "stop_code": codes[seq["n"] % 2],
                    "plan_signature": sig_current,
                    "evidence_refs": {"plan_signature": sig_current},
                    "next_action": "propose_feedback",
                    "reason_ja": "架空の被覆不足",
                    "external_sends": 0,
                    "preflight_status": "ok"}

    monkeypatch.setattr(auto, "_classify_current", _fake_classify)
    for _ in range(3):
        auto.set_enabled(manager, pid, True, actor="架空担当",
                         reason="架空の再開理由")
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
        assert out["status"] == "failed", out
    assert counter["local"] == 3, counter
    stored = ReviewStore(manager.memory.path).get(pid, "revision", sig) or {}
    assert int(stored.get("attempts") or 0) == 3
    # 4回目は呼ばない。人の判断が要る停止として記録される。
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の再開理由")
    fourth = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert fourth["status"] == "waiting_human", fourth
    assert "3回" in fourth["reason_ja"], fourth
    assert counter["local"] == 3, counter
    assert counter["external"] == 0
    assert fourth["enabled"] is True


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("case", ["no_issues", "generating", "wrong_llm"])
def test_preconditions_do_not_proceed(tmp_path, domain, case):
    import app.autopilot as auto
    manager, pid = _make_manager(tmp_path, domain, 3, tag=f"-pre-{case}")
    counter = {"local": 0, "external": 0}
    if case == "no_issues":
        # 保存済みの指摘なし (plan レビュー未保存の既定状態)。
        _install_local_stub(manager, pid, counter, mode="no_llm")
    elif case == "generating":
        _seed_missing_coverage(manager, pid)
        _install_local_stub(manager, pid, counter, mode="no_llm")
        manager.planning_projects.add(pid)
    else:
        _seed_missing_coverage(manager, pid)
        manager.llm = object()

        async def _no_local(*args):
            counter["local"] += 1
            raise AssertionError("local must not be called")

        async def _no_external(*args):
            counter["external"] += 1
            raise AssertionError("external must not be called")

        manager._local_complete = _no_local
        manager.plan_review_runner = _no_external
    try:
        auto.set_enabled(manager, pid, True, actor="架空担当",
                         reason="架空の有効化理由")
        out = auto.autopilot_tick(manager, pid, actor="架空担当")
    finally:
        manager.planning_projects.discard(pid)
    assert counter["local"] == 0, counter
    assert counter["external"] == 0
    assert out["reason_ja"], out
    if domain == "external":
        # B-3: 機能不足が先に確定するため下書きまで自走し、確認待ちで止まる。
        # ただし生成中の明示がある場合は従来どおり実行中(missing_decision)で止まる。
        assert out["status"] == "waiting_human", out
        assert out["enabled"] is True
        if case == "generating":
            assert out["stop_class"] == "missing_decision", out
            assert out["action_name"] == "record_stop_class", out
        else:
            assert out["stop_class"] == "missing_capability", out
            assert out["action_name"] == "capability_preflight", out
            assert out.get("capability_outcome") == "created", out
        return
    if case == "wrong_llm":
        # LLM種別が不正: 生成を試みずティックは失敗。
        assert out["status"] == "failed", out
        assert out["enabled"] is False
    else:
        # 指摘なし・計画生成中: 進めず人の判断が要る停止。
        assert out["status"] == "waiting_human", out
        assert out["enabled"] is True


@pytest.mark.parametrize("domain", DOMAINS)
def test_generate_candidate_never_applies_or_sends(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    import app.plan_feedback as feedback
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-bound")
    _seed_missing_coverage(manager, pid)
    counter = _install_local_stub(manager, pid, {"local": 0, "external": 0})
    touched = {"apply": 0, "approve": 0, "send": 0, "rag": 0}

    def _no_apply(*args, **kwargs):
        touched["apply"] += 1
        raise AssertionError("apply must not be called")

    async def _no_send(*args, **kwargs):
        touched["send"] += 1
        raise AssertionError("external send must not be called")

    monkeypatch.setattr(feedback, "apply", _no_apply)
    monkeypatch.setattr(manager, "plan_review_runner", _no_send)
    version_before = _version_of(manager, pid)
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    out = auto.autopilot_tick(manager, pid, actor="架空担当")
    if domain == "external":
        # B-3: 機能不足は下書きまで自走するが、適用・送信・外部評価は触れない。
        assert out["status"] == "waiting_human", out
        assert out["stop_class"] == "missing_capability", out
        assert out["action_name"] == "capability_preflight", out
        assert out.get("capability_outcome") == "created", out
        assert out["enabled"] is True
        assert touched == {"apply": 0, "approve": 0, "send": 0, "rag": 0}
        assert counter["external"] == 0
        assert counter["local"] == 0, counter
        assert _version_of(manager, pid) == version_before
        # 許可リスト外の名前は引き続き拒否。
        with pytest.raises(ValueError):
            auto.run_named_action(manager, pid, "apply", actor="架空担当")
        with pytest.raises(ValueError):
            auto.run_named_action(manager, pid, "register_rag", actor="架空担当")
        return
    assert out["status"] == "advanced", out
    assert out["action_name"] == "generate_candidate"
    assert touched == {"apply": 0, "approve": 0, "send": 0, "rag": 0}
    assert counter["external"] == 0
    assert _version_of(manager, pid) == version_before
    # 許可リスト外の名前は引き続き拒否。
    with pytest.raises(ValueError):
        auto.run_named_action(manager, pid, "apply", actor="架空担当")
    with pytest.raises(ValueError):
        auto.run_named_action(manager, pid, "register_rag", actor="架空担当")


@pytest.mark.parametrize("domain", DOMAINS)
def test_api_tick_run_and_concurrent_ticks(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-api")
    sig = _seed_missing_coverage(manager, pid)
    counter = _install_local_stub(manager, pid, {"local": 0, "external": 0})
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    enabled = client.post(f"/api/projects/{pid}/autopilot/enable",
                          json={"actor": "架空担当", "reason": "架空理由",
                                "gate_override_reason": "C-4関門の未達を理由付きで無視します"})
    assert enabled.status_code == 200
    ticked = client.post(f"/api/projects/{pid}/autopilot/tick",
                         json={"actor": "架空担当"})
    assert ticked.status_code == 200, ticked.text
    body = ticked.json()
    if domain == "external":
        assert body["status"] == "waiting_human", body
        assert body["stop_class"] == "missing_capability", body
        assert body["action_name"] == "capability_preflight", body
        assert body.get("capability_outcome") == "created", body
        assert counter["local"] == 0, counter
        ran = client.post(f"/api/projects/{pid}/autopilot/run",
                          json={"actor": "架空担当", "max_ticks": 3})
        assert ran.status_code == 200
        assert ran.json()["count"] <= 3
        # 同時ティック(asyncio.gather)で片方が busy になり、生成は増えない。
        manager2, pid2 = _make_manager(tmp_path, domain, 3, tag="-conc2")
        sig2 = _seed_missing_coverage(manager2, pid2)
        counter2 = _install_local_stub(manager2, pid2, {"local": 0, "external": 0})
        auto.set_enabled(manager2, pid2, True, actor="架空担当",
                         reason="架空の有効化理由")

        async def _gather_ext():
            return await asyncio.gather(
                asyncio.to_thread(auto.autopilot_tick, manager2, pid2,
                                  actor="架空担当"),
                asyncio.to_thread(auto.autopilot_tick, manager2, pid2,
                                  actor="架空担当"))

        results_ext = asyncio.run(_gather_ext())
        statuses_ext = sorted(str(r.get("status")) for r in results_ext)
        assert "busy" in statuses_ext, statuses_ext
        assert "waiting_human" in statuses_ext, statuses_ext
        assert counter2["local"] == 0, counter2
        assert counter2["external"] == 0
        from app.plan_feedback import issues_for as _issues_for_ext
        assert len(_issues_for_ext(manager2, pid2, sig2)) >= 1
        from app.goal_review import ReviewStore as _StoreExt
        stored_ext2 = _StoreExt(manager2.memory.path).get(pid2, "revision", sig2) or {}
        assert not stored_ext2.get("candidate_id"), stored_ext2
        return
    assert body["status"] == "advanced", body
    assert body["action_name"] == "generate_candidate", body
    assert body["detail"]["candidate_id"], body
    assert counter["local"] == 1, counter
    ran = client.post(f"/api/projects/{pid}/autopilot/run",
                      json={"actor": "架空担当", "max_ticks": 3})
    assert ran.status_code == 200
    assert ran.json()["count"] <= 3
    # 同時ティック(asyncio.gather)で候補が二重に作られない。
    manager2, pid2 = _make_manager(tmp_path, domain, 3, tag="-conc2")
    sig2 = _seed_missing_coverage(manager2, pid2)
    counter2 = _install_local_stub(manager2, pid2, {"local": 0, "external": 0})
    auto.set_enabled(manager2, pid2, True, actor="架空担当",
                     reason="架空の有効化理由")

    async def _gather():
        return await asyncio.gather(
            asyncio.to_thread(auto.autopilot_tick, manager2, pid2,
                              actor="架空担当"),
            asyncio.to_thread(auto.autopilot_tick, manager2, pid2,
                              actor="架空担当"))

    results = asyncio.run(_gather())
    statuses = sorted(str(r.get("status")) for r in results)
    assert "busy" in statuses, statuses
    assert counter2["local"] == 1, counter2
    from app.goal_review import ReviewStore
    from app.plan_feedback import issues_for
    assert len(issues_for(manager2, pid2, sig2)) >= 1
    stored = ReviewStore(manager2.memory.path).get(pid2, "revision", sig2) or {}
    assert stored.get("candidate_id"), stored
    assert counter2["external"] == 0
    _ = sig


def test_missing_capability_and_coverage_combined(tmp_path, monkeypatch):
    """被覆不足と機能不足の併発: 機能不足で止まり also に被覆不足が残る。

    疑似PJ(架空・文書だけの工程+システム構築を含む達成条件+未被覆の指摘)。
    機能不足の解消(事前照合が不足を返さない状態)に戻れば generate_candidate が動く。
    """
    import app.autopilot as auto
    import app.capability_gap as gap
    from app.goal_review import ReviewStore
    from app.workflow_readiness import build_readiness, build_stop_class

    name = "s4b-疑似-mixed-combined"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = ["架空文書01の作成と検証",
                "架空システム構築02の実施と検証",
                "架空文書03の作成と検証"]
    memory.save_mission(pid, "架空目標-mixed", "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        tasks.append(compile_task(i, text, {
            "title": f"架空文書工程{i}",
            "scope": f"架空文書{i:02d}の成果物を作成し根拠を検証する。",
            "headings": ["目的", "実施内容"],
        }, []))
    memory.replace_plan(pid, name + " plan", tasks)
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name)),
    )
    sig = _seed_missing_coverage(manager, pid)
    counter = _install_local_stub(manager, pid, {"local": 0, "external": 0})
    ext_before = _external_counters(manager, pid)
    version_before = _version_of(manager, pid)
    record = build_stop_class(manager, pid, build_readiness(manager, pid))
    assert record["stop_class"] == "missing_capability", record
    assert any(a.get("stop_class") == "missing_coverage"
               for a in (record.get("also") or [])), record
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    out = auto.autopilot_tick(manager, pid, actor="架空担当")
    assert out["stop_class"] == "missing_capability", out
    assert out["status"] == "waiting_human", out
    assert out["action_name"] == "capability_preflight", out
    assert out.get("capability_outcome") == "created", out
    assert out["enabled"] is True
    assert counter["local"] == 0, counter
    assert counter["external"] == 0
    assert out["external_delta"] == 0
    assert out["external_rounds_delta"] == 0
    assert _version_of(manager, pid) == version_before
    stored = ReviewStore(manager.memory.path).get(pid, "revision", sig) or {}
    assert not stored.get("candidate_id"), stored
    ext_after = _external_counters(manager, pid)
    assert ext_after["sends"] == ext_before["sends"]
    assert ext_after["rounds"] == ext_before["rounds"]
    assert ext_after["budget"] == ext_before["budget"]

    # 機能不足の解消: 事前照合が不足を返さない状態に戻す(差し替え)。
    real_preflight = gap.capability_preflight

    def _no_gap(m, p, plan_signature=None, ai_assist=None):
        real = real_preflight(m, p, plan_signature=plan_signature,
                              ai_assist=ai_assist)
        return {"schema": real.get("schema"), "project_id": real.get("project_id"),
                "plan_signature": real.get("plan_signature"),
                "criteria": real.get("criteria"), "gaps": [],
                "missing_capability": [], "has_missing_capability": False,
                "gap_codes": [], "ai_assist_used": False, "external_sends": 0,
                "external_calls": 0, "read_only": True}

    monkeypatch.setattr(gap, "capability_preflight", _no_gap)
    try:
        record2 = build_stop_class(manager, pid, build_readiness(manager, pid))
        assert record2["stop_class"] == "missing_coverage", record2
        out2 = auto.autopilot_tick(manager, pid, actor="架空担当")
    finally:
        monkeypatch.undo()
    assert out2["stop_class"] == "missing_coverage", out2
    assert out2["status"] == "advanced", out2
    assert out2["action_name"] == "generate_candidate", out2
    assert (out2.get("detail") or {}).get("candidate_id"), out2
    assert counter["local"] == 1, counter
    assert counter["external"] == 0


def test_registry_completeness_s4b(tmp_path):
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    # 新しいテーブルを作っていない (棚卸し対象の追加なし)。
    assert "autopilot" not in str(audit.get("unregistered") or "")


def test_generic_no_specific_project_words_s4b():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / "tests/test_s4b_generate_candidate.py").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines()
             if "販売" not in line and "1659" not in line
             and "word in" not in line and "for word" not in line]
    body = "\n".join(lines)
    assert "販売" not in body
    assert "1659" not in body
    name_lines = [line for line in text.splitlines()
                  if "s4b-" not in line and "word" not in line.lower()]
    assert "s4b-" not in "\n".join(name_lines).replace("s4b_generate", "")
    impl = (root / "app/autopilot.py").read_text(encoding="utf-8")
    assert "innerHTML" not in impl
    assert "http://" not in impl and "https://" not in impl
    assert "httpx" not in impl and "requests.post" not in impl
    # 4-Bの新規経路では例外を握りつぶさない(成功扱いにしない)。
    assert "_CandidateNotAvailable" in impl
    assert "_NeedsHumanDecision" in impl


def test_external_counter_untouched_by_local_calls(tmp_path):
    import app.autopilot as auto
    from app.stop_classifier import _EXTERNAL_SENDS_COUNTER as counter
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-cnt")
    before = int(counter.get("calls") or 0)
    with auto._count_external_calls(manager):
        pass
    assert int(counter.get("calls") or 0) == before
    db = sqlite3.connect(f"file:{manager.memory.path.as_posix()}?mode=ro", uri=True)
    try:
        db.execute("SELECT COUNT(*) FROM project_missions").fetchone()
    finally:
        db.close()


def test_human_wait_does_not_hide_external_increment(tmp_path, monkeypatch):
    import app.autopilot as auto
    from app.stop_classifier import _EXTERNAL_SENDS_COUNTER

    manager, project_id = _make_manager(tmp_path, "doc", 2, tag="-external-human")
    auto.set_enabled(manager, project_id, True, actor="架空担当", reason="架空の有効化理由")
    original = int(_EXTERNAL_SENDS_COUNTER.get("calls") or 0)
    monkeypatch.setitem(_EXTERNAL_SENDS_COUNTER, "calls", original)
    # A-1修正後: 実経路と同じ形 (preflight_status ok + 現行署名) で渡す。
    sig_current = _sig_of(manager, project_id)
    monkeypatch.setattr(auto, "_classify_current", lambda *_: ({}, {
        "stop_class": "missing_coverage", "stop_code": "coverage:incomplete",
        "plan_signature": sig_current,
        "evidence_refs": {"plan_signature": sig_current},
        "next_action": "propose_feedback", "reason_ja": "架空の被覆不足",
        "preflight_status": "ok"}))

    def unexpected_external_then_wait(*_args, **_kwargs):
        _EXTERNAL_SENDS_COUNTER["calls"] += 1
        raise auto._NeedsHumanDecision("人の判断が必要です")

    monkeypatch.setattr(auto, "run_named_action", unexpected_external_then_wait)
    try:
        result = auto.autopilot_tick(manager, project_id, actor="架空担当")
        assert result["status"] == "failed", result
        assert result["external_delta"] > 0
        assert auto.is_enabled(manager, project_id) is False
    finally:
        _EXTERNAL_SENDS_COUNTER["calls"] = original


def test_autopilot_rejects_unreadable_delete_state(tmp_path):
    import app.autopilot as auto
    from app.project_delete import delete_db_path

    manager, project_id = _make_manager(tmp_path, "doc", 2,
                                        tag="-deletedb-unreadable")
    delete_db_path(manager.memory.path).write_bytes(b"not a sqlite database")
    with pytest.raises(ValueError, match="削除状態"):
        auto.set_enabled(manager, project_id, True, actor="架空担当",
                         reason="架空の有効化理由")
