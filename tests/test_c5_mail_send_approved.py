"""C5: mail.send_approved の受入試験(疑似PJ・偽のSMTP・実メール送信なし)。

- 実際のメールを送らない・網へ出ない。smtplib.SMTP/SMTP_SSL を
  monkeypatch の偽クラスに差し替える。
- 宛先は予約ドメイン(.invalid 等)だけを使う。
"""
import smtplib
import sqlite3
import tempfile
import threading
from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.channel_skill_mail as mail_skill
import app.channel_skill_mail_send as mail_send
import app.channel_skills as skills


SECRET = "ZZ-DUMMY-SMTP-PASS-12345"


def _memory_path():
    tmp = tempfile.NamedTemporaryFile(prefix="c5mem", suffix=".db", delete=False)
    tmp.close()
    return Path(tmp.name)


def _cleanup(memory):
    try:
        memory.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        Path(str(memory) + ".item_ledger.sqlite3").unlink(missing_ok=True)
    except OSError:
        pass


class _FakeSMTP:
    instances: list = []
    lock = threading.Lock()

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls = []
        self.login_calls = []
        with _FakeSMTP.lock:
            _FakeSMTP.instances.append(self)
        self._behavior = dict(getattr(type(self), "_behavior", {}) or {})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False

    def starttls(self):
        self.calls.append("starttls")
        if self._behavior.get("starttls_error"):
            raise self._behavior["starttls_error"]
        return (220, b"ok")

    def login(self, username, password):
        self.login_calls.append((username, password))
        if self._behavior.get("auth_error"):
            raise self._behavior["auth_error"]
        return (235, b"ok")

    def send_message(self, message):
        self.calls.append(("send_message", message))
        if self._behavior.get("send_error"):
            raise self._behavior["send_error"]
        if self._behavior.get("refused"):
            return {"x@example.invalid": (550, b"refused")}
        return {}

    def close(self):
        self.calls.append("close")


class _FakeSMTPSSL(_FakeSMTP):
    pass


def _install_fake(monkeypatch, behavior=None):
    _FakeSMTP.instances = []
    _FakeSMTP._behavior = dict(behavior or {})
    _FakeSMTPSSL.instances = _FakeSMTP.instances
    _FakeSMTPSSL._behavior = dict(behavior or {})
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)


def _send_count():
    total = 0
    for inst in list(_FakeSMTP.instances):
        for call in inst.calls:
            if isinstance(call, tuple) and call[0] == "send_message":
                total += 1
    return total


def _last_message():
    for inst in reversed(list(_FakeSMTP.instances)):
        for call in reversed(inst.calls):
            if isinstance(call, tuple) and call[0] == "send_message":
                return call[1]
    return None


def _with_smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "dummy-user")
    monkeypatch.setenv("SMTP_FROM", "sender@example.invalid")
    monkeypatch.setenv("SMTP_PASSWORD", SECRET)
    monkeypatch.setenv("SMTP_STARTTLS", "true")
    monkeypatch.setenv("SMTP_SSL", "false")


def _clear_smtp_env(monkeypatch):
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME",
                 "SMTP_FROM", "SMTP_PASSWORD", "SMTP_STARTTLS", "SMTP_SSL"):
        monkeypatch.delenv(name, raising=False)


def _setup_mode(memory, pid, mode="demo_test"):
    from app.contact_registry import save_operating_mode
    save_operating_mode(memory, project_id=pid, mode=mode, actor="担当",
                        reason="疑似運用区分")


def _make_action(memory_path, pid, *, target="a@example.invalid",
                 subject="件名", body="本文"):
    from app.memory.short_term import ShortTermMemory
    holder = ShortTermMemory(memory_path, read_only=False)
    try:
        action = holder.create_action(
            pid, "email", target, f"{subject}\n{body}", None)
        holder.update_action(pid, action["id"], "approved")
        saved = holder.get_action(pid, action["id"])
    finally:
        try:
            holder.close()
        except (AttributeError, OSError, ValueError, TypeError):
            pass
    return dict(saved)


def _ledger_text(memory):
    from app.requirement_item_ledger import item_ledger_db_path
    path = item_ledger_db_path(memory)
    if not path.exists():
        return ""
    db = sqlite3.connect(str(path))
    try:
        out = []
        for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            try:
                for row in db.execute(f"SELECT * FROM {name}").fetchall():
                    out.append(f"{name}:{row}")
            except sqlite3.Error:
                continue
        return "\n".join(out)
    finally:
        db.close()


def test_binding_recorded_no_raw_values_and_requires_reapproval(monkeypatch):
    skills.clear_adapters()
    memory = _memory_path()
    pid = "pj-c5-bind"
    try:
        _with_smtp_env(monkeypatch)
        _setup_mode(memory, pid, "demo_test")
        action = _make_action(memory, pid)
        saved = mail_send.record_approve_binding(
            memory, pid, dict(action), "demo_test", actor="担当")
        assert saved["project_id"] == pid
        assert saved["action_id"] == action["id"]
        assert len(saved["binding_sha256"]) == 64
        assert saved["to_domain"] == "example.invalid"
        assert saved["operating_mode"] == "demo_test"
        assert saved["version"] == 1
        blob = _ledger_text(memory)
        assert "a@example.invalid" not in blob
        assert "本文" not in blob
        assert "件名" not in blob
        # 束縛の無い古い承認は拒否され『再承認が必要』。
        other = _make_action(memory, pid, target="b@example.invalid")
        with pytest.raises(mail_send.MailSendRefused) as excinfo:
            mail_send.run_mail_send_approved(memory, pid, other["id"])
        assert excinfo.value.reason == "needs_reapproval"
    finally:
        skills.clear_adapters()
        _cleanup(memory)


def test_approve_execute_accepted_and_message_id(monkeypatch):
    skills.clear_adapters()
    memory = _memory_path()
    pid = "pj-c5-ok"
    try:
        _install_fake(monkeypatch)
        _with_smtp_env(monkeypatch)
        _setup_mode(memory, pid, "demo_test")
        skills.register_adapter("mail.send_approved", "SMTP")
        action = _make_action(memory, pid)
        mail_send.record_approve_binding(
            memory, pid, dict(action), "demo_test", actor="担当")
        result = mail_send.run_mail_send_approved(memory, pid, action["id"])
        assert result["send_state"] == "accepted"
        assert result["delivery_confirmed"] is False
        assert "受理" in result["message"]
        assert "配達済み" not in result["message"]
        assert _send_count() == 1
        message = _last_message()
        assert isinstance(message, EmailMessage)
        assert str(message["Message-ID"] or "")
        assert str(message["Message-ID"] or "") == result["message_id"]
        assert result["binding_id"] > 0
        rows = mail_send.list_attempts(memory, pid, action["id"])
        assert len(rows) == 2
        assert {r["state"] for r in rows} == {"sending", "accepted"}
        assert rows[1]["message_id"] == result["message_id"]
        # 共通契約: accepted -> succeeded の実行・証拠。
        assert result["run"]["status"] == "succeeded"
        assert result["run"]["capability_id"] == "mail.send_approved"
        assert result["evidence"] is not None
        assert result["evidence"]["capability_id"] == "mail.send_approved"
        assert result["evidence"]["provider"] == "smtp"
        assert result["evidence"]["untrusted"] is True
        assert "a@example.invalid" not in str(result["evidence"])
        # C3の返信候補づけ: Message-ID 一致で候補になる(確定しない)。
        reply_mid = result["message_id"]
        basis = mail_skill._candidate_basis_for(
            {"in_reply_to": reply_mid, "references": [],
             "from": "x@example.invalid", "subject": "y"},
            {"kind": "email", "target": "x@example.invalid",
             "content": "件名\ny", "project_id": pid, "id": action["id"]},
            sent_message_ids=mail_send.list_message_ids(memory, pid, action["id"]))
        assert basis == "in_reply_to_match"
    finally:
        skills.clear_adapters()
        _cleanup(memory)


def test_recheck_rejects_changed_content_and_missing_binding(monkeypatch):
    skills.clear_adapters()
    memory = _memory_path()
    pid = "pj-c5-recheck"
    try:
        _install_fake(monkeypatch)
        _with_smtp_env(monkeypatch)
        _setup_mode(memory, pid, "demo_test")
        skills.register_adapter("mail.send_approved", "SMTP")
        action = _make_action(memory, pid)
        mail_send.record_approve_binding(
            memory, pid, dict(action), "demo_test", actor="担当")
        # 承認後に内容を変更したら送信されず理由『承認後に内容が変更された』。
        from app.memory.short_term import ShortTermMemory
        holder = ShortTermMemory(memory, read_only=False)
        try:
            with holder._connect() as db:
                db.execute(
                    "UPDATE project_actions SET content=? WHERE id=?",
                    ("件名\n変えた本文", action["id"]))
        finally:
            try:
                holder.close()
            except (AttributeError, OSError, ValueError, TypeError):
                pass
        with pytest.raises(mail_send.MailSendRefused) as excinfo:
            mail_send.run_mail_send_approved(memory, pid, action["id"])
        assert excinfo.value.reason == "binding_changed"
        assert _send_count() == 0
        # 運用区分 demo_test で実在ドメイン宛は拒否。
        real = _make_action(memory, pid, target="someone@example.co.jp")
        mail_send.record_approve_binding(
            memory, pid, dict(real), "demo_test", actor="担当")
        with pytest.raises(mail_send.MailSendRefused) as excinfo2:
            mail_send.run_mail_send_approved(memory, pid, real["id"])
        assert excinfo2.value.reason == "real_domain_blocked"
        assert _send_count() == 0
        # 運用区分が読めないときは拒否。
        from app.requirement_item_ledger import item_ledger_db_path
        ledger = item_ledger_db_path(memory)
        db = sqlite3.connect(str(ledger))
        try:
            db.execute("DELETE FROM operating_modes WHERE project_id=?", (pid,))
            db.commit()
        finally:
            db.close()
        unreadable = _make_action(memory, pid, target="c@example.invalid")
        # 承認時の記録は production 相当で残るが、実行時は mode が空で不一致になる。
        mail_send.record_approve_binding(
            memory, pid, dict(unreadable), "production", actor="担当")
        with pytest.raises(mail_send.MailSendRefused):
            mail_send.run_mail_send_approved(memory, pid, unreadable["id"])
        # ヘッダ注入(件名に改行)は拒否。
        # 運用区分を再設定する(直前で削除したため mode_unreadable になる)。
        # 注: 本文は content の最初の改行で件名と分けるため、件名に LF は入らない。
        # 件名に単独の CR を入れて、ヘッダ検査そのものを通す。
        _setup_mode(memory, pid, "demo_test")
        injected = _make_action(memory, pid, target="d@example.invalid",
                                subject="件名\rBcc: x@example.invalid")
        mail_send.record_approve_binding(
            memory, pid, dict(injected), "demo_test", actor="担当")
        with pytest.raises(mail_send.MailSendRefused) as excinfo3:
            mail_send.run_mail_send_approved(memory, pid, injected["id"])
        assert excinfo3.value.reason == "header_injection"
        assert _send_count() == 0
        # SMTP設定なしは needs_configuration(送信しない)。
        _clear_smtp_env(monkeypatch)
        nocfg = _make_action(memory, pid, target="e@example.invalid")
        mail_send.record_approve_binding(
            memory, pid, dict(nocfg), "demo_test", actor="担当")
        with pytest.raises(mail_send.MailSendNotConfigured):
            mail_send.run_mail_send_approved(memory, pid, nocfg["id"])
        assert _send_count() == 0
    finally:
        skills.clear_adapters()
        _cleanup(memory)


def test_result_classification_failed_and_unknown(monkeypatch):
    skills.clear_adapters()
    memory = _memory_path()
    pid = "pj-c5-class"
    try:
        _with_smtp_env(monkeypatch)
        _setup_mode(memory, pid, "demo_test")
        skills.register_adapter("mail.send_approved", "SMTP")
        # 接続失敗・認証失敗・宛先拒否 -> failed(確実に未送信)。
        _install_fake(monkeypatch, {"send_error": smtplib.SMTPRecipientsRefused({})})
        action = _make_action(memory, pid)
        mail_send.record_approve_binding(
            memory, pid, dict(action), "demo_test", actor="担当")
        refused = mail_send.run_mail_send_approved(memory, pid, action["id"])
        assert refused["send_state"] == "failed"
        assert refused["run"]["status"] == "failed"
        assert refused["run"]["status"] != "no_results"
        # 送信中の切断 -> unknown(failed でも accepted でもない)・自動再送なし。
        _install_fake(monkeypatch, {
            "send_error": smtplib.SMTPServerDisconnected("lost")})
        action2 = _make_action(memory, pid, target="u@example.invalid")
        mail_send.record_approve_binding(
            memory, pid, dict(action2), "demo_test", actor="担当")
        unknown = mail_send.run_mail_send_approved(memory, pid, action2["id"])
        assert unknown["send_state"] == "unknown"
        assert unknown["run"]["status"] == "unknown"
        assert unknown["run"]["status"] not in ("failed", "succeeded")
        assert unknown["delivery_confirmed"] is False
        # unknown の後の再実行も拒否される(自動再送なし)。
        with pytest.raises(mail_send.MailSendRefused) as excinfo:
            mail_send.run_mail_send_approved(memory, pid, action2["id"])
        assert excinfo.value.reason == "already_attempted"
        # 受理 -> accepted。
        _install_fake(monkeypatch)
        action3 = _make_action(memory, pid, target="v@example.invalid")
        mail_send.record_approve_binding(
            memory, pid, dict(action3), "demo_test", actor="担当")
        ok = mail_send.run_mail_send_approved(memory, pid, action3["id"])
        assert ok["send_state"] == "accepted"
    finally:
        skills.clear_adapters()
        _cleanup(memory)


def test_double_send_blocked_and_failed_allows_new_binding(monkeypatch):
    skills.clear_adapters()
    memory = _memory_path()
    pid = "pj-c5-double"
    try:
        _install_fake(monkeypatch)
        _with_smtp_env(monkeypatch)
        _setup_mode(memory, pid, "demo_test")
        skills.register_adapter("mail.send_approved", "SMTP")
        action = _make_action(memory, pid)
        mail_send.record_approve_binding(
            memory, pid, dict(action), "demo_test", actor="担当")
        first = mail_send.run_mail_send_approved(memory, pid, action["id"])
        assert first["send_state"] == "accepted"
        assert _send_count() == 1
        # 同じ action_id を2回実行 -> 2回目は拒否で送信回数1。
        with pytest.raises(mail_send.MailSendRefused) as excinfo:
            mail_send.run_mail_send_approved(memory, pid, action["id"])
        assert excinfo.value.reason == "already_attempted"
        assert _send_count() == 1
        # failed(送信前の失敗)の後は新しい束縛があるときだけ再実行できる。
        _install_fake(monkeypatch, {"send_error": smtplib.SMTPRecipientsRefused({})})
        action2 = _make_action(memory, pid, target="w@example.invalid")
        mail_send.record_approve_binding(
            memory, pid, dict(action2), "demo_test", actor="担当")
        bad = mail_send.run_mail_send_approved(memory, pid, action2["id"])
        assert bad["send_state"] == "failed"
        before = _send_count()
        with pytest.raises(mail_send.MailSendRefused) as excinfo2:
            mail_send.run_mail_send_approved(memory, pid, action2["id"])
        assert excinfo2.value.reason == "already_attempted"
        assert _send_count() == before
        # 新しく承認し直したとき(新しい束縛)だけ次の試行を許す。
        mail_send.record_approve_binding(
            memory, pid, dict(action2), "demo_test", actor="担当2")
        _install_fake(monkeypatch)
        retry = mail_send.run_mail_send_approved(memory, pid, action2["id"])
        assert retry["send_state"] == "accepted"
    finally:
        skills.clear_adapters()
        _cleanup(memory)


def test_concurrent_double_send_only_once(monkeypatch):
    skills.clear_adapters()
    memory = _memory_path()
    pid = "pj-c5-conc"
    try:
        _install_fake(monkeypatch)
        _with_smtp_env(monkeypatch)
        _setup_mode(memory, pid, "demo_test")
        skills.register_adapter("mail.send_approved", "SMTP")
        action = _make_action(memory, pid)
        mail_send.record_approve_binding(
            memory, pid, dict(action), "demo_test", actor="担当")
        errors: list = []
        barrier = threading.Barrier(2)

        def _run():
            try:
                barrier.wait(timeout=5)
                mail_send.run_mail_send_approved(memory, pid, action["id"])
            except mail_send.MailSendRefused as exc:
                errors.append(exc.reason)
            except Exception as exc:  # noqa: BLE001 - 試験の記録用
                errors.append(type(exc).__name__)

        threads = [threading.Thread(target=_run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert _send_count() == 1
        assert "already_attempted" in errors
    finally:
        skills.clear_adapters()
        _cleanup(memory)


def test_human_outcome_record_and_no_resend(monkeypatch):
    skills.clear_adapters()
    memory = _memory_path()
    pid = "pj-c5-outcome"
    try:
        _install_fake(monkeypatch, {
            "send_error": smtplib.SMTPServerDisconnected("lost")})
        _with_smtp_env(monkeypatch)
        _setup_mode(memory, pid, "demo_test")
        skills.register_adapter("mail.send_approved", "SMTP")
        action = _make_action(memory, pid)
        mail_send.record_approve_binding(
            memory, pid, dict(action), "demo_test", actor="担当")
        unknown = mail_send.run_mail_send_approved(memory, pid, action["id"])
        assert unknown["send_state"] == "unknown"
        # 担当者・メモなしは422相当(ValueError)。
        with pytest.raises(ValueError):
            mail_send.record_send_outcome(
                memory, pid, action["id"], outcome="delivered",
                actor="", memo="メモ")
        with pytest.raises(ValueError):
            mail_send.record_send_outcome(
                memory, pid, action["id"], outcome="delivered",
                actor="担当", memo="")
        saved = mail_send.record_send_outcome(
            memory, pid, action["id"], outcome="delivered",
            actor="担当", memo="人が到着を確認")
        assert saved["outcome"] == "delivered"
        # 記録しても再送は許可されない(新しいアクションの承認が要る)。
        with pytest.raises(mail_send.MailSendRefused):
            mail_send.run_mail_send_approved(memory, pid, action["id"])
        # unknown でない試行への記録は409相当(MailSendRefused)。
        _install_fake(monkeypatch)
        action2 = _make_action(memory, pid, target="z@example.invalid")
        mail_send.record_approve_binding(
            memory, pid, dict(action2), "demo_test", actor="担当")
        ok = mail_send.run_mail_send_approved(memory, pid, action2["id"])
        assert ok["send_state"] == "accepted"
        with pytest.raises(mail_send.MailSendRefused):
            mail_send.record_send_outcome(
                memory, pid, action2["id"], outcome="delivered",
                actor="担当", memo="メモ")
    finally:
        skills.clear_adapters()
        _cleanup(memory)


def test_contract_state_and_secret_scrubbing(monkeypatch, caplog):
    import logging
    skills.clear_adapters()
    memory = _memory_path()
    pid = "pj-c5-contract"
    try:
        _install_fake(monkeypatch)
        _with_smtp_env(monkeypatch)
        _setup_mode(memory, pid, "demo_test")
        assert skills.skill_state(memory, pid, "mail.send_approved") == "needs_configuration"
        # 接続済み = アダプタ登録 かつ SMTP設定の存在。アダプタ未登録なら偽。
        assert skills.maturity(memory, pid, "mail.send_approved")["connected"] is False
        skills.register_adapter("mail.send_approved", "SMTP")
        viewed = skills.maturity(memory, pid, "mail.send_approved")
        assert viewed["connected"] is True
        assert viewed["field_verified"] is False
        caplog.set_level(logging.INFO)
        action = _make_action(memory, pid)
        mail_send.record_approve_binding(
            memory, pid, dict(action), "demo_test", actor="担当")
        with caplog.at_level(logging.INFO):
            result = mail_send.run_mail_send_approved(memory, pid, action["id"])
        assert result["send_state"] == "accepted"
        assert SECRET not in _ledger_text(memory)
        assert SECRET not in str(result)
        assert SECRET not in caplog.text
        # 承認が無いと needs_approval。
        other = "pj-c5-needs-approval"
        skills.register_adapter("mail.send_approved", "SMTP")
        assert skills.skill_state(memory, other, "mail.send_approved") == "needs_approval"
        # SMTP設定がないと connected は偽。
        _clear_smtp_env(monkeypatch)
        assert skills.maturity(memory, pid, "mail.send_approved")["connected"] is False
    finally:
        skills.clear_adapters()
        _cleanup(memory)


def _client_with_project(monkeypatch, tmp_path):
    import app.web as web
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox
    data_dir = tmp_path / "data"
    mem = data_dir / "memory" / "conversations.db"
    mem.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(web, "DATA_DIR", data_dir)
    monkeypatch.setattr(web, "DB_PATH", mem)
    monkeypatch.setattr(web, "memory", ShortTermMemory(mem))
    monkeypatch.setattr(web, "WORKSPACE_ROOT", tmp_path / "ws")
    monkeypatch.setattr(web, "workspace", WorkspaceSandbox(tmp_path / "ws"))
    created = TestClient(web.app).post(
        "/api/projects", json={"name": "C5 API"})
    assert created.status_code == 201, created.text
    real_pid = str(created.json().get("id") or "")
    assert real_pid
    return TestClient(web.app), real_pid


def test_api_approve_execute_attempts_outcome_and_isolation(monkeypatch, tmp_path):
    skills.clear_adapters()
    client, pid = _client_with_project(monkeypatch, tmp_path)
    try:
        import app.channel_skill_mail_send as mod
        import app.web as web
        _install_fake(monkeypatch)
        _with_smtp_env(monkeypatch)
        from app.contact_registry import save_operating_mode
        save_operating_mode(web.DB_PATH, project_id=pid, mode="demo_test",
                            actor="担当", reason="疑似運用区分")
        skills.register_adapter("mail.send_approved", "SMTP")
        created = client.post(
            f"/api/projects/{pid}/actions",
            json={"kind": "email", "target": "a@example.invalid",
                  "content": "件名\n本文"})
        assert created.status_code == 200, created.text
        aid = str(created.json().get("id") or "")
        approved = client.post(f"/api/projects/{pid}/actions/{aid}/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["mail_send_binding_id"] > 0
        executed = client.post(f"/api/projects/{pid}/actions/{aid}/execute")
        assert executed.status_code == 200, executed.text
        payload = executed.json()
        assert payload["status"] == "executed"
        assert payload["send_state"] == "accepted"
        assert payload["delivery_confirmed"] is False
        assert "受理" in str(payload.get("evidence") or "")
        assert "配達済み" not in str(payload.get("evidence") or "")
        listed = client.get(
            f"/api/projects/{pid}/channel-skills/mail.send_approved/attempts")
        assert listed.status_code == 200, listed.text
        rows = listed.json()["attempts"]
        assert len(rows) == 2
        assert "a@example.invalid" not in listed.text
        assert "本文" not in listed.text
        # 2回目は拒否(既存の応答キーは保つ)。
        again = client.post(f"/api/projects/{pid}/actions/{aid}/execute")
        assert again.status_code == 409, again.text
        # 他PJの行が出ない・完全削除で当該PJだけ消える。
        other = client.post("/api/projects", json={"name": "別PJ"})
        other_pid = str(other.json().get("id") or "")
        from app.requirement_item_ledger import item_ledger_db_path
        ledger = item_ledger_db_path(web.DB_PATH)
        db = sqlite3.connect(str(ledger))
        try:
            mod.record_approve_binding(
                web.DB_PATH, other_pid,
                {"id": "x", "kind": "email", "target": "b@example.invalid",
                 "content": "件名\n本文", "project_id": other_pid},
                "demo_test", actor="担当")
            # C5の表は kind=other のため汎用の _delete_entry_rows では消えない。
            # 完全削除(purge_deleted)が呼ぶ補助purgeで当該PJの行だけ消す。
            removed = mod.purge_project_mail_send(web.DB_PATH, pid)
            assert removed["mail_send_bindings"] >= 1
            assert removed["mail_send_attempts"] >= 2
            left = db.execute(
                "SELECT COUNT(*) FROM mail_send_bindings WHERE project_id=?",
                (pid,)).fetchone()[0]
            kept = db.execute(
                "SELECT COUNT(*) FROM mail_send_bindings WHERE project_id=?",
                (other_pid,)).fetchone()[0]
            assert int(left) == 0
            assert int(kept) == 1
        finally:
            db.close()
    finally:
        skills.clear_adapters()


def test_api_send_outcome_validation(monkeypatch, tmp_path):
    skills.clear_adapters()
    client, pid = _client_with_project(monkeypatch, tmp_path)
    try:
        import app.web as web
        _install_fake(monkeypatch, {
            "send_error": smtplib.SMTPServerDisconnected("lost")})
        _with_smtp_env(monkeypatch)
        from app.contact_registry import save_operating_mode
        save_operating_mode(web.DB_PATH, project_id=pid, mode="demo_test",
                            actor="担当", reason="疑似運用区分")
        skills.register_adapter("mail.send_approved", "SMTP")
        created = client.post(
            f"/api/projects/{pid}/actions",
            json={"kind": "email", "target": "q@example.invalid",
                  "content": "件名\n本文"})
        aid = str(created.json().get("id") or "")
        assert client.post(f"/api/projects/{pid}/actions/{aid}/approve").status_code == 200
        executed = client.post(f"/api/projects/{pid}/actions/{aid}/execute")
        assert executed.status_code == 200, executed.text
        assert executed.json()["send_state"] == "unknown"
        bad = client.post(
            f"/api/projects/{pid}/actions/{aid}/send-outcome",
            json={"outcome": "delivered", "actor": "", "memo": "メモ"})
        assert bad.status_code == 422, bad.text
        good = client.post(
            f"/api/projects/{pid}/actions/{aid}/send-outcome",
            json={"outcome": "delivered", "actor": "担当",
                  "memo": "人が確認"})
        assert good.status_code == 201, good.text
        assert good.json()["outcome"]["outcome"] == "delivered"
    finally:
        skills.clear_adapters()


def test_unresolved_sending_blocks_new_binding(monkeypatch):
    """結果行の無い sending(送信中・記録失敗の可能性)は、新しい承認があっても送らない。"""
    skills.clear_adapters()
    memory = _memory_path()
    pid = "pj-c5-dangling"
    try:
        _install_fake(monkeypatch)
        _with_smtp_env(monkeypatch)
        _setup_mode(memory, pid, "demo_test")
        skills.register_adapter("mail.send_approved", "SMTP")
        action = _make_action(memory, pid)
        mail_send.record_approve_binding(
            memory, pid, dict(action), "demo_test", actor="担当")
        # 結果行の無い開始記録(送信中に落ちた状態)を直接作る。
        with mail_send._connect_rw(memory) as db:
            db.execute(
                "INSERT INTO mail_send_attempts(attempt_id, project_id, action_id,"
                " state, message_id, binding_id, started_at, finished_at,"
                " error_kind, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("dangling", pid, action["id"], "sending",
                 "<dangling@example.invalid>", 1, 0.0, None, "", 0.0))
            db.commit()
        # 新しい承認(新しい束縛)でも、結果が分からない送信があるので送らない。
        mail_send.record_approve_binding(
            memory, pid, dict(action), "demo_test", actor="担当2")
        with pytest.raises(mail_send.MailSendRefused) as excinfo:
            mail_send.run_mail_send_approved(memory, pid, action["id"])
        assert excinfo.value.reason == "already_attempted"
        assert _send_count() == 0
    finally:
        skills.clear_adapters()
        _cleanup(memory)
