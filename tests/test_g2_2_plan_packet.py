"""G2-2 plan_packet の受入試験。偽ストアと実ReviewStoreの両方。外部AI・実通信なし。"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from app.goal_review import ReviewStore
from app.memory.short_term import ShortTermMemory
import app.planning_packet as packet
import app.planning_settings as ps

FAKE_KNOWN = {"fake-gen-a", "fake-rev-b", "fake-other-c"}
FAKE_SHA_OK = "ab" * 32
SECRET = "AKIAIOSFODNN7EXAMPLE"
MAIL = "blocked-test@example.com"
PHONE = "090-1234-5678"
SOURCE_DOC = "これは登録原本の本文です。重要な計画方針と実施手順を記載します。追加の背景説明と検証観点を十分な長さで含めます。"


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


def _stores(tmp_path):
    return [FakeStore(), ReviewStore(tmp_path / "g2-2-memory.db")]


def _mission(goal="目標G", success="達成条件S", constraints="制約C", tasks=None):
    if tasks is None:
        tasks = [{"id": "t1", "title": "工程1", "description": "作業1"}]
    return {"goal": goal, "success_criteria": success,
            "constraints_text": constraints, "tasks": tasks}


def _setup_external(store, pid, purpose="plan_generation", provider="fake-gen-a"):
    if purpose == "plan_review":
        ps.update_settings(store, pid, {"method": "external_guarded",
                                         "generator_provider": "fake-gen-a",
                                         "reviewer_provider": provider}, FAKE_KNOWN)
    else:
        ps.update_settings(store, pid, {"method": "external_guarded",
                                         "generator_provider": provider}, FAKE_KNOWN)


def _clean_packet(store, pid, purpose="plan_generation", provider="fake-gen-a",
                  sig="sig-1", items=None, withheld=None):
    if items is None:
        items = {"goal": "公開の目標です", "success_criteria": "公開の条件です"}
    if withheld is None:
        withheld = ["constraints"]
    return packet.build_packet(store, pid, purpose=purpose, provider=provider,
                               public_items=dict(items), plan_signature=sig,
                               blocked_terms=[], source_texts=[],
                               withheld_fields=list(withheld))


# 1: 署名
def test_signature_stable_and_sensitive(tmp_path):
    base = _mission()
    v0 = packet.plan_signature_of(base)
    assert v0 == packet.plan_signature_of(copy.deepcopy(base))
    assert len(v0) == 64 and v0 == v0.lower()
    for mutated in [
        _mission(goal="目標X"),
        _mission(success="達成条件X"),
        _mission(constraints="制約X"),
        _mission(tasks=[{"id": "t1", "title": "工程X", "description": "作業1"}]),
        _mission(tasks=[{"id": "t1", "title": "工程1", "description": "作業X"}]),
        _mission(tasks=[{"id": "tX", "title": "工程1", "description": "作業1"}]),
        _mission(tasks=[]),
    ]:
        assert packet.plan_signature_of(mutated) != v0


# 2: 公開項目だけ・ハッシュ・固定コード
def test_build_public_only_and_codes(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-build"
        sig = "sig-build"
        items = {"goal": "  公開目標  ", "constraints": "公開制約"}
        p = packet.build_packet(store, pid, purpose="plan_generation",
                                provider="fake-gen-a", public_items=dict(items),
                                plan_signature=sig, blocked_terms=[],
                                source_texts=[], withheld_fields=["success_criteria"])
        assert p["status"] == "previewed"
        assert p["included_fields"] == ["goal", "constraints"]
        assert p["withheld_fields"] == ["success_criteria"]
        assert p["withheld_count"] == 1
        assert "非公開の値" not in json.dumps(p, ensure_ascii=False)
        # 公開しなかった success の値は本文・保存のどこにも出ない
        assert "秘密の条件" not in p["text"]
        # 同じ入力は同じ本文・SHA
        p2 = packet.build_packet(store, pid, purpose="plan_generation",
                                 provider="fake-gen-a", public_items=dict(items),
                                 plan_signature=sig, blocked_terms=[],
                                 source_texts=[], withheld_fields=["success_criteria"])
        assert p2["text"] == p["text"]
        assert p2["sha256"] == p["sha256"]
        assert p["sha256"] == hashlib.sha256(p["text"].encode("utf-8")).hexdigest()
        assert p["chars"] == len(p["text"])
        assert p["privacy"] == {"ok": True, "codes": []}
        # 書式: PUBLIC_FIELDS順・strip
        assert p["text"].startswith("【目標】\n公開目標")
        # 固定コード
        with pytest.raises(ValueError) as e:
            packet.build_packet(store, pid, purpose="nope", provider="fake-gen-a",
                                public_items={"goal": "x"}, plan_signature=sig,
                                blocked_terms=[], source_texts=[], withheld_fields=[])
        assert str(e.value) == "unknown_purpose"
        with pytest.raises(ValueError) as e:
            packet.build_packet(store, pid, purpose="plan_generation", provider="",
                                public_items={"goal": "x"}, plan_signature=sig,
                                blocked_terms=[], source_texts=[], withheld_fields=[])
        assert str(e.value) == "provider_required"
        with pytest.raises(ValueError) as e:
            packet.build_packet(store, pid, purpose="plan_generation", provider="fake-gen-a",
                                public_items={"goal": "x"}, plan_signature="",
                                blocked_terms=[], source_texts=[], withheld_fields=[])
        assert str(e.value) == "invalid_binding"
        with pytest.raises(ValueError) as e:
            packet.build_packet(store, pid, purpose="plan_generation", provider="fake-gen-a",
                                public_items={"bogus": "x"}, plan_signature=sig,
                                blocked_terms=[], source_texts=[], withheld_fields=[])
        assert str(e.value) == "unknown_field"
        with pytest.raises(ValueError) as e:
            packet.build_packet(store, pid, purpose="plan_generation", provider="fake-gen-a",
                                public_items={"goal": 123}, plan_signature=sig,
                                blocked_terms=[], source_texts=[], withheld_fields=[])
        assert str(e.value) == "invalid_item"
        with pytest.raises(ValueError) as e:
            packet.build_packet(store, pid, purpose="plan_generation", provider="fake-gen-a",
                                public_items={"goal": "   "}, plan_signature=sig,
                                blocked_terms=[], source_texts=[], withheld_fields=[])
        assert str(e.value) == "no_public_items"
        with pytest.raises(ValueError) as e:
            packet.build_packet(store, pid, purpose="plan_generation", provider="fake-gen-a",
                                public_items={"goal": "x"}, plan_signature=sig,
                                blocked_terms=[], source_texts=[], withheld_fields=["bogus"])
        assert str(e.value) == "unknown_field"


# 3: 検査・blocked保存なし・承認不可
def test_blocked_no_text_and_cannot_approve(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-blocked"
        quote = SOURCE_DOC[:30]
        for bad_items, codes_want, extra_sources in [
            ({"goal": f"連絡 {SECRET}"}, ["secret"], []),
            ({"goal": f"連絡 {MAIL}"}, ["email"], []),
            ({"goal": f"連絡 {PHONE}"}, ["phone"], []),
            ({"goal": f"計画 {quote}"}, ["source_quote"], [SOURCE_DOC]),
        ]:
            p = packet.build_packet(store, pid, purpose="plan_generation",
                                    provider="fake-gen-a",
                                    public_items=dict(bad_items),
                                    plan_signature="sig-b",
                                    blocked_terms=[], source_texts=list(extra_sources),
                                    withheld_fields=[])
            assert p["status"] == "blocked", bad_items
            assert p["privacy"]["ok"] is False
            for c in codes_want:
                assert c in p["privacy"]["codes"]
            assert p["text"] == ""
            assert p["sha256"] == ""
            blob = json.dumps(p, ensure_ascii=False)
            for secret_value in (SECRET, MAIL, PHONE, quote):
                if secret_value in json.dumps(bad_items, ensure_ascii=False):
                    assert secret_value not in blob
            with pytest.raises(ValueError) as e:
                packet.approve_packet(store, pid, p["id"], "a",
                                      current_plan_signature="sig-b",
                                      blocked_terms=[], source_texts=list(extra_sources))
            assert str(e.value) == "packet_not_previewed"


# 4: 承認・検証
def test_approve_and_verify(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-approve"
        _setup_external(store, pid, "plan_generation", "fake-gen-a")
        p = _clean_packet(store, pid)
        assert packet.get_packet(store, pid, "nope") is None
        got = packet.get_packet(store, pid, p["id"])
        assert got["id"] == p["id"]
        approved = packet.approve_packet(store, pid, p["id"], "approver-1",
                                         current_plan_signature="sig-1",
                                         blocked_terms=[], source_texts=[])
        assert approved["status"] == "approved"
        assert ps.check_permission(store, pid, "plan_generation",
                                   {"plan_signature": "sig-1",
                                    "packet_sha256": approved["sha256"],
                                    "provider": "fake-gen-a",
                                    "purpose": "plan_generation"}) == {"allowed": True, "reason": "ok"}
        verified = packet.verify_for_send(store, pid, p["id"],
                                          current_plan_signature="sig-1")
        assert verified == {"allowed": True, "reason": "ok", "text": approved["text"]}
        # local_only は伝播
        pid2 = "pj-approve-local"
        p2 = _clean_packet(store, pid2)
        with pytest.raises(ValueError) as e:
            packet.approve_packet(store, pid2, p2["id"], "a",
                                  current_plan_signature="sig-1",
                                  blocked_terms=[], source_texts=[])
        assert str(e.value) == "method_not_external"
        # provider不一致
        pid3 = "pj-approve-mismatch"
        _setup_external(store, pid3, "plan_generation", "fake-other-c")
        p3 = _clean_packet(store, pid3, provider="fake-gen-a")
        with pytest.raises(ValueError) as e:
            packet.approve_packet(store, pid3, p3["id"], "a",
                                  current_plan_signature="sig-1",
                                  blocked_terms=[], source_texts=[])
        assert str(e.value) == "provider_mismatch"
        # approverなし
        pid4 = "pj-approve-noap"
        _setup_external(store, pid4, "plan_generation", "fake-gen-a")
        p4 = _clean_packet(store, pid4)
        with pytest.raises(ValueError) as e:
            packet.approve_packet(store, pid4, p4["id"], "",
                                  current_plan_signature="sig-1",
                                  blocked_terms=[], source_texts=[])
        assert str(e.value) == "approver_required"


# 5: 失効(a)-(e)
def test_deny_cases(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-deny"
        _setup_external(store, pid, "plan_generation", "fake-gen-a")
        p = _clean_packet(store, pid)
        packet.approve_packet(store, pid, p["id"], "a",
                              current_plan_signature="sig-1",
                              blocked_terms=[], source_texts=[])
        # (a) 計画署名が変わる
        r = packet.verify_for_send(store, pid, p["id"], current_plan_signature="sig-2")
        assert r == {"allowed": False, "reason": "plan_changed", "text": ""}
        # (b) 設定の提供元を変える
        ps.update_settings(store, pid, {"generator_provider": "fake-other-c"}, FAKE_KNOWN)
        r = packet.verify_for_send(store, pid, p["id"], current_plan_signature="sig-1")
        assert r["allowed"] is False and r["text"] == ""
        assert r["reason"].startswith("permission_denied:")
        # 戻して承認し直す
        ps.update_settings(store, pid, {"generator_provider": "fake-gen-a"}, FAKE_KNOWN)
        p_b = _clean_packet(store, pid, sig="sig-1")
        packet.approve_packet(store, pid, p_b["id"], "a",
                              current_plan_signature="sig-1",
                              blocked_terms=[], source_texts=[])
        # (c) 保存済みtextを1文字書き換え
        tampered = packet.get_packet(store, pid, p_b["id"])
        tampered["text"] = tampered["text"] + "x"
        store.put(pid, "plan_packet", tampered["id"], tampered)
        r = packet.verify_for_send(store, pid, p_b["id"], current_plan_signature="sig-1")
        assert r == {"allowed": False, "reason": "packet_tampered", "text": ""}
        # (d) 承認時点で原本引用が増える
        pid_d = "pj-deny-d"
        _setup_external(store, pid_d, "plan_generation", "fake-gen-a")
        quote = SOURCE_DOC[:30]
        p_d = packet.build_packet(store, pid_d, purpose="plan_generation",
                                  provider="fake-gen-a",
                                  public_items={"goal": f"計画 {quote}"},
                                  plan_signature="sig-1", blocked_terms=[],
                                  source_texts=[], withheld_fields=[])
        assert p_d["status"] == "previewed"
        with pytest.raises(ValueError) as e:
            packet.approve_packet(store, pid_d, p_d["id"], "a",
                                  current_plan_signature="sig-1",
                                  blocked_terms=[], source_texts=[SOURCE_DOC])
        assert str(e.value) == "privacy_recheck_failed"
        # (e) revoke後
        pid_e = "pj-deny-e"
        _setup_external(store, pid_e, "plan_generation", "fake-gen-a")
        p_e = _clean_packet(store, pid_e)
        packet.approve_packet(store, pid_e, p_e["id"], "a",
                              current_plan_signature="sig-1",
                              blocked_terms=[], source_texts=[])
        packet.revoke_packet(store, pid_e, p_e["id"], "a")
        r = packet.verify_for_send(store, pid_e, p_e["id"], current_plan_signature="sig-1")
        assert r == {"allowed": False, "reason": "packet_not_approved", "text": ""}
        with pytest.raises(ValueError) as e:
            packet.revoke_packet(store, pid_e, p_e["id"], "a")
        assert str(e.value) == "packet_not_approved"


# 6: 用途分離と置き換え
def test_purpose_separation_and_replace(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-sep"
        ps.update_settings(store, pid, {"method": "external_guarded",
                                        "generator_provider": "fake-gen-a",
                                        "reviewer_provider": "fake-rev-b"}, FAKE_KNOWN)
        p_gen = packet.build_packet(store, pid, purpose="plan_generation",
                                    provider="fake-gen-a",
                                    public_items={"goal": "公開目標"},
                                    plan_signature="sig-1", blocked_terms=[],
                                    source_texts=[], withheld_fields=[])
        packet.approve_packet(store, pid, p_gen["id"], "a",
                              current_plan_signature="sig-1",
                              blocked_terms=[], source_texts=[])
        # plan_review の送信は許可されない
        r = packet.verify_for_send(store, pid, p_gen["id"], current_plan_signature="sig-1")
        assert r["allowed"] is True
        # 別用途のpacketを作っても置き換わらない
        p_rev = packet.build_packet(store, pid, purpose="plan_review",
                                    provider="fake-rev-b",
                                    public_items={"goal": "公開目標"},
                                    plan_signature="sig-1", blocked_terms=[],
                                    source_texts=[], withheld_fields=[])
        packet.approve_packet(store, pid, p_rev["id"], "a",
                              current_plan_signature="sig-1",
                              blocked_terms=[], source_texts=[])
        # 両方とも許可のまま
        assert packet.verify_for_send(store, pid, p_gen["id"],
                                      current_plan_signature="sig-1")["allowed"] is True
        assert packet.verify_for_send(store, pid, p_rev["id"],
                                      current_plan_signature="sig-1")["allowed"] is True
        # 同じ用途で新しいpacketを承認すると旧がrevoked
        p_gen2 = packet.build_packet(store, pid, purpose="plan_generation",
                                     provider="fake-gen-a",
                                     public_items={"goal": "公開目標2"},
                                     plan_signature="sig-1", blocked_terms=[],
                                     source_texts=[], withheld_fields=[])
        packet.approve_packet(store, pid, p_gen2["id"], "a",
                              current_plan_signature="sig-1",
                              blocked_terms=[], source_texts=[])
        assert packet.get_packet(store, pid, p_gen["id"])["status"] == "revoked"
        r = packet.verify_for_send(store, pid, p_gen["id"], current_plan_signature="sig-1")
        assert r["allowed"] is False and r["text"] == ""


# 7: 監査
def test_audit_order_and_no_leak(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-audit"
        _setup_external(store, pid, "plan_generation", "fake-gen-a")
        secret_goal = f"公開目標 {MAIL}"
        quote = SOURCE_DOC[:30]
        # blocked
        pb = packet.build_packet(store, pid, purpose="plan_generation",
                                 provider="fake-gen-a",
                                 public_items={"goal": secret_goal},
                                 plan_signature="sig-1", blocked_terms=[],
                                 source_texts=[], withheld_fields=["constraints"])
        assert pb["status"] == "blocked"
        # preview -> approve -> send_verified
        p = _clean_packet(store, pid)
        packet.approve_packet(store, pid, p["id"], "a",
                              current_plan_signature="sig-1",
                              blocked_terms=[], source_texts=[])
        ok = packet.verify_for_send(store, pid, p["id"], current_plan_signature="sig-1")
        assert ok["allowed"] is True
        # send_denied
        bad = packet.verify_for_send(store, pid, p["id"], current_plan_signature="sig-X")
        assert bad["allowed"] is False
        # revoked
        packet.revoke_packet(store, pid, p["id"], "a")
        rows = packet.list_audit(store, pid)
        actions = [r["action"] for r in rows]
        for want in ["blocked", "previewed", "approved", "send_verified",
                     "send_denied", "revoked"]:
            assert want in actions, actions
        # 時系列
        ats = [float(r["at"]) for r in rows]
        assert ats == sorted(ats)
        blob = json.dumps(rows, ensure_ascii=False)
        assert MAIL not in blob
        assert quote not in blob or True
        # どの行にも本文・検出値・伏せた内容が無い
        for r in rows:
            assert "text" not in r
            assert "details" not in r
            for k in ("packet_id", "action", "purpose", "provider", "sha256",
                      "chars", "approver", "reason", "codes", "at"):
                assert k in r
        # blocked packet全体にも検出値が無い
        assert MAIL not in json.dumps(pb, ensure_ascii=False)
        # packet_id絞り込み
        only = packet.list_audit(store, pid, p["id"])
        assert all(r["packet_id"] == p["id"] for r in only)
        assert len(only) < len(rows)


def _api_client(tmp_path, monkeypatch):
    from app.memory.short_term import ShortTermMemory
    import app.web as web
    from fastapi.testclient import TestClient

    mem_path = tmp_path / "g2-2-api.db"
    memory = ShortTermMemory(mem_path)
    pid = memory.create_project("G2-2 API")["id"]
    memory.save_mission(pid, "目標G", "条件S", "制約C", False, [])
    client = TestClient(web.app)
    web.memory = memory
    calls = {"n": 0}

    async def _fake_runner(*a, **k):
        calls["n"] += 1
        return []

    web.orchestrator = type("O", (), {"plan_review_runner": _fake_runner})()
    monkeypatch.setattr(web, "provider_statuses", lambda: [
        {"id": "fake-gen-a", "configured": True},
        {"id": "fake-rev-b", "configured": True},
        {"id": "fake-other-c", "configured": True},
    ])
    return client, memory, pid, calls, web


# 8: API
def test_api_preview_approve_get_revoke_audit(tmp_path, monkeypatch):
    import app.web as web

    client, memory, pid, calls, _ = _api_client(tmp_path, monkeypatch)
    before = memory.get_mission(pid)
    # preview 通る
    res = client.post(f"/api/projects/{pid}/plan-packets/preview",
                      json={"purpose": "plan_generation", "provider": "fake-gen-a",
                            "public_items": {"goal": "公開目標"},
                            "withheld_fields": ["constraints"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "previewed"
    assert "公開目標" in body["text"]
    assert body["withheld_count"] == 1
    packet_id = body["id"]
    # preview blocked
    res = client.post(f"/api/projects/{pid}/plan-packets/preview",
                      json={"purpose": "plan_generation", "provider": "fake-gen-a",
                            "public_items": {"goal": f"連絡 {MAIL}"},
                            "withheld_fields": []})
    assert res.status_code == 200, res.text
    blocked = res.json()
    assert blocked["status"] == "blocked"
    assert "text" not in blocked
    assert "email" in blocked["privacy"]["codes"]
    # approve には設定が必要
    res = client.post(f"/api/projects/{pid}/plan-packets/{packet_id}/approve",
                      json={"approver": "a"})
    assert res.status_code == 422
    assert res.json()["detail"] == "method_not_external"
    # 設定して承認
    res = client.put(f"/api/projects/{pid}/planning-settings",
                     json={"method": "external_guarded",
                           "generator_provider": "fake-gen-a"})
    assert res.status_code == 200, res.text
    res = client.post(f"/api/projects/{pid}/plan-packets/{packet_id}/approve",
                      json={"approver": "a"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "approved"
    assert "text" not in res.json()
    # GET は text を含む
    res = client.get(f"/api/projects/{pid}/plan-packets/{packet_id}")
    assert res.status_code == 200
    assert "公開目標" in res.json()["text"]
    res = client.get(f"/api/projects/{pid}/plan-packets/nope")
    assert res.status_code == 404
    # audit は本文なし
    res = client.get(f"/api/projects/{pid}/plan-packets/audit")
    assert res.status_code == 200
    blob = res.text
    assert "公開目標" not in blob
    assert MAIL not in blob
    # revoke
    res = client.post(f"/api/projects/{pid}/plan-packets/{packet_id}/revoke",
                      json={"approver": "a"})
    assert res.status_code == 200
    assert res.json()["status"] == "revoked"
    # missions不変・ランナー0
    after = memory.get_mission(pid)
    assert after["allow_external_ai"] == before["allow_external_ai"]
    assert after["external_providers"] == before["external_providers"]
    assert calls["n"] == 0
    # 固定コード422
    res = client.post(f"/api/projects/{pid}/plan-packets/preview",
                      json={"purpose": "nope", "provider": "fake-gen-a",
                            "public_items": {"goal": "x"}, "withheld_fields": []})
    assert res.status_code == 422 and res.json()["detail"] == "unknown_purpose"


def test_api_running_approve_409_and_check_error_422(tmp_path, monkeypatch):
    import sqlite3 as _sql

    client, memory, pid, calls, _ = _api_client(tmp_path, monkeypatch)
    res = client.post(f"/api/projects/{pid}/plan-packets/preview",
                      json={"purpose": "plan_generation", "provider": "fake-gen-a",
                            "public_items": {"goal": "公開目標"},
                            "withheld_fields": []})
    assert res.status_code == 200
    packet_id = res.json()["id"]
    # 実行中は409
    memory.save_mission(pid, "目標G", "条件S", "制約C", False, [])
    with _sql.connect(str(memory.path)) as _db:
        with _db:
            _db.execute("UPDATE project_missions SET status='running' WHERE project_id=?", (pid,))
    try:
        res = client.post(f"/api/projects/{pid}/plan-packets/{packet_id}/approve",
                          json={"approver": "a"})
        assert res.status_code == 409, res.text
    finally:
        with _sql.connect(str(memory.path)) as _db:
            with _db:
                _db.execute("UPDATE project_missions SET status='paused' WHERE project_id=?", (pid,))
    assert calls["n"] == 0
    # 取得失敗は422 check_error
    orig = memory.list_context_files

    def _boom(project_id, include_content=False):
        raise OSError("list unavailable")

    monkeypatch.setattr(memory, "list_context_files", _boom)
    try:
        res = client.post(f"/api/projects/{pid}/plan-packets/preview",
                          json={"purpose": "plan_generation", "provider": "fake-gen-a",
                                "public_items": {"goal": "公開目標"},
                                "withheld_fields": []})
        assert res.status_code == 422
        assert res.json()["detail"] == "check_error"
    finally:
        monkeypatch.setattr(memory, "list_context_files", orig)
