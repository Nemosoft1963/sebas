"""B2 公開前の版照合の受入試験。疑似PJ・実関数。外部通信なし。架空の値のみ。

- 登録→キット→承認→投稿画面を開く/URL登録が通る(binding_status: ok)。束縛の行にIDと
  SHA-256だけがあり、メールアドレス・ハンドルなどの値そのものは入らない。
- 承認後の (a)連絡先の版 (b)連絡先の失効 (c)媒体選択 (d)公開ID (e)manifest/キット の変更で、
  投稿画面を開く・URL登録は409 (PUBLICATION_BINDING_NOT_OK) になり、理由が具体的。
  共有行は awaiting_approval に戻り social_approval_invalidated が残る。再生成・再承認で進める。
- no_binding / unreadable / blocked で止まる。ok・stale に倒れない。
- 承認前のURL登録・不正ホスト・別媒体・投稿画面を開いただけ、では証跡にならない。
- 承認後の正しいURL登録だけが証跡になり、束縛のIDとSHA-256が共有行に残る。
- 承認の確定後に束縛の記録が失敗したら、承認を取り消して409にする。
- 束縛の元データを読めないときは承認を確定しない。
- 無効化の失敗は応答の detail に出す。GET の照合は読み取り専用。
- 完全削除は当該PJの束縛だけを消す。失敗は成功と報告しない。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.web as web
from app.contact_registry import (
    RegistryReadError,
    register_channel_selection,
    register_contact_route,
    register_public_identity,
    list_contact_routes,
    list_public_identities,
    retire_contact_route,
    verify_contact_route,
    verify_public_identity,
)
from app.memory.short_term import ShortTermMemory
from app.publication_binding import (
    list_bindings,
    purge_project_bindings,
    record_binding,
)
from app.requirement_item_ledger import item_ledger_db_path
from app.workspace_files import WorkspaceSandbox

CONTACT_VALUE = "b2-contact@example.invalid"
CONTACT_VALUE_V2 = "b2-contact-v2@example.invalid"
HANDLE = "@架空-b2"
HANDLE_V2 = "@架空-b2-改"
PROFILE = "https://social.example.invalid/b2/架空"
ROUTE_ID = "route-b2-contact"
IDENTITY_ID = "identity-b2-x"
SITE = "https://sites.google.com/view/架空-b2"
EVIDENCE_OK = "https://x.com/架空b2/status/1001"
EVIDENCE_BAD_HOST = "https://example.com/架空b2/status/1"
LINKEDIN_OK = "https://www.linkedin.com/posts/架空-b2"


# ----------------------------------------------------------------------------
# 疑似環境
# ----------------------------------------------------------------------------

@pytest.fixture()
def env(monkeypatch, tmp_path):
    mem_dir = tmp_path / "架空data" / "memory"
    mem_dir.mkdir(parents=True)
    mem = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem)
    pid = memory.create_project("架空B2疑似PJ", "架空コンテキスト")["id"]
    ws = tmp_path / "架空ws"
    ws.mkdir()
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "workspace", WorkspaceSandbox(ws))
    monkeypatch.setattr(web, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(web, "DATA_DIR", tmp_path / "架空data-web")
    return {"mem": mem, "memory": memory, "pid": pid, "ws": ws,
            "client": TestClient(web.app), "tmp": tmp_path}


def _register_base(env, identity_kind: str = "registered",
                   verify_contact: bool = True,
                   verify_identity: bool = True) -> None:
    mem, pid = env["mem"], env["pid"]
    register_contact_route(mem, project_id=pid, type="email",
                           public_value=CONTACT_VALUE, source_kind="registered",
                           actor="架空担当", route_id=ROUTE_ID)
    register_channel_selection(mem, project_id=pid, platform="X", enabled=True,
                               actor="架空担当")
    register_public_identity(mem, project_id=pid, platform="X", handle=HANDLE,
                             profile_url=PROFILE, source_kind=identity_kind,
                             actor="架空担当", identity_id=IDENTITY_ID)
    if verify_contact:
        verify_contact_route(mem, pid, ROUTE_ID, actor="架空担当", memo="疑似確認")
    if verify_identity and identity_kind == "registered":
        verify_public_identity(mem, pid, IDENTITY_ID, actor="架空担当", memo="疑似確認")


def _verify_current_public_inputs(env):
    mem, pid = env["mem"], env["pid"]
    routes = list_contact_routes(mem, pid)["current"]
    if not any(row.get("source_kind") in ("registered", "verified") for row in routes):
        register_contact_route(mem, project_id=pid, type="email",
                               public_value=CONTACT_VALUE_V2,
                               source_kind="registered", actor="架空担当",
                               route_id="route-b2-replacement")
        routes = list_contact_routes(mem, pid)["current"]
    for row in routes:
        if row.get("source_kind") == "registered":
            verify_contact_route(mem, pid, row["route_id"],
                                 actor="架空担当", memo="疑似確認")
    for row in list_public_identities(mem, pid)["current"]:
        if row.get("source_kind") == "registered":
            verify_public_identity(mem, pid, row["identity_id"],
                                   actor="架空担当", memo="疑似確認")

def _publish(env) -> str:
    pid = env["pid"]
    r = env["client"].post(
        f"/api/projects/{pid}/premarketing/campaigns",
        json={"title": "架空相談", "audience": "架空DX担当者",
              "offer": "架空課題を30分で整理します", "call_to_action": "架空診断を申し込む"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    env["memory"].update_campaign_site(
        pid, cid, "published", google_site_url=SITE,
        google_site_edit_url="https://sites.google.com/view/架空-b2/edit",
        published_at="2026-01-02T00:00:00+00:00",
        site_publication_approved_at="2026-01-01T00:00:00+00:00",
        landing_asset_version="v1", published_asset_version="v1")
    return cid


def _base(env, cid: str) -> str:
    return f"/api/projects/{env['pid']}/premarketing/campaigns/{cid}"


def _kit_and_approve(env, cid: str) -> dict:
    c = env["client"]
    base = _base(env, cid)
    r = c.post(f"{base}/social/package")
    assert r.status_code == 200, r.text
    r = c.post(f"{base}/social/request")
    assert r.status_code == 200, r.text
    r = c.post(f"{base}/social/approve")
    assert r.status_code == 200, r.text
    return r.json()


def _share(env, cid: str, channel: str = "x") -> dict:
    return env["memory"].get_social_share(env["pid"], cid, channel) or {}


def _status(env, cid: str, channel: str = "x") -> str:
    return str(_share(env, cid, channel).get("status") or "")


def _events(env) -> list[str]:
    return [e["kind"] for e in env["memory"].get_mission(env["pid"], event_limit=500)["events"]]


def _open(env, cid: str, channel: str = "x"):
    return env["client"].post(f"{_base(env, cid)}/social/{channel}/open")


def _register(env, cid: str, url: str, channel: str = "x"):
    return env["client"].post(f"{_base(env, cid)}/social/{channel}/evidence",
                              json={"public_post_url": url})


def _detail(resp) -> dict:
    body = resp.json()
    assert isinstance(body.get("detail"), dict), body
    return body["detail"]


def _approved_row(env, cid: str) -> dict:
    for row in list_bindings(env["mem"], env["pid"], cid)["bindings"]:
        if row["stage"] == "approved":
            return row
    raise AssertionError("approved binding row is missing")


def _kit_path(env, cid: str, name: str) -> Path:
    return env["ws"] / "projects" / env["pid"] / "premarketing" / cid / "social" / name


# ----------------------------------------------------------------------------
# 承認後の変更(5種類)
# ----------------------------------------------------------------------------

def _m_contact_version(env, cid):
    current = list_contact_routes(env["mem"], env["pid"])["current"]
    route = next((r for r in current if r.get("route_id") == ROUTE_ID), {})
    rid = ROUTE_ID if route.get("source_kind") == "registered" else ROUTE_ID + "-v2"
    register_contact_route(env["mem"], project_id=env["pid"], type="email",
                           public_value=CONTACT_VALUE_V2, source_kind="registered",
                           actor="架空担当", route_id=rid)


def _m_contact_retired(env, cid):
    retire_contact_route(env["mem"], env["pid"], ROUTE_ID,
                         actor="架空担当", reason="架空失効")


def _m_channel_off(env, cid):
    register_channel_selection(env["mem"], project_id=env["pid"], platform="X",
                               enabled=False, actor="架空担当")


def _m_identity(env, cid):
    register_public_identity(env["mem"], project_id=env["pid"], platform="X",
                             handle=HANDLE_V2, profile_url=PROFILE,
                             source_kind="registered", actor="架空担当",
                             identity_id=IDENTITY_ID)


def _m_manifest(env, cid):
    _kit_path(env, cid, "social_post_manifest.json").write_text(
        '{"tampered": true}\n', encoding="utf-8")


def _m_kit(env, cid):
    _kit_path(env, cid, "social_post_kit.md").write_text("# 改ざん\n", encoding="utf-8")


MUTATIONS = [
    ("contact_version", _m_contact_version, ("連絡先", "版が変わった")),
    ("contact_retired", _m_contact_retired, ("連絡先", "失効")),
    ("channel_selection", _m_channel_off, ("媒体選択が変わった",)),
    ("public_identity", _m_identity, ("公開ID", "版が変わった")),
    ("manifest_file", _m_manifest, ("公開packetが変わった",)),
    ("kit_file", _m_kit, ("承認後に成果物が変わった",)),
]


# ----------------------------------------------------------------------------
# 1. 正常系と束縛の中身
# ----------------------------------------------------------------------------

def test_happy_path_open_and_register_are_ok(env):
    _register_base(env)
    cid = _publish(env)
    approved = _kit_and_approve(env, cid)
    assert approved["binding_status"] == "ok", approved
    assert approved["binding_id"] > 0

    opened = _open(env, cid)
    assert opened.status_code == 200, opened.text
    assert opened.json()["binding_status"] == "ok"
    assert opened.json()["reasons"] == []
    assert _status(env, cid) == "composer_opened"

    registered = _register(env, cid, EVIDENCE_OK)
    assert registered.status_code == 200, registered.text
    body = registered.json()
    assert body["binding_status"] == "ok"
    assert body["status"] == "evidence_registered"
    assert body["reasons"] == []


def test_binding_rows_hold_ids_and_hashes_not_values(env):
    _register_base(env)
    cid = _publish(env)
    _kit_and_approve(env, cid)
    rows = list_bindings(env["mem"], env["pid"], cid)["bindings"]
    stages = {row["stage"] for row in rows}
    assert {"kit", "approved"} <= stages
    for row in rows:
        assert row["id"] > 0
        assert len(row["binding_sha256"]) == 64
        int(row["binding_sha256"], 16)  # 16進のSHA-256であること
    # 生の行を読み、値そのもの(メールアドレス・ハンドル・プロフィールURL)が入っていないこと。
    raw = sqlite3.connect(str(item_ledger_db_path(env["mem"])))
    try:
        text = "\n".join(str(r[0]) for r in raw.execute(
            "SELECT binding_json FROM publication_bindings WHERE project_id=?",
            (env["pid"],)).fetchall())
    finally:
        raw.close()
    assert text
    assert CONTACT_VALUE not in text
    assert HANDLE not in text
    assert PROFILE not in text
    assert "social.example.invalid" not in text


# ----------------------------------------------------------------------------
# 2. 承認後の5種類の変更
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("name, mutate, keywords", MUTATIONS,
                         ids=[m[0] for m in MUTATIONS])
def test_stale_after_approval_stops_open_and_register(env, name, mutate, keywords):
    _register_base(env, verify_contact=False, verify_identity=False)
    cid = _publish(env)
    _kit_and_approve(env, cid)
    assert _status(env, cid) == "approved"

    mutate(env, cid)

    registered = _register(env, cid, EVIDENCE_OK)
    assert registered.status_code == 409, registered.text
    detail = _detail(registered)
    assert detail["code"] == "PUBLICATION_BINDING_NOT_OK"
    assert detail["binding_status"] == "stale"
    assert any(all(k in reason for k in keywords) for reason in detail["reasons"]), detail["reasons"]
    assert detail["invalidation"]["invalidated"] is True
    assert detail["invalidation_failed"] is False
    assert _status(env, cid) == "awaiting_approval"
    assert "social_approval_invalidated" in _events(env)
    assert _share(env, cid)["evidence_url"] == ""

    opened = _open(env, cid)
    assert opened.status_code == 409, opened.text
    opened_detail = _detail(opened)
    assert opened_detail["code"] == "PUBLICATION_BINDING_NOT_OK"
    assert opened_detail["binding_status"] == "stale"
    assert any(all(k in reason for k in keywords) for reason in opened_detail["reasons"])
    assert _status(env, cid) == "awaiting_approval"

    if name != "channel_selection":
        # 再生成・再承認の後は進める(媒体選択を外した場合は投稿不要になるため対象外)。
        _verify_current_public_inputs(env)
        _kit_and_approve(env, cid)
        reopened = _open(env, cid)
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["binding_status"] == "ok"


def test_get_publication_binding_reports_stale_and_is_read_only(env):
    _register_base(env)
    cid = _publish(env)
    _kit_and_approve(env, cid)
    rows_before = len(list_bindings(env["mem"], env["pid"], cid)["bindings"])
    events_before = len(_events(env))
    url = f"/api/projects/{env['pid']}/premarketing/campaigns/{cid}/publication-binding"

    ok = env["client"].get(url)
    assert ok.status_code == 200, ok.text
    assert ok.json()["binding_status"] == "ok"
    assert ok.json()["reasons"] == []
    assert ok.json()["read_only"] is True

    _m_contact_version(env, cid)
    stale = env["client"].get(url)
    assert stale.status_code == 200, stale.text
    assert stale.json()["binding_status"] == "stale"
    assert stale.json()["reasons"]
    # GET は読むだけ。束縛も共有行も承認イベントも変えない。
    assert _status(env, cid) == "approved"
    assert len(list_bindings(env["mem"], env["pid"], cid)["bindings"]) == rows_before
    assert len(_events(env)) == events_before
    assert "social_approval_invalidated" not in _events(env)


# ----------------------------------------------------------------------------
# 3. no_binding / unreadable / blocked
# ----------------------------------------------------------------------------

def test_no_binding_old_approval_stops(env):
    _register_base(env)
    cid = _publish(env)
    env["client"].post(f"{_base(env, cid)}/social/package")
    env["client"].post(f"{_base(env, cid)}/social/request")
    # 束縛を記録しない古い承認(承認の経路を通らず状態だけ承認済みにする)。
    env["memory"].update_social_campaign_status(
        env["pid"], cid, "awaiting_approval", "approved")
    opened = _open(env, cid)
    assert opened.status_code == 409, opened.text
    detail = _detail(opened)
    assert detail["binding_status"] == "no_binding"
    assert any("承認の束縛が無い" in r for r in detail["reasons"])
    assert _status(env, cid) == "awaiting_approval"


def test_unreadable_registry_stops_and_never_ok(env, monkeypatch):
    _register_base(env)
    cid = _publish(env)
    _kit_and_approve(env, cid)
    import app.contact_registry as registry

    def _boom(*_a, **_k):
        raise RegistryReadError("row_unreadable", path_kind="relative",
                                sqlite_error="OperationalError")

    monkeypatch.setattr(registry, "list_contact_routes", _boom)
    opened = _open(env, cid)
    assert opened.status_code == 409, opened.text
    assert _detail(opened)["binding_status"] == "unreadable"
    registered = _register(env, cid, EVIDENCE_OK)
    assert registered.status_code == 409, registered.text
    assert _detail(registered)["binding_status"] == "unreadable"
    assert _status(env, cid) == "awaiting_approval"


def test_unreadable_manifest_stops_and_never_ok(env, monkeypatch):
    _register_base(env)
    cid = _publish(env)
    _kit_and_approve(env, cid)
    real_read_bytes = Path.read_bytes

    def _fake_read_bytes(self):
        if self.name == "social_post_manifest.json":
            raise OSError("injected manifest read failure")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _fake_read_bytes)
    opened = _open(env, cid)
    assert opened.status_code == 409, opened.text
    detail = _detail(opened)
    assert detail["binding_status"] == "unreadable"
    assert any("キットのハッシュ" in r for r in detail["reasons"])
    assert _status(env, cid) == "awaiting_approval"


def test_blocked_fictional_public_id_stops(env):
    _register_base(env, identity_kind="fictional")
    cid = _publish(env)
    approved = _kit_and_approve(env, cid)
    # 承認の記録自体は残る。投稿の段階で blocked にして止める。
    assert approved["binding_status"] == "blocked", approved
    opened = _open(env, cid)
    assert opened.status_code == 409, opened.text
    detail = _detail(opened)
    assert detail["binding_status"] == "blocked"
    assert any("架空" in r and "公開不可" in r for r in detail["reasons"])
    registered = _register(env, cid, EVIDENCE_OK)
    assert registered.status_code == 409, registered.text
    assert _detail(registered)["binding_status"] == "blocked"
    assert _status(env, cid) == "awaiting_approval"


# ----------------------------------------------------------------------------
# 4. 証跡の条件
# ----------------------------------------------------------------------------

def test_only_correct_post_after_approval_becomes_evidence(env):
    _register_base(env)
    cid = _publish(env)
    c = env["client"]
    c.post(f"{_base(env, cid)}/social/package")
    c.post(f"{_base(env, cid)}/social/request")

    # 承認前のURL登録は証跡にならない。
    early = _register(env, cid, EVIDENCE_OK)
    assert early.status_code == 409, early.text
    assert _detail(early)["binding_status"] == "no_binding"
    assert _share(env, cid)["evidence_url"] == ""
    assert _status(env, cid) == "awaiting_approval"

    approved = c.post(f"{_base(env, cid)}/social/approve")
    assert approved.status_code == 200, approved.text
    approved_row = _approved_row(env, cid)

    # 不正ホスト・選択媒体と異なる媒体は証跡にならない。
    bad_host = _register(env, cid, EVIDENCE_BAD_HOST)
    assert bad_host.status_code == 409, bad_host.text
    other_medium = _register(env, cid, LINKEDIN_OK, channel="linkedin")
    assert other_medium.status_code == 409, other_medium.text
    assert "選択された媒体" in other_medium.text
    assert _status(env, cid) == "approved"
    assert _share(env, cid)["evidence_url"] == ""

    # 投稿画面を開いただけでは証跡にならない。
    assert _open(env, cid).status_code == 200
    assert _status(env, cid) == "composer_opened"
    assert _share(env, cid)["evidence_url"] == ""
    assert _register(env, cid, EVIDENCE_BAD_HOST).status_code == 409
    assert _status(env, cid) == "composer_opened"

    # 承認後の正しいURL登録だけが証跡になり、束縛のIDとSHA-256を残す。
    good = _register(env, cid, EVIDENCE_OK)
    assert good.status_code == 200, good.text
    share = _share(env, cid)
    assert share["status"] == "evidence_registered"
    assert share["evidence_url"] == EVIDENCE_OK
    assert share["publication_binding_id"] == approved_row["id"]
    assert share["publication_binding_sha256"] == approved_row["binding_sha256"]


def test_later_ledger_change_does_not_rewrite_stored_evidence_binding(env):
    _register_base(env)
    cid = _publish(env)
    _kit_and_approve(env, cid)
    assert _open(env, cid).status_code == 200
    assert _register(env, cid, EVIDENCE_OK).status_code == 200
    before = _share(env, cid)
    assert before["publication_binding_id"] > 0

    _m_contact_version(env, cid)
    again = _register(env, cid, EVIDENCE_OK)
    assert again.status_code == 409, again.text
    assert _detail(again)["binding_status"] == "stale"
    after = _share(env, cid)
    assert after["status"] == "evidence_registered"
    assert after["evidence_url"] == EVIDENCE_OK
    assert after["publication_binding_id"] == before["publication_binding_id"]
    assert after["publication_binding_sha256"] == before["publication_binding_sha256"]
    # 証跡の行は無効化の対象にならない。
    assert "social_approval_invalidated" not in _events(env)


# ----------------------------------------------------------------------------
# 5. 承認の確定と束縛の記録
# ----------------------------------------------------------------------------

def test_approval_refused_when_binding_source_unreadable(env, monkeypatch):
    _register_base(env)
    cid = _publish(env)
    c = env["client"]
    c.post(f"{_base(env, cid)}/social/package")
    c.post(f"{_base(env, cid)}/social/request")
    import app.contact_registry as registry

    def _boom(*_a, **_k):
        raise RegistryReadError("row_unreadable", path_kind="relative",
                                sqlite_error="OperationalError")

    monkeypatch.setattr(registry, "list_contact_routes", _boom)
    approved = c.post(f"{_base(env, cid)}/social/approve")
    assert approved.status_code == 409, approved.text
    assert _status(env, cid) == "awaiting_approval"
    assert "social_publication_approved" not in _events(env)


@pytest.mark.parametrize("exc_type", [RuntimeError, sqlite3.OperationalError],
                         ids=["runtime", "sqlite"])
def test_record_binding_failure_after_approval_reverts_to_awaiting(env, monkeypatch, exc_type):
    _register_base(env)
    cid = _publish(env)
    c = env["client"]
    c.post(f"{_base(env, cid)}/social/package")
    c.post(f"{_base(env, cid)}/social/request")

    import app.publication_binding as binding

    def _fail(*_a, **_k):
        raise exc_type("injected binding write failure")

    monkeypatch.setattr(binding, "record_binding", _fail)
    approved = c.post(f"{_base(env, cid)}/social/approve")
    assert approved.status_code == 409, approved.text
    detail = _detail(approved)
    assert detail["code"] == "PUBLICATION_BINDING_WRITE_FAILED"
    assert detail["invalidation"]["invalidated"] is True
    assert detail["invalidation"]["reverted_to_awaiting"] is True
    # 承認済みのまま残さない。
    assert _status(env, cid) == "awaiting_approval"
    assert _share(env, cid)["approved_at"] in (None, "")
    assert "social_approval_invalidated" in _events(env)


# ----------------------------------------------------------------------------
# 6. 無効化の失敗は応答に出る
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["open", "register"])
def test_invalidation_write_failure_is_reported_in_response(env, monkeypatch, route):
    _register_base(env)
    cid = _publish(env)
    _kit_and_approve(env, cid)
    _m_contact_version(env, cid)
    real_add_event = env["memory"].add_event

    def _fail_invalidation_event(project_id, kind, message, *args, **kwargs):
        if kind == "social_approval_invalidated":
            raise sqlite3.OperationalError("injected audit write failure")
        return real_add_event(project_id, kind, message, *args, **kwargs)

    monkeypatch.setattr(env["memory"], "add_event", _fail_invalidation_event)
    if route == "open":
        resp = _open(env, cid)
    else:
        resp = _register(env, cid, EVIDENCE_OK)
    assert resp.status_code == 409, resp.text
    detail = _detail(resp)
    assert detail["binding_status"] == "stale"
    assert detail["invalidation"]["invalidated"] is False
    assert detail["invalidation"]["reason"]
    assert detail["invalidation_failed"] is True
    assert "無効化" in detail["message"]
    assert _share(env, cid)["evidence_url"] == ""


def test_invalidation_direct_write_failure_returns_not_invalidated():
    from app.web import _invalidate_social_approval_on_stale

    class _WriteFails:
        def list_social_shares(self, project_id, campaign_id):
            return [{"channel": "x", "variant": "primary", "status": "approved"}]

        def _connect(self):
            raise sqlite3.OperationalError("injected write failure")

        def add_event(self, *_a, **_k):
            raise AssertionError("must not reach the audit write")

    result = _invalidate_social_approval_on_stale(
        _WriteFails(), "架空pid", "架空cid", "stale", ["理由"])
    assert result["invalidated"] is False
    assert "無効化に失敗" in result["reason"]


# ----------------------------------------------------------------------------
# 7. 完全削除は当該PJの束縛だけを消す
# ----------------------------------------------------------------------------

def test_purge_project_bindings_removes_only_target_rows(env):
    other = env["memory"].create_project("架空別PJ", "")["id"]
    for pid in (env["pid"], other):
        record_binding(env["mem"], project_id=pid, campaign_id="架空cid",
                       stage="kit", binding={"schema_version": 1, "note": "架空"},
                       actor="")
    removed = purge_project_bindings(env["mem"], env["pid"])
    assert removed["publication_bindings"] == 1
    assert list_bindings(env["mem"], env["pid"], "架空cid")["bindings"] == []
    assert len(list_bindings(env["mem"], other, "架空cid")["bindings"]) == 1


def test_purge_without_binding_table_is_zero_not_error(env):
    # 連絡先の登録だけで作られた台帳には publication_bindings 表が無い。
    register_channel_selection(env["mem"], project_id=env["pid"], platform="X",
                               enabled=True, actor="架空担当")
    assert purge_project_bindings(env["mem"], env["pid"]) == {"publication_bindings": 0}


def _delete_and_request_purge(env, pid: str):
    """疑似PJを論理削除し、保持期間を過ぎた状態にして完全削除の直前まで進める。"""
    from app.project_delete import (
        build_delete_request_preview,
        build_purge_preview,
        delete_db_path,
        request_delete,
    )
    ws_root = env["ws"]
    backup_root = env["tmp"] / "架空退避"
    preview = build_delete_request_preview(env["memory"].path, pid, ws_root, {})
    assert preview["can_delete"] is True, preview["blocked_reasons"]
    name = str((preview.get("project") or {}).get("name") or "")
    requested = request_delete(
        env["memory"].path, pid, preview_token=preview["preview_token"],
        project_name=name, actor="架空担当", workspace_root=ws_root,
        backup_root=backup_root, retention_days=0)
    assert requested["ok"] is True, requested
    db = sqlite3.connect(str(delete_db_path(env["memory"].path)))
    try:
        with db:
            db.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                       ("2000-01-01T00:00:00+00:00", pid))
    finally:
        db.close()
    purge_preview = build_purge_preview(env["memory"].path, pid, ws_root)
    assert purge_preview["can_purge"] is True, purge_preview["blocked_reasons"]
    return purge_preview, name, backup_root


def test_full_delete_removes_only_target_bindings(env):
    from app.project_delete import purge_deleted
    other = env["memory"].create_project("架空別PJ", "")["id"]
    for pid in (env["pid"], other):
        record_binding(env["mem"], project_id=pid, campaign_id="架空cid",
                       stage="kit", binding={"schema_version": 1, "note": "架空"},
                       actor="")
    preview, name, backup_root = _delete_and_request_purge(env, env["pid"])
    result = purge_deleted(
        env["memory"].path, env["pid"], purge_token=preview["purge_token"],
        project_name=str(preview.get("project_name") or name), actor="架空担当",
        workspace_root=env["ws"], backup_root=backup_root)
    assert result["ok"] is True, result
    assert list_bindings(env["mem"], env["pid"], "架空cid")["bindings"] == []
    assert len(list_bindings(env["mem"], other, "架空cid")["bindings"]) == 1


def test_full_delete_binding_failure_is_not_reported_as_success(env):
    from app.project_delete import purge_deleted
    other = env["memory"].create_project("架空別PJ", "")["id"]
    for pid in (env["pid"], other):
        record_binding(env["mem"], project_id=pid, campaign_id="架空cid",
                       stage="kit", binding={"schema_version": 1, "note": "架空"},
                       actor="")
    preview, name, backup_root = _delete_and_request_purge(env, env["pid"])
    # 束縛の行だけ削除を拒む(表は残る)。残存は孤児検査で失敗として返る。
    ledger = sqlite3.connect(str(item_ledger_db_path(env["mem"])))
    try:
        with ledger:
            ledger.execute(
                "CREATE TRIGGER b2_block_binding_delete BEFORE DELETE ON publication_bindings "
                "BEGIN SELECT RAISE(ABORT, 'injected binding delete failure'); END")
    finally:
        ledger.close()
    result = purge_deleted(
        env["memory"].path, env["pid"], purge_token=preview["purge_token"],
        project_name=str(preview.get("project_name") or name), actor="架空担当",
        workspace_root=env["ws"], backup_root=backup_root)
    assert result["ok"] is False, result
    assert result["code"] == "ORPHANS_REMAIN"
    # 残った束縛は消えたことにしない。他PJの行も残る。
    assert len(list_bindings(env["mem"], env["pid"], "架空cid")["bindings"]) == 1
    assert len(list_bindings(env["mem"], other, "架空cid")["bindings"]) == 1


@pytest.mark.parametrize("unverified", ["contact", "identity"])
def test_registered_but_unverified_public_input_blocks_posting(env, unverified):
    """人のSNS文案承認後も、登録だけの公開値を投稿可能にしない。"""
    _register_base(env, verify_contact=unverified != "contact",
                   verify_identity=unverified != "identity")
    cid = _publish(env)
    _kit_and_approve(env, cid)
    opened = _open(env, cid)
    assert opened.status_code == 409, opened.text
    assert _detail(opened)["binding_status"] == "blocked"
    evidence = _register(env, cid, EVIDENCE_OK)
    assert evidence.status_code == 409, evidence.text
