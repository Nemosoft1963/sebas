"""AG2: 実行前の計画構造を公開範囲で送る試験。実外部AIなし。"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from app.goal_review import ReviewStore
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.plan_review_structure import PREAMBLE, build_structure_text
import app.planning_packet as packet
import app.planning_settings as ps
import app.independent_review as jobs
from app.plan_gate_decision import decide
import app.plan_gate_decision as gate_mod
from app.structured_planning import compile_task

GEN = "fake-gen"
REV = "fake-rev"
KNOWN = {GEN, REV}


def _manager(memory):
    return ProjectOrchestrator(
        memory, None, lambda project: ("", []), None, lambda: [])


def _secret_task(index, leak=False):
    criterion = f"架空手順{index}を定義する"
    if leak:
        proposal = {
            "title": f"山田太郎と顧客Xの工程{index}",
            "scope": f"架空{index}の整理。連絡は090-1234-5678とtest@example.com",
            "headings": ["目的", "実施内容"],
            "depends_on": [],
        }
    else:
        proposal = {
            "title": f"架空資料{index}",
            "scope": f"架空{index}の整理",
            "headings": ["目的", "実施内容"],
            "depends_on": [],
        }
    task = compile_task(index, criterion, proposal, [])
    if leak:
        contract = json.loads(task["acceptance_criteria"])
        contract["outputs"] = [{
            "path": "C:/secret/a.docx",
            "required_headings": ["目的", "x", "根拠と未確認事項", "実施状態と次の行動"],
            "minimum_characters": 600,
            "artifact_category": "document",
        }]
        task["acceptance_criteria"] = json.dumps(contract, ensure_ascii=False)
    contract = json.loads(task["acceptance_criteria"])
    contract["requirement_matrix"] = [{
        "item_id": "SC01-R1", "method": "count",
        "required": True, "artifact_path": "result/sc01.md",
    }]
    task["acceptance_criteria"] = json.dumps(contract, ensure_ascii=False)
    return task


def _mission3(leak=False):
    t1 = _secret_task(1, leak=leak)
    t2 = _secret_task(2, leak=leak)
    tasks = [t1, t2]
    order = ["SC01", "SC02"]
    for idx, task in enumerate(tasks):
        contract = json.loads(task["acceptance_criteria"])
        assert contract["criterion_ids"] == [order[idx]]
        task["task_key"] = order[idx]
        contract["criterion_ids"] = [order[idx]]
        task["acceptance_criteria"] = json.dumps(contract, ensure_ascii=False)
    tasks[1]["depends_on"] = ["SC01"]
    c2 = json.loads(tasks[1]["acceptance_criteria"])
    c2["approval_required"] = True
    tasks[1]["acceptance_criteria"] = json.dumps(c2, ensure_ascii=False)
    return {"goal": "疑似目標", "success_criteria": "条件", "tasks": tasks}


# 1: 構造テキスト
def test_structure_preamble_and_rows():
    text = build_structure_text(_mission3())
    assert text.startswith(PREAMBLE)
    assert "入力の事実が揃っているかは保証しません" in PREAMBLE
    assert "工程1:" in text and "工程2:" in text
    assert "種別=実行" in text
    assert "依存=なし" in text.splitlines()[1]
    assert "依存=1" in text.splitlines()[2]
    assert "出力=文書×1" in text
    assert "先行工程=なし" in text or "先行工程=あり" in text
    assert "充足" not in text and "未充足" not in text
    assert "検証=count" in text
    assert "承認境界=なし" in text or "承認境界=人間承認が必要" in text


def test_structure_no_leak():
    mission = _mission3(leak=True)
    task = mission["tasks"][0]
    task["title"] = "山田太郎と顧客Xの工程"
    task["description"] = "連絡は090-1234-5678とtest@example.com。原本名:原本A.docx"
    c = json.loads(task["acceptance_criteria"])
    c["outputs"] = [{
        "path": "C:/secret/a.docx",
        "required_headings": ["目的", "x", "根拠と未確認事項", "実施状態と次の行動"],
        "minimum_characters": 600, "artifact_category": "document",
    }]
    task["acceptance_criteria"] = json.dumps(c, ensure_ascii=False)
    text = build_structure_text(mission)
    for secret in ("山田太郎", "顧客X", "090-1234", "test@example.com",
                   "C:/secret", "/data/", "原本A", "連絡は"):
        assert secret not in text
    # 番号・連番の形式だけ残る(題名は出ない)
    assert text.splitlines()[1].startswith("工程1: 種別=")


def test_structure_prior_ordering():
    t1 = _secret_task(1)
    t2 = _secret_task(2)
    t1["task_key"] = "SC01"
    t2["task_key"] = "SC02"
    c1 = json.loads(t1["acceptance_criteria"])
    c1["criterion_ids"] = ["SC01"]
    t1["acceptance_criteria"] = json.dumps(c1, ensure_ascii=False)
    c2 = json.loads(t2["acceptance_criteria"])
    c2["criterion_ids"] = ["SC02"]
    t2["acceptance_criteria"] = json.dumps(c2, ensure_ascii=False)
    # 正しい並び
    t2["depends_on"] = ["SC01"]
    ok = build_structure_text({"goal": "g", "tasks": [t1, t2]})
    assert "先行工程=なし" in ok.splitlines()[1]
    assert "先行工程=あり" in ok.splitlines()[2]
    # 後ろへの依存は順序不正
    t1["depends_on"] = ["SC02"]
    bad = build_structure_text({"goal": "g", "tasks": [t1, t2]})
    assert "先行工程=順序不正" in bad.splitlines()[1]
    # 存在しない依存・自分自身も順序不正
    t1["depends_on"] = ["SC99"]
    assert "先行工程=順序不正" in build_structure_text(
        {"goal": "g", "tasks": [t1, t2]}).splitlines()[1]
    t1["depends_on"] = ["SC01"]
    assert "先行工程=順序不正" in build_structure_text(
        {"goal": "g", "tasks": [t1, t2]}).splitlines()[1]


def test_structure_unknown_vocab_and_empty():
    t1 = _secret_task(1)
    c = json.loads(t1["acceptance_criteria"])
    c["role"] = "mystery_role"
    c["outputs"] = [{"path": "ftp://x/y.zzz", "required_headings": ["a", "b"],
                     "minimum_characters": 600, "artifact_category": "mystery"}]
    c["requirement_matrix"] = [{"item_id": "SC01-R1", "method": "mystery_method",
                                "required": True, "artifact_path": "x"}]
    c["approval_required"] = "yes"
    t1["acceptance_criteria"] = json.dumps(c, ensure_ascii=False)
    t1["task_key"] = "SC01"
    text = build_structure_text({"goal": "g", "tasks": [t1]})
    assert "種別=不明" in text
    assert "出力=不明×1" in text
    assert "検証=不明" in text
    assert "承認境界=不明" in text
    assert build_structure_text({"goal": "g", "tasks": []}).endswith("工程情報なし")
    # 非構造化は工程情報なし
    assert build_structure_text({"goal": "g", "tasks": [
        {"task_key": "t1", "depends_on": []}]}).endswith("工程情報なし")


def test_structure_deterministic_and_fail_closed():
    mission = _mission3()
    assert build_structure_text(copy.deepcopy(mission)) == build_structure_text(mission)
    assert build_structure_text("not-a-dict") == ""
    assert build_structure_text({"goal": "g"}) == ""
    assert build_structure_text({"goal": "g", "tasks": {"SC01": {}}}) == ""
    assert build_structure_text({"goal": "g", "tasks": "broken"}) == ""


# 2: packet
class FakeStore:
    def __init__(self):
        self._d = {}

    def get(self, pid, kind, signature):
        v = self._d.get((pid, kind, signature))
        return copy.deepcopy(v) if v is not None else None

    def put(self, pid, kind, signature, payload):
        self._d[(pid, kind, signature)] = copy.deepcopy(payload)

    def list(self, pid, kind):
        return [(sig, copy.deepcopy(payload)) for (p, k, sig), payload in self._d.items()
                if p == pid and k == kind]


def test_packet_structure_only_plan_review():
    store = FakeStore()
    structure = build_structure_text(_mission3())
    items = {"goal": "公開目標", "success_criteria": "公開条件"}
    base = packet.build_packet(store, "p1", purpose="plan_review", provider="fake-rev-b",
                               public_items=dict(items), plan_signature="s1",
                               blocked_terms=[], source_texts=[], withheld_fields=[])
    with_struct = packet.build_packet(store, "p1", purpose="plan_review", provider="fake-rev-b",
                                      public_items=dict(items), plan_signature="s1",
                                      blocked_terms=[], source_texts=[], withheld_fields=[],
                                      structure_text=structure)
    assert "【計画の構造(実行前)】" not in base["text"]
    assert with_struct["text"].startswith(base["text"])
    assert "【計画の構造(実行前)】" in with_struct["text"]
    assert structure in with_struct["text"]
    assert with_struct["has_structure"] is True
    assert base["has_structure"] is False
    assert with_struct["included_fields"] == ["goal", "success_criteria"]
    assert base["included_fields"] == with_struct["included_fields"]
    assert set(PUBLIC_FIELDS_LIST := list(packet.PUBLIC_FIELDS)) == {"goal", "success_criteria", "constraints"}
    gen = packet.build_packet(store, "p1", purpose="plan_generation", provider="fake-gen-a",
                              public_items=dict(items), plan_signature="s1",
                              blocked_terms=[], source_texts=[], withheld_fields=[],
                              structure_text=structure)
    assert "【計画の構造(実行前)】" not in gen["text"]
    assert "has_structure" in gen and gen["has_structure"] is False


def test_packet_no_structure_is_byte_identical():
    store = FakeStore()
    items = {"goal": "公開目標", "success_criteria": "公開条件"}
    p1 = packet.build_packet(store, "p1", purpose="plan_review", provider="fake-rev-b",
                             public_items=dict(items), plan_signature="s1",
                             blocked_terms=[], source_texts=[], withheld_fields=[])
    p2 = packet.build_packet(store, "p2", purpose="plan_review", provider="fake-rev-b",
                             public_items=dict(items), plan_signature="s1",
                             blocked_terms=[], source_texts=[], withheld_fields=[],
                             structure_text=None)
    p3 = packet.build_packet(store, "p3", purpose="plan_review", provider="fake-rev-b",
                             public_items=dict(items), plan_signature="s1",
                             blocked_terms=[], source_texts=[], withheld_fields=[],
                             structure_text="   ")
    for other in (p2, p3):
        assert other["text"] == p1["text"]
        assert other["sha256"] == p1["sha256"]
        assert other["has_structure"] is False


def test_packet_structure_approve_verify_tamper():
    store = FakeStore()
    ps.update_settings(store, "pj", {"method": "external_guarded",
                                     "generator_provider": GEN,
                                     "reviewer_provider": REV}, {GEN, REV})
    structure = build_structure_text(_mission3())
    items = {"goal": "公開目標", "success_criteria": "公開条件"}
    p = packet.build_packet(store, "pj", purpose="plan_review", provider=REV,
                            public_items=dict(items), plan_signature="sig-1",
                            blocked_terms=[], source_texts=[], withheld_fields=[],
                            structure_text=structure)
    approved = packet.approve_packet(store, "pj", p["id"], "a",
                                     current_plan_signature="sig-1",
                                     blocked_terms=[], source_texts=[])
    assert approved["sha256"] == hashlib.sha256(approved["text"].encode("utf-8")).hexdigest()
    verified = packet.verify_for_send(store, "pj", p["id"], current_plan_signature="sig-1")
    assert verified["allowed"] is True
    assert verified["text"] == approved["text"]
    tampered = packet.get_packet(store, "pj", p["id"])
    tampered["text"] = tampered["text"] + "x"
    store.put("pj", "plan_packet", tampered["id"], tampered)
    denied = packet.verify_for_send(store, "pj", p["id"], current_plan_signature="sig-1")
    assert denied == {"allowed": False, "reason": "packet_tampered", "text": ""}


def test_packet_structure_blocked_terms():
    store = FakeStore()
    bad = "公開目標 test@example.com"
    p = packet.build_packet(store, "pj", purpose="plan_review", provider=REV,
                            public_items={"goal": "公開の目標です"},
                            plan_signature="s1", blocked_terms=[],
                            source_texts=[], withheld_fields=[],
                            structure_text=bad)
    assert p["status"] == "blocked"
    assert p["text"] == ""
    assert p["has_structure"] is False


def test_packet_included_fields_unchanged():
    assert tuple(packet.PUBLIC_FIELDS) == ("goal", "success_criteria", "constraints")
    store = FakeStore()
    structure = build_structure_text(_mission3())
    p = packet.build_packet(store, "pj", purpose="plan_review", provider=REV,
                            public_items={"goal": "g", "success_criteria": "s"},
                            plan_signature="s1", blocked_terms=[],
                            source_texts=[], withheld_fields=["constraints"],
                            structure_text=structure)
    assert p["included_fields"] == ["goal", "success_criteria"]
    assert p["withheld_fields"] == ["constraints"]


# 3: サーバー
def _web_client(tmp_path):
    from app.memory.short_term import ShortTermMemory
    import app.web as web
    from fastapi.testclient import TestClient
    mem_path = tmp_path / "ag2-web.db"
    memory = ShortTermMemory(mem_path)
    pid = memory.create_project("AG2")["id"]
    memory.save_mission(pid, "目標G", "条件S", "制約C", False, [])
    mission = memory.get_mission(pid)
    tasks = []
    for idx, base_task in enumerate(_mission3()["tasks"], 1):
        row = dict(base_task)
        row["id"] = f"t{idx}"
        tasks.append(row)
    memory.replace_plan(pid, "計画", tasks)
    client = TestClient(web.app)
    web.memory = memory
    monkeypatch_provider = None
    return client, memory, pid, web, monkeypatch_provider


def test_server_plan_review_adds_structure_and_ignores_client(tmp_path, monkeypatch):
    from app.memory.short_term import ShortTermMemory
    import app.web as web
    from fastapi.testclient import TestClient
    mem_path = tmp_path / "ag2-srv.db"
    memory = ShortTermMemory(mem_path)
    pid = memory.create_project("AG2S")["id"]
    memory.save_mission(pid, "目標G", "条件S", "制約C", False, [])
    tasks = []
    for idx, base_task in enumerate(_mission3()["tasks"], 1):
        row = dict(base_task)
        row["id"] = f"t{idx}"
        tasks.append(row)
    memory.replace_plan(pid, "計画", tasks)
    client = TestClient(web.app)
    monkeypatch.setattr(web, "memory", memory)
    res = client.post(f"/api/projects/{pid}/plan-packets/preview",
                      json={"purpose": "plan_review", "provider": "fake-gen-a",
                            "public_items": {"goal": "公開目標"},
                            "withheld_fields": [],
                            "structure_text": "偽の構造"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert "偽の構造" not in body["text"]
    assert "【計画の構造(実行前)】" in body["text"]
    assert PREAMBLE in body["text"]
    assert "山田太郎" not in body["text"]
    assert "has_structure" not in body
    # 画面のJSが見る項目名は変わらない(画面のJSを変更しない)
    res2 = client.post(f"/api/projects/{pid}/plan-packets/preview",
                       json={"purpose": "plan_generation", "provider": "fake-gen-a",
                             "public_items": {"goal": "公開目標"},
                             "withheld_fields": []})
    assert res2.status_code == 200
    assert "【計画の構造(実行前)】" not in res2.json()["text"]


# 4: 評価仕様
def test_review_spec_version_and_prompt():
    assert jobs.REVIEW_SPEC_VERSION == "plan-review-v2"
    assert "実行前の計画評価" in jobs.REVIEW_SYSTEM_PROMPT
    assert "工程・依存関係・出力契約・検証方法" in jobs.REVIEW_SYSTEM_PROMPT
    res = jobs.validate_review_response('{"verdict":"junk","findings":[]}')
    assert res["ok"] is False
    res2 = jobs.validate_review_response('{"verdict":"pass","findings":[]}')
    assert res2["ok"] is True


def test_prepare_job_stores_review_spec(tmp_path):
    store = ReviewStore(tmp_path / "ag2-job.db")
    pid = "pj-job"
    ps.update_settings(store, pid, {"method": "external_guarded",
                                    "generator_provider": GEN,
                                    "reviewer_provider": REV}, KNOWN)
    items = {"goal": "公開の目標です", "success_criteria": "公開の条件です"}
    p = packet.build_packet(store, pid, purpose="plan_review", provider=REV,
                            public_items=dict(items), plan_signature="sig-x",
                            blocked_terms=[], source_texts=[], withheld_fields=[])
    p = packet.approve_packet(store, pid, p["id"], "t",
                              current_plan_signature="sig-x",
                              blocked_terms=[], source_texts=[])
    key = jobs.idempotency_key_of(pid, "sig-x", p["sha256"], REV, jobs.PURPOSE)
    prep = jobs.prepare_job(store, pid, packet_id=p["id"], provider=REV,
                            idempotency_key=key, current_plan_signature="sig-x",
                            enabled=True, approver="t", blocked_terms=[],
                            source_texts=[])
    assert prep["job"]["review_spec"] == "plan-review-v2"
    assert jobs.public_view(prep["job"])["review_spec"] == "plan-review-v2"


# 5: 新ゲート
def _gate_ready(tmp_path, tag):
    memory = ShortTermMemory(tmp_path / f"{tag}.db")
    pid = memory.create_project(tag)["id"]
    memory.save_mission(pid, "目標G", "条件S", "制約C", False, [])
    memory.replace_plan(pid, "計画", [{
        "task_key": "t1", "depends_on": [], "title": "作業1",
        "description": "作業する", "acceptance_criteria": "完了"}])
    memory.set_mission_status(pid, "ready", "承認済み", "approved")
    store = ReviewStore(memory.path)
    ps.update_settings(store, pid, {"method": "external_guarded",
                                    "generator_provider": GEN,
                                    "reviewer_provider": REV}, KNOWN)
    manager = ProjectOrchestrator(memory, None, lambda project: ("", []), None, lambda: [])
    from app.structured_planning import compile_task, compile_plan
    criteria = ["架空手順Aを定義する", "架空手順Bを報告する"]
    memory.save_mission(pid, "疑似目標", "\n".join(
        f"{i + 1}. {c}" for i, c in enumerate(criteria)), "", False, [])
    t1 = compile_task(1, criteria[0], {
        "title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, criteria[1], {
        "title": "架空資料B", "scope": "架空Bの整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    compiled = compile_plan(criteria, [t1, t2], goal=memory.get_mission(pid)["goal"])
    memory.replace_plan(pid, "valid plan", compiled["tasks"])
    memory.set_mission_status(pid, "ready", "承認済み", "approved")
    return memory, pid, manager, store


def _put_job(store, pid, jid, sig, status, provider=REV, spec=jobs.REVIEW_SPEC_VERSION):
    import time as _time
    job = {"id": jid, "pid": pid, "plan_signature": sig,
           "packet_sha256": "ab" * 32, "provider": provider, "status": status,
           "model": "Rev-Model", "reviewed_at": _time.time(),
           "verdict": "", "findings": [], "codes": [],
           "review_spec": spec}
    store.put(pid, jobs.KIND, jid, job)
    return job


def test_v2_requires_current_review_spec(tmp_path, monkeypatch):
    import os
    memory, pid, manager, store = _gate_ready(tmp_path, "ag2-g1")
    sig = packet.plan_signature_of(memory.get_mission(pid))
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put_job(store, pid, "j-old", sig, "review_passed", spec="plan-review-v1")
    out = decide(manager, pid)
    assert out["mode"] == "v2"
    assert out["verdict"] == "unknown"
    assert out["reason"] == "review_spec_outdated"
    assert out["next_actions"] == ["run_independent_review"]
    nospec = dict(store.get(pid, jobs.KIND, "j-old"))
    del nospec["review_spec"]
    store.put(pid, jobs.KIND, "j-old", nospec)
    out2 = decide(manager, pid)
    assert out2["verdict"] == "unknown"
    assert out2["reason"] == "review_spec_outdated"


def test_v2_current_spec_passes(tmp_path, monkeypatch):
    memory, pid, manager, store = _gate_ready(tmp_path, "ag2-g2")
    sig = packet.plan_signature_of(memory.get_mission(pid))
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put_job(store, pid, "j-new", sig, "review_passed", spec=jobs.REVIEW_SPEC_VERSION)
    out = decide(manager, pid)
    assert out["verdict"] == "passed"


def test_v2_current_spec_must_be_on_eligible_review(tmp_path, monkeypatch):
    memory, pid, manager, store = _gate_ready(tmp_path, "ag2-g2-ineligible")
    sig = packet.plan_signature_of(memory.get_mission(pid))
    monkeypatch.setenv("SEBAS_PLAN_GATE_V2_PROJECTS", pid)
    _put_job(store, pid, "j-old-valid", sig, "review_passed", spec="plan-review-v1")
    invalid = _put_job(store, pid, "j-new-invalid", sig, "review_passed")
    invalid["model"] = ""
    store.put(pid, jobs.KIND, "j-new-invalid", invalid)
    out = decide(manager, pid)
    assert out["mode"] == "v2"
    assert out["verdict"] == "unknown"
    assert out["reason"] == "review_spec_outdated"


def test_gate_error_ignores_review_spec(tmp_path):
    from app.independent_review_gate import gate_error
    memory, pid, manager, store = _gate_ready(tmp_path, "ag2-g3")
    sig = packet.plan_signature_of(memory.get_mission(pid))
    _put_job(store, pid, "j-legacy", sig, "review_passed", spec="plan-review-v0")
    assert gate_error(manager, pid) is None


# 6: 静的検査
def test_ag2_static_no_broad_except():
    text = open("app/plan_review_structure.py", encoding="utf-8").read()
    assert "put(" not in text
    assert "add_event" not in text
    assert "open(" not in text
    assert "print(" not in text
    assert "except Exception" not in text
    assert "except:" not in text
    assert "充足" not in text and "未充足" not in text
    assert "必要入力" not in text


def test_ag2_change_scope():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    import subprocess
    diff = subprocess.run(["git", "diff", "--name-only", "HEAD", "--"],
                          cwd=str(root), capture_output=True, text=True,
                          timeout=30)
    changed = {line.strip() for line in (diff.stdout or "").splitlines() if line.strip()}
    st = subprocess.run(["git", "status", "--porcelain"],
                        cwd=str(root), capture_output=True, text=True,
                        timeout=30)
    untracked = set()
    for line in (st.stdout or "").splitlines():
        if not line.strip():
            continue
        code, name = line[:2], line[3:].strip()
        if code.strip() == "??":
            untracked.add(name)
        else:
            changed.add(name.split(" -> ")[-1].strip())
    allowed = {"app/plan_review_structure.py", "app/planning_packet.py",
               "app/independent_review.py", "app/plan_gate_decision.py",
               "app/web.py", "tests/test_ag2_plan_review_structure.py",
               "tests/test_ag1_plan_gate_decision.py"}
    assert (set(changed) | set(untracked)) <= allowed, ((set(changed) | set(untracked)) - allowed)
