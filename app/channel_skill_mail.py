"""C3: mail.receive の接続設定・読取専用実行・返信候補づけ。

- 実通信は作らない。実接続は利用者が接続設定を行い許可したときだけ作動する。
  試験は偽のアダプタ・偽のIMAPで行い、実際のメールサーバへ接続しない。
- 読み取り専用。既読化・移動・削除・フラグ変更は行わない。
- 資格情報は台帳・応答・ログ・証拠に入れない。環境変数の名前だけを参照する。
- 取得した本文は未信頼データ。中の命令を実行せず、証拠の抜粋だけ残す。
- 取得失敗・未設定を0件にしない。no_results と failed/unknown/needs_configuration を区別する。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from app.requirement_item_ledger import (
    _ensure_tables as _ensure_item_tables,
    classify_ledger_path_kind,
    item_ledger_db_path,
    ledger_readonly_uri,
)

LOGGER = logging.getLogger(__name__)

CAPABILITY_ID = "mail.receive"

DEFAULT_MAX_PERIOD_DAYS = 31
LIMIT_MAX_PERIOD_DAYS = 90
UNLINKED_PLAN_ITEM = "MAIL_UNLINKED"

PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,40}$")
_EMAIL_MASK_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_MASK_RE = re.compile(
    r"(?<![0-9A-Za-z])(?:\+81[\s\-]*0?\d{1,4}[\s\-]*\d{1,4}[\s\-]*\d{3,4}"
    r"|0\d{1,3}[\s\-]+\d{1,4}[\s\-]+\d{3,4}"
    r"|0[5789]0\d{7,8})(?![0-9A-Za-z])")


class MailReceiveRefused(ValueError):
    """書込・外部呼出の前の拒否(設定・フォルダ・期間・目的)。"""

    def __init__(self, reason: str = "", detail: str = "") -> None:
        super().__init__(reason or "mail receive refused")
        self.reason = str(reason or "")
        self.detail = str(detail or "")[:500]


class MailReceiveNotConfigured(MailReceiveRefused):
    def __init__(self, reason: str = "needs_configuration") -> None:
        super().__init__(reason)
        self.reason = str(reason or "needs_configuration")


class MailReceiveReadError(RuntimeError):
    """台帳の読取失敗。『設定なし』とは区別する。"""

    def __init__(self, kind: str, *, path_kind: str = "",
                 sqlite_error: str = "", detail: str = "") -> None:
        super().__init__(f"mail receive read failed: {kind}")
        self.kind = str(kind or "")
        self.path_kind = str(path_kind or "")
        self.sqlite_error = str(sqlite_error or "")
        self.detail = str(detail or "")[:500]


class MailConnectionError(RuntimeError):
    pass


class MailAuthError(RuntimeError):
    pass


class MailTimeoutError(RuntimeError):
    pass


def _type_name(exc: BaseException) -> str:
    try:
        return str(type(exc).__name__ or "Error")
    except Exception:
        return "Error"


# ----------------------------------------------------------------------------
# 台帳5表
# ----------------------------------------------------------------------------

SETTINGS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS mail_receive_settings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    account_label TEXT NOT NULL,
    allowed_folders_json TEXT NOT NULL DEFAULT '[]',
    max_period_days INTEGER NOT NULL DEFAULT 31,
    purpose TEXT NOT NULL DEFAULT '',
    retention_days INTEGER NOT NULL DEFAULT 90,
    allowed_viewers_json TEXT NOT NULL DEFAULT '[]',
    credential_env_prefix TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    supersedes_id INTEGER,
    created_at REAL NOT NULL)"""

MESSAGES_TABLE_DDL = """CREATE TABLE IF NOT EXISTS mail_messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    in_reply_to TEXT NOT NULL DEFAULT '',
    references_json TEXT NOT NULL DEFAULT '[]',
    from_masked TEXT NOT NULL DEFAULT '',
    from_sha256 TEXT NOT NULL DEFAULT '',
    to_masked TEXT NOT NULL DEFAULT '',
    to_sha256 TEXT NOT NULL DEFAULT '',
    subject_masked TEXT NOT NULL DEFAULT '',
    body_sha256 TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '',
    attachment_sha256s_json TEXT NOT NULL DEFAULT '[]',
    folder TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL DEFAULT '',
    period_start TEXT NOT NULL DEFAULT '',
    period_end TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(project_id, provider_id))"""

CURSORS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS mail_receive_cursors(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    folder TEXT NOT NULL,
    uidvalidity TEXT NOT NULL DEFAULT '',
    last_uid INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    UNIQUE(project_id, folder))"""

CANDIDATES_TABLE_DDL = """CREATE TABLE IF NOT EXISTS mail_reply_candidates(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    message_provider_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    basis TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at REAL NOT NULL)"""

CONFIRM_TABLE_DDL = """CREATE TABLE IF NOT EXISTS mail_reply_candidate_confirmations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    candidate_id INTEGER NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL)"""


def ensure_mail_tables(db: sqlite3.Connection) -> None:
    _ensure_item_tables(db)
    db.execute(SETTINGS_TABLE_DDL)
    db.execute(MESSAGES_TABLE_DDL)
    db.execute(CURSORS_TABLE_DDL)
    db.execute(CANDIDATES_TABLE_DDL)
    db.execute(CONFIRM_TABLE_DDL)
    db.execute("""CREATE INDEX IF NOT EXISTS idx_mail_settings_project
        ON mail_receive_settings(project_id, version DESC)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_mail_messages_project
        ON mail_messages(project_id, provider_id)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_mail_candidates_project
        ON mail_reply_candidates(project_id, message_provider_id)""")


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
            ensure_mail_tables(db)
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
        raise MailReceiveReadError(
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
        raise MailReceiveReadError(
            "table_unreadable", sqlite_error=_type_name(exc)) from exc
    return {str(row[0]) for row in rows}


# ----------------------------------------------------------------------------
# 接続設定の検証・保存・読取(資格情報なし・追記型)
# ----------------------------------------------------------------------------

def _reject_secret_fields(fields: dict[str, str]) -> None:
    from app.plan_review_loop import contains_secret
    for name, raw in fields.items():
        value = str(raw or "")
        if not value.strip():
            continue
        try:
            label = contains_secret(value)
        except (AttributeError, TypeError, ValueError):
            label = ""
        if label in ("メールアドレス", "電話番号様", "電話番号様(語付き)"):
            continue
        if label:
            raise ValueError(f"secret suspected in {name}: {label}")


def validate_mail_settings(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("settings are required")
    account = str(payload.get("account_label") or "").strip()
    if not account or len(account) > 200:
        raise ValueError("account_label is required")
    folders = payload.get("allowed_folders")
    if not isinstance(folders, list) or not folders:
        raise ValueError("allowed_folders is required")
    cleaned_folders: list[str] = []
    for item in folders:
        text = str(item or "").strip()
        if not text or len(text) > 128:
            raise ValueError("allowed_folders must be non-empty strings")
        cleaned_folders = cleaned_folders + [text]
    if not cleaned_folders:
        raise ValueError("allowed_folders is required")
    try:
        max_days = int(payload.get("max_period_days", DEFAULT_MAX_PERIOD_DAYS))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("max_period_days is invalid") from None
    if max_days < 1 or max_days > LIMIT_MAX_PERIOD_DAYS:
        raise ValueError("max_period_days is out of range")
    purpose = str(payload.get("purpose") or "").strip()
    if not purpose or len(purpose) > 2000:
        raise ValueError("purpose is required")
    try:
        retention = int(payload.get("retention_days", 90))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("retention_days is invalid") from None
    if retention < 0:
        raise ValueError("retention_days is out of range")
    viewers = payload.get("allowed_viewers")
    if not isinstance(viewers, list) or not viewers:
        raise ValueError("allowed_viewers is required")
    cleaned_viewers: list[str] = []
    for item in viewers:
        text = str(item or "").strip()
        if not text:
            raise ValueError("allowed_viewers must be non-empty strings")
        cleaned_viewers = cleaned_viewers + [text]
    prefix = str(payload.get("credential_env_prefix") or "").strip()
    if not PREFIX_RE.fullmatch(prefix):
        raise ValueError("credential_env_prefix is invalid")
    actor = str(payload.get("actor") or "").strip()
    if not actor or len(actor) > 100:
        raise ValueError("actor is required")
    _reject_secret_fields({
        "account_label": account, "purpose": purpose, "actor": actor,
        "credential_env_prefix": prefix,
        "allowed_folders": " ".join(cleaned_folders),
        "allowed_viewers": " ".join(cleaned_viewers),
    })
    return {
        "account_label": account,
        "allowed_folders": list(cleaned_folders),
        "max_period_days": int(max_days),
        "purpose": purpose,
        "retention_days": int(retention),
        "allowed_viewers": list(cleaned_viewers),
        "credential_env_prefix": prefix,
        "actor": actor,
    }


def save_mail_settings(memory_path: str | Path, project_id: str,
                       payload: dict) -> dict:
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    viewed = validate_mail_settings(payload)
    import json as _json
    path = item_ledger_db_path(memory_path)
    guard = _lock_for(path)
    with guard:
        with _connect_rw(memory_path) as db:
            prev = db.execute(
                "SELECT * FROM mail_receive_settings WHERE project_id=? "
                "ORDER BY version DESC, id DESC LIMIT 1",
                (pid,)).fetchone()
            if prev is not None:
                old = dict(prev)
                try:
                    version = int(old.get("version") or 0) + 1
                except (TypeError, ValueError):
                    version = 1
                if version < 1:
                    version = 1
                supersedes = old.get("id")
                try:
                    supersedes = int(supersedes) if supersedes is not None else None
                except (TypeError, ValueError):
                    supersedes = None
            else:
                version = 1
                supersedes = None
            now = time.time()
            cursor = db.execute(
                """INSERT INTO mail_receive_settings(
                    project_id, account_label, allowed_folders_json,
                    max_period_days, purpose, retention_days,
                    allowed_viewers_json, credential_env_prefix, actor,
                    version, supersedes_id, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, viewed["account_label"],
                 _json.dumps(viewed["allowed_folders"], ensure_ascii=False),
                 int(viewed["max_period_days"]), viewed["purpose"],
                 int(viewed["retention_days"]),
                 _json.dumps(viewed["allowed_viewers"], ensure_ascii=False),
                 viewed["credential_env_prefix"], viewed["actor"],
                 int(version), supersedes, now),
            )
            db.commit()
            kept = db.execute(
                "SELECT * FROM mail_receive_settings WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    return _settings_view(dict(kept))


def _settings_view(row: dict) -> dict:
    import json as _json
    try:
        folders = _json.loads(str(row.get("allowed_folders_json") or "[]"))
    except (TypeError, ValueError):
        folders = []
    if not isinstance(folders, list):
        folders = []
    try:
        viewers = _json.loads(str(row.get("allowed_viewers_json") or "[]"))
    except (TypeError, ValueError):
        viewers = []
    if not isinstance(viewers, list):
        viewers = []
    try:
        supersedes = row.get("supersedes_id")
        supersedes = int(supersedes) if supersedes is not None else None
    except (TypeError, ValueError):
        supersedes = None
    return {
        "id": int(row.get("id") or 0),
        "project_id": str(row.get("project_id") or ""),
        "account_label": str(row.get("account_label") or ""),
        "allowed_folders": [str(x) for x in folders],
        "max_period_days": int(row.get("max_period_days") or 0),
        "purpose": str(row.get("purpose") or ""),
        "retention_days": int(row.get("retention_days") or 0),
        "allowed_viewers": [str(x) for x in viewers],
        "credential_env_prefix": str(row.get("credential_env_prefix") or ""),
        "actor": str(row.get("actor") or ""),
        "version": int(row.get("version") or 0),
        "supersedes_id": supersedes,
        "created_at": row.get("created_at"),
    }


def _split_current(items: list[dict]) -> tuple[list[dict], list[dict]]:
    gone: set[int] = set()
    for item in items:
        target = item.get("supersedes_id")
        if target is None:
            continue
        try:
            gone.add(int(target))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    def _rid(entry: dict) -> int | None:
        try:
            return int(entry.get("id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    current = [item for item in items if _rid(item) not in gone]
    return current, list(items)


def get_mail_settings(memory_path: str | Path, project_id: str) -> dict:
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return {"project_id": pid, "current": None, "history": [],
                "status": "unset"}
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "mail_receive_settings" not in names:
            return {"project_id": pid, "current": None, "history": [],
                    "status": "unset"}
        try:
            rows = db.execute(
                "SELECT * FROM mail_receive_settings WHERE project_id=? ORDER BY id",
                (pid,)).fetchall()
        except sqlite3.Error as exc:
            raise MailReceiveReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        items = [dict(r) for r in rows]
    current, history = _split_current(items)
    viewed_current = [_settings_view(r) for r in current]
    return {"project_id": pid,
            "current": viewed_current[0] if viewed_current else None,
            "history": [_settings_view(r) for r in history],
            "status": "ok" if viewed_current else "unset"}


def get_current_settings(memory_path: str | Path, project_id: str) -> dict | None:
    viewed = get_mail_settings(memory_path, project_id)
    current = viewed.get("current") if isinstance(viewed, dict) else None
    return dict(current) if isinstance(current, dict) else None


# ----------------------------------------------------------------------------
# 資格情報の存在確認(値は見ない・保存しない)
# ----------------------------------------------------------------------------

def required_env_names(prefix: str) -> list[str]:
    base = str(prefix or "").strip()
    return [f"{base}_HOST", f"{base}_PORT", f"{base}_USERNAME",
            f"{base}_PASSWORD"]


def mail_env_ready(prefix: str) -> bool:
    base = str(prefix or "").strip()
    if not PREFIX_RE.fullmatch(base):
        return False
    for name in required_env_names(base):
        try:
            present = os.environ.get(name)
        except (OSError, TypeError, AttributeError):
            return False
        if not isinstance(present, str) or not present:
            return False
    return True


def mail_connection_view(memory_path: str | Path, project_id: str) -> dict:
    """接続の要否判定用。読取失敗は readable=False(設定なしに倒さない)。"""
    pid = str(project_id or "").strip()
    try:
        import app.channel_skills as _skills
        adapter_present = bool(_skills.has_adapter(CAPABILITY_ID))
    except (ImportError, AttributeError, TypeError, ValueError):
        adapter_present = False
    try:
        current = get_current_settings(memory_path, pid)
    except MailReceiveReadError as exc:
        return {"configured": False, "readable": False,
                "adapter_present": bool(adapter_present),
                "reason": "unreadable", "failure_kind": exc.kind,
                "settings": None}
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return {"configured": False, "readable": False,
                "adapter_present": bool(adapter_present),
                "reason": "unreadable", "settings": None}
    if not isinstance(current, dict) or not current:
        return {"configured": False, "readable": True,
                "adapter_present": bool(adapter_present),
                "reason": "no_settings", "settings": None}
    prefix = str(current.get("credential_env_prefix") or "")
    ready = mail_env_ready(prefix)
    if not bool(adapter_present) or not bool(ready):
        return {"configured": False, "readable": True,
                "adapter_present": bool(adapter_present),
                "env_ready": bool(ready), "reason": "not_connected",
                "settings": dict(current)}
    return {"configured": True, "readable": True,
            "adapter_present": True, "env_ready": True,
            "reason": "connected", "settings": dict(current)}


def build_standard_adapter(settings: dict) -> Any:
    """本番の保持先。接続設定から標準の読取専用IMAPアダプタを作る。

    資格情報の値は環境変数から読み、台帳・応答・ログに入れない。
    試験ではこの保持先を monkeypatch で偽に差し替える。
    """
    if not isinstance(settings, dict) or not settings:
        raise ValueError("settings are required")
    prefix = str(settings.get("credential_env_prefix") or "").strip()
    if not PREFIX_RE.fullmatch(prefix):
        raise ValueError("credential_env_prefix is invalid")
    names = required_env_names(prefix)
    try:
        host = os.environ.get(names[0], "")
        port_text = os.environ.get(names[1], "")
        username = os.environ.get(names[2], "")
        secret = os.environ.get(names[3], "")
    except (OSError, TypeError, AttributeError):
        raise MailConnectionError("connection settings are unreadable") from None
    if not host or not username or not secret:
        raise MailConnectionError("connection settings are missing")
    try:
        port = int(str(port_text or "").strip() or "993")
    except (TypeError, ValueError):
        raise MailConnectionError("connection port is invalid") from None
    if port < 1 or port > 65535:
        raise MailConnectionError("connection port is invalid")
    return StandardImapReadonlyAdapter(
        str(host), int(port), str(username), str(secret))


# ----------------------------------------------------------------------------
# 標準の読取専用IMAPアダプタ(標準ライブラリのみ)
# ----------------------------------------------------------------------------

class StandardImapReadonlyAdapter:
    """読取専用の標準IMAPアダプタ。平文接続は行わない。

    - IMAP4_SSL だけを使い、選択は readonly=True で行う。
    - 取得は UID SEARCH で期間と UID 範囲を絞り、UID FETCH は BODY.PEEK で行う。
    - 書込系の操作は行わない。
    - 資格情報の値は保存・記録・例外文に含めない。
    """

    def __init__(self, host: str, port: int, username: str, secret: str,
                 timeout: float = 30.0) -> None:
        self._host = str(host or "")
        self._port = int(port or 993)
        self._username = str(username or "")
        self._secret = str(secret or "")
        try:
            limit = float(timeout)
        except (TypeError, ValueError):
            limit = 30.0
        if limit <= 0:
            limit = 30.0
        self._timeout = min(limit, 120.0)

    def __repr__(self) -> str:
        return "StandardImapReadonlyAdapter(<withheld>)"

    def _scrubbed_error(self, exc: BaseException) -> RuntimeError:
        text = str(getattr(exc, "args", "") or "").lower()
        joined = " ".join(str(a).lower() for a in getattr(exc, "args", []))
        blob = f"{text} {joined}"
        if "auth" in blob or "login" in blob or "credential" in blob:
            return MailAuthError("authentication failed")
        if "timed out" in blob or "timeout" in blob:
            return MailTimeoutError("imap operation timed out")
        return MailConnectionError("imap connection failed")

    def _imap_date(self, value: str) -> str:
        text = str(value or "").strip()[:10]
        try:
            moment = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            raise MailReceiveRefused("period_invalid",
                                     "since/until must be YYYY-MM-DD") from None
        return moment.strftime("%d-%b-%Y")

    def fetch(self, folder: str, since: str, until: str,
              after_cursor: dict | None = None) -> dict:
        import imaplib as _imap

        box = str(folder or "").strip()
        if not box:
            raise MailReceiveRefused("folder_invalid", "folder is required")
        try:
            begin = datetime.fromisoformat(str(since or "").strip()[:10])
            finish = datetime.fromisoformat(str(until or "").strip()[:10])
        except (TypeError, ValueError):
            raise MailReceiveRefused("period_invalid",
                                     "since/until must be YYYY-MM-DD") from None
        if begin > finish:
            raise MailReceiveRefused("period_invalid", "since must precede until")
        wanted_uidvalidity = ""
        wanted_last = 0
        if isinstance(after_cursor, dict):
            wanted_uidvalidity = str(after_cursor.get("uidvalidity") or "")
            try:
                wanted_last = int(after_cursor.get("last_uid") or 0)
            except (TypeError, ValueError):
                wanted_last = 0
            if wanted_last < 0:
                wanted_last = 0
        try:
            link = _imap.IMAP4_SSL(self._host, self._port,
                                   timeout=self._timeout)
        except Exception as exc:
            raise self._scrubbed_error(exc) from None
        try:
            try:
                link.login(self._username, self._secret)
            except Exception as exc:
                raise self._scrubbed_error(exc) from None
            try:
                status, _ = link.select(box, readonly=True)
            except Exception as exc:
                raise self._scrubbed_error(exc) from None
            if status != "OK":
                raise MailConnectionError("mailbox selection failed")
            try:
                kind, validity_rows = link.status(
                    box, "(UIDVALIDITY)")
            except Exception as exc:
                raise self._scrubbed_error(exc) from None
            validity = ""
            try:
                blob = b" ".join(validity_rows or b"")
                text = blob.decode("ascii", errors="ignore")
                found = re.search(r"UIDVALIDITY\s+(\d+)", text)
                if found:
                    validity = str(found.group(1))
            except (AttributeError, TypeError, ValueError, UnicodeError):
                validity = ""
            if not validity:
                raise MailConnectionError("mailbox validity is unreadable")
            criteria: list[str] = ["SINCE",
                                   self._imap_date(str(since or ""))]
            # until は inclusive として扱い、IMAP の BEFORE(排他)へ +1 日で写す。
            try:
                end_exclusive = finish.strftime("%d-%b-%Y")
                from datetime import timedelta as _delta
                end_exclusive = (finish + _delta(days=1)).strftime("%d-%b-%Y")
            except (AttributeError, TypeError, ValueError):
                raise MailConnectionError("period mapping failed") from None
            criteria = criteria + ["BEFORE", str(end_exclusive)]
            use_floor = 0
            if wanted_uidvalidity == validity and wanted_last > 0:
                use_floor = int(wanted_last) + 1
                criteria = criteria + ["UID", f"{use_floor}:*"]
            try:
                kind, found_rows = link.uid("SEARCH", None, *criteria)
            except Exception as exc:
                raise self._scrubbed_error(exc) from None
            if kind != "OK":
                raise MailConnectionError("message search failed")
            uids: list[int] = []
            try:
                for chunk in found_rows or []:
                    for token in (chunk or b"").split():
                        try:
                            number = int(token)
                        except (TypeError, ValueError):
                            continue
                        if number > 0:
                            uids = uids + [number]
            except (AttributeError, TypeError, ValueError):
                raise MailConnectionError("search result is unreadable") from None
            uids = sorted(set(uids))
            # 差分の起点をまたぐ古い UID は期間内でも取り直さない。
            if use_floor:
                uids = [n for n in uids if n >= use_floor]
            messages: list[dict] = []
            for number in uids[:500]:
                try:
                    kind, fetched = link.uid("FETCH", str(number),
                                             "(UID BODY.PEEK[] INTERNALDATE)")
                except Exception as exc:
                    raise self._scrubbed_error(exc) from None
                if kind != "OK":
                    raise MailConnectionError("message fetch failed")
                raw: bytes | None = None
                try:
                    for part in fetched or []:
                        if isinstance(part, tuple) and len(part) >= 2 \
                                and isinstance(part[1], (bytes, bytearray)):
                            raw = bytes(part[1])
                            break
                except (AttributeError, TypeError, ValueError):
                    raw = None
                if not raw:
                    raise MailConnectionError("message body is unreadable")
                messages = messages + [self._parse_raw(
                    str(validity), int(number), bytes(raw))]
            top = wanted_last
            for entry in messages:
                try:
                    uid_part = int(str(entry.get("uid") or 0))
                except (TypeError, ValueError):
                    uid_part = 0
                if uid_part > top:
                    top = int(uid_part)
            if not messages and wanted_uidvalidity == validity:
                top = int(wanted_last)
            return {"messages": messages,
                    "cursor": {"uidvalidity": str(validity),
                               "last_uid": int(top)},
                    "completed_ok": True}
        finally:
            try:
                link.logout()
            except Exception as exc:
                # 切断の失敗は握りつぶさない(型名だけ記録し、取得結果は変えない)。
                LOGGER.warning("imap logout failed: %s", _type_name(exc))

    def _parse_raw(self, validity: str, uid: int, raw: bytes) -> dict:
        import email as _email

        try:
            parsed = _email.message_from_bytes(bytes(raw))
        except (TypeError, ValueError, AttributeError):
            raise MailConnectionError("message parse failed") from None
        def _header(name: str) -> str:
            try:
                value = parsed.get(name, "")
            except (AttributeError, TypeError, ValueError):
                return ""
            return str(value or "")

        message_id = _header("Message-ID").strip()
        in_reply = _header("In-Reply-To").strip()
        refs_text = _header("References").strip()
        refs: list[str] = []
        if refs_text:
            refs = [t for t in re.split(r"\s+", refs_text) if t]
            refs = refs[:20]
        from_raw = _header("From").strip()
        to_raw = _header("To").strip()
        subject = _header("Subject").strip()
        when = _header("Date").strip()
        if not when:
            when = datetime.now(timezone.utc).isoformat()
        plain_parts: list[bytes] = []
        attach_digests: list[str] = []
        try:
            if parsed.is_multipart():
                for part in parsed.walk():
                    try:
                        disp = str(part.get_content_disposition() or "")
                    except (AttributeError, TypeError, ValueError):
                        disp = ""
                    try:
                        payload = part.get_payload(decode=True) or b""
                    except (TypeError, ValueError, AttributeError):
                        payload = b""
                    if disp == "attachment":
                        attach_digests = attach_digests + [
                            hashlib.sha256(bytes(payload)).hexdigest()]
                    elif part.get_content_type() == "text/plain" and not disp:
                        plain_parts = plain_parts + [bytes(payload)]
            else:
                try:
                    payload = parsed.get_payload(decode=True) or b""
                except (TypeError, ValueError, AttributeError):
                    payload = b""
                plain_parts = plain_parts + [bytes(payload)]
        except (AttributeError, TypeError, ValueError):
            plain_parts = []
        body_text = ""
        for chunk in plain_parts:
            try:
                body_text = body_text + bytes(chunk).decode("utf-8", errors="replace")
            except (TypeError, ValueError, UnicodeError):
                continue
            if len(body_text) >= 20000:
                break
        body_text = body_text[:20000]
        return {
            "provider_id": f"{validity}:{uid}",
            "uid": int(uid),
            "uidvalidity": str(validity),
            "message_id": str(message_id)[:500],
            "from": str(from_raw)[:1000],
            "to": str(to_raw)[:2000],
            "subject": str(subject)[:1000],
            "date": str(when)[:100],
            "in_reply_to": str(in_reply)[:500],
            "references": list(refs),
            "body_text": str(body_text),
            "attachment_sha256s": list(attach_digests[:20]),
        }


# ----------------------------------------------------------------------------
# 伏せ・ハッシュ(本文は保存しない)
# ----------------------------------------------------------------------------

def _sha_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _mask_emails_text(text: str) -> str:
    masked = _EMAIL_MASK_RE.sub("[伏せ]", str(text or ""))
    return _PHONE_MASK_RE.sub("[伏せ]", masked)


def _address_list(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    parts = re.split(r"[;,]\s*", text)
    found: list[str] = []
    for part in parts:
        _name, addr = parseaddr(part)
        addr = str(addr or "").strip().lower()
        if addr and "@" in addr:
            found = found + [addr]
    if not found:
        solo = _EMAIL_MASK_RE.search(text)
        if solo:
            found = found + [str(solo.group(0)).lower()]
    return found[:10]


def _mask_header_addresses(raw: str) -> tuple[str, str]:
    addrs = _address_list(raw)
    if not addrs:
        masked = _mask_emails_text(raw)[:500]
        return masked, _sha_text(str(raw or "").strip().lower())
    joined = ",".join(sorted(set(addrs)))
    return "[伏せ]", _sha_text(joined)


def _mask_subject(subject: str) -> str:
    return _mask_emails_text(subject)[:200]


# ----------------------------------------------------------------------------
# 差分の起点・メッセージ・候補の読取
# ----------------------------------------------------------------------------

def get_cursor(memory_path: str | Path, project_id: str,
               folder: str) -> dict | None:
    pid = str(project_id or "").strip()
    box = str(folder or "").strip()
    if not pid or not box:
        raise ValueError("project_id and folder are required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return None
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "mail_receive_cursors" not in names:
            return None
        try:
            row = db.execute(
                "SELECT * FROM mail_receive_cursors WHERE project_id=? AND folder=?",
                (pid, box)).fetchone()
        except sqlite3.Error as exc:
            raise MailReceiveReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        if row is None:
            return None
        kept = dict(row)
    try:
        last = int(kept.get("last_uid") or 0)
    except (TypeError, ValueError):
        last = 0
    return {"uidvalidity": str(kept.get("uidvalidity") or ""),
            "last_uid": int(last if last > 0 else 0)}


def save_cursor(memory_path: str | Path, project_id: str, folder: str,
                uidvalidity: str, last_uid: int) -> dict:
    pid = str(project_id or "").strip()
    box = str(folder or "").strip()
    validity = str(uidvalidity or "")
    try:
        top = int(last_uid or 0)
    except (TypeError, ValueError):
        top = 0
    if not pid or not box or not validity or top < 0:
        raise ValueError("cursor values are invalid")
    path = item_ledger_db_path(memory_path)
    guard = _lock_for(path)
    with guard:
        with _connect_rw(memory_path) as db:
            now = time.time()
            db.execute(
                """INSERT INTO mail_receive_cursors(
                    project_id, folder, uidvalidity, last_uid, updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(project_id, folder) DO UPDATE SET
                    uidvalidity=excluded.uidvalidity,
                    last_uid=excluded.last_uid,
                    updated_at=excluded.updated_at""",
                (pid, box, validity, int(top), now),
            )
            db.commit()
            kept = db.execute(
                "SELECT * FROM mail_receive_cursors WHERE project_id=? AND folder=?",
                (pid, box)).fetchone()
    current = dict(kept)
    return {"project_id": str(current.get("project_id") or ""),
            "folder": str(current.get("folder") or ""),
            "uidvalidity": str(current.get("uidvalidity") or ""),
            "last_uid": int(current.get("last_uid") or 0)}


def _message_view(row: dict) -> dict:
    import json as _json
    try:
        refs = _json.loads(str(row.get("references_json") or "[]"))
    except (TypeError, ValueError):
        refs = []
    if not isinstance(refs, list):
        refs = []
    try:
        attach = _json.loads(str(row.get("attachment_sha256s_json") or "[]"))
    except (TypeError, ValueError):
        attach = []
    if not isinstance(attach, list):
        attach = []
    return {
        "id": int(row.get("id") or 0),
        "project_id": str(row.get("project_id") or ""),
        "provider_id": str(row.get("provider_id") or ""),
        "message_id": str(row.get("message_id") or ""),
        "in_reply_to": str(row.get("in_reply_to") or ""),
        "references": [str(x) for x in refs],
        "from_masked": str(row.get("from_masked") or ""),
        "from_sha256": str(row.get("from_sha256") or ""),
        "to_masked": str(row.get("to_masked") or ""),
        "to_sha256": str(row.get("to_sha256") or ""),
        "subject_masked": str(row.get("subject_masked") or ""),
        "body_sha256": str(row.get("body_sha256") or ""),
        "excerpt": str(row.get("excerpt") or ""),
        "attachment_sha256s": [str(x) for x in attach],
        "folder": str(row.get("folder") or ""),
        "fetched_at": str(row.get("fetched_at") or ""),
        "period_start": str(row.get("period_start") or ""),
        "period_end": str(row.get("period_end") or ""),
        "created_at": row.get("created_at"),
    }


def list_messages(memory_path: str | Path, project_id: str) -> list[dict]:
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return []
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "mail_messages" not in names:
            return []
        try:
            rows = db.execute(
                "SELECT * FROM mail_messages WHERE project_id=? ORDER BY id",
                (pid,)).fetchall()
        except sqlite3.Error as exc:
            raise MailReceiveReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
    return [_message_view(dict(r)) for r in rows]


def _candidate_view(row: dict) -> dict:
    return {
        "id": int(row.get("id") or 0),
        "project_id": str(row.get("project_id") or ""),
        "message_provider_id": str(row.get("message_provider_id") or ""),
        "action_id": str(row.get("action_id") or ""),
        "basis": str(row.get("basis") or ""),
        "status": str(row.get("status") or "candidate"),
        "created_at": row.get("created_at"),
    }


def list_reply_candidates(memory_path: str | Path,
                          project_id: str) -> list[dict]:
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return []
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "mail_reply_candidates" not in names:
            return []
        try:
            rows = db.execute(
                "SELECT * FROM mail_reply_candidates WHERE project_id=? ORDER BY id",
                (pid,)).fetchall()
        except sqlite3.Error as exc:
            raise MailReceiveReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
    return [_candidate_view(dict(r)) for r in rows]


def list_candidate_confirmations(memory_path: str | Path,
                                 project_id: str) -> list[dict]:
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return []
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "mail_reply_candidate_confirmations" not in names:
            return []
        try:
            rows = db.execute(
                "SELECT * FROM mail_reply_candidate_confirmations "
                "WHERE project_id=? ORDER BY id",
                (pid,)).fetchall()
        except sqlite3.Error as exc:
            raise MailReceiveReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
    out: list[dict] = []
    for item in rows:
        kept = dict(item)
        out = out + [{
            "id": int(kept.get("id") or 0),
            "project_id": str(kept.get("project_id") or ""),
            "candidate_id": int(kept.get("candidate_id") or 0),
            "actor": str(kept.get("actor") or ""),
            "memo": str(kept.get("memo") or ""),
            "created_at": kept.get("created_at"),
        }]
    return out


def confirm_reply_candidate(memory_path: str | Path, project_id: str,
                            candidate_id: int, *, actor: str,
                            memo: str) -> dict:
    pid = str(project_id or "").strip()
    try:
        target = int(candidate_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("candidate_id is required") from None
    who = str(actor or "").strip()
    note = str(memo or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    if not who or not note:
        raise ValueError("actor and memo are required")
    _reject_secret_fields({"actor": who, "memo": note})
    path = item_ledger_db_path(memory_path)
    guard = _lock_for(path)
    with guard:
        with _connect_rw(memory_path) as db:
            current = db.execute(
                "SELECT * FROM mail_reply_candidates WHERE id=?",
                (int(target),)).fetchone()
            if current is None:
                raise ValueError("candidate not found")
            kept = dict(current)
            if str(kept.get("project_id") or "") != pid:
                raise ValueError("candidate not found")
            now = time.time()
            cursor = db.execute(
                """INSERT INTO mail_reply_candidate_confirmations(
                    project_id, candidate_id, actor, memo, created_at)
                   VALUES(?,?,?,?,?)""",
                (pid, int(target), who[:100], note[:2000], now),
            )
            db.commit()
            kept_confirm = db.execute(
                "SELECT * FROM mail_reply_candidate_confirmations WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    saved = dict(kept_confirm)
    return {
        "id": int(saved.get("id") or 0),
        "project_id": str(saved.get("project_id") or ""),
        "candidate_id": int(saved.get("candidate_id") or 0),
        "actor": str(saved.get("actor") or ""),
        "memo": str(saved.get("memo") or ""),
        "created_at": saved.get("created_at"),
        "candidate": _candidate_view(dict(kept)),
    }


# ----------------------------------------------------------------------------
# 実行(読み取り)
# ----------------------------------------------------------------------------

def _checked_mode(operating_mode: str | None) -> str:
    mode = str(operating_mode or "").strip()
    if mode not in ("demo_test", "production"):
        return "production"
    return mode


def _parse_day(value: str, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise MailReceiveRefused(f"{field}_invalid", f"{field} is required")
    try:
        moment = datetime.fromisoformat(text[:10])
    except (TypeError, ValueError):
        raise MailReceiveRefused(f"{field}_invalid",
                                 f"{field} must be YYYY-MM-DD") from None
    return moment


def _normalize_subject(subject: str) -> str:
    text = str(subject or "").strip()
    lowered = text.lower()
    while lowered.startswith("re:"):
        text = text[3:].strip()
        lowered = text.lower()
    return text


def _list_executed_email_actions(memory_path: str | Path,
                                 project_id: str) -> list[dict]:
    pid = str(project_id or "").strip()
    if not pid:
        return []
    try:
        from app.memory.short_term import ShortTermMemory
        resolved = Path(str(memory_path or ""))
        db_path = resolved if resolved.suffix else resolved.parent / "conversations.db"
        if not db_path.exists():
            return []
        holder = ShortTermMemory(db_path, read_only=True)
        try:
            items = holder.list_actions(pid)
        finally:
            try:
                holder.close()
            except (AttributeError, OSError, ValueError, TypeError):
                pass
    except (OSError, ValueError, TypeError, AttributeError, KeyError,
            sqlite3.Error, ImportError):
        return []
    out: list[dict] = []
    if not isinstance(items, list):
        return []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("kind") or "") != "email":
            continue
        if str(entry.get("status") or "") != "executed":
            continue
        out = out + [dict(entry)]
    return out


def _action_subject(action: dict) -> str:
    content = str(action.get("content") or "")
    first = content.split("\n", 1)[0].strip()
    if first.lower().startswith("件名:"):
        first = first[3:].strip()
    return first


def _candidate_basis_for(message: dict, action: dict,
                           sent_message_ids: list[str] | None = None) -> str:
    in_reply = str(message.get("in_reply_to") or "").strip()
    refs = message.get("references") or []
    if not isinstance(refs, list):
        refs = []
    candidates: list[str] = []
    for key in ("message_id", "messageId", "msg_id", "email_message_id"):
        try:
            sent_mid = str(action.get(key) or "").strip()
        except (AttributeError, TypeError):
            sent_mid = ""
        if sent_mid:
            candidates = candidates + [sent_mid]
    # C5: mail.send_approved の送信記録(message_id)との照合を根拠(a)として追加。
    # C3の依頼文で予告した接続。候補は candidate のまま、確定にしない。
    for sent_mid in list(sent_message_ids or []):
        text = str(sent_mid or "").strip()
        if text and text not in candidates:
            candidates = candidates + [text]
    for sent_mid in candidates:
        if sent_mid and (sent_mid == in_reply or sent_mid in [str(x) for x in refs]):
            return "in_reply_to_match"
    try:
        target = str(action.get("target") or "").strip().lower()
    except (AttributeError, TypeError):
        target = ""
    sender_addrs = _address_list(str(message.get("from") or ""))
    if target and sender_addrs and target in sender_addrs:
        want = _normalize_subject(_action_subject(action)).lower()
        got = _normalize_subject(str(message.get("subject") or "")).lower()
        if want and got and want == got:
            return "from_subject_match"
    return ""


def _validate_fetch_result(result: Any) -> dict:
    if not isinstance(result, dict):
        raise ValueError("result is unreadable")
    if "completed_ok" not in result:
        raise ValueError("completed_ok is missing")
    if not isinstance(result.get("completed_ok"), bool):
        raise ValueError("completed_ok is invalid")
    messages = result.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages are unreadable")
    cursor = result.get("cursor")
    if not isinstance(cursor, dict):
        raise ValueError("cursor is unreadable")
    if not str(cursor.get("uidvalidity") or ""):
        raise ValueError("cursor uidvalidity is missing")
    try:
        last = int(cursor.get("last_uid") or 0)
    except (TypeError, ValueError):
        raise ValueError("cursor last_uid is invalid") from None
    if last < 0:
        raise ValueError("cursor last_uid is invalid")
    for entry in messages:
        if not isinstance(entry, dict):
            raise ValueError("message is unreadable")
        if not str(entry.get("provider_id") or "").strip():
            raise ValueError("provider_id is required")
        body = entry.get("body_text")
        if not isinstance(body, str):
            raise ValueError("body_text is unreadable")
        attach = entry.get("attachment_sha256s")
        if attach is None:
            entry["attachment_sha256s"] = []
        elif not isinstance(attach, list):
            raise ValueError("attachment_sha256s is unreadable")
    return {"messages": list(messages),
            "cursor": {"uidvalidity": str(cursor.get("uidvalidity") or ""),
                       "last_uid": int(last)},
            "completed_ok": bool(result.get("completed_ok"))}


def run_mail_receive(memory_path: str | Path, project_id: str, *,
                     folder: str, since: str, until: str, purpose: str,
                     operating_mode: str | None = None,
                     adapter: Any = None) -> dict:
    """許可された専用箱の対象期間だけを読み取り専用で差分取得する。

    拒否(設定なし・許可外・期間・目的)は MailReceiveRefused(呼出側が409/422にする)。
    取得器の例外は failed として記録して返す。形の不正は unknown。
    """
    import app.channel_skills as _skills

    pid = str(project_id or "").strip()
    if not pid:
        raise MailReceiveRefused("project_invalid", "project_id is required")
    box = str(folder or "").strip()
    if not box:
        raise MailReceiveRefused("folder_invalid", "folder is required")
    reason_text = str(purpose or "").strip()
    if not reason_text:
        raise MailReceiveRefused("purpose_invalid", "purpose is required")
    begin = _parse_day(since, "since")
    finish = _parse_day(until, "until")
    if begin > finish:
        raise MailReceiveRefused("period_invalid", "since must precede until")
    mode = _checked_mode(operating_mode)
    if adapter is None:
        raise MailReceiveRefused("adapter_missing",
                                 "mail adapter is not configured")

    # 外部呼出の前に済ませる検査(設定・フォルダ・期間・目的)。
    try:
        current = get_current_settings(memory_path, pid)
    except MailReceiveReadError:
        raise MailReceiveRefused("unreadable",
                                 "mail settings are unreadable") from None
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        raise MailReceiveRefused("unreadable",
                                 "mail settings are unreadable") from None
    if not isinstance(current, dict) or not current:
        raise MailReceiveNotConfigured("needs_configuration")
    allowed = current.get("allowed_folders") or []
    if not isinstance(allowed, list) or box not in [str(x) for x in allowed]:
        raise MailReceiveRefused("forbidden_folder",
                                 "folder is not allowed")
    try:
        limit_days = int(current.get("max_period_days") or DEFAULT_MAX_PERIOD_DAYS)
    except (TypeError, ValueError):
        limit_days = DEFAULT_MAX_PERIOD_DAYS
    span = (finish.date() - begin.date()).days + 1
    if span < 1:
        raise MailReceiveRefused("period_invalid", "period is invalid")
    if span > int(limit_days):
        raise MailReceiveRefused("period_too_long",
                                 f"period exceeds {int(limit_days)} days")
    if str(current.get("purpose") or "").strip() != reason_text:
        raise MailReceiveRefused("purpose_mismatch",
                                 "purpose does not match settings")
    try:
        retention = int(current.get("retention_days") or 90)
    except (TypeError, ValueError):
        retention = 90
    account = str(current.get("account_label") or "")

    try:
        before = get_cursor(memory_path, pid, box)
    except MailReceiveReadError:
        raise MailReceiveRefused("unreadable",
                                 "mail cursor is unreadable") from None
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        raise MailReceiveRefused("unreadable",
                                 "mail cursor is unreadable") from None

    since_text = begin.strftime("%Y-%m-%d")
    until_text = finish.strftime("%Y-%m-%d")
    started = time.time()
    label = type(adapter).__name__ or "mail_adapter"
    label = str(label)[:200] or "mail_adapter"

    try:
        raw_result = adapter.fetch(box, since_text, until_text, before)
    except (MailAuthError, MailConnectionError, MailTimeoutError) as exc:
        finished = time.time()
        kept = _skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="failed", purpose=reason_text[:2000],
            operating_mode=mode, adapter=label,
            params_summary={"folder": box},
            started_at=started, finished_at=finished,
            error_kind=_type_name(exc), evidence_count=0,
            completed_ok=False,
        )
        return {"status": "failed", "run": kept, "evidence": [],
                "messages": [], "candidates": [],
                "reason": f"fetch failed: {_type_name(exc)}"}
    except Exception as exc:
        finished = time.time()
        kept = _skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="failed", purpose=reason_text[:2000],
            operating_mode=mode, adapter=label,
            params_summary={"folder": box},
            started_at=started, finished_at=finished,
            error_kind=_type_name(exc), evidence_count=0,
            completed_ok=False,
        )
        return {"status": "failed", "run": kept, "evidence": [],
                "messages": [], "candidates": [],
                "reason": f"fetch failed: {_type_name(exc)}"}

    try:
        viewed = _validate_fetch_result(raw_result)
    except ValueError as exc:
        finished = time.time()
        kept = _skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="unknown", purpose=reason_text[:2000],
            operating_mode=mode, adapter=label,
            params_summary={"folder": box},
            started_at=started, finished_at=finished,
            error_kind="malformed_result", evidence_count=0,
            completed_ok=False,
        )
        return {"status": "unknown", "run": kept, "evidence": [],
                "messages": [], "candidates": [],
                "reason": str(exc)[:200] or "result is unreadable"}

    if not viewed["completed_ok"]:
        finished = time.time()
        kept = _skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="unknown", purpose=reason_text[:2000],
            operating_mode=mode, adapter=label,
            params_summary={"folder": box},
            started_at=started, finished_at=finished,
            error_kind="incomplete_fetch", evidence_count=0,
            completed_ok=False,
        )
        return {"status": "unknown", "run": kept, "evidence": [],
                "messages": [], "candidates": [],
                "reason": "fetch did not complete"}

    fetched = viewed["messages"]
    if not fetched:
        try:
            save_cursor(memory_path, pid, box,
                        str(viewed["cursor"].get("uidvalidity") or ""),
                        int(viewed["cursor"].get("last_uid") or 0))
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error,
                MailReceiveReadError):
            pass
        finished = time.time()
        kept = _skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="no_results", purpose=reason_text[:2000],
            operating_mode=mode, adapter=label,
            params_summary={"folder": box},
            started_at=started, finished_at=finished,
            error_kind="", evidence_count=0, completed_ok=True,
        )
        return {"status": "no_results", "run": kept, "evidence": [],
                "messages": [], "candidates": [],
                "reason": "no new messages"}

    import json as _json
    kept_messages: list[dict] = []
    kept_evidence: list[dict] = []
    for entry in fetched:
        provider_id = str(entry.get("provider_id") or "").strip()
        body_text = str(entry.get("body_text") or "")
        attach = entry.get("attachment_sha256s") or []
        if not isinstance(attach, list):
            attach = []
        clean_attach: list[str] = []
        for item in attach:
            text = str(item or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", text or ""):
                clean_attach = clean_attach + [text]
        from_masked, from_digest = _mask_header_addresses(str(entry.get("from") or ""))
        to_masked, to_digest = _mask_header_addresses(str(entry.get("to") or ""))
        subject_masked = _mask_subject(str(entry.get("subject") or ""))
        refs = entry.get("references") or []
        if not isinstance(refs, list):
            refs = []
        refs = [str(x)[:500] for x in refs[:20]]
        when = str(entry.get("date") or "").strip() or datetime.now(timezone.utc).isoformat()
        # 本文は保存しない。ハッシュと伏せた抜粋だけ残す。
        try:
            built = _skills.build_evidence(
                provider="mail",
                account_or_public_id=account or "mailbox",
                external_id=provider_id,
                fetched_at=when,
                period_start=since_text,
                period_end=until_text,
                body=body_text,
                project_id=pid,
                plan_item_id=UNLINKED_PLAN_ITEM,
                purpose=reason_text[:2000],
                retention_days=int(retention),
            )
        except ValueError as exc:
            finished = time.time()
            kept = _skills.record_run(
                memory_path, project_id=pid, capability_id=CAPABILITY_ID,
                status="unknown", purpose=reason_text[:2000],
                operating_mode=mode, adapter=label,
                params_summary={"folder": box},
                started_at=started, finished_at=finished,
                error_kind="evidence_rejected",
                evidence_count=len(kept_evidence),
                completed_ok=False,
            )
            return {"status": "unknown", "run": kept,
                    "evidence": list(kept_evidence),
                    "messages": list(kept_messages),
                    "candidates": [],
                    "reason": f"evidence rejected: {_type_name(exc)}"}
        path = item_ledger_db_path(memory_path)
        guard = _lock_for(path)
        is_new = True
        with guard:
            with _connect_rw(memory_path) as db:
                try:
                    db.execute(
                        """INSERT INTO mail_messages(
                            project_id, provider_id, message_id, in_reply_to,
                            references_json, from_masked, from_sha256,
                            to_masked, to_sha256, subject_masked, body_sha256,
                            excerpt, attachment_sha256s_json, folder,
                            fetched_at, period_start, period_end, created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (pid, provider_id,
                         str(entry.get("message_id") or "")[:500],
                         str(entry.get("in_reply_to") or "")[:500],
                         _json.dumps(refs, ensure_ascii=False),
                         from_masked[:500], from_digest,
                         to_masked[:2000], to_digest,
                         subject_masked[:200],
                         str(built.get("body_sha256") or ""),
                         str(built.get("excerpt") or ""),
                         _json.dumps(clean_attach, ensure_ascii=False),
                         box, when[:100], since_text, until_text,
                         time.time()),
                    )
                    db.commit()
                except sqlite3.IntegrityError:
                    is_new = False
                kept_row = db.execute(
                    "SELECT * FROM mail_messages WHERE project_id=? AND provider_id=?",
                    (pid, provider_id)).fetchone()
        if kept_row is None:
            finished = time.time()
            kept = _skills.record_run(
                memory_path, project_id=pid, capability_id=CAPABILITY_ID,
                status="unknown", purpose=reason_text[:2000],
                operating_mode=mode, adapter=label,
                params_summary={"folder": box},
                started_at=started, finished_at=finished,
                error_kind="message_unreadable",
                evidence_count=len(kept_evidence),
                completed_ok=False,
            )
            return {"status": "unknown", "run": kept,
                    "evidence": list(kept_evidence),
                    "messages": list(kept_messages),
                    "candidates": [],
                    "reason": "saved message is unreadable"}
        if not is_new:
            continue
        try:
            saved_evidence = _skills.record_evidence(
                memory_path, dict(built), capability_id=CAPABILITY_ID)
        except ValueError as exc:
            finished = time.time()
            kept = _skills.record_run(
                memory_path, project_id=pid, capability_id=CAPABILITY_ID,
                status="unknown", purpose=reason_text[:2000],
                operating_mode=mode, adapter=label,
                params_summary={"folder": box},
                started_at=started, finished_at=finished,
                error_kind="evidence_rejected",
                evidence_count=len(kept_evidence),
                completed_ok=False,
            )
            return {"status": "unknown", "run": kept,
                    "evidence": list(kept_evidence),
                    "messages": list(kept_messages),
                    "candidates": [],
                    "reason": f"evidence rejected: {_type_name(exc)}"}
        kept_evidence = kept_evidence + [dict(saved_evidence)]
        kept_messages = kept_messages + [_message_view(dict(kept_row))]

    try:
        save_cursor(memory_path, pid, box,
                    str(viewed["cursor"].get("uidvalidity") or ""),
                    int(viewed["cursor"].get("last_uid") or 0))
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error,
            MailReceiveReadError):
        pass

    # 返信の候補づけ(確定にしない)。
    candidates: list[dict] = []
    try:
        sent = _list_executed_email_actions(memory_path, pid)
    except (OSError, ValueError, TypeError, AttributeError):
        sent = []
    if sent and kept_messages:
        # provider_id -> 保存行の対応で候補を結ぶ。
        rows_by_provider = {str(r.get("provider_id") or ""): r
                            for r in kept_messages}
        for provider_id, kept_row in rows_by_provider.items():
            message_view = {
                "in_reply_to": str(kept_row.get("in_reply_to") or ""),
                "references": list(kept_row.get("references") or []),
                "from": "",
                "subject": "",
            }
            # 照合に要る生の差出人・件名は今回の取得結果から求める(保存値は伏せ)。
            for entry in fetched:
                if str(entry.get("provider_id") or "") == provider_id:
                    message_view["from"] = str(entry.get("from") or "")
                    message_view["subject"] = str(entry.get("subject") or "")
                    message_view["in_reply_to"] = str(
                        entry.get("in_reply_to") or "")
                    refs = entry.get("references") or []
                    message_view["references"] = list(refs) if isinstance(refs, list) else []
                    break
            # C5: Message-ID照合用の一覧(読取失敗は照合不能として既存根拠だけ使う)。
            c5_mids: list[str] = []
            try:
                from app.channel_skill_mail_send import (
                    list_message_ids as _c5_list_mids)
                try:
                    c5_mids = []
                except Exception:
                    c5_mids = []
            except (ImportError, AttributeError, TypeError, ValueError):
                c5_mids = []
            for action in sent:
                try:
                    action_id = str(action.get("id") or "")
                except (AttributeError, TypeError):
                    continue
                if not action_id:
                    continue
                try:
                    c5_mids = _c5_list_mids(memory_path, pid, action_id)
                except (NameError, OSError, ValueError, TypeError, AttributeError):
                    c5_mids = []
                basis = _candidate_basis_for(
                    message_view, action,
                    sent_message_ids=c5_mids)
                if not basis:
                    continue
                path = item_ledger_db_path(memory_path)
                guard = _lock_for(path)
                with guard:
                    with _connect_rw(memory_path) as db:
                        now = time.time()
                        cursor = db.execute(
                            """INSERT INTO mail_reply_candidates(
                                project_id, message_provider_id, action_id,
                                basis, status, created_at)
                               VALUES(?,?,?,?,?,?)""",
                            (pid, provider_id, action_id, basis,
                             "candidate", now),
                        )
                        db.commit()
                        kept_candidate = db.execute(
                            "SELECT * FROM mail_reply_candidates WHERE id=?",
                            (int(cursor.lastrowid),)).fetchone()
                candidates = candidates + [_candidate_view(dict(kept_candidate))]

    finished = time.time()
    if not kept_messages:
        kept = _skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="no_results", purpose=reason_text[:2000],
            operating_mode=mode, adapter=label,
            params_summary={"folder": box},
            started_at=started, finished_at=finished,
            error_kind="", evidence_count=0, completed_ok=True,
        )
        return {"status": "no_results", "run": kept, "evidence": [],
                "messages": [], "candidates": list(candidates),
                "reason": "no new messages"}
    kept = _skills.record_run(
        memory_path, project_id=pid, capability_id=CAPABILITY_ID,
        status="succeeded", purpose=reason_text[:2000],
        operating_mode=mode, adapter=label,
        params_summary={"folder": box},
        started_at=started, finished_at=finished,
        error_kind="", evidence_count=len(kept_evidence),
        completed_ok=True,
    )
    return {"status": "succeeded", "run": kept,
            "evidence": list(kept_evidence),
            "messages": list(kept_messages),
            "candidates": list(candidates), "reason": ""}


# ----------------------------------------------------------------------------
# 世代切替では消さない
# ----------------------------------------------------------------------------

def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    return {"project_id": str(project_id or ""), "reset": False,
            "note": "メール受信の台帳は世代切替で変えません"}
