"""P0-A前半(項目1〜3)の受入試験。疑似PJ・実関数・外部通信なし。特定案件の語彙なし。"""
import asyncio
import json

from app.goal_review import (
    build_final_packet,
    build_public_purpose_summary,
    check_public_consistency,
    is_generic_public_summary,
    is_valid_packet_hash,
    public_structure,
    verify_packet_hash,
)
from app.structured_planning import compile_plan, compile_task


FAKE_CONTACT = "kaku-test-001@example.invalid"
FAKE_PHONE = "090-0000-0001"
FAKE_SECRET = "sk-fake0123456789abcdef"


def _proposal(title, scope, headings, extra=None):
    row = {"title": title, "scope": scope, "headings": headings, "depends_on": []}
    if extra:
        row.update(extra)
    return row


def _plan():
    t1 = compile_task(1, "架空手順Aを定義する", {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, "架空手順Bを報告する", {"title": "架空資料B", "scope": "架空Bの整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    return compile_plan(
        ["架空手順Aを定義する", "架空手順Bを報告する"], [t1, t2],
        goal="疑似目標")


def _manager(tmp_path, providers=("chatgpt",)):
    from types import SimpleNamespace
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p0a-pseudo")["id"]
    mem.save_mission(
        pid,
        "疑似目標\n1. 架空手順Aを定義する\n2. 架空手順Bを報告する",
        "疑似目標\n1. 架空手順Aを定義する\n2. 架空手順Bを報告する",
        "",
        True,
        list(providers),
    )
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = SimpleNamespace(
        memory=mem, workspace=ws, planning_projects=set(), llm=None,
        workers={}, _sync_memos=lambda pid: None,
        provider_statuses=lambda: [{"id": p, "configured": True} for p in providers],
        plan_review_runner=None,
                  _outbound_guard=lambda pid, purpose, text, providers=(): {"ok": True, "codes": []})
    return mgr, mem, pid


def _prepare(mgr, mem, pid):
    plan = _plan()
    mem.replace_plan(pid, "p0a plan", plan["tasks"])
    from app.goal_contract import activate
    activate(mgr, pid)
    return mem.get_mission(pid)


def _explicit():
    return ("利用者向けに架空手順Aの定義書1件と架空手順Bの報告書1件を作る計画で、"
            "利用者向け完成成果物は2件、内部記録・検証報告は2件。達成条件SC01・SC02に対応する。")


def test_public_purpose_uses_only_public_info_and_counts_match():
    snapshot = {"tasks": _plan()["tasks"]}
    structure = public_structure(snapshot)
    summary = build_public_purpose_summary(snapshot, _explicit())
    assert summary["complete"] is True
    assert summary["deliverable_count"] == 2
    assert summary["criterion_ids"] == ["SC01", "SC02"]
    assert summary["criterion_count"] == 2
    blob = json.dumps(summary, ensure_ascii=False)
    assert FAKE_CONTACT not in blob and FAKE_PHONE not in blob and FAKE_SECRET not in blob
    check = check_public_consistency(snapshot, structure, _explicit())
    assert check["passed"] is True
    assert check["status"] == "ok"
    # P0-0: スロット数と成果物数が一致する。
    assert check["slot_ok"] is True


def _with_outputs(task, outputs, criterion=None):
    row = dict(task)
    contract = json.loads(row["acceptance_criteria"])
    contract["outputs"] = outputs
    if criterion is not None:
        contract["criterion"] = criterion
    row["acceptance_criteria"] = json.dumps(contract, ensure_ascii=False)
    return row


def test_slot_check_allows_extra_outputs_without_explicit_count():
    tasks = _plan()["tasks"]
    target = next(i for i, task in enumerate(tasks) if task["task_key"] == "SC01")
    contract = json.loads(tasks[target]["acceptance_criteria"])
    extra = dict(contract["outputs"][0], path="result/sc01_extra.md")
    tasks[target] = _with_outputs(tasks[target], [contract["outputs"][0], extra],
                                  "架空手順Aの資料を作る")
    snapshot = {"tasks": tasks}
    check = check_public_consistency(snapshot, public_structure(snapshot), _explicit())
    assert check["passed"] is True
    detail = next(row for row in check["slot_detail"] if row.get("criterion_ids") == ["SC01"])
    assert detail["required"] == 1 and detail["actual"] == 2
    assert detail["count_is_explicit"] is False


def test_slot_check_rejects_explicit_count_over_and_under():
    for output_count in (1, 3):
        tasks = _plan()["tasks"]
        target = next(i for i, task in enumerate(tasks) if task["task_key"] == "SC01")
        contract = json.loads(tasks[target]["acceptance_criteria"])
        base = contract["outputs"][0]
        outputs = [dict(base, path=("result/sc01.md" if i == 0 else f"result/sc01_{i + 1}.md"))
                   for i in range(output_count)]
        tasks[target] = _with_outputs(tasks[target], outputs, "成果物を合計2ファイル作る")
        snapshot = {"tasks": tasks}
        check = check_public_consistency(snapshot, public_structure(snapshot), _explicit())
        assert check["passed"] is False
        assert check["slot_ok"] is False
        assert "成果物スロット数と成果物数が一致しません" in check["reasons"]


def test_private_project_stops_before_send_without_guessing():
    snapshot = {"tasks": _plan()["tasks"]}
    structure = public_structure(snapshot)
    explicit = ("私的な原本の連絡先 " + FAKE_CONTACT + " と秘密 " + FAKE_SECRET
                + " と内部パス result/sc01.md を含む説明")
    summary = build_public_purpose_summary(snapshot, explicit)
    assert summary["complete"] is False
    assert summary["secret_suspected"]
    check = check_public_consistency(snapshot, structure, explicit)
    # 秘密混入の疑いは目的要約側で不完全になる(推測で補わない)。
    assert summary["missing"] or summary["secret_suspected"]
    blob = json.dumps(structure, ensure_ascii=False)
    assert FAKE_CONTACT not in blob and FAKE_SECRET not in blob and FAKE_PHONE not in blob


def test_missing_public_info_is_unverifiable_not_pass():
    snapshot = {"tasks": _plan()["tasks"]}
    structure = public_structure(snapshot)
    check = check_public_consistency(snapshot, structure, "")
    assert check["passed"] is False
    assert check["status"] == "packet_incomplete"
    assert check["generic_summary"] is True
    assert is_generic_public_summary("") is True
    assert is_generic_public_summary(structure) is True


def test_preview_start_hash_binding_and_byte_identity(tmp_path):
    import app.plan_review_loop as loop

    mgr, mem, pid = _manager(tmp_path)
    _prepare(mgr, mem, pid)

    async def fake_runner(_prompt, providers):
        return [{"id": "chatgpt", "ok": True,
                 "review": json.dumps({"verdict": "pass", "issues": []})}]

    mgr.plan_review_runner = fake_runner
    view = loop.preview_run(mgr, pid, ["chatgpt"], explicit_summary=_explicit())
    if view["can_start"] is not True:
        raise AssertionError("preview blockers: " + " / ".join(view.get("start_blockers") or []))
    assert view["can_start"] is True
    assert is_valid_packet_hash(view["packet_hash"]) is True
    # previewのバイト内容と実送信のバイト内容が一致する。
    packet, packet_hash = build_final_packet(_explicit(), loop._current(mgr, pid)[1])
    assert packet_hash == view["packet_hash"]
    assert packet == view["packet_preview"]

    async def _run():
        called = []
        orig = mgr.plan_review_runner

        async def counting_runner(prompt, providers):
            called.append(list(providers))
            return await orig(prompt, providers)

        mgr.plan_review_runner = counting_runner
        started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], _explicit(),
                                 "idem-p0a-ok", expected_packet_hash=view["packet_hash"])
        assert started["packet_hash"] == view["packet_hash"]
        out = await loop.run_loop(mgr, pid, started["id"])
        assert out["state"] == "awaiting_human"
        assert called == [["chatgpt"]]
        return out

    asyncio.new_event_loop().run_until_complete(_run())

    # 編集後の古いプレビューは409。
    with __import__("pytest").raises(ValueError, match="409"):
        loop.start_run(mgr, pid, "human-a", ["chatgpt"], _explicit() + "追記",
                       "idem-p0a-stale", expected_packet_hash=view["packet_hash"])
    # 空ハッシュ・不正形式は不一致(409)。
    for bad in ("", "zzz", "0" * 63, "0" * 65, None):
        checked = verify_packet_hash(bad, view["packet_preview"])
        assert checked["ok"] is False
    with __import__("pytest").raises(ValueError, match="409"):
        loop.start_run(mgr, pid, "human-a", ["chatgpt"], _explicit(),
                       "idem-p0a-empty", expected_packet_hash="")
    # 版変更(計画変更)は開始前の再プレビューで止まる(署名の変化を検出)。
    mission = mem.get_mission(pid)
    old_signature = view["plan_signature"]
    mem.replace_plan(pid, "changed", mission["tasks"])
    changed_view = loop.preview_run(mgr, pid, ["chatgpt"], explicit_summary=_explicit())
    assert changed_view["plan_signature"] != old_signature


def test_generic_only_stops_before_external_call(tmp_path):
    import app.plan_review_loop as loop

    mgr, mem, pid = _manager(tmp_path)
    _prepare(mgr, mem, pid)
    calls = []

    async def fake_runner(_prompt, providers):
        calls.append(1)
        return []

    mgr.plan_review_runner = fake_runner
    view = loop.preview_run(mgr, pid, ["chatgpt"], explicit_summary="")
    assert view["can_start"] is False
    assert "packet_incomplete" in " / ".join(view["start_blockers"])
    with __import__("pytest").raises(ValueError, match="packet_incomplete"):
        loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-p0a-generic",
                       expected_packet_hash=view["packet_hash"])
    assert calls == []


def test_packet_change_after_start_stops_before_external_call(tmp_path, monkeypatch):
    """A changed packet after approval must not reach the provider."""
    import app.goal_review as review
    import app.plan_review_loop as loop

    mgr, mem, pid = _manager(tmp_path)
    _prepare(mgr, mem, pid)
    calls = []

    async def fake_runner(_prompt, providers):
        calls.append(list(providers))
        return []

    mgr.plan_review_runner = fake_runner
    preview = loop.preview_run(mgr, pid, ["chatgpt"], explicit_summary=_explicit())
    assert preview["can_start"] is True
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], _explicit(),
                             "idem-p0a-mutated", expected_packet_hash=preview["packet_hash"])
    original = review.public_structure

    def changed_structure(snapshot):
        result = original(snapshot)
        result["packet_revision_marker"] = "changed-after-preview"
        return result

    monkeypatch.setattr(review, "public_structure", changed_structure)
    result = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert result["state"] == "stopped"
    assert "packet" in result["stop_reason"]
    assert calls == []