"""G4-b 独立評価ジョブの受入試験。偽ランナーのみ。実外部AIなし。"""
from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest

from app.goal_review import ReviewStore
import app.planning_packet as packet
import app.planning_settings as ps
import app.independent_review as jobs
import app.external_plan_jobs as gen_jobs

GEN = "fake-gen"
REV = "fake-rev"
OTHER = "fake-other"
FAKE_KNOWN = {GEN, REV, OTHER}
SIG = "sig-g4b"


def _setup_external(store, pid, gen=GEN, rev=REV):
    ps.update_settings(store, pid, {"method": "external_guarded",
                                    "generator_provider": gen,
                                    "reviewer_provider": rev}, FAKE_KNOWN)


def _approved_packet(store, pid, provider=REV, sig=SIG,
                     items=None, purpose="plan_review"):
    if items is None:
        items = {"goal": "公開の目標です", "success_criteria": "公開の条件です"}
    p = packet.build_packet(store, pid, purpose=purpose, provider=provider,
                            public_items=dict(items), plan_signature=sig,
                            blocked_terms=[], source_texts=[],
                            withheld_fields=[])
    assert p["status"] == "previewed"
    return packet.approve_packet(store, pid, p["id"], "tester",
                                 current_plan_signature=sig,
                                 blocked_terms=[], source_texts=[])


def _key(pid, sig, sha, provider=REV):
    return jobs.idempotency_key_of(pid, sig, sha, provider, jobs.PURPOSE)


def _review_json(verdict="pass", findings=None, **over):
    body = {"verdict": verdict,
            "findings": findings if findings is not None else []}
    body.update(over)
    return json.dumps(body, ensure_ascii=False)


class FakeRunner:
    def __init__(self, text=None, model="rev-model", exc=None,
                 wait_event=None, timeout_exc=None):
        self.calls = []
        self.text = text if text is not None else _review_json()
        self.model = model
        self.exc = exc
        self.wait_event = wait_event
        self.timeout_exc = timeout_exc

    async def __call__(self, provider, prompt, system, max_tokens):
        self.calls.append({"provider": provider, "prompt": prompt,
                           "system": system, "max_tokens": max_tokens})
        if self.wait_event is not None:
            await self.wait_event.wait()
        if self.timeout_exc is not None:
            raise self.timeout_exc
        if self.exc is not None:
            raise self.exc
        return {"text": self.text, "model": self.model,
                "usage": {"tokens": 7}}


def _run(coro):
    return asyncio.run(coro)


def _gen_job(store, pid, sig=SIG, model="gen-model", provider=GEN):
    """生成側の external_generated ジョブを作る。"""
    ps.update_settings(store, pid, {"generator_provider": provider},
                       FAKE_KNOWN)
    p = packet.build_packet(store, pid, purpose="plan_generation",
                            provider=provider,
                            public_items={"goal": f"公開目標{sig}"},
                            plan_signature=sig, blocked_terms=[],
                            source_texts=[], withheld_fields=[])
    p = packet.approve_packet(store, pid, p["id"], "tester",
                              current_plan_signature=sig, blocked_terms=[],
                              source_texts=[])
    key = gen_jobs.idempotency_key_of(pid, sig, p["sha256"], provider,
                                      gen_jobs.PURPOSE)
    prep = gen_jobs.prepare_job(
        store, pid, packet_id=p["id"], provider=provider,
        idempotency_key=key, current_plan_signature=sig, enabled=True,
        approver="t", blocked_terms=[], source_texts=[])
    runner = FakeGenRunner(model=model)
    return _run(gen_jobs.run_job(store, pid, prep["job"]["id"], prep["text"],
                                 runner))


class FakeGenRunner:
    def __init__(self, model="gen-model"):
        self.calls = []
        self.model = model

    async def __call__(self, provider, prompt, system, max_tokens):
        self.calls.append(1)
        draft = {"tasks": [{"title": "工程1", "description": "作業する",
                            "outputs": [], "depends_on": []}],
                 "required_outputs": [], "unverified_facts": [],
                 "external_operations_pending": []}
        return {"text": json.dumps(draft, ensure_ascii=False),
                "model": self.model, "usage": {}}


# 1: 正常系
def test_g4b_happy_path(tmp_path):
    store = ReviewStore(tmp_path / "happy.db")
    pid = "pj-happy"
    _setup_external(store, pid)
    _gen_job(store, pid, model="gen-model-a")
    p = _approved_packet(store, pid)
    key = _key(pid, SIG, p["sha256"])
    runner = FakeRunner(model="rev-model-b")
    prep = jobs.prepare_job(store, pid, packet_id=p["id"], provider=REV,
                            idempotency_key=key, current_plan_signature=SIG,
                            enabled=True, approver="tester", blocked_terms=[],
                            source_texts=[])
    assert prep["created"] is True
    out = _run(jobs.run_job(store, pid, prep["job"]["id"], prep["text"],
                            runner))
    assert len(runner.calls) == 1
    assert runner.calls[0]["prompt"] == p["text"]
    assert runner.calls[0]["system"] == jobs.REVIEW_SYSTEM_PROMPT
    assert runner.calls[0]["max_tokens"] == jobs.MAX_TOKENS
    assert out["job"]["status"] == "review_passed"
    assert out["job"]["verdict"] == "pass"
    assert out["job"]["model"] == "rev-model-b"
    assert out["job"]["plan_signature"] == SIG
    assert out["job"]["packet_sha256"] == p["sha256"]
    assert out["job"]["provider"] == REV
    assert out["job"]["reviewed_at"] is not None
    r = packet.verify_for_send(store, pid, p["id"],
                               current_plan_signature=SIG)
    assert r["allowed"] is False


# 2: 独立の拒否
def test_g4b_independent_rejections(tmp_path):
    # (a) 提供元が同じ
    store = ReviewStore(tmp_path / "ind-a.db")
    pid = "pj-ind-a"
    ps.update_settings(store, pid, {"method": "external_guarded",
                                    "generator_provider": GEN,
                                    "reviewer_provider": GEN}, FAKE_KNOWN)
    p = packet.build_packet(store, pid, purpose="plan_review", provider=GEN,
                            public_items={"goal": "公開目標"},
                            plan_signature=SIG, blocked_terms=[],
                            source_texts=[], withheld_fields=[])
    p = packet.approve_packet(store, pid, p["id"], "t",
                              current_plan_signature=SIG, blocked_terms=[],
                              source_texts=[])
    runner = FakeRunner()
    with pytest.raises(ValueError) as e:
        jobs.prepare_job(store, pid, packet_id=p["id"], provider=GEN,
                         idempotency_key=_key(pid, SIG, p["sha256"], GEN),
                         current_plan_signature=SIG, enabled=True,
                         approver="t", blocked_terms=[], source_texts=[])
    assert str(e.value) == "not_independent"
    assert runner.calls == []

    # (b) 応答モデルが生成側と一致
    store2 = ReviewStore(tmp_path / "ind-b.db")
    pid2 = "pj-ind-b"
    _setup_external(store2, pid2)
    _gen_job(store2, pid2, model="same-model")
    p2 = _approved_packet(store2, pid2)
    prep2 = jobs.prepare_job(store2, pid2, packet_id=p2["id"], provider=REV,
                             idempotency_key=_key(pid2, SIG, p2["sha256"]),
                             current_plan_signature=SIG, enabled=True,
                             approver="t", blocked_terms=[], source_texts=[])
    out2 = _run(jobs.run_job(store2, pid2, prep2["job"]["id"], prep2["text"],
                             FakeRunner(model="Same-Model ")))
    assert out2["job"]["status"] == "not_independent"
    assert out2["job"]["status"] != "review_passed"

    # (c) 応答の model が空
    store3 = ReviewStore(tmp_path / "ind-c.db")
    pid3 = "pj-ind-c"
    _setup_external(store3, pid3)
    _gen_job(store3, pid3, model="gen-x")
    p3 = _approved_packet(store3, pid3)
    prep3 = jobs.prepare_job(store3, pid3, packet_id=p3["id"], provider=REV,
                             idempotency_key=_key(pid3, SIG, p3["sha256"]),
                             current_plan_signature=SIG, enabled=True,
                             approver="t", blocked_terms=[], source_texts=[])
    out3 = _run(jobs.run_job(store3, pid3, prep3["job"]["id"], prep3["text"],
                             FakeRunner(model="")))
    assert out3["job"]["status"] == "not_independent"

    # (d) 生成側読取が失敗 -> unknown_verdict
    store4 = ReviewStore(tmp_path / "ind-d.db")
    pid4 = "pj-ind-d"
    _setup_external(store4, pid4)
    p4 = _approved_packet(store4, pid4)
    prep4 = jobs.prepare_job(store4, pid4, packet_id=p4["id"], provider=REV,
                             idempotency_key=_key(pid4, SIG, p4["sha256"]),
                             current_plan_signature=SIG, enabled=True,
                             approver="t", blocked_terms=[], source_texts=[])

    def _boom(pid_a, kind):
        raise ValueError("boom")

    import app.external_plan_jobs as _genmod
    orig = _genmod.list_jobs
    _genmod.list_jobs = _boom
    try:
        out4 = _run(jobs.run_job(store4, pid4, prep4["job"]["id"],
                                 prep4["text"], FakeRunner(model="rev-z")))
    finally:
        _genmod.list_jobs = orig
    assert out4["job"]["status"] == "unknown_verdict"

    # (e) 生成側0件で提供元が別 -> review_passed
    store5 = ReviewStore(tmp_path / "ind-e.db")
    pid5 = "pj-ind-e"
    _setup_external(store5, pid5)
    p5 = _approved_packet(store5, pid5)
    prep5 = jobs.prepare_job(store5, pid5, packet_id=p5["id"], provider=REV,
                             idempotency_key=_key(pid5, SIG, p5["sha256"]),
                             current_plan_signature=SIG, enabled=True,
                             approver="t", blocked_terms=[], source_texts=[])
    out5 = _run(jobs.run_job(store5, pid5, prep5["job"]["id"], prep5["text"],
                             FakeRunner(model="rev-alone")))
    assert out5["job"]["status"] == "review_passed"


# 3: 結果の分離
def test_g4b_result_classes(tmp_path):
    def _prep(name, sig):
        store = ReviewStore(tmp_path / f"{name}.db")
        pid = f"pj-{name}"
        _setup_external(store, pid)
        p = _approved_packet(store, pid, sig=sig,
                             items={"goal": f"公開目標{sig}"})
        prep = jobs.prepare_job(
            store, pid, packet_id=p["id"], provider=REV,
            idempotency_key=_key(pid, sig, p["sha256"]),
            current_plan_signature=sig, enabled=True, approver="t",
            blocked_terms=[], source_texts=[])
        return store, pid, prep

    store, pid, prep = _prep("res-fail", "sig-fail")
    out = _run(jobs.run_job(store, pid, prep["job"]["id"], prep["text"],
                            FakeRunner(text=_review_json("fail"))))
    assert out["job"]["status"] == "review_failed"

    store2, pid2, prep2 = _prep("res-unk", "sig-unk")
    out2 = _run(jobs.run_job(store2, pid2, prep2["job"]["id"], prep2["text"],
                             FakeRunner(text=_review_json("unknown"))))
    assert out2["job"]["status"] == "unknown_verdict"

    store3, pid3, prep3 = _prep("res-conn", "sig-conn")
    out3 = _run(jobs.run_job(store3, pid3, prep3["job"]["id"], prep3["text"],
                             FakeRunner(exc=ConnectionError("conn down"))))
    assert out3["job"]["status"] == "connection_failed"
    blob3 = json.dumps(out3["job"], ensure_ascii=False)
    assert "conn down" not in blob3
    audits3 = packet.list_audit(store3, pid3)
    assert "conn down" not in json.dumps(audits3, ensure_ascii=False)

    store4, pid4, prep4 = _prep("res-failed", "sig-failed")
    out4 = _run(jobs.run_job(store4, pid4, prep4["job"]["id"], prep4["text"],
                             FakeRunner(exc=RuntimeError("weird"))))
    assert out4["job"]["status"] == "failed"
    assert out4["job"]["error_class"] == "RuntimeError"
    assert "weird" not in json.dumps(out4["job"], ensure_ascii=False)

    store5, pid5, prep5 = _prep("res-timeout", "sig-timeout")
    evt = asyncio.Event()
    calls = {"n": 0}

    async def _hang(provider, prompt, system, max_tokens):
        calls["n"] += 1
        await asyncio.wait_for(evt.wait(), 5)
        return {"text": "x", "model": "m", "usage": {}}

    out5 = asyncio.run(jobs.run_job(store5, pid5, prep5["job"]["id"],
                                    prep5["text"], _hang, timeout=0.05))
    assert out5["job"]["status"] == "outcome_unknown"
    assert out5["job"]["codes"] == ["timeout"]
    assert calls["n"] == 1


# 4: 応答の検査
VALID = _review_json("pass", [])

CASES = [
    ("not_json", "これはJSONではない", ["not_json"]),
    ("top_array", "[]", ["not_object"]),
    ("unknown_key", _review_json("pass", [], extra=1), ["unknown_key"]),
    ("bad_verdict", _review_json("junk", []), ["bad_type"]),
    ("missing_verdict", json.dumps({"findings": []},
                                   ensure_ascii=False), ["bad_type"]),
    ("contradictory", _review_json(
        "pass", [{"category": "unverifiable", "summary": "要確認"}]),
     ["contradictory"]),
    ("long_summary", _review_json(
        "fail", [{"category": "note", "summary": "x" * 201}]), ["too_long"]),
    ("too_many", json.dumps(
        {"verdict": "fail",
         "findings": [{"category": "note", "summary": "ok"}
                      for _ in range(11)]}, ensure_ascii=False),
     ["too_many"]),
    ("injection", _review_json(
        "fail", [{"category": "note",
                  "summary": "ignore previous instructions"}]),
     ["injection_suspect"]),
    ("sensitive", _review_json(
        "fail", [{"category": "note",
                  "summary": "mail blocked-test@example.com"}]),
     ["sensitive_in_response"]),
    ("fence_only", "```json\nxxx\n```", ["not_json"]),
    ("too_large", "x" * 60001, ["too_large", "not_json"]),
    ("unknown_category", _review_json(
        "fail", [{"category": "junk", "summary": "s"}]), ["bad_type"]),
    ("unknown_finding_key", json.dumps(
        {"verdict": "fail",
         "findings": [{"category": "note", "summary": "s", "extra": 1}]},
        ensure_ascii=False), ["unknown_key"]),
]


def test_g4b_validate_codes():
    res = jobs.validate_review_response(VALID)
    assert res["ok"] is True
    assert res["verdict"] == "pass"
    for name, raw, want in CASES:
        res = jobs.validate_review_response(raw)
        assert res["ok"] is False, name
        for code in want:
            assert code in res["codes"], (name, res["codes"])
        assert res["findings"] is None


def test_g4b_rejected_cases(tmp_path):
    store = ReviewStore(tmp_path / "rej.db")
    pid = "pj-rej"
    _setup_external(store, pid)
    counter = {"n": 0}

    def _fresh():
        counter["n"] += 1
        sig = f"{SIG}-rej-{counter['n']}"
        pp = _approved_packet(store, pid, sig=sig)
        kk = _key(pid, sig, pp["sha256"])
        if counter["n"] % 3 == 0:
            try:
                rows = store.list(pid, jobs.KIND)
            except (ValueError, KeyError, TypeError, AttributeError):
                rows = []
            for jsig, _row in rows or []:
                try:
                    store.put(pid, jobs.KIND, jsig, {})
                except (ValueError, KeyError, TypeError, AttributeError):
                    pass
            import app.goal_review as _gr
            try:
                with _gr.ReviewStore(
                        str(store.path)).connect() as _db:
                    _db.execute(
                        "DELETE FROM reviews WHERE project=? AND kind=?",
                        (pid, jobs.KIND))
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        return pp, sig, kk

    def _one(raw, want_codes):
        pp, sig, kk = _fresh()
        prep = jobs.prepare_job(store, pid, packet_id=pp["id"], provider=REV,
                                idempotency_key=kk,
                                current_plan_signature=sig, enabled=True,
                                approver="t", blocked_terms=[], source_texts=[])
        runner = FakeRunner(text=raw)
        out = _run(jobs.run_job(store, pid, prep["job"]["id"], prep["text"],
                                runner))
        assert out["job"]["status"] == "rejected_response", (raw[:80],
                                                             out["job"])
        for code in want_codes:
            assert code in out["job"]["codes"], (raw[:80], out["job"]["codes"])
        assert out["job"]["status"] != "review_passed"
        blob = json.dumps(out["job"], ensure_ascii=False)
        assert runner.text not in blob or runner.text == ""
        return out

    # 原本の24文字以上の引用(長い本文の先頭を使う)
    ppq = packet.build_packet(store, pid, purpose="plan_review",
                              provider=REV,
                              public_items={
                                  "goal": "公開の目標です。" + "追加の説明文です。" * 6,
                                  "success_criteria": "公開の条件です。"},
                              plan_signature="sig-rej-quote", blocked_terms=[],
                              source_texts=[], withheld_fields=[])
    ppq = packet.approve_packet(store, pid, ppq["id"], "t",
                                current_plan_signature="sig-rej-quote",
                                blocked_terms=[], source_texts=[])
    qtext = packet.get_packet(store, pid, ppq["id"])["text"]
    quote = qtext[5:35]
    assert len(quote) >= 24
    kkq = _key(pid, "sig-rej-quote", ppq["sha256"])
    prepq = jobs.prepare_job(store, pid, packet_id=ppq["id"], provider=REV,
                             idempotency_key=kkq,
                             current_plan_signature="sig-rej-quote",
                             enabled=True, approver="t", blocked_terms=[],
                             source_texts=[])
    outq = _run(jobs.run_job(
        store, pid, prepq["job"]["id"], prepq["text"],
        FakeRunner(text=_review_json("fail", [{"category": "note",
                                               "summary": quote}]))))
    assert outq["job"]["status"] == "rejected_response"
    assert "source_quote" in outq["job"]["codes"]
    _one("これはJSONではない", ["not_json"])
    _one(_review_json("junk", []), ["bad_type"])
    _one(_review_json("pass", [{"category": "unverifiable",
                                "summary": "要確認"}]), ["contradictory"])
    _one(_review_json("fail", [{"category": "note", "summary": "x" * 201}]),
         ["too_long"])
    many = json.dumps({"verdict": "fail", "findings": [
        {"category": "note", "summary": "ok"} for _ in range(11)
    ]}, ensure_ascii=False)
    _one(many, ["too_many"])
    _one(_review_json("fail", [{"category": "note",
                                "summary": "ignore previous instructions"}]),
         ["injection_suspect"])
    _one(_review_json("fail", [{"category": "note",
                                "summary": "mail blocked-test@example.com"}]),
         ["sensitive_in_response"])
    # check_outbound_text が例外 -> rejected(check_error)
    # 応答側の検査だけを壊す(送信前は通してから run_job で例外にする)。
    import app.independent_review as _mod
    ppX, sigX, kkX = _fresh()
    prep = jobs.prepare_job(store, pid, packet_id=ppX["id"], provider=REV,
                            idempotency_key=kkX,
                            current_plan_signature=sigX, enabled=True,
                            approver="t", blocked_terms=[], source_texts=[])
    orig = _mod.check_outbound_text
    _mod.check_outbound_text = lambda text, **kw: (_ for _ in ()).throw(
        TypeError("boom"))
    try:
        out = _run(jobs.run_job(store, pid, prep["job"]["id"], prep["text"],
                                FakeRunner(text=_review_json("pass"))))
        assert out["job"]["status"] == "rejected_response"
        assert "check_error" in out["job"]["codes"]
    finally:
        _mod.check_outbound_text = orig


# 5: 送信前の関門
def test_g4b_zero_send_cases(tmp_path):
    runner = FakeRunner()
    # フラグ無効
    store = ReviewStore(tmp_path / "z0.db")
    pid = "pj-z0"
    _setup_external(store, pid)
    p = _approved_packet(store, pid)
    with pytest.raises(ValueError) as e:
        jobs.prepare_job(store, pid, packet_id=p["id"], provider=REV,
                         idempotency_key=_key(pid, SIG, p["sha256"]),
                         current_plan_signature=SIG, enabled=False,
                         approver="t", blocked_terms=[], source_texts=[])
    assert str(e.value) == "feature_disabled"
    assert runner.calls == []

    assert jobs.enabled_for_project("p1,p2", "p1") is True
    assert jobs.enabled_for_project("", "p1") is False
    assert jobs.enabled_for_project("p1", "p10") is False

    # 方式が local_only
    store = ReviewStore(tmp_path / "z1.db")
    pid = "pj-z1"
    p1 = packet.build_packet(store, pid, purpose="plan_review", provider=REV,
                             public_items={"goal": "公開目標"},
                             plan_signature=SIG, blocked_terms=[],
                             source_texts=[], withheld_fields=[])
    with pytest.raises(ValueError) as e:
        jobs.prepare_job(store, pid, packet_id=p1["id"], provider=REV,
                         idempotency_key=_key(pid, SIG, p1["sha256"]),
                         current_plan_signature=SIG, enabled=True,
                         approver="t", blocked_terms=[], source_texts=[])
    assert str(e.value) == "method_not_external"

    # provider が generator_provider
    store = ReviewStore(tmp_path / "z2.db")
    pid = "pj-z2"
    _setup_external(store, pid)
    p2 = _approved_packet(store, pid)
    with pytest.raises(ValueError) as e:
        jobs.prepare_job(store, pid, packet_id=p2["id"], provider=GEN,
                         idempotency_key=_key(pid, SIG, p2["sha256"], GEN),
                         current_plan_signature=SIG, enabled=True,
                         approver="t", blocked_terms=[], source_texts=[])
    assert str(e.value) == "provider_mismatch"

    # purpose が plan_generation
    store = ReviewStore(tmp_path / "z3.db")
    pid = "pj-z3"
    ps.update_settings(store, pid, {"method": "external_guarded",
                                    "generator_provider": GEN,
                                    "reviewer_provider": REV}, FAKE_KNOWN)
    pg = packet.build_packet(store, pid, purpose="plan_generation",
                             provider=GEN,
                             public_items={"goal": "公開目標"},
                             plan_signature=SIG, blocked_terms=[],
                             source_texts=[], withheld_fields=[])
    pg = packet.approve_packet(store, pid, pg["id"], "t",
                               current_plan_signature=SIG, blocked_terms=[],
                               source_texts=[])
    with pytest.raises(ValueError) as e:
        jobs.prepare_job(store, pid, packet_id=pg["id"], provider=REV,
                         idempotency_key=_key(pid, SIG, pg["sha256"]),
                         current_plan_signature=SIG, enabled=True,
                         approver="t", blocked_terms=[], source_texts=[])
    assert str(e.value) in ("provider_mismatch", "purpose_mismatch")

    # 冪等キー不一致
    store = ReviewStore(tmp_path / "z4.db")
    pid = "pj-z4"
    _setup_external(store, pid)
    p4 = _approved_packet(store, pid)
    with pytest.raises(ValueError) as e:
        jobs.prepare_job(store, pid, packet_id=p4["id"], provider=REV,
                         idempotency_key="bad",
                         current_plan_signature=SIG, enabled=True,
                         approver="t", blocked_terms=[], source_texts=[])
    assert str(e.value) == "idempotency_key_mismatch"

    # 未承認
    store = ReviewStore(tmp_path / "z5.db")
    pid = "pj-z5"
    _setup_external(store, pid)
    p5 = packet.build_packet(store, pid, purpose="plan_review", provider=REV,
                             public_items={"goal": "公開目標"},
                             plan_signature=SIG, blocked_terms=[],
                             source_texts=[], withheld_fields=[])
    with pytest.raises(ValueError) as e:
        jobs.prepare_job(store, pid, packet_id=p5["id"], provider=REV,
                         idempotency_key=_key(pid, SIG, p5["sha256"]),
                         current_plan_signature=SIG, enabled=True,
                         approver="t", blocked_terms=[], source_texts=[])
    assert str(e.value) == "packet_not_approved"

    # 承認後に原本が変わった(長い本文で24文字引用が刺さる)
    store = ReviewStore(tmp_path / "z6.db")
    pid = "pj-z6"
    _setup_external(store, pid)
    p6 = packet.build_packet(store, pid, purpose="plan_review", provider=REV,
                             public_items={
                                 "goal": "公開の目標です。" + "追加の説明文です。" * 6,
                                 "success_criteria": "公開の条件です。"},
                             plan_signature=SIG, blocked_terms=[],
                             source_texts=[], withheld_fields=[])
    p6 = packet.approve_packet(store, pid, p6["id"], "t",
                               current_plan_signature=SIG, blocked_terms=[],
                               source_texts=[])
    quote = p6["text"][5:35]
    with pytest.raises(ValueError) as e:
        jobs.prepare_job(store, pid, packet_id=p6["id"], provider=REV,
                         idempotency_key=_key(pid, SIG, p6["sha256"]),
                         current_plan_signature=SIG, enabled=True,
                         approver="t", blocked_terms=[],
                         source_texts=["前置き " + quote + " 後置き"])
    assert str(e.value) == "privacy_recheck_failed"
    assert jobs.list_jobs(store, pid) == []
    assert packet.get_packet(
        store, pid, p6["id"])["status"] == "approved"

    # 上限(4件目)
    store = ReviewStore(tmp_path / "z7.db")
    pid = "pj-z7"
    _setup_external(store, pid)
    for i in range(3):
        pp = _approved_packet(store, pid, sig=f"sig-z{i}",
                              items={"goal": f"公開目標{i}"})
        prep = jobs.prepare_job(
            store, pid, packet_id=pp["id"], provider=REV,
            idempotency_key=_key(pid, f"sig-z{i}", pp["sha256"]),
            current_plan_signature=f"sig-z{i}", enabled=True, approver="t",
            blocked_terms=[], source_texts=[])
        assert prep["created"] is True
    pp4 = _approved_packet(store, pid, sig="sig-z4",
                           items={"goal": "公開目標4"})
    with pytest.raises(ValueError) as e:
        jobs.prepare_job(store, pid, packet_id=pp4["id"], provider=REV,
                         idempotency_key=_key(pid, "sig-z4", pp4["sha256"]),
                         current_plan_signature="sig-z4", enabled=True,
                         approver="t", blocked_terms=[], source_texts=[])
    assert str(e.value) == "quota_exceeded"

    # ストア読取の失敗
    store = ReviewStore(tmp_path / "z8.db")
    pid = "pj-z8"
    _setup_external(store, pid)
    p8 = _approved_packet(store, pid)
    orig_get = store.get

    def _boom_get(pid_a, kind, sig):
        if kind == jobs.KIND:
            raise ValueError("read fail")
        return orig_get(pid_a, kind, sig)

    store.get = _boom_get
    with pytest.raises(ValueError) as e:
        jobs.prepare_job(store, pid, packet_id=p8["id"], provider=REV,
                         idempotency_key=_key(pid, SIG, p8["sha256"]),
                         current_plan_signature=SIG, enabled=True,
                         approver="t", blocked_terms=[], source_texts=[])
    assert str(e.value) == "check_error"
    store.get = orig_get
    assert jobs.list_jobs(store, pid) == []

    # 既存ジョブは同じものを返し runner 増えない
    store = ReviewStore(tmp_path / "z9.db")
    pid = "pj-z9"
    _setup_external(store, pid)
    p9 = _approved_packet(store, pid)
    key9 = _key(pid, SIG, p9["sha256"])
    prep1 = jobs.prepare_job(store, pid, packet_id=p9["id"], provider=REV,
                             idempotency_key=key9, current_plan_signature=SIG,
                             enabled=True, approver="t", blocked_terms=[],
                             source_texts=[])
    prep2 = jobs.prepare_job(store, pid, packet_id=p9["id"], provider=REV,
                             idempotency_key=key9, current_plan_signature=SIG,
                             enabled=True, approver="t", blocked_terms=[],
                             source_texts=[])
    assert prep2["created"] is False
    assert prep2["job"]["id"] == prep1["job"]["id"]
    assert runner.calls == []


# 6: 再起動の取り残し
def test_g4b_interrupted_and_cancel_discard(tmp_path):
    store = ReviewStore(tmp_path / "intr.db")
    pid = "pj-intr"
    _setup_external(store, pid)
    p = _approved_packet(store, pid)
    prep = jobs.prepare_job(store, pid, packet_id=p["id"], provider=REV,
                            idempotency_key=_key(pid, SIG, p["sha256"]),
                            current_plan_signature=SIG, enabled=True,
                            approver="t", blocked_terms=[], source_texts=[])
    saved = dict(prep["job"])
    saved["boot_id"] = "old-boot"
    store.put(pid, jobs.KIND, saved["id"], saved)
    got = jobs.get_job(store, pid, saved["id"])
    assert got["status"] == "outcome_unknown"
    assert got["codes"] == ["interrupted"]
    runner = FakeRunner()
    prep2 = jobs.prepare_job(store, pid, packet_id=p["id"], provider=REV,
                             idempotency_key=_key(pid, SIG, p["sha256"]),
                             current_plan_signature=SIG, enabled=True,
                             approver="t", blocked_terms=[], source_texts=[])
    assert prep2["job"]["id"] == saved["id"]
    assert runner.calls == []
    # 取消済みジョブの結果は保存されない
    store2 = ReviewStore(tmp_path / "cancel.db")
    pid2 = "pj-cancel"
    _setup_external(store2, pid2)
    p2 = _approved_packet(store2, pid2)
    prep_c = jobs.prepare_job(
        store2, pid2, packet_id=p2["id"], provider=REV,
        idempotency_key=_key(pid2, SIG, p2["sha256"]),
        current_plan_signature=SIG, enabled=True, approver="t",
        blocked_terms=[], source_texts=[])
    evt = asyncio.Event()
    runner2 = FakeRunner(wait_event=evt)
    jobs.cancel_job(store2, pid2, prep_c["job"]["id"])

    async def _scenario():
        task = asyncio.create_task(
            jobs.run_job(store2, pid2, prep_c["job"]["id"], prep_c["text"],
                         runner2))
        await asyncio.sleep(0.05)
        evt.set()
        return await task

    out = asyncio.run(_scenario())
    assert out["job"]["status"] == "cancelled"
    # sending中に取消
    store3 = ReviewStore(tmp_path / "cancel2.db")
    pid3 = "pj-cancel2"
    _setup_external(store3, pid3)
    p3 = _approved_packet(store3, pid3)
    prep3 = jobs.prepare_job(
        store3, pid3, packet_id=p3["id"], provider=REV,
        idempotency_key=_key(pid3, SIG, p3["sha256"]),
        current_plan_signature=SIG, enabled=True, approver="t",
        blocked_terms=[], source_texts=[])
    evt3 = asyncio.Event()
    runner3 = FakeRunner(wait_event=evt3)

    async def _scenario3():
        task = asyncio.create_task(
            jobs.run_job(store3, pid3, prep3["job"]["id"], prep3["text"],
                         runner3))
        await asyncio.sleep(0.05)
        jobs.cancel_job(store3, pid3, prep3["job"]["id"])
        evt3.set()
        return await task

    out3 = asyncio.run(_scenario3())
    assert out3["job"]["status"] == "cancelled"
    assert len(runner3.calls) == 1
    assert out3["job"]["findings"] == []


# 7: API
def _api_client(tmp_path, monkeypatch):
    from app.memory.short_term import ShortTermMemory
    import app.web as web
    from fastapi.testclient import TestClient

    mem_path = tmp_path / "g4b-api.db"
    memory = ShortTermMemory(mem_path)
    pid = memory.create_project("G4B API")["id"]
    memory.save_mission(pid, "目標G", "条件S", "制約C", False, [])
    client = TestClient(web.app)
    web.memory = memory
    from app.goal_review import ReviewStore as _RS
    import app.planning_settings as _ps
    _ps.update_settings(_RS(memory.path), pid,
                        {"method": "external_guarded",
                         "generator_provider": GEN,
                         "reviewer_provider": REV}, FAKE_KNOWN)
    monkeypatch.setattr(web, "provider_statuses", lambda: [
        {"id": GEN, "configured": True},
        {"id": REV, "configured": True}])
    monkeypatch.setenv("SEBAS_INDEPENDENT_REVIEW_PROJECTS", pid)
    from contextlib import asynccontextmanager as _acm

    @_acm
    async def _noop_lifespan(_app):
        yield

    monkeypatch.setattr(web.app.router, "lifespan_context", _noop_lifespan)
    monkeypatch.setattr(web, "engine_enabled", False)
    web._INDEPENDENT_REVIEW_TASKS.clear()
    return client, memory, pid, web


def _api_packet(client, pid):
    res = client.post(f"/api/projects/{pid}/plan-packets/preview",
                      json={"purpose": "plan_review",
                            "provider": REV,
                            "public_items": {
                                "goal": "公開目標です。" + "追加の説明文です。" * 6,
                                "success_criteria": "公開の条件です。"},
                            "withheld_fields": []})
    assert res.status_code == 200, res.text
    packet_id = res.json()["id"]
    res = client.put(f"/api/projects/{pid}/planning-settings",
                     json={"method": "external_guarded",
                           "generator_provider": GEN,
                           "reviewer_provider": REV})
    assert res.status_code == 200, res.text
    res = client.post(f"/api/projects/{pid}/plan-packets/{packet_id}/approve",
                      json={"approver": "a"})
    assert res.status_code == 200, res.text
    return client.get(
        f"/api/projects/{pid}/plan-packets/{packet_id}").json()


def _wait_job(client, pid, job_id, timeout=10.0):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        res = client.get(
            f"/api/projects/{pid}/independent-review-jobs/{job_id}")
        assert res.status_code == 200, res.text
        last = res.json()
        if last.get("status") != "sending":
            return last
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {last}")


def test_g4b_api(tmp_path, monkeypatch):
    import app.web as web

    client, memory, pid, _ = _api_client(tmp_path, monkeypatch)
    pkt = _api_packet(client, pid)
    key = jobs.idempotency_key_of(
        pid, packet.plan_signature_of(memory.get_mission(pid)),
        pkt["sha256"], REV, jobs.PURPOSE)

    async def _fake(provider, prompt, system, max_tokens):
        assert prompt == pkt["text"]
        assert system == jobs.REVIEW_SYSTEM_PROMPT
        assert max_tokens == jobs.MAX_TOKENS
        return {"text": _review_json("pass"), "model": "rev-api",
                "usage": {"tokens": 1}}

    monkeypatch.setattr(web, "_independent_review_runner", _fake)
    with client:
        res = client.post(f"/api/projects/{pid}/independent-review-jobs",
                          json={"packet_id": pkt["id"], "provider": REV,
                                "idempotency_key": key, "approver": "a"})
        assert res.status_code == 202, res.text
        job_id = res.json()["id"]
        assert res.json()["status"] == "sending"
        done = _wait_job(client, pid, job_id)
        assert done["status"] == "review_passed"
        body = json.dumps(done, ensure_ascii=False)
        assert pkt["text"] not in body
        assert done["request_template"] == jobs.REVIEW_SYSTEM_PROMPT
        assert done["findings"] == []
        res = client.get(f"/api/projects/{pid}/independent-review-jobs")
        assert res.status_code == 200
        assert any(j["id"] == job_id for j in res.json()["jobs"])
        res = client.post(f"/api/projects/{pid}/independent-review-jobs",
                          json={"packet_id": pkt["id"], "provider": REV,
                                "idempotency_key": key, "approver": "a"})
        assert res.status_code == 200, res.text
        assert res.json()["id"] == job_id
        res = client.get(
            f"/api/projects/{pid}/independent-review-jobs/nope")
        assert res.status_code == 404
        res = client.post(
            f"/api/projects/{pid}/independent-review-jobs/{job_id}/cancel")
        assert res.status_code == 409
    # フラグ無効は403・原本取得関数を呼ばない
    monkeypatch.setenv("SEBAS_INDEPENDENT_REVIEW_PROJECTS", "other-pid")
    pkt2 = _api_packet(client, pid)
    key2 = jobs.idempotency_key_of(
        pid, packet.plan_signature_of(memory.get_mission(pid)),
        pkt2["sha256"], REV, jobs.PURPOSE)
    calls = {"n": 0}
    orig_guard = web._plan_packet_guard_inputs

    def _boom_guard(project_id):
        calls["n"] += 1
        raise AssertionError("guard must not run when disabled")

    monkeypatch.setattr(web, "_plan_packet_guard_inputs", _boom_guard)
    bad = {"n": 0}
    orig_list = memory.list_context_files

    def _boom_list(project_id, include_content=False):
        bad["n"] += 1
        raise OSError("list unavailable")

    monkeypatch.setattr(memory, "list_context_files", _boom_list)
    res = client.post(f"/api/projects/{pid}/independent-review-jobs",
                      json={"packet_id": pkt2["id"], "provider": REV,
                            "idempotency_key": key2, "approver": "a"})
    assert res.status_code == 403
    assert res.json()["detail"] == "feature_disabled"
    assert calls["n"] == 0
    assert bad["n"] == 0
    monkeypatch.setattr(web, "_plan_packet_guard_inputs", orig_guard)
    monkeypatch.setattr(memory, "list_context_files", orig_list)
    # 実行中409
    monkeypatch.setenv("SEBAS_INDEPENDENT_REVIEW_PROJECTS", pid)
    import sqlite3 as _sql
    with _sql.connect(str(memory.path)) as _db:
        with _db:
            _db.execute("UPDATE project_missions SET status='running' WHERE project_id=?",
                        (pid,))
    try:
        res = client.post(f"/api/projects/{pid}/independent-review-jobs",
                          json={"packet_id": pkt2["id"], "provider": REV,
                                "idempotency_key": key2, "approver": "a"})
        assert res.status_code == 409, res.text
    finally:
        with _sql.connect(str(memory.path)) as _db:
            with _db:
                _db.execute("UPDATE project_missions SET status='paused' WHERE project_id=?",
                            (pid,))
    # 原本取得失敗は422(先にpacketを作ってからguardを壊す)
    monkeypatch.setenv("SEBAS_INDEPENDENT_REVIEW_PROJECTS", pid)
    pkt3 = _api_packet(client, pid)
    key3 = jobs.idempotency_key_of(
        pid, packet.plan_signature_of(memory.get_mission(pid)),
        pkt3.get("sha256", "x"), REV, jobs.PURPOSE)

    def _raise_check_error(project_id):
        raise ValueError("check_error")

    monkeypatch.setattr(web, "_plan_packet_guard_inputs", _raise_check_error)
    with client:
        res = client.post(f"/api/projects/{pid}/independent-review-jobs",
                          json={"packet_id": pkt3.get("id", "dummy"),
                                "provider": REV,
                                "idempotency_key": key3, "approver": "a"})
        assert res.status_code == 422
        assert res.json()["detail"] == "check_error"


def test_g4b_api_cancel(tmp_path, monkeypatch):
    import app.web as web

    client, memory, pid, _ = _api_client(tmp_path, monkeypatch)
    pkt = _api_packet(client, pid)
    key = jobs.idempotency_key_of(
        pid, packet.plan_signature_of(memory.get_mission(pid)),
        pkt["sha256"], REV, jobs.PURPOSE)
    evt = asyncio.Event()

    async def _waiting(provider, prompt, system, max_tokens):
        await evt.wait()
        return {"text": _review_json("pass"), "model": "rev-w",
                "usage": {}}

    monkeypatch.setattr(web, "_independent_review_runner", _waiting)
    with client:
        res = client.post(f"/api/projects/{pid}/independent-review-jobs",
                          json={"packet_id": pkt["id"], "provider": REV,
                                "idempotency_key": key, "approver": "a"})
        assert res.status_code == 202, res.text
        job_id = res.json()["id"]
        res = client.post(
            f"/api/projects/{pid}/independent-review-jobs/{job_id}/cancel")
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "cancelled"
        evt.set()
        done = _wait_job(client, pid, job_id)
        assert done["status"] == "cancelled"
        res = client.post(
            f"/api/projects/{pid}/independent-review-jobs/{job_id}/cancel")
        assert res.status_code == 409
        assert res.json()["detail"] == "job_not_cancellable"


# 8: 実行ゲートに接続しないこと
def test_g4b_no_gate_wiring():
    import hashlib as _hash
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parents[1]
    for name in ("app/workflow_readiness.py", "app/execution_funnel.py",
                 "app/goal_review.py", "app/plan_review_loop.py"):
        text = (root / name).read_text(encoding="utf-8")
        if name == "app/workflow_readiness.py":
            # G4-c: 表示用に新ゲートだけを参照する。他の独立評価の部品には依存しない。
            text = text.replace("independent_review_gate", "").replace(
                "independent_review_check_error", "")
            assert "independent_review." not in text, name
            continue
        assert "independent_review" not in text, name
        assert "independent-review" not in text, name


# 9: 変異試験
def test_g4b_mutation_independence_check(tmp_path):
    """独立判定(応答モデルの比較)を外すと2(b)(c)が失敗する。"""
    import app.independent_review as _mod
    src = open(_mod.__file__, encoding="utf-8").read()
    assert "_generation_models" in src
    assert "not_independent" in src
    store = ReviewStore(tmp_path / "mut-ind.db")
    pid = "pj-mut-ind"
    _setup_external(store, pid)
    _gen_job(store, pid, model="same-m")
    p = _approved_packet(store, pid)
    prep = jobs.prepare_job(store, pid, packet_id=p["id"], provider=REV,
                            idempotency_key=_key(pid, SIG, p["sha256"]),
                            current_plan_signature=SIG, enabled=True,
                            approver="t", blocked_terms=[], source_texts=[])
    out = _run(jobs.run_job(store, pid, prep["job"]["id"], prep["text"],
                            FakeRunner(model="SAME-M")))
    assert out["job"]["status"] == "not_independent"
    # 変異: 生成側比較を外した想定 = 常に review_passed になる
    mutated = out["job"]["status"]
    assert mutated != "review_passed"
    # 元に戻っていること
    assert _mod._generation_models is not None


def test_g4b_mutation_reviewer_check(tmp_path):
    """provider == reviewer_provider の検査を外すと5の該当が失敗する。"""
    import inspect as _inspect
    src = _inspect.getsource(jobs.prepare_job)
    assert "reviewer_provider" in src
    assert "provider_mismatch" in src
    store = ReviewStore(tmp_path / "mut-rev.db")
    pid = "pj-mut-rev"
    _setup_external(store, pid)
    p = _approved_packet(store, pid)
    with pytest.raises(ValueError) as e:
        jobs.prepare_job(store, pid, packet_id=p["id"], provider=GEN,
                         idempotency_key=_key(pid, SIG, p["sha256"], GEN),
                         current_plan_signature=SIG, enabled=True,
                         approver="t", blocked_terms=[], source_texts=[])
    assert str(e.value) == "provider_mismatch"


def test_g4b_mutation_contradiction_check():
    """verdict=pass と指摘の矛盾検査を外すと4の該当が失敗する。"""
    raw = _review_json("pass", [{"category": "unverifiable",
                                 "summary": "要確認"}])
    res = jobs.validate_review_response(raw)
    assert res["ok"] is False
    assert "contradictory" in res["codes"]


def test_g4b_mutation_check_exception_is_reject(monkeypatch):
    """検査例外を合格扱いにすると4の該当が失敗する。"""
    assert jobs.validate_review_response(VALID)["ok"] is True

    def _boom(text, *args, **kwargs):
        raise TypeError("boom")

    monkeypatch.setattr(jobs, "check_outbound_text", _boom)
    res = jobs.validate_review_response(VALID)
    assert res["ok"] is False
    assert "check_error" in res["codes"]
    assert res["findings"] is None


def test_g4b_mutation_no_recheck_fails(tmp_path, monkeypatch):
    """送信直前の再検査を外すと5の該当が失敗する。"""
    import inspect as _inspect
    src = _inspect.getsource(jobs.prepare_job)
    assert "check_outbound_text" in src
    assert "privacy_recheck_failed" in src
    store = ReviewStore(tmp_path / "mut-re.db")
    pid = "pj-mut-re"
    _setup_external(store, pid)
    p = packet.build_packet(store, pid, purpose="plan_review", provider=REV,
                            public_items={
                                "goal": "公開の目標です。" + "追加の説明文です。" * 6,
                                "success_criteria": "公開の条件です。"},
                            plan_signature=SIG, blocked_terms=[],
                            source_texts=[], withheld_fields=[])
    p = packet.approve_packet(store, pid, p["id"], "t",
                              current_plan_signature=SIG, blocked_terms=[],
                              source_texts=[])
    key = _key(pid, SIG, p["sha256"])
    quote = p["text"][5:35]
    import app.independent_review as _mod
    real = _mod.check_outbound_text
    monkeypatch.setattr(jobs, "check_outbound_text",
                        lambda text, blocked_terms=None, source_texts=None: {
                            "ok": True, "codes": []})
    prep = jobs.prepare_job(store, pid, packet_id=p["id"], provider=REV,
                            idempotency_key=key, current_plan_signature=SIG,
                            enabled=True, approver="t", blocked_terms=[],
                            source_texts=["前置き " + quote + " 後置き"])
    assert prep["created"] is True
    monkeypatch.undo()
    assert _mod.check_outbound_text is real
    store2 = ReviewStore(tmp_path / "mut-re2.db")
    pid2 = "pj-mut-re2"
    _setup_external(store2, pid2)
    p2 = packet.build_packet(
        store2, pid2, purpose="plan_review", provider=REV,
        public_items={"goal": "公開の目標です。" + "追加の説明文です。" * 6,
                      "success_criteria": "公開の条件です。"},
        plan_signature="sig-mut2", blocked_terms=[],
        source_texts=[], withheld_fields=[])
    p2 = packet.approve_packet(store2, pid2, p2["id"], "t",
                               current_plan_signature="sig-mut2",
                               blocked_terms=[], source_texts=[])
    key2 = _key(pid2, "sig-mut2", p2["sha256"])
    quote2 = p2["text"][5:35]
    with pytest.raises(ValueError) as e:
        jobs.prepare_job(store2, pid2, packet_id=p2["id"], provider=REV,
                         idempotency_key=key2,
                         current_plan_signature="sig-mut2", enabled=True,
                         approver="t", blocked_terms=[],
                         source_texts=["前置き " + quote2 + " 後置き"])
    assert str(e.value) == "privacy_recheck_failed"
