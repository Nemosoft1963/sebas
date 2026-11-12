"""A2: 問い合わせ窓口・発信媒体・使用IDの登録モデルとpreflight.

- 保存先は項目別台帳と同じSQLite(item_ledger_db_path)への追記型(追加型)。
  訂正・変更は新しい版として追加し、古い版を消さない(supersedes_id)。
  現在の値は置き換えられていない最新の版。
- 値の出所は fictional / registered / verified / retired。
  fictional は内部デモのみ、registered は公開用途で要確認、verified のみ
  公開可、retired・失効・未登録は不可(理由つき)。
- 認証情報(パスワード・Cookie・APIキー・OAuth/Bearerトークン等)は登録拒否。
  既存の秘密検出 contains_secret を使い、検出時は種別ラベルのみ返す。
- 読取は読み取り専用接続(ledger_readonly_uri)。読取失敗は例外として明示し、
  『登録なし』に倒さない。台帳DBが無い・表がまだ無い場合だけ未登録。
- PJ初期化(世代切替)では記録を変えない。完全削除・バックアップは
  project_lifecycle_registry の汎用経路(sqlite_table)で行う。
- A1の差し替え口とはこの単位では接続しない(A3で行う)。A3用の
  registry_contact_provider を用意するが、この単位では呼ばれない。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from app.requirement_item_ledger import (
    _ensure_tables as _ensure_item_tables,
    classify_ledger_path_kind,
    item_ledger_db_path,
    ledger_readonly_uri,
)

LOGGER = logging.getLogger(__name__)

CONTACT_TYPES = frozenset({"form", "email", "phone"})
PLATFORMS = frozenset({"X", "LinkedIn", "Facebook", "LINE", "Instagram", "Threads"})
SOURCE_KINDS = frozenset({"fictional", "registered", "verified", "retired"})
PURPOSES = frozenset({"internal_demo", "public"})
REGISTERABLE_KINDS = frozenset({"fictional", "registered"})

CONTACT_TABLE_DDL = """CREATE TABLE IF NOT EXISTS contact_routes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL,
    public_value TEXT NOT NULL,
    display_label TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    verified_at REAL,
    expires_at REAL,
    version INTEGER NOT NULL DEFAULT 1,
    supersedes_id INTEGER,
    actor TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL)"""

CHANNEL_TABLE_DDL = """CREATE TABLE IF NOT EXISTS channel_selections(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    purpose TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    account_ref TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    supersedes_id INTEGER,
    actor TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL)"""

IDENTITY_TABLE_DDL = """CREATE TABLE IF NOT EXISTS public_identities(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL,
    handle TEXT NOT NULL DEFAULT '',
    profile_url TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    owner_label TEXT NOT NULL DEFAULT '',
    verified_at REAL,
    expires_at REAL,
    version INTEGER NOT NULL DEFAULT 1,
    supersedes_id INTEGER,
    actor TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL)"""

OPERATING_MODE_TABLE_DDL = """CREATE TABLE IF NOT EXISTS operating_modes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    supersedes_id INTEGER,
    created_at REAL NOT NULL)"""

OPERATING_MODES = frozenset({"demo_test", "production"})


class RegistryReadError(RuntimeError):
    """登録台帳の読取失敗。『未登録』とは区別する。

    kind: 'connect_failed' | 'table_missing' | 'table_unreadable' | 'row_unreadable'
    """

    def __init__(self, kind: str, *, path_kind: str = "",
                 sqlite_error: str = "", detail: str = "") -> None:
        super().__init__(f"contact registry read failed: {kind}")
        self.kind = str(kind or "")
        self.path_kind = str(path_kind or "")
        self.sqlite_error = str(sqlite_error or "")
        self.detail = str(detail or "")[:500]


class RegistryInputError(ValueError):
    """入力検証の失敗。Webでは422に写像する。"""


class RegistryConflictError(RuntimeError):
    """状態との衝突(昇格・失効後の変更等)。Webでは409に写像する。"""


def _type_name(exc: BaseException) -> str:
    try:
        return str(type(exc).__name__ or "Error")
    except Exception:
        return "Error"


def ensure_contact_tables(db: sqlite3.Connection) -> None:
    """4表と索引を作成する。失敗は呼出側へ送出する(握りつぶさない)。"""
    _ensure_item_tables(db)
    db.execute(CONTACT_TABLE_DDL)
    db.execute(CHANNEL_TABLE_DDL)
    db.execute(IDENTITY_TABLE_DDL)
    db.execute(OPERATING_MODE_TABLE_DDL)
    db.execute("""CREATE INDEX IF NOT EXISTS idx_contact_routes_project
        ON contact_routes(project_id, route_id, version DESC)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_channel_sel_project
        ON channel_selections(project_id, platform, campaign_id, version DESC)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_public_id_project
        ON public_identities(project_id, identity_id, version DESC)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_operating_mode_project
        ON operating_modes(project_id, version DESC)""")


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(db_path: Path) -> threading.Lock:
    key = str(db_path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


@contextmanager
def _connect_rw(memory_path: str | Path):
    path = item_ledger_db_path(memory_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            ensure_contact_tables(db)
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
        raise RegistryReadError(
            "connect_failed", path_kind=kind,
            sqlite_error=_type_name(exc)) from exc
    try:
        yield db
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


# ----------------------------------------------------------------------------
# 検証用ヘルパー
# ----------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s、。）)\"'<>]+")
_USERINFO_RE = re.compile(r"https?://[^/\s:@]+:[^/\s:@]+@[^/\s]+")
_COOKIE_RE = re.compile(r"(?:\bcookie\b|\bCookie\b|\bCOOKIE\b)\s*[:=]\s*\S+")
_RESERVED_EXACT = frozenset({"example.com", "example.org", "example.net"})
_RESERVED_SUFFIX = (".invalid", ".test", ".example")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-()]{5,30}$")
_FICTIONAL_MARKERS = ("invalid", "example", "test", "fictional", "fiction",
                      "dummy", "sample", "架空")

_AUTH_SECRET_LABELS = frozenset({
    "AWSキー", "Authorizationヘッダー", "秘密鍵(PEM)", "GitHubトークン",
    "Slackトークン", "Stripeキー", "JWT", "パスワード等", "資格情報付きURL",
    "APIキー(sk-)", "xAIキー", "Google APIキー", "LLMキー", "Bearerトークン",
    "Cookie", "メールアドレス",
})


_API_KEY_LIKE_RE = re.compile(r"sk-\S{2,}")
_BEARER_LIKE_RE = re.compile(r"Bearer\s+\S+")
_SK_PLACEHOLDER_RE = re.compile(r"sk-(?:架空|テスト|test|dummy|sample|example)")


def _reject_auth_secrets(field_map: dict[str, str]) -> None:
    """認証情報系の値が含まれていれば登録を拒否する。

    既存の contains_secret を使うが、連絡先の正規値であるメールアドレス・
    電話番号の検出では拒否しない(認証情報系ラベルの場合のみ拒否)。
    ただし日本語テキスト中のプレースホルダ的な検証語まで contains_secret が
    メールとして拾うため、フィールド単体がメール形式の場合だけ許し、
    他の文と混ざっている場合は拒否する。
    検出時のメッセージに秘密の値そのものは含めない(種別ラベルのみ)。
    """
    from app.plan_review_loop import contains_secret

    for name, raw in field_map.items():
        value = str(raw or "")
        if not value.strip():
            continue
        stripped = value.strip()
        try:
            label = contains_secret(value)
        except (AttributeError, TypeError, ValueError):
            label = ""
        if label == "メールアドレス":
            if re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
                             stripped):
                label = ""
        if label and label in _AUTH_SECRET_LABELS:
            raise RegistryInputError(
                f"secret suspected in {name}: {label}")
        if _USERINFO_RE.search(value):
            raise RegistryInputError(
                "secret suspected in %s: 資格情報付きURL" % name)
        if _COOKIE_RE.search(value):
            raise RegistryInputError(
                "secret suspected in %s: Cookie" % name)
        if _SK_PLACEHOLDER_RE.search(value):
            raise RegistryInputError(
                "secret suspected in %s: APIキー(sk-)" % name)
        if _API_KEY_LIKE_RE.search(value):
            raise RegistryInputError(
                "secret suspected in %s: APIキー(sk-)" % name)
        if _BEARER_LIKE_RE.search(value):
            raise RegistryInputError(
                "secret suspected in %s: Bearerトークン" % name)


def _strip_port(host: str) -> str:
    return str(host or "").split(":")[0].strip().lower()


def _is_reserved_host(host: str) -> bool:
    text = _strip_port(host)
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


def _is_reserved_email(value: str) -> bool:
    text = str(value or "").strip()
    if "@" not in text:
        return False
    domain = text.rsplit("@", 1)[1].strip().lower().rstrip("。、.")
    return _is_reserved_host(domain)


def _host_of_url(value: str) -> str:
    try:
        return str(urlparse(str(value or "").strip()).hostname or "")
    except (AttributeError, TypeError, ValueError):
        return ""


def _looks_like_url(value: str) -> bool:
    text = str(value or "").strip()
    if _URL_RE.search(text):
        return True
    if text.startswith("www."):
        return True
    return False


def _has_fictional_marker(value: str) -> bool:
    low = str(value or "").lower()
    for marker in _FICTIONAL_MARKERS:
        if marker in low:
            return True
    if re.search(r"0{4,}|9{4,}", str(value or "")):
        return True
    return False


def _ensure_fictional_allowed(value_fields: dict[str, str],
                              source_kind: str) -> None:
    """fictional は予約済みの無効な値に限る。実在しそうな値は拒否する。"""
    if source_kind != "fictional":
        return
    for name, raw in value_fields.items():
        value = str(raw or "").strip()
        if not value:
            continue
        emails = _EMAIL_RE.findall(value)
        if emails:
            for email in emails:
                if not _is_reserved_email(email):
                    raise RegistryInputError(
                        f"fictional must use reserved domains: {name}")
            continue
        if _looks_like_url(value):
            host = _host_of_url(value)
            if not host:
                host = value
            if not _is_reserved_host(host):
                raise RegistryInputError(
                    f"fictional must use reserved hosts: {name}")
            continue
        if not _has_fictional_marker(value):
            raise RegistryInputError(
                f"fictional must be reserved or invalid values: {name}")


def _require_actor(actor: str) -> str:
    name = str(actor or "").strip()
    if not name:
        raise RegistryInputError("actor is required")
    return name[:100]


def _require_memo(memo: str, field: str = "memo") -> str:
    text = str(memo or "").strip()
    if not text:
        raise RegistryInputError(f"{field} is required")
    return text[:2000]


def _parse_epoch(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RegistryInputError(f"{field} is invalid")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise RegistryInputError(f"{field} is invalid") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _validate_contact_value(kind: str, value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RegistryInputError("public_value is required")
    if len(text) > 2000:
        raise RegistryInputError("public_value is too long")
    if kind == "email":
        if not re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
                             text):
            raise RegistryInputError("email value is invalid")
    elif kind == "phone":
        digits = re.sub(r"\D", "", text)
        if not _PHONE_RE.match(text) or len(digits) < 7:
            raise RegistryInputError("phone value is invalid")
    elif kind == "form":
        try:
            parsed = urlparse(text)
        except (AttributeError, TypeError, ValueError):
            raise RegistryInputError("form value must be a URL") from None
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise RegistryInputError("form value must be a URL")
        if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
            raise RegistryInputError("form value must not contain credentials")
    else:
        raise RegistryInputError(f"unknown contact type: {kind}")
    return text


def _validate_profile_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 2000:
        raise RegistryInputError("profile_url is too long")
    try:
        parsed = urlparse(text)
    except (AttributeError, TypeError, ValueError):
        raise RegistryInputError("profile_url must be a URL") from None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RegistryInputError("profile_url must be a URL")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise RegistryInputError("profile_url must not contain credentials")
    return text


def _validate_handle(value: str) -> str:
    text = str(value or "").strip()
    if len(text) > 200:
        raise RegistryInputError("handle is too long")
    if text and re.search(r"\s", text):
        raise RegistryInputError("handle is invalid")
    return text


# ----------------------------------------------------------------------------
# 読取(現在と履歴の分離)
# ----------------------------------------------------------------------------

def _table_names(db: sqlite3.Connection) -> set[str]:
    try:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.Error as exc:
        raise RegistryReadError(
            "table_unreadable", sqlite_error=_type_name(exc)) from exc
    return {str(row[0]) for row in rows}


def _split_current_and_history(items: list[dict]) -> tuple[list[dict], list[dict]]:
    superseded: set[int] = set()
    for item in items:
        target = item.get("supersedes_id")
        if target is None:
            continue
        try:
            superseded.add(int(target))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    current = [item for item in items
               if _row_id(item) not in superseded]
    return current, list(items)


def _row_id(item: dict) -> int | None:
    try:
        return int(item.get("id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _sup_id(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _version_of(row: dict) -> int:
    """行の版番号。読めない・1未満は1に倒さず1として扱うのではなく1を返す。

    失敗値を成功の大きな版に読み替えないため、範囲外は最小の1にする。
    """
    try:
        value = int(row.get("version"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1
    if value < 1:
        return 1
    return value


def _contact_view(row: dict) -> dict:
    return {
        "id": int(row.get("id") or 0),
        "route_id": str(row.get("route_id") or ""),
        "project_id": str(row.get("project_id") or ""),
        "campaign_id": str(row.get("campaign_id") or ""),
        "type": str(row.get("type") or ""),
        "public_value": str(row.get("public_value") or ""),
        "display_label": str(row.get("display_label") or ""),
        "source_kind": str(row.get("source_kind") or ""),
        "source_ref": str(row.get("source_ref") or ""),
        "verified_at": row.get("verified_at"),
        "expires_at": row.get("expires_at"),
        "version": _version_of(row),
        "supersedes_id": _sup_id(row.get("supersedes_id")),
        "actor": str(row.get("actor") or ""),
        "created_at": row.get("created_at"),
    }


def _channel_view(row: dict) -> dict:
    return {
        "id": int(row.get("id") or 0),
        "project_id": str(row.get("project_id") or ""),
        "campaign_id": str(row.get("campaign_id") or ""),
        "platform": str(row.get("platform") or ""),
        "enabled": bool(row.get("enabled")),
        "purpose": str(row.get("purpose") or ""),
        "priority": int(row.get("priority") or 0),
        "account_ref": str(row.get("account_ref") or ""),
        "version": _version_of(row),
        "supersedes_id": _sup_id(row.get("supersedes_id")),
        "actor": str(row.get("actor") or ""),
        "created_at": row.get("created_at"),
    }


def _identity_view(row: dict) -> dict:
    return {
        "id": int(row.get("id") or 0),
        "identity_id": str(row.get("identity_id") or ""),
        "project_id": str(row.get("project_id") or ""),
        "campaign_id": str(row.get("campaign_id") or ""),
        "platform": str(row.get("platform") or ""),
        "handle": str(row.get("handle") or ""),
        "profile_url": str(row.get("profile_url") or ""),
        "source_kind": str(row.get("source_kind") or ""),
        "source_ref": str(row.get("source_ref") or ""),
        "owner_label": str(row.get("owner_label") or ""),
        "verified_at": row.get("verified_at"),
        "expires_at": row.get("expires_at"),
        "version": _version_of(row),
        "supersedes_id": _sup_id(row.get("supersedes_id")),
        "actor": str(row.get("actor") or ""),
        "created_at": row.get("created_at"),
    }


def _operating_mode_view(row: dict) -> dict:
    return {
        "id": int(row.get("id") or 0),
        "project_id": str(row.get("project_id") or ""),
        "mode": str(row.get("mode") or ""),
        "actor": str(row.get("actor") or ""),
        "reason": str(row.get("reason") or ""),
        "version": _version_of(row),
        "supersedes_id": _sup_id(row.get("supersedes_id")),
        "created_at": row.get("created_at"),
    }


def _validate_operating_mode(mode: str) -> str:
    text = str(mode or "").strip()
    if text not in OPERATING_MODES:
        raise RegistryInputError("operating mode must be demo_test or production")
    return text


def list_operating_modes(memory_path: str | Path, project_id: str) -> dict:
    """運用区分の現在と履歴を分けて返す。読取失敗は例外。DB無し・表無しは未設定。"""
    pid = str(project_id or "")
    if not pid:
        raise RegistryInputError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return {"project_id": pid, "current": None, "history": [],
                "status": "unset"}
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "operating_modes" not in names:
            return {"project_id": pid, "current": None, "history": [],
                    "status": "unset"}
        try:
            rows = db.execute(
                "SELECT * FROM operating_modes WHERE project_id=? ORDER BY id",
                (pid,)).fetchall()
        except sqlite3.Error as exc:
            raise RegistryReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        items = [dict(r) for r in rows]
    current, history = _split_current_and_history(items)
    viewed_current = [_operating_mode_view(r) for r in current]
    return {"project_id": pid,
            "current": viewed_current[0] if viewed_current else None,
            "history": [_operating_mode_view(r) for r in history],
            "status": "ok" if viewed_current else "unset"}


def current_operating_mode(memory_path: str | Path, project_id: str) -> dict | None:
    """現在の版だけ返す。読取失敗は例外。DB無し・表無し・空はNone(未設定)。"""
    viewed = list_operating_modes(memory_path, project_id)
    current = viewed.get("current") if isinstance(viewed, dict) else None
    return dict(current) if isinstance(current, dict) else None


def save_operating_mode(memory_path: str | Path, *, project_id: str,
                        mode: str, actor: str, reason: str = "") -> dict:
    """運用区分を追記型の版として保存する。demo_test は担当者と理由メモが必須。

    production への切替は明示の操作だけ。自動では demo_test から切り替えない
    (この関数は mode をそのまま記録し、推測で production にしない)。
    """
    pid = str(project_id or "")
    if not pid:
        raise RegistryInputError("project_id is required")
    wanted = _validate_operating_mode(mode)
    who = _require_actor(actor)
    note = str(reason or "").strip()
    if wanted == "demo_test":
        note = _require_memo(reason)
    elif len(note) > 2000:
        raise RegistryInputError("reason is too long")
    _reject_auth_secrets({"reason": note, "actor": who})
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            prev = db.execute(
                "SELECT * FROM operating_modes WHERE project_id=? "
                "ORDER BY version DESC, id DESC LIMIT 1",
                (pid,)).fetchone()
            if prev is not None:
                old = dict(prev)
                # 同じ区分の再保存は新しい版として残す(履歴のため冪等にしない)。
                # 自動の production 切替は行わない(明示の保存だけ)。
                version = _version_of(old) + 1
                supersedes = int(old.get("id") or 0)
            else:
                version = 1
                supersedes = None
            now = time.time()
            cursor = db.execute(
                """INSERT INTO operating_modes(
                    project_id, mode, actor, reason, version,
                    supersedes_id, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (pid, wanted, who, note[:2000], version, supersedes, now),
            )
            db.commit()
            saved = db.execute(
                "SELECT * FROM operating_modes WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    return _operating_mode_view(dict(saved))


def operating_mode_to_purpose(mode: str | None) -> str:
    """運用区分から検証用途を決める。未設定は public(厳しい側)。"""
    return "internal_demo" if str(mode or "") == "demo_test" else "public"


def resolve_contact_purpose(memory_path: str | Path, project_id: str) -> dict:
    """運用区分から用途を解決する。未設定・読取不能は public(厳しい側)。

    読取失敗を internal_demo に倒さない。戻り値は {purpose, mode, status}。
    status は ok/unset/unreadable。未設定は mode=None・purpose=public。
    読取不能は mode=None・purpose=public・status=unreadable。
    """
    pid = str(project_id or "")
    if not pid:
        raise RegistryInputError("project_id is required")
    try:
        current = current_operating_mode(memory_path, pid)
    except RegistryReadError as exc:
        return {"purpose": "public", "mode": None, "status": "unreadable",
                "failure_kind": exc.kind, "path_kind": exc.path_kind,
                "sqlite_error": exc.sqlite_error, "project_id": pid}
    if not isinstance(current, dict) or not str(current.get("mode") or ""):
        return {"purpose": "public", "mode": None, "status": "unset",
                "project_id": pid}
    mode = str(current.get("mode") or "")
    if mode not in OPERATING_MODES:
        return {"purpose": "public", "mode": None, "status": "unset",
                "project_id": pid}
    return {"purpose": operating_mode_to_purpose(mode), "mode": mode,
            "status": "ok", "project_id": pid,
            "version": current.get("version"),
            "actor": str(current.get("actor") or ""),
            "reason": str(current.get("reason") or "")}


def _load_all(db: sqlite3.Connection, project_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    try:
        route_rows = db.execute(
            "SELECT * FROM contact_routes WHERE project_id=? ORDER BY id",
            (project_id,)).fetchall()
        channel_rows = db.execute(
            "SELECT * FROM channel_selections WHERE project_id=? ORDER BY id",
            (project_id,)).fetchall()
        identity_rows = db.execute(
            "SELECT * FROM public_identities WHERE project_id=? ORDER BY id",
            (project_id,)).fetchall()
    except sqlite3.Error as exc:
        raise RegistryReadError(
            "row_unreadable", sqlite_error=_type_name(exc)) from exc
    return ([dict(r) for r in route_rows],
            [dict(r) for r in channel_rows],
            [dict(r) for r in identity_rows])


def list_contact_routes(memory_path: str | Path, project_id: str) -> dict:
    """現在の値と履歴を分けて返す。読取失敗は例外。DB無し・表無しは未登録。"""
    pid = str(project_id or "")
    if not pid:
        raise RegistryInputError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return {"project_id": pid, "current": [], "history": [],
                "status": "unregistered"}
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "contact_routes" not in names:
            return {"project_id": pid, "current": [], "history": [],
                    "status": "unregistered"}
        try:
            rows = db.execute(
                "SELECT * FROM contact_routes WHERE project_id=? ORDER BY id",
                (pid,)).fetchall()
        except sqlite3.Error as exc:
            raise RegistryReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        items = [dict(r) for r in rows]
    current, history = _split_current_and_history(items)
    return {"project_id": pid,
            "current": [_contact_view(r) for r in current],
            "history": [_contact_view(r) for r in history],
            "status": "ok"}


def list_channel_selections(memory_path: str | Path, project_id: str) -> dict:
    pid = str(project_id or "")
    if not pid:
        raise RegistryInputError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return {"project_id": pid, "current": [], "history": [],
                "status": "unregistered"}
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "channel_selections" not in names:
            return {"project_id": pid, "current": [], "history": [],
                    "status": "unregistered"}
        try:
            rows = db.execute(
                "SELECT * FROM channel_selections WHERE project_id=? ORDER BY id",
                (pid,)).fetchall()
        except sqlite3.Error as exc:
            raise RegistryReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        items = [dict(r) for r in rows]
    current, history = _split_current_and_history(items)
    return {"project_id": pid,
            "current": [_channel_view(r) for r in current],
            "history": [_channel_view(r) for r in history],
            "status": "ok"}


def list_public_identities(memory_path: str | Path, project_id: str) -> dict:
    pid = str(project_id or "")
    if not pid:
        raise RegistryInputError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return {"project_id": pid, "current": [], "history": [],
                "status": "unregistered"}
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "public_identities" not in names:
            return {"project_id": pid, "current": [], "history": [],
                    "status": "unregistered"}
        try:
            rows = db.execute(
                "SELECT * FROM public_identities WHERE project_id=? ORDER BY id",
                (pid,)).fetchall()
        except sqlite3.Error as exc:
            raise RegistryReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        items = [dict(r) for r in rows]
    current, history = _split_current_and_history(items)
    return {"project_id": pid,
            "current": [_identity_view(r) for r in current],
            "history": [_identity_view(r) for r in history],
            "status": "ok"}


# ----------------------------------------------------------------------------
# 登録(追記型の版)
# ----------------------------------------------------------------------------

def register_contact_route(
    memory_path: str | Path, *, project_id: str, type: str,
    public_value: str, display_label: str = "", source_kind: str = "",
    source_ref: str = "", campaign_id: str = "", route_id: str = "",
    actor: str = "", expires_at: object = None,
) -> dict:
    """問い合わせ窓口を登録する。同じ route_id は新しい版として追加する。

    初期登録の source_kind は fictional / registered のみ。verified /
    retired は確認・失効の操作で行う。fictional から他への変更は不可。
    """
    pid = str(project_id or "")
    kind = str(type or "").strip()
    value = str(public_value or "").strip()
    label = str(display_label or "")
    skind = str(source_kind or "").strip()
    sref = str(source_ref or "")
    camp = str(campaign_id or "")
    rid = str(route_id or "").strip()
    who = _require_actor(actor)
    if not pid:
        raise RegistryInputError("project_id is required")
    if kind not in CONTACT_TYPES:
        raise RegistryInputError(f"unknown contact type: {kind}")
    if skind not in REGISTERABLE_KINDS:
        raise RegistryInputError(
            "source_kind must be fictional or registered for new registration")
    if len(camp) > 128:
        raise RegistryInputError("campaign_id is too long")
    if rid and len(rid) > 128:
        raise RegistryInputError("route_id is too long")
    if len(label) > 500:
        raise RegistryInputError("display_label is too long")
    if len(sref) > 500:
        raise RegistryInputError("source_ref is too long")
    value = _validate_contact_value(kind, value)
    _reject_auth_secrets({
        "public_value": value, "display_label": label,
        "source_ref": sref, "account_ref": "", "handle": "",
        "profile_url": "",
    })
    _ensure_fictional_allowed({"public_value": value}, skind)
    expires = _parse_epoch(expires_at, "expires_at")
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            current_id = rid
            if not current_id:
                current_id = f"route_{uuid.uuid4().hex[:16]}"
            prev = db.execute(
                "SELECT * FROM contact_routes WHERE project_id=? AND route_id=? "
                "ORDER BY version DESC, id DESC LIMIT 1",
                (pid, current_id)).fetchone()
            if prev is not None:
                old = dict(prev)
                if str(old.get("source_kind") or "") == "retired":
                    raise RegistryConflictError(
                        "retired route cannot be changed; register a new route")
                if str(old.get("source_kind") or "") != skind:
                    raise RegistryInputError(
                        "source_kind change requires verify or retire operation")
                replaced = db.execute(
                    "SELECT id FROM contact_routes WHERE project_id=? AND route_id=? "
                    "AND supersedes_id IS NULL AND id != ? LIMIT 1",
                    (pid, current_id, int(old.get("id") or 0))).fetchone()
                if replaced is not None:
                    raise RegistryConflictError("route has multiple current versions")
                version = _version_of(old) + 1
                supersedes = int(old.get("id") or 0)
            else:
                version = 1
                supersedes = None
            now = time.time()
            cursor = db.execute(
                """INSERT INTO contact_routes(
                    route_id, project_id, campaign_id, type, public_value,
                    display_label, source_kind, source_ref, verified_at,
                    expires_at, version, supersedes_id, actor, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (current_id, pid, camp, kind, value, label[:500], skind,
                 sref[:500], None, expires, version, supersedes, who, now),
            )
            db.commit()
            saved = db.execute(
                "SELECT * FROM contact_routes WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    return _contact_view(dict(saved))


def register_channel_selection(
    memory_path: str | Path, *, project_id: str, platform: str,
    enabled: bool, purpose: str = "", priority: int = 0,
    account_ref: str = "", campaign_id: str = "", actor: str = "",
) -> dict:
    """発信媒体の選択を登録する。同じ媒体・キャンペーンは新しい版として追加する。"""
    pid = str(project_id or "")
    plat = str(platform or "").strip()
    camp = str(campaign_id or "")
    ref = str(account_ref or "").strip()
    purp = str(purpose or "")
    who = _require_actor(actor)
    if not pid:
        raise RegistryInputError("project_id is required")
    if plat not in PLATFORMS:
        raise RegistryInputError(f"unknown platform: {plat}")
    if len(camp) > 128:
        raise RegistryInputError("campaign_id is too long")
    if len(purp) > 500:
        raise RegistryInputError("purpose is too long")
    if len(ref) > 200:
        raise RegistryInputError("account_ref is too long")
    if ref and re.search(r"\s", ref):
        raise RegistryInputError("account_ref is invalid")
    try:
        prio = int(priority)
    except (TypeError, ValueError):
        raise RegistryInputError("priority is invalid") from None
    _reject_auth_secrets({"account_ref": ref, "purpose": purp})
    _ensure_fictional_allowed({"account_ref": ref}, "registered")
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            prev = db.execute(
                "SELECT * FROM channel_selections WHERE project_id=? AND platform=? "
                "AND campaign_id=? ORDER BY version DESC, id DESC LIMIT 1",
                (pid, plat, camp)).fetchone()
            if prev is not None:
                old = dict(prev)
                version = _version_of(old) + 1
                supersedes = int(old.get("id") or 0)
            else:
                version = 1
                supersedes = None
            now = time.time()
            cursor = db.execute(
                """INSERT INTO channel_selections(
                    project_id, campaign_id, platform, enabled, purpose,
                    priority, account_ref, version, supersedes_id, actor,
                    created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, camp, plat, 1 if enabled else 0, purp[:500], prio,
                 ref, version, supersedes, who, now),
            )
            db.commit()
            saved = db.execute(
                "SELECT * FROM channel_selections WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    return _channel_view(dict(saved))


def register_public_identity(
    memory_path: str | Path, *, project_id: str, platform: str,
    handle: str = "", profile_url: str = "", source_kind: str = "",
    source_ref: str = "", owner_label: str = "", campaign_id: str = "",
    identity_id: str = "", actor: str = "", expires_at: object = None,
) -> dict:
    """使用IDを登録する。同じ identity_id は新しい版として追加する。

    初期登録の source_kind は fictional / registered のみ。秘密情報は
    保存しない。verified への変更は verify_public_identity で行う。
    """
    pid = str(project_id or "")
    plat = str(platform or "").strip()
    hand = _validate_handle(handle)
    url = _validate_profile_url(profile_url)
    skind = str(source_kind or "").strip()
    sref = str(source_ref or "")
    owner = str(owner_label or "")
    camp = str(campaign_id or "")
    iid = str(identity_id or "").strip()
    who = _require_actor(actor)
    if not pid:
        raise RegistryInputError("project_id is required")
    if plat not in PLATFORMS:
        raise RegistryInputError(f"unknown platform: {plat}")
    if not hand and not url:
        raise RegistryInputError("handle or profile_url is required")
    if skind not in REGISTERABLE_KINDS:
        raise RegistryInputError(
            "source_kind must be fictional or registered for new registration")
    if len(camp) > 128:
        raise RegistryInputError("campaign_id is too long")
    if iid and len(iid) > 128:
        raise RegistryInputError("identity_id is too long")
    if len(sref) > 500:
        raise RegistryInputError("source_ref is too long")
    if len(owner) > 500:
        raise RegistryInputError("owner_label is too long")
    _reject_auth_secrets({
        "handle": hand, "profile_url": url, "owner_label": owner,
        "source_ref": sref, "public_value": "", "display_label": "",
        "account_ref": "",
    })
    _ensure_fictional_allowed({"handle": hand, "profile_url": url}, skind)
    expires = _parse_epoch(expires_at, "expires_at")
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            current_id = iid
            if not current_id:
                current_id = f"identity_{uuid.uuid4().hex[:16]}"
            prev = db.execute(
                "SELECT * FROM public_identities WHERE project_id=? AND identity_id=? "
                "ORDER BY version DESC, id DESC LIMIT 1",
                (pid, current_id)).fetchone()
            if prev is not None:
                old = dict(prev)
                if str(old.get("source_kind") or "") == "retired":
                    raise RegistryConflictError(
                        "retired identity cannot be changed; register a new identity")
                if str(old.get("source_kind") or "") != skind:
                    raise RegistryInputError(
                        "source_kind change requires verify or retire operation")
                version = _version_of(old) + 1
                supersedes = int(old.get("id") or 0)
            else:
                version = 1
                supersedes = None
            now = time.time()
            cursor = db.execute(
                """INSERT INTO public_identities(
                    identity_id, project_id, campaign_id, platform, handle,
                    profile_url, source_kind, source_ref, owner_label,
                    verified_at, expires_at, version, supersedes_id, actor,
                    created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (current_id, pid, camp, plat, hand, url, skind, sref[:500],
                 owner[:500], None, expires, version, supersedes, who, now),
            )
            db.commit()
            saved = db.execute(
                "SELECT * FROM public_identities WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    return _identity_view(dict(saved))


def _current_route_locked(db: sqlite3.Connection, project_id: str,
                          route_id: str) -> dict:
    row = db.execute(
        "SELECT * FROM contact_routes WHERE project_id=? AND route_id=? "
        "ORDER BY version DESC, id DESC LIMIT 1",
        (project_id, route_id)).fetchone()
    if row is None:
        raise RegistryConflictError(f"route not found: {route_id}")
    return dict(row)


def _current_identity_locked(db: sqlite3.Connection, project_id: str,
                             identity_id: str) -> dict:
    row = db.execute(
        "SELECT * FROM public_identities WHERE project_id=? AND identity_id=? "
        "ORDER BY version DESC, id DESC LIMIT 1",
        (project_id, identity_id)).fetchone()
    if row is None:
        raise RegistryConflictError(f"identity not found: {identity_id}")
    return dict(row)


def verify_contact_route(
    memory_path: str | Path, project_id: str, route_id: str, *,
    actor: str, memo: str,
) -> dict:
    """確認操作。registered → verified への変更はこの操作だけで行う。

    担当者と確認の根拠のメモが必須。fictional の昇格は不可。retired は不可。
    既に verified なら冪等に成功を返す。
    """
    pid = str(project_id or "")
    rid = str(route_id or "").strip()
    who = _require_actor(actor)
    note = _require_memo(memo)
    if not pid or not rid:
        raise RegistryInputError("project_id and route_id are required")
    _reject_auth_secrets({"memo": note})
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            old = _current_route_locked(db, pid, rid)
            skind = str(old.get("source_kind") or "")
            if skind == "retired":
                raise RegistryConflictError("retired route cannot be verified")
            if skind == "fictional":
                raise RegistryConflictError(
                    "fictional cannot be promoted; register a real value anew")
            if skind == "verified":
                return {**_contact_view(old), "idempotent": True}
            if skind != "registered":
                raise RegistryConflictError(
                    f"route cannot be verified from {skind}")
            now = time.time()
            cursor = db.execute(
                """INSERT INTO contact_routes(
                    route_id, project_id, campaign_id, type, public_value,
                    display_label, source_kind, source_ref, verified_at,
                    expires_at, version, supersedes_id, actor, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(old.get("route_id") or ""),
                 str(old.get("project_id") or ""),
                 str(old.get("campaign_id") or ""),
                 str(old.get("type") or ""),
                 str(old.get("public_value") or ""),
                 str(old.get("display_label") or ""),
                 "verified",
                 ("verify: " + note)[:500],
                 now, old.get("expires_at"),
                 _version_of(old) + 1,
                 int(old.get("id") or 0), who, now),
            )
            db.commit()
            saved = db.execute(
                "SELECT * FROM contact_routes WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    result = _contact_view(dict(saved))
    result["idempotent"] = False
    return result


def retire_contact_route(
    memory_path: str | Path, project_id: str, route_id: str, *,
    actor: str, reason: str,
) -> dict:
    """失効操作。retired は元に戻せない。既に retired なら冪等に成功を返す。"""
    pid = str(project_id or "")
    rid = str(route_id or "").strip()
    who = _require_actor(actor)
    note = _require_memo(reason, "reason")
    if not pid or not rid:
        raise RegistryInputError("project_id and route_id are required")
    _reject_auth_secrets({"reason": note})
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            old = _current_route_locked(db, pid, rid)
            if str(old.get("source_kind") or "") == "retired":
                return {**_contact_view(old), "idempotent": True}
            now = time.time()
            cursor = db.execute(
                """INSERT INTO contact_routes(
                    route_id, project_id, campaign_id, type, public_value,
                    display_label, source_kind, source_ref, verified_at,
                    expires_at, version, supersedes_id, actor, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(old.get("route_id") or ""),
                 str(old.get("project_id") or ""),
                 str(old.get("campaign_id") or ""),
                 str(old.get("type") or ""),
                 str(old.get("public_value") or ""),
                 str(old.get("display_label") or ""),
                 "retired",
                 ("retire: " + note)[:500],
                 old.get("verified_at"), old.get("expires_at"),
                 _version_of(old) + 1,
                 int(old.get("id") or 0), who, now),
            )
            db.commit()
            saved = db.execute(
                "SELECT * FROM contact_routes WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    result = _contact_view(dict(saved))
    result["idempotent"] = False
    return result


def verify_public_identity(
    memory_path: str | Path, project_id: str, identity_id: str, *,
    actor: str, memo: str,
) -> dict:
    """使用IDの確認操作。担当者と根拠メモが必須。fictional の昇格は不可。"""
    pid = str(project_id or "")
    iid = str(identity_id or "").strip()
    who = _require_actor(actor)
    note = _require_memo(memo)
    if not pid or not iid:
        raise RegistryInputError("project_id and identity_id are required")
    _reject_auth_secrets({"memo": note})
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            old = _current_identity_locked(db, pid, iid)
            skind = str(old.get("source_kind") or "")
            if skind == "retired":
                raise RegistryConflictError("retired identity cannot be verified")
            if skind == "fictional":
                raise RegistryConflictError(
                    "fictional cannot be promoted; register a real value anew")
            if skind == "verified":
                return {**_identity_view(old), "idempotent": True}
            if skind != "registered":
                raise RegistryConflictError(
                    f"identity cannot be verified from {skind}")
            now = time.time()
            cursor = db.execute(
                """INSERT INTO public_identities(
                    identity_id, project_id, campaign_id, platform, handle,
                    profile_url, source_kind, source_ref, owner_label,
                    verified_at, expires_at, version, supersedes_id, actor,
                    created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(old.get("identity_id") or ""),
                 str(old.get("project_id") or ""),
                 str(old.get("campaign_id") or ""),
                 str(old.get("platform") or ""),
                 str(old.get("handle") or ""),
                 str(old.get("profile_url") or ""),
                 "verified",
                 ("verify: " + note)[:500],
                 str(old.get("owner_label") or ""),
                 now, old.get("expires_at"),
                 _version_of(old) + 1,
                 int(old.get("id") or 0), who, now),
            )
            db.commit()
            saved = db.execute(
                "SELECT * FROM public_identities WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    result = _identity_view(dict(saved))
    result["idempotent"] = False
    return result


def retire_public_identity(
    memory_path: str | Path, project_id: str, identity_id: str, *,
    actor: str, reason: str,
) -> dict:
    """使用IDの失効操作。retired は元に戻せない。"""
    pid = str(project_id or "")
    iid = str(identity_id or "").strip()
    who = _require_actor(actor)
    note = _require_memo(reason, "reason")
    if not pid or not iid:
        raise RegistryInputError("project_id and identity_id are required")
    _reject_auth_secrets({"reason": note})
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            old = _current_identity_locked(db, pid, iid)
            if str(old.get("source_kind") or "") == "retired":
                return {**_identity_view(old), "idempotent": True}
            now = time.time()
            cursor = db.execute(
                """INSERT INTO public_identities(
                    identity_id, project_id, campaign_id, platform, handle,
                    profile_url, source_kind, source_ref, owner_label,
                    verified_at, expires_at, version, supersedes_id, actor,
                    created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(old.get("identity_id") or ""),
                 str(old.get("project_id") or ""),
                 str(old.get("campaign_id") or ""),
                 str(old.get("platform") or ""),
                 str(old.get("handle") or ""),
                 str(old.get("profile_url") or ""),
                 "retired",
                 ("retire: " + note)[:500],
                 str(old.get("owner_label") or ""),
                 old.get("verified_at"), old.get("expires_at"),
                 _version_of(old) + 1,
                 int(old.get("id") or 0), who, now),
            )
            db.commit()
            saved = db.execute(
                "SELECT * FROM public_identities WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    result = _identity_view(dict(saved))
    result["idempotent"] = False
    return result


# ----------------------------------------------------------------------------
# 使用可否の判定(純粋関数)
# ----------------------------------------------------------------------------

def _expires_value(entry: dict) -> float | None:
    raw = entry.get("expires_at")
    if raw is None or raw == "":
        return None
    try:
        if isinstance(raw, bool):
            return None
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()


def usable_for(entry: dict | None, purpose: str, now: float | None = None) -> dict:
    """値が目的に使えるかを判定する純粋関数。副作用なし。

    purpose は internal_demo / public。fictional は internal_demo のみ可。
    registered は public で要確認(使えない)。verified のみ可。retired・
    失効・未登録は不可(理由つき)。空文字を『問い合わせ不要』と解釈しない。
    """
    if purpose not in PURPOSES:
        raise RegistryInputError(f"unknown purpose: {purpose}")
    at = float(now) if now is not None else time.time()
    if not isinstance(entry, dict) or not entry:
        return {"usable": False, "reason": "unregistered",
                "message_ja": "未登録のため使えません",
                "source_kind": "", "purpose": purpose}
    skind = str(entry.get("source_kind") or "")
    if not skind:
        return {"usable": False, "reason": "unregistered",
                "message_ja": "出所が不明のため使えません",
                "source_kind": skind, "purpose": purpose}
    expires = _expires_value(entry)
    if expires is not None and at >= expires:
        return {"usable": False, "reason": "expired",
                "message_ja": "期限切れのため失効として扱います",
                "source_kind": skind, "purpose": purpose}
    if skind == "retired":
        return {"usable": False, "reason": "retired",
                "message_ja": "失効のため使えません",
                "source_kind": skind, "purpose": purpose}
    if skind == "fictional":
        if purpose == "internal_demo":
            return {"usable": True, "reason": "ok_fictional_internal",
                    "message_ja": "内部デモに限り使えます",
                    "source_kind": skind, "purpose": purpose}
        return {"usable": False, "reason": "fictional_not_for_public",
                "message_ja": "架空値は公開・外部送信・顧客接触に使えません",
                "source_kind": skind, "purpose": purpose}
    if skind == "registered":
        if purpose == "internal_demo":
            return {"usable": True, "reason": "ok_registered_internal",
                    "message_ja": "内部利用に限り参照できます",
                    "source_kind": skind, "purpose": purpose}
        return {"usable": False, "reason": "needs_confirmation",
                "message_ja": "未確認のため公開用途では要確認です",
                "source_kind": skind, "purpose": purpose}
    if skind == "verified":
        return {"usable": True, "reason": "ok_verified",
                "message_ja": "確認済みのため使えます",
                "source_kind": skind, "purpose": purpose}
    return {"usable": False, "reason": "unknown_source",
            "message_ja": "不明な出所のため使えません",
            "source_kind": skind, "purpose": purpose}


# ----------------------------------------------------------------------------
# 事前確認(preflight)
# ----------------------------------------------------------------------------

def _normalize_purposes(purposes: object) -> set[str]:
    if purposes is None:
        return {"public"}
    if isinstance(purposes, str):
        items = [purposes]
    else:
        try:
            items = list(purposes)  # type: ignore[arg-type]
        except TypeError:
            raise RegistryInputError("purposes is invalid") from None
    normalized = {str(item or "").strip() for item in items if str(item or "").strip()}
    if not normalized:
        return {"public"}
    unknown = normalized - PURPOSES
    if unknown:
        raise RegistryInputError(f"unknown purpose: {sorted(unknown)[0]}")
    return normalized


def _campaign_match(row_campaign: str, wanted: str | None) -> bool:
    if wanted is None:
        return True
    if str(row_campaign or "") == "":
        return True
    return str(row_campaign or "") == str(wanted)


def preflight(memory_path: str | Path, project_id: str,
              campaign_id: str | None = None,
              purposes: object = None,
              now: float | None = None) -> dict:
    """計画生成前の事前確認。必要な値と状態を検査する。

    missing_input は欠落している項目・入力欄の名前・次の行動の文を返す。
    独立した作業(案内文の草案など)は続行可能とし、公開・投稿・顧客接触を
    伴うタスクだけが値と承認が揃うまで実行不可(blocking_tasks)。
    方針だけではURLがあるとみなさない(実値が無ければ入力待ち)。
    読取失敗は例外(未登録に倒さない)。DB無し・表無しは未登録扱い。
    """
    pid = str(project_id or "")
    if not pid:
        raise RegistryInputError("project_id is required")
    wanted = str(campaign_id) if campaign_id is not None else None
    if wanted is not None and len(wanted) > 128:
        raise RegistryInputError("campaign_id is too long")
    need = _normalize_purposes(purposes)
    need_public = "public" in need
    check_purpose = "public" if need_public else "internal_demo"
    at = float(now) if now is not None else time.time()
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        routes: list[dict] = []
        channels: list[dict] = []
        identities: list[dict] = []
    else:
        with _connect_ro(memory_path) as db:
            names = _table_names(db)
            if ("contact_routes" not in names
                    and "channel_selections" not in names
                    and "public_identities" not in names):
                routes = []
                channels = []
                identities = []
            else:
                for required in ("contact_routes", "channel_selections",
                                 "public_identities"):
                    if required not in names:
                        continue
                try:
                    route_rows = db.execute(
                        "SELECT * FROM contact_routes WHERE project_id=? ORDER BY id",
                        (pid,)).fetchall() if "contact_routes" in names else []
                    channel_rows = db.execute(
                        "SELECT * FROM channel_selections WHERE project_id=? ORDER BY id",
                        (pid,)).fetchall() if "channel_selections" in names else []
                    identity_rows = db.execute(
                        "SELECT * FROM public_identities WHERE project_id=? ORDER BY id",
                        (pid,)).fetchall() if "public_identities" in names else []
                except sqlite3.Error as exc:
                    raise RegistryReadError(
                        "row_unreadable",
                        path_kind=classify_ledger_path_kind(path),
                        sqlite_error=_type_name(exc)) from exc
                routes = [dict(r) for r in route_rows]
                channels = [dict(r) for r in channel_rows]
                identities = [dict(r) for r in identity_rows]
    route_current, _ = _split_current_and_history(routes)
    channel_current, _ = _split_current_and_history(channels)
    identity_current, _ = _split_current_and_history(identities)
    if wanted is not None:
        route_current = [r for r in route_current
                         if _campaign_match(str(r.get("campaign_id") or ""), wanted)]
        channel_current = [r for r in channel_current
                           if _campaign_match(str(r.get("campaign_id") or ""), wanted)]
        identity_current = [r for r in identity_current
                            if _campaign_match(str(r.get("campaign_id") or ""), wanted)]

    missing_input: list[dict] = []
    blocking_tasks: list[str] = []

    usable_routes = [r for r in route_current
                     if bool(usable_for(r, check_purpose, at).get("usable"))]
    if not usable_routes:
        if not route_current:
            reason = "unregistered"
            action = "問い合わせ方法(フォーム/メール/電話)と実値を登録してください"
        else:
            kinds = {str(r.get("source_kind") or "") for r in route_current}
            if kinds == {"fictional"} and need_public:
                reason = "fictional_not_for_public"
                action = "実値を新しく登録してください(架空値の昇格はできません)"
            elif "registered" in kinds and need_public:
                reason = "needs_confirmation"
                action = "担当者と確認の根拠のメモで確認操作を行ってください"
            elif kinds == {"retired"}:
                reason = "retired"
                action = "新しい登録で置き換えてください(失効は元に戻せません)"
            else:
                expired_only = all(
                    _expires_value(r) is not None and at >= float(_expires_value(r) or 0)
                    for r in route_current)
                if expired_only:
                    reason = "expired"
                    action = "期限が切れたため新しい登録で置き換えてください"
                else:
                    reason = "not_usable"
                    action = "問い合わせ方法と実値・出所を確認してください"
        missing_input.append({
            "field": "contact_route",
            "input": "発信・問い合わせ設定の問い合わせ方法と実値",
            "next_action": action,
            "reason": reason,
        })
        for task in ("customer_contact", "publish"):
            if task not in blocking_tasks:
                blocking_tasks.append(task)

    if not channel_current:
        missing_input.append({
            "field": "channel_selection",
            "input": "発信・問い合わせ設定の使用SNSの選択",
            "next_action": "使用するSNSにチェックを入れてください(使わない場合は無効のまま保存してください)",
            "reason": "unregistered",
        })
        if "social_post" not in blocking_tasks:
            blocking_tasks.append("social_post")
    else:
        enabled = [c for c in channel_current if bool(c.get("enabled"))]
        if enabled:
            for sel in sorted(enabled, key=lambda r: str(r.get("platform") or "")):
                plat = str(sel.get("platform") or "")
                matched = [e for e in identity_current
                           if str(e.get("platform") or "") == plat]
                usable_ids = [e for e in matched
                              if bool(usable_for(e, check_purpose, at).get("usable"))]
                if usable_ids:
                    continue
                if not matched:
                    reason = "unregistered"
                    action = (f"{plat}の公開ID(ハンドルまたはプロフィールURL)を"
                              "登録してください")
                else:
                    kinds = {str(e.get("source_kind") or "") for e in matched}
                    if kinds == {"fictional"} and need_public:
                        reason = "fictional_not_for_public"
                        action = (f"{plat}の実値を新しく登録してください"
                                  "(架空値の昇格はできません)")
                    elif "registered" in kinds and need_public:
                        reason = "needs_confirmation"
                        action = (f"{plat}の使用IDを確認操作で承認してください"
                                  "(担当者と根拠メモが必須です)")
                    else:
                        reason = "not_usable"
                        action = f"{plat}の使用IDを確認してください"
                missing_input.append({
                    "field": f"public_identity:{plat}",
                    "input": f"発信・問い合わせ設定の{plat}の公開ID",
                    "next_action": action,
                    "reason": reason,
                })
                if "social_post" not in blocking_tasks:
                    blocking_tasks.append("social_post")

    return {
        "project_id": pid,
        "campaign_id": wanted,
        "purposes": sorted(need),
        "operating_mode": _preflight_operating_mode(memory_path, pid),
        "public_input_ready": _preflight_public_ready(
            missing_input, blocking_tasks, need_public),
        "missing_input": missing_input,
        "blocking_tasks": sorted(blocking_tasks),
        "can_proceed_independent": True,
        "ready_for_public": (not blocking_tasks) if need_public else True,
        "note": ("独立した作業(案内文の草案など)は続行可能です。"
                 "公開・投稿・顧客接触を伴うタスクだけが値と承認が揃うまで実行不可です。"
                 "方針だけではURLがあるとみなしません。"),
        "checked_at": at,
    }


def _preflight_operating_mode(memory_path: str | Path, project_id: str) -> dict:
    """preflight用の読み取り。失敗しても例外にせず読取不能として返す。"""
    try:
        current = current_operating_mode(memory_path, project_id)
    except RegistryReadError as exc:
        return {"mode": None, "status": "unreadable",
                "failure_kind": exc.kind, "path_kind": exc.path_kind,
                "sqlite_error": exc.sqlite_error}
    except RegistryInputError as exc:
        return {"mode": None, "status": "unreadable",
                "failure_kind": "input_invalid", "detail": str(exc)[:200]}
    if not isinstance(current, dict) or not str(current.get("mode") or ""):
        return {"mode": None, "status": "unset"}
    mode = str(current.get("mode") or "")
    if mode not in OPERATING_MODES:
        return {"mode": None, "status": "unset"}
    return {"mode": mode, "status": "ok",
            "version": current.get("version"),
            "actor": str(current.get("actor") or "")}


def _preflight_public_ready(missing_input: list[dict], blocking_tasks: list[str],
                            need_public: bool) -> dict:
    """公開用途の入力が揃っているかの要約。失敗値は成功にしない。"""
    missing = [str(item.get("field") or "") for item in (missing_input or [])
               if isinstance(item, dict) and str(item.get("field") or "")]
    blocked = sorted({str(task) for task in (blocking_tasks or []) if str(task)})
    ready = (not blocked) if need_public else True
    return {"ready": bool(ready), "missing_fields": sorted(set(missing)),
            "blocking_tasks": blocked}


# ----------------------------------------------------------------------------
# A3用の提供関数(A3で本番配線する)
# ----------------------------------------------------------------------------

def registry_contact_provider(memory_path: str | Path, project_id: str,
                              campaign_id: str | None = None) -> list[dict]:
    """A3が使う提供関数。各要素 {kind, value, source_kind, source_ref,
    route_id, version, expires_at}。

    fictional を含めて返す。呼出側(A3)が usable_for で目的別に選別すること。
    fictional は internal_demo の文脈でのみ使う。公開・外部送信・顧客接触の
    入力に fictional を使ってはならない。A3で本番の計画生成経路から呼ぶ。
    """
    pid = str(project_id or "")
    if not pid:
        raise RegistryInputError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return []
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "contact_routes" not in names:
            return []
        try:
            rows = db.execute(
                "SELECT * FROM contact_routes WHERE project_id=? ORDER BY id",
                (pid,)).fetchall()
        except sqlite3.Error as exc:
            raise RegistryReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        items = [dict(r) for r in rows]
    current, _ = _split_current_and_history(items)
    if campaign_id is not None:
        current = [r for r in current
                   if _campaign_match(str(r.get("campaign_id") or ""),
                                      str(campaign_id))]
    provided: list[dict] = []
    for row in current:
        kind = str(row.get("type") or "").strip()
        if kind not in CONTACT_TYPES:
            continue
        value = str(row.get("public_value") or "").strip()
        if not value:
            continue
        provided.append({
            "kind": kind,
            "value": value,
            "source_kind": str(row.get("source_kind") or ""),
            "source_ref": str(row.get("source_ref") or ""),
            "route_id": str(row.get("route_id") or ""),
            "version": _version_of(row),
            "expires_at": row.get("expires_at"),
        })
    provided.sort(key=lambda e: (e["kind"], e["value"]))
    return provided


def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    """PJ初期化(世代切替)では記録を変えない。証拠として残す。"""
    return {"project_id": str(project_id or ""), "reset": False,
            "note": "問い合わせ・媒体・使用IDの登録は世代切替で変えません"}
