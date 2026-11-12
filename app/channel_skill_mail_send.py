"""C5: mail.send_approved の承認版の束縛・送信直前の再照合・結果分類・二重送信防止。

- 実通信は既存のSMTP送信と同じ形で行う。試験では smtplib.SMTP/SMTP_SSL を
  monkeypatch の偽クラスに差し替え、実際のメールを送らない・網へ出ない。
- 承認時に宛先・件名・本文・運用区分の正規化SHA-256を追記型で記録する。
  値そのもの(宛先・本文)は保存しない(ハッシュと宛先ドメインだけ)。
- 送信直前に再照合し、1つでも満たさなければ送信しない(理由つきで拒否)。
- SMTP受理は配達・既読・返信の証明ではない(『受理』と書き『配達済み』と書かない)。
- 結果不明のときに自動で再送しない。二重送信を防ぐ(1つの承認で1回)。
- 資格情報の値は台帳・応答・ログ・例外・証拠に出さない(存在だけ確認する)。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import smtplib
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path
from typing import Any

from app.requirement_item_ledger import (
    _ensure_tables as _ensure_item_tables,
    classify_ledger_path_kind,
    item_ledger_db_path,
    ledger_readonly_uri,
)

LOGGER = logging.getLogger(__name__)

CAPABILITY_ID = "mail.send_approved"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_RESERVED_EXACT = frozenset({"example.com", "example.org", "example.net"})
_RESERVED_SUFFIX = (".invalid", ".test", ".example")

SEND_STATES = frozenset({"sending", "accepted", "failed", "unknown"})
BLOCKING_STATES = frozenset({"sending", "accepted", "unknown"})
OUTCOMES = frozenset({"delivered", "not_delivered", "unconfirmed"})

ACCEPTED_MESSAGE = (
    "SMTPサーバーが受理した(配達・既読・返信の証明ではない)")


class MailSendRefused(ValueError):
    """書込・外部呼出の前の拒否(束縛・再照合・運用区分・形式・設定・二重実行)。"""

    def __init__(self, reason: str = "", detail: str = "") -> None:
        super().__init__(reason or detail or "mail send refused")
        self.reason = str(reason or "")
        self.detail = str(detail or "")[:500]


class MailSendNotConfigured(MailSendRefused):
    def __init__(self, reason: str = "needs_configuration",
                 detail: str = "") -> None:
        super().__init__(reason, detail)
        self.reason = str(reason or "needs_configuration")


class MailSendReadError(RuntimeError):
    """台帳の読取失敗。『束縛なし』『試行なし』とは区別する。"""

    def __init__(self, kind: str, *, path_kind: str = "",
                 sqlite_error: str = "", detail: str = "") -> None:
        super().__init__(f"mail send read failed: {kind}")
        self.kind = str(kind or "")
        self.path_kind = str(path_kind or "")
        self.sqlite_error = str(sqlite_error or "")
        self.detail = str(detail or "")[:500]


class MailSendWriteError(RuntimeError):
    """台帳の書込失敗。呼出側は承認の確定・送信の成功として扱わない。"""


def _type_name(exc: BaseException) -> str:
    try:
        return str(type(exc).__name__ or "Error")
    except Exception:
        return "Error"


# ----------------------------------------------------------------------------
# 台帳3表(束縛・試行・結果記録)。追記型。値そのものは保存しない。
# ----------------------------------------------------------------------------

BINDINGS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS mail_send_bindings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    binding_sha256 TEXT NOT NULL,
    to_domain TEXT NOT NULL DEFAULT '',
    to_sha256 TEXT NOT NULL DEFAULT '',
    subject_sha256 TEXT NOT NULL DEFAULT '',
    body_sha256 TEXT NOT NULL DEFAULT '',
    operating_mode TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL)"""

ATTEMPTS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS mail_send_attempts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    state TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    binding_id INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    finished_at REAL,
    error_kind TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(action_id, attempt_id))"""

OUTCOMES_TABLE_DDL = """CREATE TABLE IF NOT EXISTS mail_send_outcomes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL)"""


def ensure_mail_send_tables(db: sqlite3.Connection) -> None:
    _ensure_item_tables(db)
    db.execute(BINDINGS_TABLE_DDL)
    db.execute(ATTEMPTS_TABLE_DDL)
    db.execute(OUTCOMES_TABLE_DDL)
    db.execute("""CREATE INDEX IF NOT EXISTS idx_mail_send_bindings_action
        ON mail_send_bindings(project_id, action_id, id DESC)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_mail_send_attempts_action
        ON mail_send_attempts(project_id, action_id, id DESC)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_mail_send_outcomes_action
        ON mail_send_outcomes(project_id, action_id, id DESC)""")


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(db_path: Path) -> threading.Lock:
    key = str(db_path)
    with _locks_guard:
        held = _locks.get(key)
        if held is None:
            held = threading.Lock()
            _locks[key] = held
        return held


@contextmanager
def _connect_rw(memory_path: str | Path):
    path = item_ledger_db_path(memory_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            ensure_mail_send_tables(db)
        yield db
    finally:
        db.close()


@contextmanager
def _connect_ro(memory_path: str | Path):
    path = item_ledger_db_path(memory_path)
    kind = classify_ledger_path_kind(path)
    try:
        db = sqlite3.connect(ledger_readonly_uri(path), uri=True, timeout=15)
        db.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise MailSendReadError(
            "connect_failed", path_kind=kind,
            sqlite_error=_type_name(exc)) from exc
    try:
        yield db
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _table_names(db: sqlite3.Connection) -> set[str]:
    try:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.Error as exc:
        raise MailSendReadError(
            "table_unreadable", sqlite_error=_type_name(exc)) from exc
    return {str(row[0]) for row in rows}


# ----------------------------------------------------------------------------
# 資格情報の扱い(値は保存・表示・記録しない。存在だけ確認する)
# ----------------------------------------------------------------------------

def _secret_values() -> list[str]:
    found: list[str] = []
    for name in ("SMTP_PASSWORD", "SMTP_USERNAME", "SMTP_FROM", "SMTP_HOST"):
        try:
            value = os.environ.get(name, "")
        except (OSError, TypeError, AttributeError):
            continue
        if isinstance(value, str) and len(value) >= 4:
            found.append(value)
    return found


def _scrubbed_text(text: str) -> str:
    cleaned = str(text or "")
    for secret in _secret_values():
        if secret and secret in cleaned:
            cleaned = cleaned.replace(secret, "[withheld]")
    return cleaned[:500]


def _scrubbed_error(exc: BaseException) -> str:
    try:
        blob = str(exc)[:500]
    except (AttributeError, TypeError, ValueError):
        return _type_name(exc)
    return _scrubbed_text(blob)


# ----------------------------------------------------------------------------
# 宛先・件名・本文の正規化と束縛ハッシュ
# ----------------------------------------------------------------------------

def _split_action_content(action: dict) -> tuple[str, str, str]:
    """既存の _send_approved_email と同じ切り分け(件名/本文)。

    content の1行目が件名、残りが本文。改行が無ければ既定の件名を使う。
    """
    content = str(action.get("content") or "")
    subject, separator, body = content.partition("\n")
    if not separator:
        subject, body = "Local Coworkからのご連絡", subject
    subject = subject.removeprefix("件名:").strip()[:200]
    return subject, body.strip(), content


def _recipient_of(action: dict) -> str:
    _name, addr = parseaddr(str(action.get("target") or ""))
    return str(addr or "").strip().lower()


def _domain_of(recipient: str) -> str:
    text = str(recipient or "").strip().lower()
    if "@" not in text:
        return ""
    return text.rsplit("@", 1)[1].strip()


def _is_reserved_domain(domain: str) -> bool:
    text = str(domain or "").strip().lower()
    if not text:
        return False
    if text in _RESERVED_EXACT:
        return True
    for exact in _RESERVED_EXACT:
        if text.endswith("." + exact):
            return True
    for suffix in _RESERVED_SUFFIX:
        if text.endswith(suffix):
            return True
    return False


def compute_binding(action: dict, operating_mode: str) -> dict:
    """承認時の束縛の元データ。正規化してSHA-256を取る。値そのものは返さない。"""
    recipient = _recipient_of(action)
    subject, body, _raw = _split_action_content(action)
    mode = str(operating_mode or "")
    canonical = "\n".join((recipient, subject, body, mode))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "binding_sha256": digest,
        "to_domain": _domain_of(recipient),
        "to_sha256": hashlib.sha256(recipient.encode("utf-8")).hexdigest(),
        "subject_sha256": hashlib.sha256(subject.encode("utf-8")).hexdigest(),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "operating_mode": mode,
        "recipient": recipient,
        "subject": subject,
        "body": body,
    }


def _binding_view(row: dict) -> dict:
    return {
        "id": int(row.get("id") or 0),
        "project_id": str(row.get("project_id") or ""),
        "action_id": str(row.get("action_id") or ""),
        "binding_sha256": str(row.get("binding_sha256") or ""),
        "to_domain": str(row.get("to_domain") or ""),
        "to_sha256": str(row.get("to_sha256") or ""),
        "subject_sha256": str(row.get("subject_sha256") or ""),
        "body_sha256": str(row.get("body_sha256") or ""),
        "operating_mode": str(row.get("operating_mode") or ""),
        "approved_at": str(row.get("approved_at") or ""),
        "actor": str(row.get("actor") or ""),
        "version": int(row.get("version") or 0),
        "created_at": row.get("created_at"),
    }


def _attempt_view(row: dict) -> dict:
    try:
        binding_id = int(row.get("binding_id") or 0)
    except (TypeError, ValueError):
        binding_id = 0
    return {
        "id": int(row.get("id") or 0),
        "attempt_id": str(row.get("attempt_id") or ""),
        "project_id": str(row.get("project_id") or ""),
        "action_id": str(row.get("action_id") or ""),
        "state": str(row.get("state") or ""),
        "message_id": str(row.get("message_id") or ""),
        "binding_id": int(binding_id),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "error_kind": str(row.get("error_kind") or ""),
        "created_at": row.get("created_at"),
    }


def _outcome_view(row: dict) -> dict:
    return {
        "id": int(row.get("id") or 0),
        "project_id": str(row.get("project_id") or ""),
        "action_id": str(row.get("action_id") or ""),
        "attempt_id": str(row.get("attempt_id") or ""),
        "outcome": str(row.get("outcome") or ""),
        "actor": str(row.get("actor") or ""),
        "memo": str(row.get("memo") or ""),
        "created_at": row.get("created_at"),
    }


# ----------------------------------------------------------------------------
# 承認時の束縛の記録(承認を確定する前に元データを読めることを確認する)
# ----------------------------------------------------------------------------

def record_approve_binding(memory_path: str | Path, project_id: str,
                           action: dict, operating_mode: str,
                           actor: str = "") -> dict:
    """承認版の束縛を追記する。値そのものは保存しない。

    失敗したら MailSendWriteError(呼出側は承認を確定しない・元に戻す)。
    """
    pid = str(project_id or "").strip()
    if not pid:
        raise MailSendWriteError("project_id is required")
    if not isinstance(action, dict) or not str(action.get("id") or ""):
        raise MailSendWriteError("action is required")
    if str(action.get("kind") or "") != "email":
        raise MailSendWriteError("binding is for email actions only")
    aid = str(action.get("id") or "")
    try:
        computed = compute_binding(action, operating_mode)
    except (AttributeError, TypeError, ValueError) as exc:
        raise MailSendWriteError(
            f"binding source is unreadable: {_type_name(exc)}") from exc
    if not computed["recipient"] or "@" not in computed["recipient"]:
        raise MailSendWriteError("binding source has no valid recipient")
    approved_at = datetime.now(timezone.utc).isoformat()
    who = str(actor or "")[:100]
    path = item_ledger_db_path(memory_path)
    guard = _lock_for(path)
    with guard:
        try:
            with _connect_rw(memory_path) as db:
                prev = db.execute(
                    "SELECT * FROM mail_send_bindings "
                    "WHERE project_id=? AND action_id=? "
                    "ORDER BY version DESC, id DESC LIMIT 1",
                    (pid, aid)).fetchone()
                if prev is not None:
                    try:
                        version = int(dict(prev).get("version") or 0) + 1
                    except (TypeError, ValueError):
                        version = 1
                    if version < 1:
                        version = 1
                else:
                    version = 1
                now = time.time()
                cursor = db.execute(
                    """INSERT INTO mail_send_bindings(
                        project_id, action_id, binding_sha256, to_domain,
                        to_sha256, subject_sha256, body_sha256,
                        operating_mode, approved_at, actor, version,
                        created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, aid, computed["binding_sha256"],
                     computed["to_domain"][:200],
                     computed["to_sha256"], computed["subject_sha256"],
                     computed["body_sha256"], computed["operating_mode"][:32],
                     approved_at, who, int(version), now),
                )
                db.commit()
                kept = db.execute(
                    "SELECT * FROM mail_send_bindings WHERE id=?",
                    (int(cursor.lastrowid),)).fetchone()
        except (OSError, sqlite3.Error, ValueError, TypeError,
                AttributeError) as exc:
            raise MailSendWriteError(
                f"binding write failed: {_type_name(exc)}") from exc
    if kept is None:
        raise MailSendWriteError("binding write failed: unreadable row")
    return _binding_view(dict(kept))


def get_latest_binding(memory_path: str | Path, project_id: str,
                       action_id: str) -> dict | None:
    """最新の束縛。無いときはNone。読取失敗は例外(『束縛なし』に倒さない)。"""
    pid = str(project_id or "").strip()
    aid = str(action_id or "").strip()
    if not pid or not aid:
        raise ValueError("project_id and action_id are required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return None
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "mail_send_bindings" not in names:
            return None
        try:
            row = db.execute(
                "SELECT * FROM mail_send_bindings "
                "WHERE project_id=? AND action_id=? "
                "ORDER BY id DESC LIMIT 1",
                (pid, aid)).fetchone()
        except sqlite3.Error as exc:
            raise MailSendReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        if row is None:
            return None
        return _binding_view(dict(row))


def list_attempts(memory_path: str | Path, project_id: str,
                  action_id: str = "") -> list[dict]:
    """送信の試行の一覧(当該PJだけ)。宛先・本文を出さず状態とハッシュだけ。"""
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return []
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "mail_send_attempts" not in names:
            return []
        query = ("SELECT a.*, b.to_domain AS to_domain, b.binding_sha256 "
                 "AS binding_sha256 FROM mail_send_attempts a "
                 "LEFT JOIN mail_send_bindings b ON b.id=a.binding_id "
                 "WHERE a.project_id=?")
        args: list[Any] = [pid]
        if str(action_id or "").strip():
            query += " AND a.action_id=?"
            args.append(str(action_id).strip())
        query += " ORDER BY a.id"
        try:
            rows = db.execute(query, tuple(args)).fetchall()
        except sqlite3.Error as exc:
            raise MailSendReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
    out: list[dict] = []
    for item in rows:
        kept = dict(item)
        view = _attempt_view(kept)
        view["to_domain"] = str(kept.get("to_domain") or "")
        view["binding_sha256"] = str(kept.get("binding_sha256") or "")
        out.append(view)
    return out


def list_message_ids(memory_path: str | Path, project_id: str,
                     action_id: str) -> list[str]:
    """そのアクションの送信済みMessage-IDの一覧(C3の返信照合用)。

    読取失敗は空にせず例外(呼出側が『照合不能』として扱う)。
    """
    rows = list_attempts(memory_path, project_id, action_id)
    return [str(r.get("message_id") or "") for r in rows
            if str(r.get("message_id") or "")]


def smtp_configured() -> bool:
    """SMTP設定の存在だけを確認する(値は見ない・保存しない)。"""
    try:
        host = os.environ.get("SMTP_HOST", "")
        sender = os.environ.get("SMTP_FROM", "")
    except (OSError, TypeError, AttributeError):
        return False
    return bool(isinstance(host, str) and host.strip()
                and isinstance(sender, str) and sender.strip())


def mail_send_connection_view(memory_path: str | Path,
                              project_id: str) -> dict:
    """接続の要否判定用。読取失敗は readable=False(設定なしに倒さない)。"""
    pid = str(project_id or "").strip()
    try:
        import app.channel_skills as _skills
        adapter_present = bool(_skills.has_adapter(CAPABILITY_ID))
    except (ImportError, AttributeError, TypeError, ValueError):
        adapter_present = False
    if not bool(adapter_present) or not smtp_configured():
        return {"configured": False, "readable": True,
                "adapter_present": bool(adapter_present),
                "smtp_ready": bool(smtp_configured()),
                "reason": "not_connected"}
    return {"configured": True, "readable": True,
            "adapter_present": True, "smtp_ready": True,
            "reason": "connected"}


# ----------------------------------------------------------------------------
# 送信直前の再照合・分類・追記
# ----------------------------------------------------------------------------

def _read_action(memory_path: str | Path, project_id: str,
                 action_id: str) -> dict:
    from app.memory.short_term import ShortTermMemory
    resolved = Path(str(memory_path or ""))
    db_path = resolved if resolved.suffix else resolved.parent / "conversations.db"
    try:
        holder = ShortTermMemory(db_path, read_only=True)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise MailSendRefused(
            "unreadable",
            f"action is unreadable: {_type_name(exc)}") from None
    try:
        try:
            action = holder.get_action(str(project_id or ""),
                                       str(action_id or ""))
        except (OSError, ValueError, TypeError, AttributeError, KeyError,
                sqlite3.Error) as exc:
            raise MailSendRefused(
                "unreadable",
                f"action is unreadable: {_type_name(exc)}") from None
    finally:
        try:
            holder.close()
        except (AttributeError, OSError, ValueError, TypeError):
            pass
    if not isinstance(action, dict) or not action:
        raise MailSendRefused("action_not_found", "action is not found")
    if str(action.get("project_id") or "") != str(project_id or ""):
        raise MailSendRefused("action_not_found", "action is not found")
    return dict(action)


def _read_operating_mode(memory_path: str | Path,
                         project_id: str) -> str:
    from app.contact_registry import current_operating_mode
    try:
        current = current_operating_mode(memory_path, str(project_id or ""))
    except Exception as exc:
        raise MailSendRefused(
            "mode_unreadable",
            f"operating mode is unreadable: {_type_name(exc)}") from None
    if not isinstance(current, dict):
        raise MailSendRefused("mode_unreadable",
                              "operating mode is unreadable")
    mode = str(current.get("mode") or "")
    if mode in ("demo_test", "production"):
        return mode
    return ""


def _checked_shape(computed: dict) -> None:
    recipient = str(computed.get("recipient") or "")
    subject = str(computed.get("subject") or "")
    body = str(computed.get("body") or "")
    if not recipient or "@" not in recipient:
        raise MailSendRefused("address_invalid", "recipient is invalid")
    if not EMAIL_RE.fullmatch(recipient):
        raise MailSendRefused("address_invalid", "recipient is invalid")
    for field, name in ((recipient, "recipient"), (subject, "subject")):
        if "\n" in field or "\r" in field:
            raise MailSendRefused("header_injection",
                                  f"{name} must not contain line breaks")
    try:
        sender = str(os.environ.get("SMTP_FROM", "") or "")
    except (OSError, TypeError, AttributeError):
        sender = ""
    if "\n" in sender or "\r" in sender:
        raise MailSendRefused("header_injection",
                              "sender must not contain line breaks")
    if not subject:
        raise MailSendRefused("subject_invalid", "subject is required")
    if not body:
        raise MailSendRefused("body_invalid", "body is required")


def _record_refusal_run(memory_path: str | Path, project_id: str,
                        reason: str, detail: str,
                        operating_mode: str) -> None:
    try:
        import app.channel_skills as _skills
        _skills.record_run(
            memory_path, project_id=str(project_id or ""),
            capability_id=CAPABILITY_ID, status="failed",
            purpose="承認済みメール送信の再照合",
            operating_mode=str(operating_mode or "")
            if str(operating_mode or "") in ("demo_test", "production") else "",
            adapter="smtp",
            params_summary={"rejected": str(reason or "")},
            error_kind=_scrubbed_text(f"{reason}: {detail}")[:200],
            evidence_count=0, completed_ok=False)
    except (OSError, ValueError, TypeError, AttributeError,
            sqlite3.Error) as exc:
        LOGGER.warning("mail send refusal run unwritable: %s",
                       _type_name(exc))


def _refuse(memory_path: str | Path, project_id: str, reason: str,
            detail: str, operating_mode: str = "") -> MailSendRefused:
    _record_refusal_run(memory_path, project_id, reason, detail,
                        operating_mode)
    if reason == "needs_configuration":
        return MailSendNotConfigured(reason, detail)
    return MailSendRefused(reason, detail)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _attempt_gate(rows: list[dict], binding_id: int) -> str:
    """送信の試行の可否。空文字なら送ってよい。空でなければ拒否の理由。

    - accepted / unknown の試行が1つでもあれば送らない(受理済みは再送しない。
      結果不明は自動で再送しない)。
    - 開始記録(sending)に、同じ message_id の結果行が無いものは送らない。
      送信中・記録失敗で未送信とは言い切れないため、新しい束縛があっても許さない。
    - failed(送信前の失敗で確実に未送信)は、新しい束縛(より大きい ID)のときだけ
      次の試行を許す。sending と結果行は同じ message_id で対応づける。
    """
    finished_ids = {str(r.get("message_id") or "") for r in rows
                    if str(r.get("state") or "") != "sending"}
    failed_bindings: list[int] = []
    for row in rows:
        state = str(row.get("state") or "")
        message_id = str(row.get("message_id") or "")
        if state in ("accepted", "unknown"):
            return "受理済みまたは結果不明の試行があるため送らない"
        if state == "sending" and (not message_id
                                   or message_id not in finished_ids):
            return "結果が記録されていない送信があるため送らない(結果不明)"
        if state == "failed":
            failed_bindings.append(_as_int(row.get("binding_id")))
    if failed_bindings and int(binding_id) <= max(failed_bindings):
        return "新しい承認(新しい束縛)が無いため送らない"
    return ""


def run_mail_send_approved(memory_path: str | Path, project_id: str,
                           action_id: str, *,
                           operating_mode: str | None = None,
                           actor: str = "") -> dict:
    """承認済みメールを送信直前に再照合して送る。

    戻り値は {"send_state", "run", "evidence", "reasons",
    "delivery_confirmed", "message", "message_id", "binding_id",
    "attempt_id"}。拒否は MailSendRefused(呼出側が409/422にする)。
    自動再送はしない。例外を握りつぶして成功・失敗に倒さない。
    """
    import app.channel_skills as _skills

    pid = str(project_id or "").strip()
    aid = str(action_id or "").strip()
    if not pid:
        raise MailSendRefused("project_invalid", "project_id is required")
    if not aid:
        raise MailSendRefused("action_invalid", "action_id is required")

    # (a)アクションが存在し kind='email'・status='approved'。
    action = _read_action(memory_path, pid, aid)
    if str(action.get("kind") or "") != "email":
        raise _refuse(memory_path, pid, "kind_unsupported",
                      "自動実行できるのはemailです")
    if str(action.get("status") or "") != "approved":
        raise _refuse(memory_path, pid, "not_approved",
                      "人間が承認済みのアクションだけを実行できます")

    # 運用区分(読めなければ拒否。未設定は空として扱う)。
    if operating_mode is None:
        mode = _read_operating_mode(memory_path, pid)
    else:
        mode = str(operating_mode or "")
        if mode not in ("demo_test", "production"):
            mode = ""

    # (b)承認の束縛が存在する(束縛の無い古い承認は再承認が必要)。
    try:
        binding = get_latest_binding(memory_path, pid, aid)
    except MailSendReadError as exc:
        raise _refuse(memory_path, pid, "binding_unreadable",
                      f"承認の束縛を読めない: {exc.kind}",
                      mode) from None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise _refuse(memory_path, pid, "binding_unreadable",
                      f"承認の束縛を読めない: {_type_name(exc)}",
                      mode) from None
    if binding is None:
        raise _refuse(memory_path, pid, "needs_reapproval",
                      "承認の束縛が無いため再承認が必要です", mode)

    # (c)現在の内容の再計算ハッシュが承認時と一致する。
    try:
        computed = compute_binding(action, mode)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _refuse(memory_path, pid, "binding_unreadable",
                      f"承認の束縛を読めない: {_type_name(exc)}",
                      mode) from None
    if str(computed.get("binding_sha256") or "") != str(
            binding.get("binding_sha256") or ""):
        raise _refuse(memory_path, pid, "binding_changed",
                      "承認後に内容が変更された", mode)

    # (e)宛先・件名の形式検査(ヘッダ注入の拒否・本文の空)。
    try:
        _checked_shape(computed)
    except MailSendRefused as exc:
        raise _refuse(memory_path, pid, str(exc.reason or "shape_invalid"),
                      str(exc.detail or ""), mode) from None

    # (d)運用区分の検査。
    domain = str(computed.get("to_domain") or "")
    reserved = _is_reserved_domain(domain)
    if mode == "demo_test":
        if not reserved:
            raise _refuse(memory_path, pid, "real_domain_blocked",
                          "demo_test では実在の宛先へ送らない", mode)
    elif mode in ("production", ""):
        if reserved:
            raise _refuse(memory_path, pid, "reserved_domain_blocked",
                          "production では予約ドメインへ送らない", mode)
    else:
        raise _refuse(memory_path, pid, "mode_unreadable",
                      "運用区分が読めない", mode)

    # (f)SMTPの接続設定の存在だけを確認する(値は見ない)。
    if not smtp_configured():
        raise _refuse(memory_path, pid, "needs_configuration",
                      "SMTP_HOST・SMTP_FROMの設定がありません", mode)

    # (g)この action_id に既に送信の試行が無い。
    try:
        rows = list_attempts(memory_path, pid, aid)
    except MailSendReadError as exc:
        raise _refuse(memory_path, pid, "attempts_unreadable",
                      f"送信の試行を読めない: {exc.kind}", mode) from None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise _refuse(memory_path, pid, "attempts_unreadable",
                      f"送信の試行を読めない: {_type_name(exc)}",
                      mode) from None
    gate = _attempt_gate(rows, _as_int(binding.get("id")))
    if gate:
        raise _refuse(memory_path, pid, "already_attempted", gate, mode)

    # 送信の前に sending 行を他と競合しない形で書く(トランザクション+ロック)。
    try:
        sender = str(os.environ.get("SMTP_FROM", "") or "").strip()
    except (OSError, TypeError, AttributeError):
        sender = ""
    from_domain = sender.rsplit("@", 1)[1].strip().lower() \
        if "@" in sender else "localhost"
    try:
        message_id = make_msgid(domain=from_domain or "localhost")
    except (TypeError, ValueError, AttributeError):
        message_id = make_msgid()
    message_id = str(message_id or "").strip()
    attempt_id = uuid.uuid4().hex
    started = time.time()
    binding_id = 0
    try:
        binding_id = int(binding.get("id") or 0)
    except (TypeError, ValueError):
        binding_id = 0
    path = item_ledger_db_path(memory_path)
    guard = _lock_for(path)
    with guard:
        try:
            with _connect_rw(memory_path) as db:
                existing = db.execute(
                    "SELECT * FROM mail_send_attempts "
                    "WHERE project_id=? AND action_id=?",
                    (pid, aid)).fetchall()
                gate_now = _attempt_gate(
                    [_attempt_view(dict(item)) for item in existing],
                    int(binding_id))
                if gate_now:
                    raise MailSendRefused("already_attempted", gate_now)
                db.execute(
                    """INSERT INTO mail_send_attempts(
                        attempt_id, project_id, action_id, state,
                        message_id, binding_id, started_at, finished_at,
                        error_kind, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (attempt_id, pid, aid, "sending", message_id,
                     int(binding_id), started, None, "", time.time()),
                )
                db.commit()
        except MailSendRefused:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError,
                AttributeError) as exc:
            raise MailSendRefused(
                "attempt_unwritable",
                f"送信の試行を記録できない: {_type_name(exc)}") from None

    recipient = str(computed.get("recipient") or "")
    subject = str(computed.get("subject") or "")
    body = str(computed.get("body") or "")
    label = "smtp"

    def _finish(state: str, error_kind: str) -> dict:
        finished = time.time()
        with guard:
            with _connect_rw(memory_path) as db:
                db.execute(
                    """INSERT INTO mail_send_attempts(
                        attempt_id, project_id, action_id, state,
                        message_id, binding_id, started_at, finished_at,
                        error_kind, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex, pid, aid, state, message_id,
                     int(binding_id), started, finished,
                     _scrubbed_text(error_kind)[:200], time.time()),
                )
                db.commit()
        return {"finished": finished}

    # 送信前の失敗(接続・STARTTLS・認証・宛先の拒否)→ failed。
    # 送信中・送信後の結果不明→ unknown(自動再送しない)。受理→ accepted。
    try:
        try:
            host = str(os.environ.get("SMTP_HOST", "") or "").strip()
            port = int(str(os.environ.get("SMTP_PORT", "587") or "587").strip())
            username = str(os.environ.get("SMTP_USERNAME", "") or "").strip()
            sender_check = str(os.environ.get("SMTP_FROM", "") or username).strip()
        except (OSError, TypeError, AttributeError, ValueError) as exc:
            raise RuntimeError(
                f"SMTP settings are unreadable: {_type_name(exc)}") from None
        if not host or not sender_check:
            raise RuntimeError("SMTP_HOST、SMTP_FROM、有効な送信先が必要です")
        try:
            use_ssl = str(os.environ.get("SMTP_SSL", "false") or "") \
                .strip().lower() in {"1", "true", "yes"}
        except (OSError, TypeError, AttributeError):
            use_ssl = False
        smtp_type = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        try:
            client = smtp_type(host, port, timeout=30)
        except (smtplib.SMTPConnectError, smtplib.SMTPException,
                OSError, socket.error) as exc:
            _finish("failed", f"connection_failed: {_type_name(exc)}")
            return _classified(memory_path, pid, aid, binding, attempt_id,
                               message_id, started, "failed",
                               f"connection_failed: {_type_name(exc)}",
                               mode, recipient, subject, body)
        with client:
            if not use_ssl:
                try:
                    starttls = str(os.environ.get("SMTP_STARTTLS", "true")
                                   or "").strip().lower() \
                        in {"1", "true", "yes"}
                except (OSError, TypeError, AttributeError):
                    starttls = True
                if starttls:
                    try:
                        client.starttls()
                    except (smtplib.SMTPException, OSError,
                            socket.error) as exc:
                        _finish("failed",
                                f"starttls_failed: {_type_name(exc)}")
                        return _classified(
                            memory_path, pid, aid, binding, attempt_id,
                            message_id, started, "failed",
                            f"starttls_failed: {_type_name(exc)}",
                            mode, recipient, subject, body)
            if username:
                try:
                    password = os.environ.get("SMTP_PASSWORD", "")
                except (OSError, TypeError, AttributeError):
                    password = ""
                try:
                    client.login(username, str(password or ""))
                except smtplib.SMTPAuthenticationError as exc:
                    _finish("failed",
                            f"auth_failed: {_type_name(exc)}")
                    return _classified(
                        memory_path, pid, aid, binding, attempt_id,
                        message_id, started, "failed",
                        f"auth_failed: {_type_name(exc)}",
                        mode, recipient, subject, body)
                except (smtplib.SMTPException, OSError,
                        socket.error) as exc:
                    _finish("failed",
                            f"auth_failed: {_type_name(exc)}")
                    return _classified(
                        memory_path, pid, aid, binding, attempt_id,
                        message_id, started, "failed",
                        f"auth_failed: {_type_name(exc)}",
                        mode, recipient, subject, body)
            message = EmailMessage()
            message["From"] = sender_check
            message["To"] = recipient
            message["Subject"] = subject
            message["Message-ID"] = message_id
            message.set_content(body)
            try:
                refused = client.send_message(message)
            except smtplib.SMTPRecipientsRefused as exc:
                _finish("failed",
                        f"recipient_refused: {_type_name(exc)}")
                return _classified(
                    memory_path, pid, aid, binding, attempt_id,
                    message_id, started, "failed",
                    f"recipient_refused: {_type_name(exc)}",
                    mode, recipient, subject, body)
            except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
                _finish("failed", f"send_refused: {_type_name(exc)}")
                return _classified(
                    memory_path, pid, aid, binding, attempt_id,
                    message_id, started, "failed",
                    f"send_refused: {_type_name(exc)}",
                    mode, recipient, subject, body)
            except (smtplib.SMTPServerDisconnected, TimeoutError,
                    socket.timeout, ConnectionError, OSError) as exc:
                _finish("unknown", f"result_unknown: {_type_name(exc)}")
                return _classified(
                    memory_path, pid, aid, binding, attempt_id,
                    message_id, started, "unknown",
                    f"result_unknown: {_type_name(exc)}",
                    mode, recipient, subject, body)
            except Exception as exc:
                _finish("unknown", f"result_unknown: {_type_name(exc)}")
                return _classified(
                    memory_path, pid, aid, binding, attempt_id,
                    message_id, started, "unknown",
                    f"result_unknown: {_type_name(exc)}",
                    mode, recipient, subject, body)
            if refused:
                _finish("failed", "recipient_refused: SMTPRecipientsRefused")
                return _classified(
                    memory_path, pid, aid, binding, attempt_id,
                    message_id, started, "failed",
                    "recipient_refused: SMTPRecipientsRefused",
                    mode, recipient, subject, body)
    except MailSendRefused:
        raise
    except (OSError, ValueError, TypeError, AttributeError,
            sqlite3.Error) as exc:
        # 送信前の整理で失敗した(確実に未送信)。failed として残す。
        try:
            _finish("failed", f"pre_send_failed: {_type_name(exc)}")
        except (OSError, sqlite3.Error, ValueError, TypeError,
                AttributeError):
            pass
        return _classified(memory_path, pid, aid, binding, attempt_id,
                           message_id, started, "failed",
                           f"pre_send_failed: {_type_name(exc)}",
                           mode, recipient, subject, body)
    _finish("accepted", "")
    return _classified(memory_path, pid, aid, binding, attempt_id,
                       message_id, started, "accepted", "", mode,
                       recipient, subject, body)


def _classified(memory_path: str | Path, project_id: str, action_id: str,
                binding: dict, attempt_id: str, message_id: str,
                started: float, send_state: str, error_kind: str,
                operating_mode: str, recipient: str, subject: str,
                body: str) -> dict:
    """結果の分類を実行台帳・証拠に記録する。no_results は使わない。"""
    import app.channel_skills as _skills

    pid = str(project_id or "")
    finished = time.time()
    binding_id = 0
    try:
        binding_id = int(binding.get("id") or 0)
    except (TypeError, ValueError):
        binding_id = 0
    domain = _domain_of(recipient)
    try:
        sender = str(os.environ.get("SMTP_FROM", "") or "").strip()
    except (OSError, TypeError, AttributeError):
        sender = ""
    sender_shown = "[withheld]" if sender else ""
    if send_state == "accepted":
        status = "succeeded"
        completed = True
    elif send_state == "unknown":
        status = "unknown"
        completed = False
    else:
        status = "failed"
        completed = False
    evidence: dict | None = None
    evidence_count = 0
    if send_state == "accepted":
        try:
            built = _skills.build_evidence(
                provider="smtp",
                account_or_public_id=sender_shown or "smtp",
                external_id=message_id,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                period_start=datetime.now(timezone.utc).isoformat(),
                period_end=datetime.now(timezone.utc).isoformat(),
                body=body,
                project_id=pid,
                plan_item_id="MAIL_UNLINKED",
                approved_binding_id=str(binding_id),
                purpose="承認済みメール送信",
                retention_days=90,
            )
            saved = _skills.record_evidence(
                memory_path, dict(built), capability_id=CAPABILITY_ID)
            evidence = dict(saved)
            evidence_count = 1
        except ValueError as exc:
            import traceback as _tb
            LOGGER.warning("mail send evidence failed: %s\n%s",
                           _type_name(exc), _tb.format_exc())
            status = "unknown"
            send_state = "unknown"
            error_kind = f"evidence_rejected: {_type_name(exc)}"
    run = _skills.record_run(
        memory_path, project_id=pid, capability_id=CAPABILITY_ID,
        status=status, purpose="承認済みメール送信",
        operating_mode=str(operating_mode or "")
        if str(operating_mode or "") in ("demo_test", "production") else "",
        adapter="smtp",
        params_summary={"action_id": str(action_id or ""),
                        "binding_id": int(binding_id)},
        started_at=started, finished_at=finished,
        error_kind=_scrubbed_text(error_kind)[:200],
        evidence_count=int(evidence_count), completed_ok=bool(completed),
        run_id=f"csm_{attempt_id[:16]}" if attempt_id else "")
    reasons: list[str] = []
    if send_state == "accepted":
        message = ACCEPTED_MESSAGE
    elif send_state == "unknown":
        message = "結果不明のため自動再送しない。人の判断待ち"
        reasons = [_scrubbed_text(error_kind)[:200] or "result_unknown"]
    else:
        message = "送信前の失敗(確実に未送信)"
        reasons = [_scrubbed_text(error_kind)[:200] or "send_failed"]
    return {"send_state": send_state, "run": dict(run),
            "evidence": dict(evidence) if evidence is not None else None,
            "reasons": list(reasons), "delivery_confirmed": False,
            "message": message, "message_id": message_id,
            "binding_id": int(binding_id), "attempt_id": attempt_id,
            "to_domain": domain,
            "error_kind": _scrubbed_text(error_kind)[:200]}


# ----------------------------------------------------------------------------
# 人の結果記録(再送の許可ではない)
# ----------------------------------------------------------------------------

def record_send_outcome(memory_path: str | Path, project_id: str,
                        action_id: str, *, outcome: str, actor: str,
                        memo: str) -> dict:
    """unknown の試行に対する人の判断の記録(追記型)。再送の許可ではない。

    不正は ValueError、unknown でない試行への記録は MailSendRefused(409)。
    """
    pid = str(project_id or "").strip()
    aid = str(action_id or "").strip()
    result = str(outcome or "").strip()
    who = str(actor or "").strip()
    note = str(memo or "").strip()
    if not pid or not aid:
        raise ValueError("project_id and action_id are required")
    if result not in OUTCOMES:
        raise ValueError("outcome must be delivered/not_delivered/unconfirmed")
    if not who or not note:
        raise ValueError("actor and memo are required")
    from app.plan_review_loop import contains_secret
    try:
        label = contains_secret(note)
    except (AttributeError, TypeError, ValueError):
        label = ""
    if label and label not in ("メールアドレス", "電話番号様",
                               "電話番号様(語付き)"):
        raise ValueError(f"secret suspected in memo: {label}")
    try:
        rows = list_attempts(memory_path, pid, aid)
    except MailSendReadError:
        raise
    if not rows:
        raise MailSendRefused("no_attempt", "送信の試行がありません")
    latest = max(rows, key=lambda r: int(r.get("id") or 0))
    if str(latest.get("state") or "") != "unknown":
        raise MailSendRefused("not_unknown",
                              "結果不明の試行にだけ記録できます")
    attempt_id = str(latest.get("attempt_id") or "")
    path = item_ledger_db_path(memory_path)
    guard = _lock_for(path)
    with guard:
        try:
            with _connect_rw(memory_path) as db:
                now = time.time()
                cursor = db.execute(
                    """INSERT INTO mail_send_outcomes(
                        project_id, action_id, attempt_id, outcome,
                        actor, memo, created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (pid, aid, attempt_id, result, who[:100],
                     note[:2000], now),
                )
                db.commit()
                kept = db.execute(
                    "SELECT * FROM mail_send_outcomes WHERE id=?",
                    (int(cursor.lastrowid),)).fetchone()
        except (OSError, sqlite3.Error, ValueError, TypeError,
                AttributeError) as exc:
            raise MailSendWriteError(
                f"outcome write failed: {_type_name(exc)}") from exc
    if kept is None:
        raise MailSendWriteError("outcome write failed: unreadable row")
    return _outcome_view(dict(kept))


def list_outcomes(memory_path: str | Path, project_id: str,
                  action_id: str = "") -> list[dict]:
    """人の結果記録の一覧(当該PJだけ)。"""
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return []
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "mail_send_outcomes" not in names:
            return []
        query = "SELECT * FROM mail_send_outcomes WHERE project_id=?"
        args: list[Any] = [pid]
        if str(action_id or "").strip():
            query += " AND action_id=?"
            args.append(str(action_id).strip())
        query += " ORDER BY id"
        try:
            rows = db.execute(query, tuple(args)).fetchall()
        except sqlite3.Error as exc:
            raise MailSendReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
    return [_outcome_view(dict(r)) for r in rows]


# ----------------------------------------------------------------------------
# 世代切替では消さない
# ----------------------------------------------------------------------------

def purge_project_mail_send(memory_path: str | Path, project_id: str) -> dict:
    """完全削除の補助purge。当該PJの送信記録3表の行だけを消す。

    他PJの行には触れない。表が無ければ0件。失敗は例外のまま送出する
    (呼出側が『完全削除の成功』として扱わないため)。
    """
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    counts = {"mail_send_bindings": 0, "mail_send_attempts": 0,
              "mail_send_outcomes": 0}
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return counts
    guard = _lock_for(path)
    with guard:
        db = sqlite3.connect(str(path), timeout=30)
        try:
            names = {str(r[0]) for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            with db:
                for table in counts:
                    if table not in names:
                        continue
                    cur = db.execute(
                        f"DELETE FROM {table} WHERE project_id=?", (pid,))
                    counts[table] = int(cur.rowcount or 0)
        finally:
            db.close()
    return counts


def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    return {"project_id": str(project_id or ""), "reset": False,
            "note": "承認済みメール送信の台帳は世代切替で変えません"}
