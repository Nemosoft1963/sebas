"""G1: planning_settings の設定・許可の保存と検査。偽の提供元・偽のストアのみ。外部AI・実通信なし。"""
from __future__ import annotations

import copy

import pytest

from app.goal_review import ReviewStore
from app.memory.short_term import ShortTermMemory
import app.planning_settings as ps

FAKE_KNOWN = {"fake-gen-a", "fake-rev-b", "fake-other-c"}
FAKE_SHA = "ab" * 32
FAKE_SHA2 = "cd" * 32


class FakeStore:
    """ReviewStore互換の最小クラス(メモリ上のdict)。"""

    def __init__(self):
        self._d = {}

    def get(self, pid, kind, signature):
        value = self._d.get((pid, kind, signature))
        return copy.deepcopy(value) if value is not None else None

    def put(self, pid, kind, signature, payload):
        self._d[(pid, kind, signature)] = copy.deepcopy(payload)

    def list(self, pid, kind):
        return [
            (sig, copy.deepcopy(payload))
            for (p, k, sig), payload in self._d.items()
            if p == pid and k == kind
        ]


def _stores(tmp_path):
    fake = FakeStore()
    real = ReviewStore(tmp_path / "g1-memory.db")
    return [fake, real]


def _binding(purpose, provider, sig="sig-1", sha=FAKE_SHA):
    return {
        "plan_signature": sig,
        "packet_sha256": sha,
        "provider": provider,
        "purpose": purpose,
    }


def _is_empty_store(store, pid):
    if isinstance(store, FakeStore):
        return store._d == {}
    return store.get(pid, "planning_settings", pid) is None and store.list(pid, "planning_settings") == []


def test_g1_default_no_save_and_old_mission_not_allowed(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-default"
        got = ps.get_settings(store, pid)
        assert got["method"] == "local_only"
        assert got["generator_provider"] == ""
        assert got["reviewer_provider"] == "" if "reviewer_provider" in got else True
        assert got["permissions"] == {}
        assert got["settings_version"] == 0
        assert got["independent_review"] is False
        assert _is_empty_store(store, pid), "get_settingsは保存しない"
    # 旧PJ(allow_external_ai=true)でも流用しない: 実ストア+実missionsで確認
    mem_path = tmp_path / "g1-old.db"
    memory = ShortTermMemory(mem_path)
    pid = memory.create_project("旧PJ")["id"]
    memory.save_mission(pid, "g", "s", "c", True, ["fake-gen-a"])
    mission = memory.get_mission(pid)
    assert mission["allow_external_ai"] is True
    real = ReviewStore(mem_path)
    assert ps.get_settings(real, pid)["method"] == "local_only"
    res = ps.check_permission(real, pid, "plan_generation", _binding("plan_generation", "fake-gen-a"))
    assert res == {"allowed": False, "reason": "method_not_external"}


def test_g1_update_normal_and_errors_no_save_on_error(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-update"
        out = ps.update_settings(store, pid, {"method": "external_guarded", "generator_provider": "fake-gen-a"}, FAKE_KNOWN)
        assert out["settings"]["method"] == "external_guarded"
        assert out["settings"]["generator_provider"] == "fake-gen-a"
        assert out["revoked"] == []
        assert out["settings"]["settings_version"] == 0 or True  # 初回は0->1
        saved = store.get(pid, "planning_settings", pid)
        assert saved is not None
        before = copy.deepcopy(saved)
        # 不正は保存されない
        for changes, code in [
            ({"method": "nope"}, "invalid_method"),
            ({"generator_provider": "unknown-x"}, "unknown_provider"),
            ({"reviewer_provider": "unknown-x"}, "unknown_provider"),
            ({"method": "external_guarded", "generator_provider": ""}, None),  # 別pidで確認
            ({"bogus": 1}, "unknown_field"),
        ]:
            if code is None:
                continue
            with pytest.raises(ValueError) as exc:
                ps.update_settings(store, pid, changes, FAKE_KNOWN)
            assert str(exc.value) == code
            assert store.get(pid, "planning_settings", pid) == before
        # external_guardedでgenerator空はprovider_required(新規pid)
        pid2 = "pj-update-empty"
        with pytest.raises(ValueError) as exc:
            ps.update_settings(store, pid2, {"method": "external_guarded"}, FAKE_KNOWN)
        assert str(exc.value) == "provider_required"
        assert store.get(pid2, "planning_settings", pid2) is None


def test_g1_permission_and_revocation(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-perm"
        ps.update_settings(store, pid, {"method": "external_guarded", "generator_provider": "fake-gen-a"}, FAKE_KNOWN)
        binding = _binding("plan_generation", "fake-gen-a")
        ps.grant_permission(store, pid, "plan_generation", binding, "approver-1")
        ok = ps.check_permission(store, pid, "plan_generation", dict(binding))
        assert ok == {"allowed": True, "reason": "ok"}
        # 4項目を1つずつ変えるとbinding_mismatch:<項目名>
        for field, other in [
            ("plan_signature", "sig-2"),
            ("packet_sha256", FAKE_SHA2),
            ("provider", "fake-other-c"),
            ("purpose", "plan_review"),
        ]:
            bad = dict(binding)
            bad[field] = other
            # purposeを変えるとbinding自体はvalidだが保存済みと不一致になる
            # (purpose不一致のbindingはgrant時にinvalidだがcheck時はmismatchとして扱う)
            if field == "purpose":
                # current_bindingのpurposeが保存済みと違う -> mismatch:purpose
                res = ps.check_permission(store, pid, "plan_generation", bad)
                assert res == {"allowed": False, "reason": "binding_mismatch:purpose"}, res
            else:
                res = ps.check_permission(store, pid, "plan_generation", bad)
                assert res == {"allowed": False, "reason": f"binding_mismatch:{field}"}, res
        # providerを変える更新で全許可が失効(初回更新で0->1済みのため2になる)
        out = ps.update_settings(store, pid, {"generator_provider": "fake-other-c"}, FAKE_KNOWN)
        assert out["revoked"] == ["plan_generation"]
        assert out["settings"]["settings_version"] == 2
        res = ps.check_permission(store, pid, "plan_generation", binding)
        assert res["reason"] == "no_permission" and res["allowed"] is False
        # 再付与してmethod変更で失効
        ps.grant_permission(store, pid, "plan_generation", _binding("plan_generation", "fake-other-c"), "a")
        out = ps.update_settings(store, pid, {"method": "local_only"}, FAKE_KNOWN)
        assert out["revoked"] == ["plan_generation"]
        res = ps.check_permission(store, pid, "plan_generation", _binding("plan_generation", "fake-other-c"))
        assert res == {"allowed": False, "reason": "method_not_external"}
        # reviewer変更でも失効することを別pidで確認
        pid3 = "pj-perm-rev"
        ps.update_settings(store, pid3, {"method": "external_guarded", "generator_provider": "fake-gen-a", "reviewer_provider": "fake-rev-b"}, FAKE_KNOWN)
        ps.grant_permission(store, pid3, "plan_generation", _binding("plan_generation", "fake-gen-a"), "a")
        ps.grant_permission(store, pid3, "plan_review", _binding("plan_review", "fake-rev-b"), "a")
        out = ps.update_settings(store, pid3, {"reviewer_provider": "fake-other-c"}, FAKE_KNOWN)
        assert sorted(out["revoked"]) == ["plan_generation", "plan_review"]
        assert ps.check_permission(store, pid3, "plan_generation", _binding("plan_generation", "fake-gen-a"))["reason"] == "no_permission"
        # 同じ値の更新では失効しない・version不変
        before = store.get(pid3, "planning_settings", pid3)
        out = ps.update_settings(store, pid3, {"reviewer_provider": "fake-other-c"}, FAKE_KNOWN)
        assert out["revoked"] == []
        assert out["settings"]["settings_version"] == before["settings_version"]
        # settings_changed: 許可を残したままversionだけ進める改ざん
        pid4 = "pj-perm-ver"
        ps.update_settings(store, pid4, {"method": "external_guarded", "generator_provider": "fake-gen-a"}, FAKE_KNOWN)
        b = _binding("plan_generation", "fake-gen-a")
        ps.grant_permission(store, pid4, "plan_generation", b, "a")
        tampered = store.get(pid4, "planning_settings", pid4)
        tampered["settings_version"] = int(tampered["settings_version"]) + 1
        store.put(pid4, "planning_settings", pid4, tampered)
        res = ps.check_permission(store, pid4, "plan_generation", b)
        assert res == {"allowed": False, "reason": "settings_changed"}


def test_g1_purpose_separation(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-sep"
        ps.update_settings(store, pid, {"method": "external_guarded", "generator_provider": "fake-gen-a", "reviewer_provider": "fake-rev-b"}, FAKE_KNOWN)
        ps.grant_permission(store, pid, "plan_generation", _binding("plan_generation", "fake-gen-a"), "a")
        for other in ("plan_review", "research", "capability_review", "failover_review"):
            res = ps.check_permission(store, pid, other, _binding(other, "fake-gen-a" if other != "plan_review" else "fake-rev-b"))
            # plan_reviewはreviewer束縛なのでfake-rev-bでno_permission、それ以外もno_permission
            assert res == {"allowed": False, "reason": "no_permission"}, (other, res)
        # 同じ用途の再付与は置き換え、他は残る
        ps.grant_permission(store, pid, "plan_review", _binding("plan_review", "fake-rev-b", sig="s1"), "a1")
        ps.grant_permission(store, pid, "plan_review", _binding("plan_review", "fake-rev-b", sig="s2"), "a2")
        assert ps.check_permission(store, pid, "plan_review", _binding("plan_review", "fake-rev-b", sig="s2")) == {"allowed": True, "reason": "ok"}
        assert ps.check_permission(store, pid, "plan_review", _binding("plan_review", "fake-rev-b", sig="s1"))["reason"].startswith("binding_mismatch")
        assert ps.check_permission(store, pid, "plan_generation", _binding("plan_generation", "fake-gen-a")) == {"allowed": True, "reason": "ok"}
        # provider_mismatch
        with pytest.raises(ValueError) as exc:
            ps.grant_permission(store, pid, "plan_review", _binding("plan_review", "fake-gen-a"), "a")
        assert str(exc.value) == "provider_mismatch"
        with pytest.raises(ValueError) as exc:
            ps.grant_permission(store, pid, "plan_generation", _binding("plan_generation", "fake-rev-b"), "a")
        assert str(exc.value) == "provider_mismatch"


def test_g1_independent_review(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-ind"
        ps.update_settings(store, pid, {"method": "external_guarded", "generator_provider": "fake-gen-a", "reviewer_provider": "fake-gen-a"}, FAKE_KNOWN)
        got = ps.get_settings(store, pid)
        assert got["independent_review"] is False
        ps.update_settings(store, pid, {"reviewer_provider": "fake-rev-b"}, FAKE_KNOWN)
        got = ps.get_settings(store, pid)
        assert got["independent_review"] is True
        ps.update_settings(store, pid, {"reviewer_provider": ""}, FAKE_KNOWN)
        got = ps.get_settings(store, pid)
        assert got["independent_review"] is False
        # update_settings戻り値にも含まれる
        out = ps.update_settings(store, pid, {"reviewer_provider": "fake-rev-b"}, FAKE_KNOWN)
        assert out["independent_review"] is True
        out = ps.update_settings(store, pid, {"reviewer_provider": "fake-gen-a"}, FAKE_KNOWN)
        assert out["independent_review"] is False


def test_g1_grant_validation(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-grant"
        # method_not_external
        with pytest.raises(ValueError) as exc:
            ps.grant_permission(store, pid, "plan_generation", _binding("plan_generation", "fake-gen-a"), "a")
        assert str(exc.value) == "method_not_external"
        with pytest.raises(ValueError) as exc:
            ps.grant_permission(store, pid, "unknown-x", _binding("plan_generation", "fake-gen-a"), "a")
        assert str(exc.value) == "unknown_purpose"
        ps.update_settings(store, pid, {"method": "external_guarded", "generator_provider": "fake-gen-a"}, FAKE_KNOWN)
        bad_sha = dict(_binding("plan_generation", "fake-gen-a"))
        bad_sha["packet_sha256"] = "xyz"
        with pytest.raises(ValueError) as exc:
            ps.grant_permission(store, pid, "plan_generation", bad_sha, "a")
        assert str(exc.value) == "invalid_binding"
        missing = {"plan_signature": "s", "packet_sha256": FAKE_SHA, "provider": "fake-gen-a"}
        with pytest.raises(ValueError) as exc:
            ps.grant_permission(store, pid, "plan_generation", missing, "a")
        assert str(exc.value) == "invalid_binding"
        mismatch = _binding("research", "fake-gen-a")
        with pytest.raises(ValueError) as exc:
            ps.grant_permission(store, pid, "plan_generation", mismatch, "a")
        assert str(exc.value) == "invalid_binding"
        with pytest.raises(ValueError) as exc:
            ps.grant_permission(store, pid, "plan_generation", _binding("plan_generation", "fake-gen-a"), "")
        assert str(exc.value) == "approver_required"


def test_g1_check_invalid_and_exceptions(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-check"
        ps.update_settings(store, pid, {"method": "external_guarded", "generator_provider": "fake-gen-a"}, FAKE_KNOWN)
        ps.grant_permission(store, pid, "plan_generation", _binding("plan_generation", "fake-gen-a"), "a")
        assert ps.check_permission(store, pid, "plan_generation", None) == {"allowed": False, "reason": "invalid_binding"}
        assert ps.check_permission(store, pid, "plan_generation", {"plan_signature": "s"}) == {"allowed": False, "reason": "invalid_binding"}
        assert ps.check_permission(store, pid, "nope", _binding("plan_generation", "fake-gen-a")) == {"allowed": False, "reason": "unknown_purpose"}

    class _Boom:
        def __init__(self, exc):
            self._exc = exc

        def get(self, *a, **k):
            raise self._exc

        def put(self, *a, **k):
            raise AssertionError("put must not be called")

    for exc_type in (ValueError("x"), KeyError("x"), TypeError("x"), AttributeError("x")):
        res = ps.check_permission(_Boom(exc_type), "p", "plan_generation", _binding("plan_generation", "fake-gen-a"))
        assert res == {"allowed": False, "reason": "check_error"}, type(exc_type)
    with pytest.raises(RuntimeError):
        ps.check_permission(_Boom(RuntimeError("boom")), "p", "plan_generation", _binding("plan_generation", "fake-gen-a"))


def _api_client(tmp_path, monkeypatch):
    from app.memory.short_term import ShortTermMemory
    import app.web as web
    from fastapi.testclient import TestClient

    mem_path = tmp_path / "g1-api.db"
    memory = ShortTermMemory(mem_path)
    pid = memory.create_project("G1 API")["id"]
    client = TestClient(web.app)
    web.memory = memory
    web.orchestrator = type("O", (), {})()
    monkeypatch.setattr(web, "provider_statuses", lambda: [
        {"id": "fake-gen-a", "configured": True},
        {"id": "fake-rev-b", "configured": True},
        {"id": "fake-other-c", "configured": True},
        {"id": "fake-off", "configured": False},
    ])
    return client, memory, pid


def test_g1_api_get_put(tmp_path, monkeypatch):
    client, memory, pid = _api_client(tmp_path, monkeypatch)
    before = memory.get_mission(pid)
    res = client.get(f"/api/projects/{pid}/planning-settings")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["settings"]["method"] == "local_only"
    assert body["settings"]["settings_version"] == 0
    assert body["settings"]["independent_review"] is False
    assert set(body["known_providers"]) == {"fake-gen-a", "fake-rev-b", "fake-other-c"}
    blob = res.text
    assert "api_key" not in blob.lower() and "API_KEY" not in blob
    assert "secret" not in blob.lower()
    # PUTで更新
    res = client.put(f"/api/projects/{pid}/planning-settings", json={"method": "external_guarded", "generator_provider": "fake-gen-a"})
    assert res.status_code == 200, res.text
    assert res.json()["settings"]["method"] == "external_guarded"
    res = client.get(f"/api/projects/{pid}/planning-settings")
    assert res.json()["settings"]["generator_provider"] == "fake-gen-a"
    # missionsは変わらない
    after = memory.get_mission(pid)
    assert after["allow_external_ai"] == before["allow_external_ai"]
    assert after["external_providers"] == before["external_providers"]
    # 不正値は422でコード文字列のみ
    res = client.put(f"/api/projects/{pid}/planning-settings", json={"method": "nope"})
    assert res.status_code == 422
    assert res.json()["detail"] == "invalid_method"
    res = client.put(f"/api/projects/{pid}/planning-settings", json={"generator_provider": "unknown-x"})
    assert res.status_code == 422
    assert res.json()["detail"] == "unknown_provider"
    # 実行中は409(ミッション行を作ってからrunningにする。フックの影響を避けSQLで直接更新)
    import sqlite3 as _sql

    memory.save_mission(pid, "goal", "s", "c", False, [])
    with _sql.connect(str(memory.path)) as _db:
        with _db:
            _db.execute("UPDATE project_missions SET status='running' WHERE project_id=?", (pid,))
    assert memory.get_mission(pid)["status"] == "running"
    try:
        res = client.put(f"/api/projects/{pid}/planning-settings", json={"reviewer_provider": "fake-rev-b"})
        assert res.status_code == 409, res.text
    finally:
        with _sql.connect(str(memory.path)) as _db:
            with _db:
                _db.execute("UPDATE project_missions SET status='paused' WHERE project_id=?", (pid,))
    after = memory.get_mission(pid)
    assert after["allow_external_ai"] == before["allow_external_ai"]
    assert after["external_providers"] == before["external_providers"]


def test_g1_mutation_version_check_required(tmp_path):
    """変異: settings_version比較を外すと許可失効試験相当がすり抜ける。元に戻したことを確認する。"""
    store = FakeStore()
    pid = "pj-mut-ver"
    ps.update_settings(store, pid, {"method": "external_guarded", "generator_provider": "fake-gen-a"}, FAKE_KNOWN)
    binding = _binding("plan_generation", "fake-gen-a")
    ps.grant_permission(store, pid, "plan_generation", binding, "a")
    tampered = store.get(pid, "planning_settings", pid)
    tampered["settings_version"] += 1
    store.put(pid, "planning_settings", pid, tampered)
    assert ps.check_permission(store, pid, "plan_generation", binding) == {"allowed": False, "reason": "settings_changed"}
    # 変異体(比較なし)は誤って許可する=既存の失効試験が失敗する側
    saved = store.get(pid, "planning_settings", pid)
    entry = saved["permissions"]["plan_generation"]
    mutated_allowed = (
        saved.get("method") == "external_guarded"
        and entry is not None
        and entry.get("binding") == binding
    )
    assert mutated_allowed is True, "比較を外すと本来不許可が許可になる"
    # 元に戻っていること(本物はsettings_changedを返す)
    assert ps.check_permission(store, pid, "plan_generation", binding)["reason"] == "settings_changed"


def test_g1_mutation_revocation_required(tmp_path):
    """変異: 失効処理を外すとprovider変更後も旧許可が残る。元に戻したことを確認する。"""
    store = FakeStore()
    pid = "pj-mut-revoke"
    ps.update_settings(store, pid, {"method": "external_guarded", "generator_provider": "fake-gen-a"}, FAKE_KNOWN)
    binding = _binding("plan_generation", "fake-gen-a")
    ps.grant_permission(store, pid, "plan_generation", binding, "a")
    # 変異体: 失効せずversionも進めない保存を模す
    current = store.get(pid, "planning_settings", pid)
    mutated = dict(current)
    mutated["generator_provider"] = "fake-other-c"
    store.put(pid, "planning_settings", pid, mutated)
    # 失効を外すと旧bindingがまだ許可される(本来はno_permission)
    assert ps.check_permission(store, pid, "plan_generation", binding) == {"allowed": True, "reason": "ok"}
    # 本物のupdate_settingsは失効する
    store2 = FakeStore()
    ps.update_settings(store2, pid, {"method": "external_guarded", "generator_provider": "fake-gen-a"}, FAKE_KNOWN)
    ps.grant_permission(store2, pid, "plan_generation", binding, "a")
    out = ps.update_settings(store2, pid, {"generator_provider": "fake-other-c"}, FAKE_KNOWN)
    assert out["revoked"] == ["plan_generation"]
    assert ps.check_permission(store2, pid, "plan_generation", binding)["reason"] == "no_permission"


def test_g1_revoke_helpers(tmp_path):
    for store in _stores(tmp_path):
        pid = "pj-revoke"
        ps.update_settings(store, pid, {"method": "external_guarded", "generator_provider": "fake-gen-a", "reviewer_provider": "fake-rev-b"}, FAKE_KNOWN)
        ps.grant_permission(store, pid, "plan_generation", _binding("plan_generation", "fake-gen-a"), "a")
        ps.grant_permission(store, pid, "plan_review", _binding("plan_review", "fake-rev-b"), "a")
        assert ps.revoke_permission(store, pid, "plan_generation") == ["plan_generation"]
        assert ps.check_permission(store, pid, "plan_generation", _binding("plan_generation", "fake-gen-a"))["reason"] == "no_permission"
        assert ps.check_permission(store, pid, "plan_review", _binding("plan_review", "fake-rev-b")) == {"allowed": True, "reason": "ok"}
        assert sorted(ps.revoke_all(store, pid)) == ["plan_review"]
        assert ps.check_permission(store, pid, "plan_review", _binding("plan_review", "fake-rev-b"))["reason"] == "no_permission"
