"""B1 媒体選択と表示の受入試験。疑似PJ・実関数。外部通信なし。

- Xのみ選択 → X用の文案・manifest・保存された共有行だけ。イベントの channels も ["x"]。
- 選択なし(未登録) → 従来どおり6媒体。応答に channel_basis: legacy_all_not_configured。
- 全媒体を enabled=false で登録 → キットを作らず none_needed。
- 台帳が読めない(RegistryReadError の注入) → 409 で全媒体にフォールバックしない。
- X の public_identity が fictional → 応答の identity_status が fictional、理由『架空・公開不可』。
- 選択から外れた媒体に公開URL登録済みの証跡があるとき、再生成は拒否され、証跡は消えない。
- 非選択媒体の共有行が残っているとき unselected_channels_present に出る(削除されない)。
- 選択した媒体だけが達成条件・公開タスクに現れる。
- 既存の試験は壊さない(social_package(channels=None) は6媒体のまま)。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.contact_registry import (
    RegistryReadError,
    register_channel_selection,
    register_public_identity,
)
from app.memory.short_term import ShortTermMemory
from app.social_premarketing import (
    CHANNELS,
    channel_identity_statuses,
    resolve_channel_selection,
    social_package,
    unselected_channel_drift,
)
from app import social_premarketing


def _campaign(cid: str = "campaign-b1"):
    return {
        "id": cid,
        "title": "架空相談",
        "audience": "架空DX担当者",
        "offer": "架空課題を30分で整理します",
        "call_to_action": "架空診断を申し込む",
        "google_site_url": "https://sites.google.com/view/架空-b1",
    }


def _env(tmp_path, name: str = "架空B1"):
    mem_dir = tmp_path / name / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空B1疑似PJ", "架空コンテキスト")
    pid = project["id"]
    return mem_path, memory, pid


def _select_only(mem_path, pid, platform="X", campaign_id=""):
    refs = {}
    for plat in ("X", "LinkedIn", "Facebook", "LINE", "Instagram", "Threads"):
        saved = register_channel_selection(
            mem_path, project_id=pid, platform=plat,
            enabled=(plat == platform), campaign_id=campaign_id,
            actor="架空担当")
        refs[plat] = saved
    return refs


def _disable_all(mem_path, pid):
    for plat in ("X", "LinkedIn", "Facebook", "LINE", "Instagram", "Threads"):
        register_channel_selection(
            mem_path, project_id=pid, platform=plat,
            enabled=False, actor="架空担当")


def _published_campaign(memory, pid, cid="campaign-b1"):
    campaign = memory.create_campaign(
        pid, "架空相談", "架空DX担当者", "架空課題を整理", "架空申込", "")
    db_campaign = dict(memory.get_campaign(pid, campaign["id"]))
    db_campaign.update({
        "id": db_campaign["id"],
        "title": "架空相談", "audience": "架空DX担当者",
        "offer": "架空課題を整理", "call_to_action": "架空申込",
        "google_site_url": "https://sites.google.com/view/架空-b1",
    })
    memory.update_campaign_site(
        pid, campaign["id"], "published",
        google_site_url="https://sites.google.com/view/架空-b1",
        google_site_edit_url="https://sites.google.com/架空-b1/edit",
        published_at="2026-01-02T00:00:00+00:00",
        site_publication_approved_at="2026-01-01T00:00:00+00:00",
        landing_asset_version="v1", published_asset_version="v1")
    return memory.get_campaign(pid, campaign["id"])


def test_resolve_selected_only_x():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        mem_path, _memory, pid = _env(Path(td), "r1")
        _select_only(mem_path, pid, "X")
        result = resolve_channel_selection(mem_path, pid, None)
        assert result["status"] == "selected"
        assert result["channels"] == ["x"]


def test_resolve_campaign_scope_overrides_project():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        mem_path, _memory, pid = _env(Path(td), "r2")
        _select_only(mem_path, pid, "LINE")
        register_channel_selection(
            mem_path, project_id=pid, platform="X", enabled=True,
            campaign_id="campaign-b1", actor="架空担当")
        result = resolve_channel_selection(mem_path, pid, "campaign-b1")
        assert result["status"] == "selected"
        assert result["channels"] == ["x"]


def test_resolve_not_configured_and_none_needed():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        mem_path, _memory, pid = _env(Path(td), "r3")
        assert resolve_channel_selection(mem_path, pid, None)["status"] == "not_configured"
        _disable_all(mem_path, pid)
        none = resolve_channel_selection(mem_path, pid, None)
        assert none["status"] == "none_needed"
        assert none["channels"] == []


def test_resolve_unreadable_is_not_not_configured(tmp_path):
    _mem_path, _memory, pid = _env(tmp_path, "r4")
    import app.social_premarketing as sp
    import app.contact_registry as reg
    original = reg.list_channel_selections

    def _boom(*_a, **_k):
        raise RegistryReadError("row_unreadable", path_kind="relative",
                                sqlite_error="OperationalError")
    reg.list_channel_selections = _boom
    try:
        result = sp.resolve_channel_selection(_mem_path, pid, None)
    finally:
        reg.list_channel_selections = original
    assert result["status"] == "unreadable"
    assert result["channels"] == []


def test_social_package_channels_subset_and_unknown_rejected():
    items = social_package(_campaign(), ["x"])
    assert [item["channel"] for item in items] == ["x"]
    assert social_package(_campaign()) and len(social_package(_campaign())) == 6
    assert {item["channel"] for item in social_package(_campaign())} == set(CHANNELS)
    with pytest.raises(ValueError):
        social_package(_campaign(), ["x", "架空"])


def test_unselected_drift_lists_only():
    shares = [{"channel": "x"}, {"channel": "line"}, {"channel": "x"}]
    assert unselected_channel_drift(["x"], shares) == ["line"]
    assert unselected_channel_drift(["x", "line"], shares) == []


def test_identity_fictional_is_not_publishable(tmp_path):
    mem_path, _memory, pid = _env(tmp_path, "id1")
    _select_only(mem_path, pid, "X")
    saved = register_public_identity(
        mem_path, project_id=pid, platform="X",
        handle="@架空-x", profile_url="https://social.example.invalid/x/架空",
        source_kind="fictional", actor="架空担当")
    # 選択の account_ref に公開IDを紐付ける(台帳にあれば)。
    register_channel_selection(
        mem_path, project_id=pid, platform="X", enabled=True,
        account_ref=saved["identity_id"], actor="架空担当")
    statuses = channel_identity_statuses(mem_path, pid, None, ["x"])
    assert statuses["x"]["identity_status"] == "fictional"
    assert "架空" in statuses["x"]["reason_ja"]
    assert "公開不可" in statuses["x"]["reason_ja"]


def _client(monkeypatch, tmp_path, label: str):
    import app.web as web
    from app.workspace_files import WorkspaceSandbox
    mem_path, memory, pid = _env(tmp_path, label)
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "workspace",
                        WorkspaceSandbox(tmp_path / f"{label}-ws"))
    monkeypatch.setattr(web, "WORKSPACE_ROOT", tmp_path / f"{label}-ws")
    monkeypatch.setattr(
        web, "DATA_DIR", tmp_path / f"{label}-data")
    client = TestClient(web.app)
    return client, memory, pid


def _publish(client, pid):
    campaign = client.post(
        f"/api/projects/{pid}/premarketing/campaigns",
        json={"title": "架空相談", "audience": "架空DX担当者",
              "offer": "架空課題を整理", "call_to_action": "架空申込"}).json()
    cid = campaign["id"]
    return cid


def test_build_package_selected_only_x(monkeypatch, tmp_path):
    client, memory, pid = _client(monkeypatch, tmp_path, "b1x")
    _select_only(memory.path, pid, "X")
    cid = _publish(client, pid)
    memory.update_campaign_site(
        pid, cid, "published",
        google_site_url="https://sites.google.com/view/架空-b1",
        site_publication_approved_at="2026-01-01T00:00:00+00:00",
        published_at="2026-01-02T00:00:00+00:00")
    owned = [row for row in memory.list_social_shares(pid, cid)]
    assert owned == []
    response = client.post(
        f"/api/projects/{pid}/premarketing/campaigns/{cid}/social/package")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["channel"] for item in body["shares"]] == ["x"]
    assert body["channels"] == ["x"]
    assert body["channel_basis"] == "selected"
    assert body["identity_status"]["x"]["identity_status"] in {
        "registered", "fictional", "unregistered"}
    saved = memory.list_social_shares(pid, cid)
    assert [item["channel"] for item in saved] == ["x"]
    events = memory.get_mission(pid)["events"]
    created = next(e for e in events if e["kind"] == "social_package_created")
    import json as _json
    assert _json.loads(created["detail"])["channels"] == ["x"]


def test_build_package_legacy_all_when_not_configured(monkeypatch, tmp_path):
    client, memory, pid = _client(monkeypatch, tmp_path, "b1legacy")
    cid = _publish(client, pid)
    memory.update_campaign_site(
        pid, cid, "published",
        google_site_url="https://sites.google.com/view/架空-b1",
        site_publication_approved_at="2026-01-01T00:00:00+00:00",
        published_at="2026-01-02T00:00:00+00:00")
    response = client.post(
        f"/api/projects/{pid}/premarketing/campaigns/{cid}/social/package")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["channel_basis"] == "legacy_all_not_configured"
    assert {item["channel"] for item in body["shares"]} == set(CHANNELS)
    assert "媒体" in body["reason_ja"]


def test_build_package_none_needed_makes_nothing(monkeypatch, tmp_path):
    client, memory, pid = _client(monkeypatch, tmp_path, "b1none")
    _disable_all(memory.path, pid)
    cid = _publish(client, pid)
    memory.update_campaign_site(
        pid, cid, "published",
        google_site_url="https://sites.google.com/view/架空-b1",
        site_publication_approved_at="2026-01-01T00:00:00+00:00",
        published_at="2026-01-02T00:00:00+00:00")
    response = client.post(
        f"/api/projects/{pid}/premarketing/campaigns/{cid}/social/package")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "none_needed"
    assert body["shares"] == []
    assert memory.list_social_shares(pid, cid) == []
    assert "投稿不要" in body["reason_ja"]


def test_identity_read_failure_precedes_package_writes(monkeypatch, tmp_path):
    client, memory, pid = _client(monkeypatch, tmp_path, "b1identity-read")
    cid = _publish(client, pid)
    memory.update_campaign_site(
        pid, cid, "published",
        google_site_url="https://sites.google.com/view/架空-b1",
        site_publication_approved_at="2026-01-01T00:00:00+00:00",
        published_at="2026-01-02T00:00:00+00:00")
    def _fail(*_a, **_k):
        raise RegistryReadError("row_unreadable", sqlite_error="OperationalError")
    monkeypatch.setattr("app.web.channel_identity_statuses", _fail)
    response = client.post(
        f"/api/projects/{pid}/premarketing/campaigns/{cid}/social/package")
    assert response.status_code == 409
    assert memory.list_social_shares(pid, cid) == []
    assert not (tmp_path / "b1identity-read-ws" / pid / "premarketing" / cid / "social" / "social_post_manifest.json").exists()


def test_build_package_unreadable_is_409_without_fallback(monkeypatch, tmp_path):
    client, memory, pid = _client(monkeypatch, tmp_path, "b1unread")
    cid = _publish(client, pid)
    memory.update_campaign_site(
        pid, cid, "published",
        google_site_url="https://sites.google.com/view/架空-b1",
        site_publication_approved_at="2026-01-01T00:00:00+00:00",
        published_at="2026-01-02T00:00:00+00:00")
    monkeypatch.setattr(
        "app.web.resolve_channel_selection",
        lambda *_a, **_k: {"status": "unreadable", "channels": [],
                           "reason_ja": "媒体の選択を読めない"})
    response = client.post(
        f"/api/projects/{pid}/premarketing/campaigns/{cid}/social/package")
    assert response.status_code == 409, response.text
    assert "読めない" in response.text
    assert memory.list_social_shares(pid, cid) == []


def test_deselected_evidence_blocks_regeneration(monkeypatch, tmp_path):
    client, memory, pid = _client(monkeypatch, tmp_path, "b1deselect")
    cid = _publish(client, pid)
    memory.update_campaign_site(
        pid, cid, "published",
        google_site_url="https://sites.google.com/view/架空-b1",
        site_publication_approved_at="2026-01-01T00:00:00+00:00",
        published_at="2026-01-02T00:00:00+00:00")
    # まず全媒体で生成し、LINE にだけ公開証跡を登録する。
    first = client.post(
        f"/api/projects/{pid}/premarketing/campaigns/{cid}/social/package")
    assert first.status_code == 200, first.text
    memory.update_social_campaign_status(pid, cid, "draft_ready", "awaiting_approval")
    memory.update_social_campaign_status(pid, cid, "awaiting_approval", "approved")
    memory.register_social_evidence(
        pid, cid, "line", "https://line.me/R/ti/p/架空123")
    before = memory.get_social_share(pid, cid, "line")
    # X だけに選択を変えて再生成しようとすると拒否される。証跡は消えない。
    _select_only(memory.path, pid, "X")
    response = client.post(
        f"/api/projects/{pid}/premarketing/campaigns/{cid}/social/package")
    assert response.status_code == 409, response.text
    assert "line" in response.text
    after = memory.get_social_share(pid, cid, "line")
    assert after and after["evidence_url"] == before["evidence_url"]
    assert after["status"] == "evidence_registered"


def test_fictional_identity_blocks_publish_but_keeps_kit(monkeypatch, tmp_path):
    client, memory, pid = _client(monkeypatch, tmp_path, "b1fic")
    _select_only(memory.path, pid, "X")
    saved = register_public_identity(
        memory.path, project_id=pid, platform="X",
        handle="@架空-x", profile_url="https://social.example.invalid/x/架空",
        source_kind="fictional", actor="架空担当")
    register_channel_selection(
        memory.path, project_id=pid, platform="X", enabled=True,
        account_ref=saved["identity_id"], actor="架空担当")
    cid = _publish(client, pid)
    memory.update_campaign_site(
        pid, cid, "published",
        google_site_url="https://sites.google.com/view/架空-b1",
        site_publication_approved_at="2026-01-01T00:00:00+00:00",
        published_at="2026-01-02T00:00:00+00:00")
    response = client.post(
        f"/api/projects/{pid}/premarketing/campaigns/{cid}/social/package")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["identity_status"]["x"]["identity_status"] == "fictional"
    assert "架空" in body["identity_status"]["x"]["reason_ja"]
    assert "公開不可" in body["identity_status"]["x"]["reason_ja"]
    assert [item["channel"] for item in body["shares"]] == ["x"]


def test_unselected_shares_are_listed_not_deleted(monkeypatch, tmp_path):
    client, memory, pid = _client(monkeypatch, tmp_path, "b1drift")
    cid = _publish(client, pid)
    memory.update_campaign_site(
        pid, cid, "published",
        google_site_url="https://sites.google.com/view/架空-b1",
        site_publication_approved_at="2026-01-01T00:00:00+00:00",
        published_at="2026-01-02T00:00:00+00:00")
    first = client.post(
        f"/api/projects/{pid}/premarketing/campaigns/{cid}/social/package")
    assert first.status_code == 200, first.text
    # X だけに選択を変える。既存の共有行は消さず、差分として出す。
    _select_only(memory.path, pid, "X")
    drift = unselected_channel_drift(["x"], memory.list_social_shares(pid, cid))
    assert set(drift) == set(CHANNELS) - {"x"}
    remaining = memory.list_social_shares(pid, cid)
    assert len(remaining) == 6


def test_selected_channels_limit_goal_evidence(monkeypatch, tmp_path):
    import app.generic_goal_checks as checks
    _client(monkeypatch, tmp_path, "b1ev")
    from pathlib import Path as _P
    mem_path, memory, pid = _env(_P(str(tmp_path / "b1ev2")), "b1ev2")
    from types import SimpleNamespace
    from app.campaign_evidence import campaign_evidence
    campaign = {"id": "campaign-ev", "google_site_url": "",
                "site_publication_status": ""}
    shares = [
        {"campaign_id": "campaign-ev", "channel": "x",
         "status": "evidence_registered",
         "approved_at": "2026-01-01T00:00:00+00:00",
         "evidence_registered_at": "2026-01-02T00:00:00+00:00",
         "evidence_url": "https://x.com/架空/status/1",
         "id": "s-x"},
        {"campaign_id": "campaign-ev", "channel": "line",
         "status": "evidence_registered",
         "approved_at": "2026-01-01T00:00:00+00:00",
         "evidence_registered_at": "2026-01-02T00:00:00+00:00",
         "evidence_url": "https://line.me/架空/1",
         "id": "s-line"},
    ]
    all_events = campaign_evidence(campaign, shares, [], selected_channels=None)
    limited = campaign_evidence(campaign, shares, [], selected_channels=["x"])
    assert any(e.get("channel") == "x" for e in all_events)
    assert all(e.get("channel", "x") == "x"
               for e in limited if e.get("kind") == "social_post")
    assert not any(e.get("channel") == "line" and e.get("kind") == "social_post"
                   for e in limited)
    manager = SimpleNamespace(memory=memory)
    _select_only(memory.path, pid, "X")
    assert checks._selected_channels_for_project(manager, pid, None) == ["x"]
    _disable_all(memory.path, pid)
    assert checks._selected_channels_for_project(manager, pid, None) == []


def test_identity_matching_rules(tmp_path):
    mem_path, _memory, pid = _env(tmp_path, "identity-rules")
    _select_only(mem_path, pid, "X")
    only = register_public_identity(
        mem_path, project_id=pid, platform="X", handle="@only",
        profile_url="https://social.example.invalid/only",
        source_kind="registered", actor="架空担当")
    assert channel_identity_statuses(mem_path, pid, None, ["x"])["x"]["identity_status"] == "registered"
    register_public_identity(
        mem_path, project_id=pid, platform="X", handle="@second",
        profile_url="https://social.example.invalid/second",
        source_kind="registered", actor="架空担当")
    status = channel_identity_statuses(mem_path, pid, None, ["x"])["x"]
    assert status["identity_status"] == "ambiguous"
    assert "複数" in status["reason_ja"]
    register_channel_selection(
        mem_path, project_id=pid, platform="X", enabled=True,
        account_ref=only["identity_id"], actor="架空担当")
    assert channel_identity_statuses(mem_path, pid, None, ["x"])["x"]["identity_status"] == "registered"
    register_channel_selection(
        mem_path, project_id=pid, platform="X", enabled=True,
        account_ref="missing-id", actor="架空担当")
    assert channel_identity_statuses(mem_path, pid, None, ["x"])["x"]["identity_status"] == "unregistered"


def test_identity_retired_status(tmp_path):
    mem_path, _memory, pid = _env(tmp_path, "identity-retired")
    _select_only(mem_path, pid, "X")
    saved = register_public_identity(
        mem_path, project_id=pid, platform="X", handle="@retired",
        profile_url="https://social.example.invalid/retired",
        source_kind="registered", actor="架空担当")
    from app.contact_registry import retire_public_identity
    retire_public_identity(mem_path, pid, saved["identity_id"],
                           actor="架空担当", reason="失効試験")
    status = channel_identity_statuses(mem_path, pid, None, ["x"])["x"]
    assert status == {"identity_status": "retired", "reason_ja": "失効"}


def test_external_evidence_campaign_scope_and_unreadable(monkeypatch, tmp_path):
    import app.generic_goal_checks as checks
    from types import SimpleNamespace
    mem_path, memory, pid = _env(tmp_path, "evidence-scope")
    campaign = memory.create_campaign(pid, "架空", "架空対象", "架空提供", "架空CTA", "")
    cid = campaign["id"]
    _select_only(mem_path, pid, "X", campaign_id=cid)
    items = social_package({**campaign, "google_site_url": "https://sites.google.com/view/test"}, ["linkedin"])
    memory.save_social_drafts(pid, cid, items)
    memory.update_social_campaign_status(pid, cid, "draft_ready", "awaiting_approval")
    memory.update_social_campaign_status(pid, cid, "awaiting_approval", "approved")
    memory.register_social_evidence(pid, cid, "linkedin", "https://www.linkedin.com/posts/test")
    manager = SimpleNamespace(memory=memory)
    evidence = checks.collect_external_evidence(manager, pid)
    assert evidence["social"] == []
    assert not any(item.get("kind") == "social_post" for item in evidence["operations"])
    monkeypatch.setattr(
        "app.social_premarketing.resolve_channel_selection",
        lambda *_a, **_k: {"status": "unreadable", "channels": [], "reason_ja": "読取不能"})
    unreadable = checks.collect_external_evidence(manager, pid)
    assert unreadable["social"] == []
    assert unreadable["social_selection_unreadable"] is True
    assert "読取不能" in unreadable["social_selection_reason"]

