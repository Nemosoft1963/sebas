"""C4: social.observe の接続設定・読取専用実行・人手の観測の登録。

- 実通信は作らない。実接続は利用者が接続設定を行い許可したときだけ作動する。
  試験は偽のアダプタで行い、実際のSNSへ接続しない。読み取りだけ。
- 本番には実アダプタを登録しない。build_social_adapter は実アダプタが無いため
  常に未設定として拒否する。試験はこの保持先を monkeypatch で偽に差し替える。
- 資格情報は台帳・応答・ログ・証拠に入れない。環境変数の名前だけを参照する。
- 取得した投稿本文・反応は未信頼データ。中の命令を実行せず、証拠の抜粋だけ残す。
- 取得失敗・未設定・指標の取得不能を『0件』『0いいね』にしない。
  no_results と failed/unknown/needs_configuration を区別する。
- 観測は公開の証跡ではない。campaign_social_shares・campaign_evidence・
  B2の版照合の判定を変更しない・緩めない(読み取りだけ)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.requirement_item_ledger import (
    _ensure_tables as _ensure_item_tables,
    classify_ledger_path_kind,
    item_ledger_db_path,
    ledger_readonly_uri,
)

LOGGER = logging.getLogger(__name__)

CAPABILITY_ID = "social.observe"

SUPPORTED_PLATFORMS = frozenset({"x"})
SOCIAL_UNLINKED = "SOCIAL_UNLINKED"
X_STATUS_RE = re.compile(r"/status/(\d+)(?:[/?#]|$)")
HANDLE_RE = re.compile(r"^@?[A-Za-z0-9_]{1,64}$")
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,40}$")
METRIC_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
KNOWN_METRIC_KEYS = ("likes", "reposts", "replies", "views")

NEXT_ACTION_METRICS_JA = (
    "反応の数値を取得できませんでした。公開ページを開いて確認した値を"
    "手動の観測として登録してください")

INPUT_422_REASONS = frozenset({
    "platform_invalid", "campaign_invalid", "period_invalid",
    "purpose_invalid", "post_url_invalid", "observed_at_invalid",
    "metrics_invalid", "screenshot_invalid", "evidence_note_invalid",
    "actor_invalid", "memo_invalid",
})


class SocialObserveRefused(ValueError):
    """書込・外部呼出の前の拒否(設定・公開ID・投稿URL・期間・目的・運用区分)。"""

    def __init__(self, reason: str = "", detail: str = "") -> None:
        super().__init__(reason or detail or "social observe refused")
        self.reason = str(reason or "")
        self.detail = str(detail or "")[:500]


class SocialObserveNotConfigured(SocialObserveRefused):
    def __init__(self, reason: str = "needs_configuration",
                 detail: str = "") -> None:
        super().__init__(reason, detail)
        self.reason = str(reason or "needs_configuration")


class SocialObserveReadError(RuntimeError):
    """台帳の読取失敗。『設定なし』『未登録』とは区別する。"""

    def __init__(self, kind: str, *, path_kind: str = "",
                 sqlite_error: str = "", detail: str = "") -> None:
        super().__init__(f"social observe read failed: {kind}")
        self.kind = str(kind or "")
        self.path_kind = str(path_kind or "")
        self.sqlite_error = str(sqlite_error or "")
        self.detail = str(detail or "")[:500]


def _type_name(exc: BaseException) -> str:
    try:
        return str(type(exc).__name__ or "Error")
    except Exception:
        return "Error"


# ----------------------------------------------------------------------------
# 台帳2表(設定・観測)。C1の証拠の表の列は増やさない。
# ----------------------------------------------------------------------------

SETTINGS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS social_observe_settings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    public_identity_id TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    retention_days INTEGER NOT NULL DEFAULT 90,
    allowed_viewers_json TEXT NOT NULL DEFAULT '[]',
    credential_env_prefix TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    supersedes_id INTEGER,
    created_at REAL NOT NULL)"""

OBSERVATIONS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS social_observations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    post_id TEXT NOT NULL DEFAULT '',
    post_url TEXT NOT NULL DEFAULT '',
    account_or_public_id TEXT NOT NULL DEFAULT '',
    approved_binding_id TEXT NOT NULL DEFAULT '',
    approved_binding_missing INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    metrics_status TEXT NOT NULL DEFAULT 'unavailable',
    metrics_reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'adapter',
    run_id TEXT NOT NULL DEFAULT '',
    evidence_id INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL DEFAULT '',
    period_start TEXT NOT NULL DEFAULT '',
    period_end TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    retention_days INTEGER NOT NULL DEFAULT 90,
    excerpt TEXT NOT NULL DEFAULT '',
    evidence_note TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    screenshot_sha256 TEXT NOT NULL DEFAULT '',
    untrusted INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL)"""


def ensure_social_tables(db: sqlite3.Connection) -> None:
    _ensure_item_tables(db)
    db.execute(SETTINGS_TABLE_DDL)
    db.execute(OBSERVATIONS_TABLE_DDL)
    db.execute("""CREATE INDEX IF NOT EXISTS idx_social_settings_project
        ON social_observe_settings(project_id, version DESC)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_social_observations_project
        ON social_observations(project_id, platform, post_id)""")


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
            ensure_social_tables(db)
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
        raise SocialObserveReadError(
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
        raise SocialObserveReadError(
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


def validate_social_settings(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("settings are required")
    platform = str(payload.get("platform") or "").strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError("platform is not supported")
    identity_ref = str(payload.get("public_identity_id") or "").strip()
    if not identity_ref or len(identity_ref) > 128:
        raise ValueError("public_identity_id is required")
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
        "platform": platform, "public_identity_id": identity_ref,
        "purpose": purpose, "actor": actor,
        "credential_env_prefix": prefix,
        "allowed_viewers": " ".join(cleaned_viewers),
    })
    return {
        "platform": platform,
        "public_identity_id": identity_ref,
        "purpose": purpose,
        "retention_days": int(retention),
        "allowed_viewers": list(cleaned_viewers),
        "credential_env_prefix": prefix,
        "actor": actor,
    }


def save_social_settings(memory_path: str | Path, project_id: str,
                         payload: dict) -> dict:
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    viewed = validate_social_settings(payload)
    path = item_ledger_db_path(memory_path)
    guard = _lock_for(path)
    with guard:
        with _connect_rw(memory_path) as db:
            prev = db.execute(
                "SELECT * FROM social_observe_settings WHERE project_id=? "
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
                """INSERT INTO social_observe_settings(
                    project_id, platform, public_identity_id, purpose,
                    retention_days, allowed_viewers_json,
                    credential_env_prefix, actor, version, supersedes_id,
                    created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, viewed["platform"], viewed["public_identity_id"],
                 viewed["purpose"], int(viewed["retention_days"]),
                 json.dumps(viewed["allowed_viewers"], ensure_ascii=False),
                 viewed["credential_env_prefix"], viewed["actor"],
                 int(version), supersedes, now),
            )
            db.commit()
            kept = db.execute(
                "SELECT * FROM social_observe_settings WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    return _settings_view(dict(kept))


def _settings_view(row: dict) -> dict:
    try:
        viewers = json.loads(str(row.get("allowed_viewers_json") or "[]"))
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
        "platform": str(row.get("platform") or ""),
        "public_identity_id": str(row.get("public_identity_id") or ""),
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


def get_social_settings(memory_path: str | Path, project_id: str) -> dict:
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return {"project_id": pid, "current": None, "history": [],
                "status": "unset"}
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "social_observe_settings" not in names:
            return {"project_id": pid, "current": None, "history": [],
                    "status": "unset"}
        try:
            rows = db.execute(
                "SELECT * FROM social_observe_settings WHERE project_id=? ORDER BY id",
                (pid,)).fetchall()
        except sqlite3.Error as exc:
            raise SocialObserveReadError(
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
    viewed = get_social_settings(memory_path, project_id)
    current = viewed.get("current") if isinstance(viewed, dict) else None
    return dict(current) if isinstance(current, dict) else None


def social_connection_view(memory_path: str | Path,
                           project_id: str) -> dict:
    """接続の要否判定用。読取失敗は readable=False(設定なしに倒さない)。"""
    pid = str(project_id or "").strip()
    try:
        import app.channel_skills as _skills
        adapter_present = bool(_skills.has_adapter(CAPABILITY_ID))
    except (ImportError, AttributeError, TypeError, ValueError):
        adapter_present = False
    try:
        current = get_current_settings(memory_path, pid)
    except SocialObserveReadError as exc:
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
    if not bool(adapter_present):
        return {"configured": False, "readable": True,
                "adapter_present": False,
                "reason": "not_connected", "settings": dict(current)}
    return {"configured": True, "readable": True,
            "adapter_present": True,
            "reason": "connected", "settings": dict(current)}


def build_social_adapter(settings: dict) -> Any:
    """本番の保持先。実アダプタは本番に登録しないため常に未設定として拒否する。

    試験ではこの保持先を monkeypatch で偽のアダプタに差し替える。
    """
    if not isinstance(settings, dict) or not settings:
        raise ValueError("settings are required")
    raise SocialObserveNotConfigured(
        "adapter_not_connected",
        "social observe adapter is not connected")


# ----------------------------------------------------------------------------
# 観測の対象の決定(推測しない)
# ----------------------------------------------------------------------------

def _checked_mode(operating_mode: str | None) -> str:
    mode = str(operating_mode or "").strip()
    if mode not in ("demo_test", "production"):
        return "production"
    return mode


def _parse_day(value: str, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise SocialObserveRefused(f"{field}_invalid", f"{field} is required")
    try:
        moment = datetime.fromisoformat(text[:10])
    except (TypeError, ValueError):
        raise SocialObserveRefused(f"{field}_invalid",
                                   f"{field} must be YYYY-MM-DD") from None
    return moment


def _normalize_handle(value: str) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _identity_sort_key(row: dict) -> str:
    return str(row.get("identity_id") or "")


def _current_platform_identities(memory_path: str | Path, project_id: str,
                                 platform: str) -> list[dict]:
    """そのPJの現在の版から対象媒体の公開IDだけを返す。読取失敗は拒否(未登録に倒さない)。"""
    from app.contact_registry import list_public_identities
    try:
        viewed = list_public_identities(memory_path, str(project_id or ""))
    except Exception as exc:
        raise SocialObserveRefused(
            "unreadable",
            f"public identities are unreadable: {_type_name(exc)}") from None
    current = viewed.get("current") if isinstance(viewed, dict) else []
    if not isinstance(current, list):
        raise SocialObserveRefused(
            "unreadable", "public identities are unreadable")
    try:
        from app.social_premarketing import _platform_to_channel as _to_channel
    except (ImportError, AttributeError, ValueError):
        raise SocialObserveRefused(
            "unreadable", "platform mapping is unreadable") from None
    matched: list[dict] = []
    for row in current:
        if not isinstance(row, dict):
            continue
        try:
            mapped = _to_channel(row.get("platform"))
        except (AttributeError, TypeError, ValueError):
            continue
        if str(mapped or "") == str(platform or ""):
            matched = matched + [dict(row)]
    matched.sort(key=_identity_sort_key)
    return matched


def _expires_at_value(raw: object) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        if isinstance(raw, bool):
            return None
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _select_observable_identities(entries: list[dict],
                                  operating_mode: str) -> tuple[list[dict], list[dict]]:
    """観測に使える公開IDを選ぶ。read-only の観測では registered も使える。

    除外するのは retired・失効と、実運用での fictional だけ。
    fictional は demo_test のときだけ使える。理由は件数とともに返す。
    """
    usable: list[dict] = []
    problems: list[dict] = []
    now = time.time()
    for row in entries:
        kind = str(row.get("source_kind") or "")
        if kind == "retired":
            problems = problems + [{
                "identity_id": str(row.get("identity_id") or ""),
                "reason": "retired"}]
            continue
        expires = _expires_at_value(row.get("expires_at"))
        if expires is not None and now >= expires:
            problems = problems + [{
                "identity_id": str(row.get("identity_id") or ""),
                "reason": "expired"}]
            continue
        if kind == "fictional":
            if operating_mode != "demo_test":
                problems = problems + [{
                    "identity_id": str(row.get("identity_id") or ""),
                    "reason": "fictional_not_for_public"}]
                continue
            usable = usable + [row]
            continue
        if kind in ("registered", "verified"):
            usable = usable + [row]
            continue
        problems = problems + [{
            "identity_id": str(row.get("identity_id") or ""),
            "reason": "unknown_source"}]
    return usable, problems


def _read_social_shares(memory_path: str | Path,
                        project_id: str) -> list[dict]:
    """メインDBの campaign_social_shares を読み取り専用で読む。表が無ければ空。

    共有行・状態・証跡は変えない。読取失敗は例外(『投稿URLなし』に倒さない)。
    """
    mem = Path(memory_path)
    if not mem.exists():
        raise SocialObserveReadError("connect_failed", detail="no main db")
    try:
        db = sqlite3.connect(ledger_readonly_uri(mem), uri=True, timeout=15)
        db.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise SocialObserveReadError(
            "connect_failed", sqlite_error=_type_name(exc)) from exc
    try:
        try:
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error as exc:
            raise SocialObserveReadError(
                "table_unreadable", sqlite_error=_type_name(exc)) from exc
        if "campaign_social_shares" not in names:
            return []
        try:
            rows = db.execute(
                "SELECT * FROM campaign_social_shares WHERE project_id=?",
                (str(project_id or ""),)).fetchall()
        except sqlite3.Error as exc:
            raise SocialObserveReadError(
                "row_unreadable", sqlite_error=_type_name(exc)) from exc
        return [dict(r) for r in rows]
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _extract_x_post_id(url: str) -> str | None:
    """Xの公開投稿URLから投稿IDを取り出す。媒体ごとの規則。推測・発明しない。"""
    text = str(url or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() != "https":
        return None
    host = str(parts.hostname or "").lower()
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return None
    found = X_STATUS_RE.search(str(parts.path or ""))
    if found is None:
        return None
    return str(found.group(1))


def _extract_post_id(platform: str, url: str) -> str | None:
    if str(platform or "") == "x":
        return _extract_x_post_id(url)
    return None


def _collect_post_targets(shares: list[dict], platform: str,
                          campaign_id: str) -> list[dict]:
    """登録済みの公開投稿URLから(投稿ID, URL, 承認版)の対応だけを作る。

    対象は evidence_registered の共有行だけ。投稿IDを推測・発明しない。
    """
    targets: list[dict] = []
    for share in shares or []:
        if not isinstance(share, dict):
            continue
        if str(share.get("campaign_id") or "") != str(campaign_id or ""):
            continue
        if str(share.get("channel") or "") != str(platform or ""):
            continue
        if str(share.get("status") or "") != "evidence_registered":
            continue
        url = str(share.get("evidence_url") or "").strip()
        if not url:
            continue
        post_id = _extract_post_id(platform, url)
        if not post_id:
            continue
        binding = share.get("publication_binding_id")
        try:
            binding_text = "" if binding in (None, "", 0, "0") else str(binding)
        except (TypeError, ValueError):
            binding_text = ""
        targets.append({
            "post_id": str(post_id),
            "url": url,
            "campaign_id": str(share.get("campaign_id") or ""),
            "publication_binding_id": binding_text,
        })
    targets.sort(key=lambda e: str(e.get("post_id") or ""))
    return targets


# ----------------------------------------------------------------------------
# 反応の指標(0にしない)
# ----------------------------------------------------------------------------

def _normalize_metrics(raw: Any) -> tuple[dict | None, str, str]:
    """指標を正規化する。取得できない指標は None のまま残し、省略しない。

    戻り値は (metrics, metrics_status, reason)。形が不正なら ValueError。
    """
    if raw is None:
        return None, "unavailable", "metrics_unavailable"
    if not isinstance(raw, dict):
        raise ValueError("metrics are unreadable")
    cleaned: dict[str, int | None] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if not name or len(name) > 64:
            raise ValueError("metric name is invalid")
        if not METRIC_KEY_RE.fullmatch(name):
            raise ValueError("metric name is invalid")
        if value is None:
            cleaned[name] = None
        elif isinstance(value, bool):
            raise ValueError("metric value is invalid")
        elif not isinstance(value, int) or int(value) < 0:
            raise ValueError("metric value is invalid")
        else:
            cleaned[name] = int(value)
    for name in KNOWN_METRIC_KEYS:
        if name not in cleaned:
            cleaned[name] = None
    numeric = [v for v in cleaned.values()
               if isinstance(v, int) and not isinstance(v, bool)]
    if numeric:
        # 一部だけ取得不能でも、取得できた数値は保存し、理由を残す。
        if any(v is None for v in cleaned.values()):
            return dict(cleaned), "unavailable", "metrics_partially_unavailable"
        return dict(cleaned), "available", ""
    return dict(cleaned), "unavailable", "metrics_unavailable"


def _validate_observe_result(result: Any) -> dict:
    """アダプタの戻り値の形を検査する。不正・読めないときは ValueError。

    completed_ok が無い・bool でないときも不正(unknown 側へ倒す)。
    """
    if not isinstance(result, dict):
        raise ValueError("result is unreadable")
    if "completed_ok" not in result:
        raise ValueError("completed_ok is missing")
    completed = result.get("completed_ok")
    if not isinstance(completed, bool):
        raise ValueError("completed_ok is invalid")
    posts = result.get("posts")
    if not isinstance(posts, list):
        raise ValueError("posts are unreadable")
    cleaned: list[dict] = []
    for entry in posts:
        if not isinstance(entry, dict):
            raise ValueError("post is unreadable")
        post_id = str(entry.get("post_id") or "").strip()
        if not post_id:
            raise ValueError("post_id is required")
        url = str(entry.get("url") or "").strip()
        if not url:
            raise ValueError("url is required")
        author = str(entry.get("author_public_id") or "")
        text = entry.get("text")
        if not isinstance(text, str):
            raise ValueError("text is unreadable")
        posted = entry.get("posted_at")
        if posted is None:
            posted = ""
        if not isinstance(posted, str):
            raise ValueError("posted_at is unreadable")
        available = entry.get("metrics_available")
        if not isinstance(available, bool):
            raise ValueError("metrics_available is invalid")
        metrics, status, reason = _normalize_metrics(entry.get("metrics"))
        if not bool(available):
            status = "unavailable"
            if not reason:
                reason = "metrics_unavailable"
        cleaned.append({
            "post_id": post_id,
            "url": url[:2000],
            "author_public_id": author[:200],
            "posted_at": posted[:100],
            "text": text,
            "metrics": metrics,
            "metrics_status": status,
            "metrics_reason": reason,
        })
    return {"posts": cleaned, "completed_ok": bool(completed)}


# ----------------------------------------------------------------------------
# 観測の保存(追記型・伏せた表示)
# ----------------------------------------------------------------------------

def _observation_view(row: dict) -> dict:
    try:
        metrics = json.loads(str(row.get("metrics_json") or "{}"))
    except (TypeError, ValueError):
        metrics = {}
    if metrics is None:
        metrics_view = None
    elif isinstance(metrics, dict):
        metrics_view = {str(k): v for k, v in metrics.items()}
    else:
        metrics_view = {}
    try:
        evidence_id = int(row.get("evidence_id") or 0)
    except (TypeError, ValueError):
        evidence_id = 0
    return {
        "id": int(row.get("id") or 0),
        "project_id": str(row.get("project_id") or ""),
        "platform": str(row.get("platform") or ""),
        "post_id": str(row.get("post_id") or ""),
        "post_url": str(row.get("post_url") or ""),
        "account_or_public_id": str(row.get("account_or_public_id") or ""),
        "approved_binding_id": str(row.get("approved_binding_id") or ""),
        "approved_binding_missing": bool(row.get("approved_binding_missing")),
        "metrics": metrics_view,
        "metrics_status": str(row.get("metrics_status") or "unavailable"),
        "metrics_reason": str(row.get("metrics_reason") or ""),
        "source": str(row.get("source") or "adapter"),
        "run_id": str(row.get("run_id") or ""),
        "evidence_id": int(evidence_id),
        "observed_at": str(row.get("observed_at") or ""),
        "period_start": str(row.get("period_start") or ""),
        "period_end": str(row.get("period_end") or ""),
        "untrusted": True,
        "excerpt": str(row.get("excerpt") or ""),
        "evidence_note": str(row.get("evidence_note") or ""),
        "actor": str(row.get("actor") or ""),
        "memo": str(row.get("memo") or ""),
        "screenshot_sha256": str(row.get("screenshot_sha256") or ""),
        "created_at": row.get("created_at"),
    }


def _save_observation(memory_path: str | Path, row: dict) -> dict:
    path = item_ledger_db_path(memory_path)
    guard = _lock_for(path)
    with guard:
        with _connect_rw(memory_path) as db:
            now = time.time()
            cursor = db.execute(
                """INSERT INTO social_observations(
                    project_id, platform, post_id, post_url,
                    account_or_public_id, approved_binding_id,
                    approved_binding_missing, metrics_json, metrics_status,
                    metrics_reason, source, run_id, evidence_id,
                    observed_at, period_start, period_end, purpose,
                    retention_days, excerpt, evidence_note, actor, memo,
                    screenshot_sha256, untrusted, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (str(row.get("project_id") or ""),
                 str(row.get("platform") or ""),
                 str(row.get("post_id") or ""),
                 str(row.get("post_url") or "")[:2000],
                 str(row.get("account_or_public_id") or "")[:200],
                 str(row.get("approved_binding_id") or "")[:100],
                 1 if row.get("approved_binding_missing") else 0,
                 json.dumps(row.get("metrics"), ensure_ascii=False,
                            sort_keys=True) if row.get("metrics") is not None else "null",
                 str(row.get("metrics_status") or "unavailable")[:32],
                 str(row.get("metrics_reason") or "")[:200],
                 str(row.get("source") or "adapter")[:16],
                 str(row.get("run_id") or "")[:100],
                 int(row.get("evidence_id") or 0),
                 str(row.get("observed_at") or "")[:100],
                 str(row.get("period_start") or "")[:32],
                 str(row.get("period_end") or "")[:32],
                 str(row.get("purpose") or "")[:2000],
                 int(row.get("retention_days") or 0),
                 str(row.get("excerpt") or "")[:500],
                 str(row.get("evidence_note") or "")[:2000],
                 str(row.get("actor") or "")[:100],
                 str(row.get("memo") or "")[:2000],
                 str(row.get("screenshot_sha256") or "")[:128],
                 now),
            )
            db.commit()
            kept = db.execute(
                "SELECT * FROM social_observations WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    return _observation_view(dict(kept))


def list_observations(memory_path: str | Path, project_id: str,
                      platform: str = "") -> list[dict]:
    """保存した観測の伏せた表示。他PJの行を返さない。読取失敗は例外。"""
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return []
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "social_observations" not in names:
            return []
        query = "SELECT * FROM social_observations WHERE project_id=?"
        args: list[Any] = [pid]
        if str(platform or "").strip():
            query += " AND platform=?"
            args.append(str(platform).strip().lower())
        query += " ORDER BY id"
        try:
            rows = db.execute(query, tuple(args)).fetchall()
        except sqlite3.Error as exc:
            raise SocialObserveReadError(
                "row_unreadable", sqlite_error=_type_name(exc)) from exc
    return [_observation_view(dict(r)) for r in rows]


# ----------------------------------------------------------------------------
# 実行(読み取り)
# ----------------------------------------------------------------------------

def run_social_observe(memory_path: str | Path, project_id: str, *,
                       platform: str, campaign_id: str,
                       since: str, until: str, purpose: str,
                       operating_mode: str | None = None,
                       adapter: Any = None) -> dict:
    """登録済みの公開ID・投稿IDから実投稿と反応を確認する(読み取りだけ)。

    拒否(設定なし・公開IDなし・投稿URLなし・架空の実運用利用・期間・目的)は
    SocialObserveRefused(呼出側が409/422にする)。
    取得器の例外は failed として記録して返す。形の不正は unknown。
    接続失敗・認証失敗・タイムアウトを no_results にしない。
    """
    import app.channel_skills as _skills

    pid = str(project_id or "").strip()
    if not pid:
        raise SocialObserveRefused("project_invalid", "project_id is required")
    plat = str(platform or "").strip().lower()
    if plat not in SUPPORTED_PLATFORMS:
        raise SocialObserveRefused("platform_invalid",
                                   "platform is not supported")
    cid = str(campaign_id or "").strip()
    if not cid:
        raise SocialObserveRefused("campaign_invalid", "campaign_id is required")
    reason_text = str(purpose or "").strip()
    if not reason_text:
        raise SocialObserveRefused("purpose_invalid", "purpose is required")
    begin = _parse_day(since, "since")
    finish = _parse_day(until, "until")
    if begin > finish:
        raise SocialObserveRefused("period_invalid", "since must precede until")
    mode = _checked_mode(operating_mode)
    if adapter is None:
        raise SocialObserveNotConfigured(
            "adapter_not_connected",
            "social observe adapter is not connected")

    # 外部呼出の前に済ませる検査(設定・目的・運用区分・公開ID・投稿ID・期間)。
    try:
        current = get_current_settings(memory_path, pid)
    except SocialObserveReadError:
        raise SocialObserveRefused("unreadable",
                                   "social settings are unreadable") from None
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        raise SocialObserveRefused("unreadable",
                                   "social settings are unreadable") from None
    if not isinstance(current, dict) or not current:
        raise SocialObserveNotConfigured("needs_configuration",
                                         "social observe settings are missing")
    if str(current.get("platform") or "") != plat:
        raise SocialObserveRefused("platform_mismatch",
                                   "platform does not match settings")
    if str(current.get("purpose") or "").strip() != reason_text:
        raise SocialObserveRefused("purpose_mismatch",
                                   "purpose does not match settings")
    try:
        retention = int(current.get("retention_days") or 90)
    except (TypeError, ValueError):
        retention = 90

    identities = _current_platform_identities(memory_path, pid, plat)
    if not identities:
        raise SocialObserveNotConfigured(
            "no_public_id", "公開IDが未登録のため観測できません")
    usable, problems = _select_observable_identities(identities, mode)
    fictional_blocked = [p for p in problems
                         if str(p.get("reason") or "")
                         == "fictional_not_for_public"]
    if not usable:
        if fictional_blocked and mode == "production":
            raise SocialObserveRefused(
                "fictional_blocked", "架空IDは実運用で使えません")
        raise SocialObserveNotConfigured(
            "no_public_id", "公開IDが未登録のため観測できません")
    try:
        shares = _read_social_shares(memory_path, pid)
    except SocialObserveReadError:
        raise SocialObserveRefused("unreadable",
                                   "social shares are unreadable") from None
    targets = _collect_post_targets(shares, plat, cid)
    if not targets:
        raise SocialObserveNotConfigured(
            "no_post_url", "投稿URLが未登録のため観測できません")

    handle_set = {_normalize_handle(r.get("handle"))
                  for r in usable if _normalize_handle(r.get("handle"))}
    id_set = {str(r.get("identity_id") or "")
              for r in usable if str(r.get("identity_id") or "")}
    canonical_by_norm: dict[str, str] = {}
    for row in usable:
        norm = _normalize_handle(row.get("handle"))
        if norm and norm not in canonical_by_norm:
            canonical_by_norm[norm] = str(row.get("handle") or "")
        iid = str(row.get("identity_id") or "")
        if iid and iid not in canonical_by_norm:
            canonical_by_norm[iid] = str(row.get("handle") or iid)
    target_ids = [t["post_id"] for t in targets]
    target_by_id = {t["post_id"]: t for t in targets}
    public_id = str(current.get("public_identity_id") or "")

    since_text = begin.strftime("%Y-%m-%d")
    until_text = finish.strftime("%Y-%m-%d")
    started = time.time()
    try:
        label = type(adapter).__name__ or "social_adapter"
    except (AttributeError, TypeError):
        label = "social_adapter"
    label = str(label)[:200] or "social_adapter"

    try:
        raw_result = adapter.observe(plat, public_id, list(target_ids),
                                     since_text, until_text)
    except Exception as exc:
        finished = time.time()
        kept = _skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="failed", purpose=reason_text[:2000],
            operating_mode=mode, adapter=label,
            params_summary={"platform": plat, "campaign_id": cid},
            started_at=started, finished_at=finished,
            error_kind=_type_name(exc), evidence_count=0,
            completed_ok=False,
        )
        return {"status": "failed", "run": kept, "evidence": [],
                "observations": [], "account_mismatch": 0,
                "mismatch_details": [], "next_actions": [],
                "reason": f"fetch failed: {_type_name(exc)}"}

    try:
        viewed = _validate_observe_result(raw_result)
    except ValueError as exc:
        finished = time.time()
        kept = _skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="unknown", purpose=reason_text[:2000],
            operating_mode=mode, adapter=label,
            params_summary={"platform": plat, "campaign_id": cid},
            started_at=started, finished_at=finished,
            error_kind="malformed_result", evidence_count=0,
            completed_ok=False,
        )
        return {"status": "unknown", "run": kept, "evidence": [],
                "observations": [], "account_mismatch": 0,
                "mismatch_details": [], "next_actions": [],
                "reason": str(exc)[:200] or "result is unreadable"}

    if bool(viewed["completed_ok"]) is False:
        finished = time.time()
        kept = _skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="failed", purpose=reason_text[:2000],
            operating_mode=mode, adapter=label,
            params_summary={"platform": plat, "campaign_id": cid},
            started_at=started, finished_at=finished,
            error_kind="incomplete_result", evidence_count=0,
            completed_ok=False,
        )
        return {"status": "failed", "run": kept, "evidence": [],
                "observations": [], "account_mismatch": 0,
                "mismatch_details": [], "next_actions": [],
                "reason": "observation did not complete"}

    fetched = viewed["posts"]
    # 観測対象の投稿だけを扱う。対象外の投稿IDは数えて無視する(記録しない)。
    unrequested = 0
    for entry in fetched:
        if str(entry.get("post_id") or "") not in target_by_id:
            unrequested += 1
    requested_posts = [e for e in fetched
                       if str(e.get("post_id") or "") in target_by_id]
    if not requested_posts:
        finished = time.time()
        kept = _skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="no_results", purpose=reason_text[:2000],
            operating_mode=mode, adapter=label,
            params_summary={"platform": plat, "campaign_id": cid,
                            "post_count": 0, "account_mismatch": 0,
                            "unrequested": int(unrequested)},
            started_at=started, finished_at=finished,
            error_kind="", evidence_count=0, completed_ok=True,
        )
        return {"status": "no_results", "run": kept, "evidence": [],
                "observations": [], "account_mismatch": 0,
                "mismatch_details": [], "next_actions": [],
                "reason": "no posts returned"}

    kept_evidence: list[dict] = []
    kept_observations: list[dict] = []
    mismatch = 0
    mismatch_details: list[dict] = []
    metrics_missing = False
    provider = f"social_{plat}"
    fetched_now = datetime.now(timezone.utc).isoformat()
    for entry in requested_posts:
        author_norm = _normalize_handle(entry.get("author_public_id"))
        if not author_norm or (author_norm not in handle_set
                               and str(entry.get("author_public_id") or "")
                               not in id_set):
            # 主体の照合: 登録された公開IDの handle と一致しない投稿は
            # そのPJの投稿として記録しない(件数と理由だけ残す)。
            mismatch += 1
            mismatch_details = mismatch_details + [{
                "post_id": str(entry.get("post_id") or ""),
                "reason": "account_mismatch"}]
            continue
        target = target_by_id[str(entry.get("post_id") or "")]
        canonical = canonical_by_norm.get(
            author_norm, canonical_by_norm.get(
                str(entry.get("author_public_id") or ""),
                str(entry.get("author_public_id") or "")))
        binding_id = str(target.get("publication_binding_id") or "")
        binding_missing = not bool(binding_id)
        try:
            built = _skills.build_evidence(
                provider=provider,
                account_or_public_id=canonical or author_norm,
                external_id=str(entry.get("post_id") or ""),
                url=str(entry.get("url") or target.get("url") or ""),
                fetched_at=fetched_now,
                period_start=since_text,
                period_end=until_text,
                body=str(entry.get("text") or ""),
                project_id=pid,
                plan_item_id=SOCIAL_UNLINKED,
                approved_binding_id=binding_id,
                purpose=reason_text[:2000],
                retention_days=int(retention),
            )
        except ValueError as exc:
            finished = time.time()
            kept = _skills.record_run(
                memory_path, project_id=pid, capability_id=CAPABILITY_ID,
                status="unknown", purpose=reason_text[:2000],
                operating_mode=mode, adapter=label,
                params_summary={"platform": plat, "campaign_id": cid,
                                "post_count": len(kept_evidence),
                                "account_mismatch": int(mismatch)},
                started_at=started, finished_at=finished,
                error_kind="evidence_rejected",
                evidence_count=len(kept_evidence),
                completed_ok=False,
            )
            return {"status": "unknown", "run": kept,
                    "evidence": list(kept_evidence),
                    "observations": list(kept_observations),
                    "account_mismatch": int(mismatch),
                    "mismatch_details": list(mismatch_details),
                    "next_actions": [],
                    "reason": f"evidence rejected: {_type_name(exc)}"}
        try:
            saved_evidence = _skills.record_evidence(
                memory_path, dict(built), capability_id=CAPABILITY_ID)
        except ValueError as exc:
            finished = time.time()
            kept = _skills.record_run(
                memory_path, project_id=pid, capability_id=CAPABILITY_ID,
                status="unknown", purpose=reason_text[:2000],
                operating_mode=mode, adapter=label,
                params_summary={"platform": plat, "campaign_id": cid,
                                "post_count": len(kept_evidence),
                                "account_mismatch": int(mismatch)},
                started_at=started, finished_at=finished,
                error_kind="evidence_rejected",
                evidence_count=len(kept_evidence),
                completed_ok=False,
            )
            return {"status": "unknown", "run": kept,
                    "evidence": list(kept_evidence),
                    "observations": list(kept_observations),
                    "account_mismatch": int(mismatch),
                    "mismatch_details": list(mismatch_details),
                    "next_actions": [],
                    "reason": f"evidence rejected: {_type_name(exc)}"}
        kept_evidence = kept_evidence + [dict(saved_evidence)]
        try:
            saved_observation = _save_observation(memory_path, {
                "project_id": pid, "platform": plat,
                "post_id": str(entry.get("post_id") or ""),
                "post_url": str(entry.get("url") or target.get("url") or ""),
                "account_or_public_id": canonical or author_norm,
                "approved_binding_id": binding_id,
                "approved_binding_missing": bool(binding_missing),
                "metrics": entry.get("metrics"),
                "metrics_status": str(entry.get("metrics_status")
                                      or "unavailable"),
                "metrics_reason": str(entry.get("metrics_reason") or ""),
                "source": "adapter", "run_id": "",
                "evidence_id": int(saved_evidence.get("id") or 0),
                "observed_at": fetched_now,
                "period_start": since_text, "period_end": until_text,
                "purpose": reason_text[:2000],
                "retention_days": int(retention),
                "excerpt": str(saved_evidence.get("excerpt") or ""),
                "evidence_note": "", "actor": "", "memo": "",
                "screenshot_sha256": "",
            })
        except (OSError, ValueError, TypeError, AttributeError,
                sqlite3.Error) as exc:
            finished = time.time()
            kept = _skills.record_run(
                memory_path, project_id=pid, capability_id=CAPABILITY_ID,
                status="unknown", purpose=reason_text[:2000],
                operating_mode=mode, adapter=label,
                params_summary={"platform": plat, "campaign_id": cid,
                                "post_count": len(kept_evidence),
                                "account_mismatch": int(mismatch)},
                started_at=started, finished_at=finished,
                error_kind="observation_unwritable",
                evidence_count=len(kept_evidence),
                completed_ok=False,
            )
            return {"status": "unknown", "run": kept,
                    "evidence": list(kept_evidence),
                    "observations": list(kept_observations),
                    "account_mismatch": int(mismatch),
                    "mismatch_details": list(mismatch_details),
                    "next_actions": [],
                    "reason": f"observation unwritable: {_type_name(exc)}"}
        kept_observations = kept_observations + [dict(saved_observation)]
        if str(saved_observation.get("metrics_status") or "") != "available":
            metrics_missing = True

    finished = time.time()
    if not kept_evidence:
        kept = _skills.record_run(
            memory_path, project_id=pid, capability_id=CAPABILITY_ID,
            status="no_results", purpose=reason_text[:2000],
            operating_mode=mode, adapter=label,
            params_summary={"platform": plat, "campaign_id": cid,
                            "post_count": 0,
                            "account_mismatch": int(mismatch),
                            "unrequested": int(unrequested)},
            started_at=started, finished_at=finished,
            error_kind="", evidence_count=0, completed_ok=True,
        )
        return {"status": "no_results", "run": kept, "evidence": [],
                "observations": [], "account_mismatch": int(mismatch),
                "mismatch_details": list(mismatch_details),
                "next_actions": [],
                "reason": "no matching posts for this project"}
    next_actions: list[str] = []
    if metrics_missing:
        next_actions = next_actions + [NEXT_ACTION_METRICS_JA]
    kept = _skills.record_run(
        memory_path, project_id=pid, capability_id=CAPABILITY_ID,
        status="succeeded", purpose=reason_text[:2000],
        operating_mode=mode, adapter=label,
        params_summary={"platform": plat, "campaign_id": cid,
                        "post_count": len(kept_evidence),
                        "account_mismatch": int(mismatch),
                        "unrequested": int(unrequested)},
        started_at=started, finished_at=finished,
        error_kind="", evidence_count=len(kept_evidence),
        completed_ok=True,
    )
    # run_id を観測行へ結ぶ(追記で行う。失敗しても成功を偽らない)。
    run_id_text = str(kept.get("run_id") or "")
    if run_id_text:
        try:
            path = item_ledger_db_path(memory_path)
            guard = _lock_for(path)
            with guard:
                with _connect_rw(memory_path) as db:
                    for item in kept_observations:
                        try:
                            row_id = int(item.get("id") or 0)
                        except (TypeError, ValueError):
                            continue
                        if row_id > 0:
                            db.execute(
                                "UPDATE social_observations SET run_id=? WHERE id=?",
                                (run_id_text, int(row_id)))
                    db.commit()
            for item in kept_observations:
                item["run_id"] = run_id_text
        except (OSError, ValueError, TypeError, AttributeError,
                sqlite3.Error) as exc:
            LOGGER.warning("social observe run_id linking failed: %s",
                           _type_name(exc))
    return {"status": "succeeded", "run": kept,
            "evidence": list(kept_evidence),
            "observations": list(kept_observations),
            "account_mismatch": int(mismatch),
            "mismatch_details": list(mismatch_details),
            "next_actions": list(next_actions), "reason": ""}


# ----------------------------------------------------------------------------
# 人手の観測の登録(証拠の誘導先)
# ----------------------------------------------------------------------------

def record_manual_observation(memory_path: str | Path, project_id: str, *,
                              platform: str, post_url: str, observed_at: str,
                              metrics: Any = None, evidence_note: str = "",
                              screenshot_sha256: str = "",
                              actor: str, memo: str) -> dict:
    """公開ページを見た人が値を手で登録する。自動の観測と区別する。

    post_url は B2の validate_evidence_url と同じホスト検査を通し、そのPJの
    登録済みの公開投稿URLに一致するものだけ受け付ける。
    担当者・メモ必須。不正は SocialObserveRefused(呼出側が422/409にする)。
    人手の観測は field_verified を真にしない(確認記録に触れない)。
    """
    from app.social_premarketing import validate_evidence_url

    pid = str(project_id or "").strip()
    if not pid:
        raise SocialObserveRefused("project_invalid", "project_id is required")
    plat = str(platform or "").strip().lower()
    if plat not in SUPPORTED_PLATFORMS:
        raise SocialObserveRefused("platform_invalid",
                                   "platform is not supported")
    who = str(actor or "").strip()
    if not who:
        raise SocialObserveRefused("actor_invalid", "actor is required")
    note = str(memo or "").strip()
    if not note:
        raise SocialObserveRefused("memo_invalid", "memo is required")
    text_url = str(post_url or "").strip()
    if not text_url:
        raise SocialObserveRefused("post_url_invalid", "post_url is required")
    try:
        checked_url = validate_evidence_url(plat, text_url)
    except ValueError as exc:
        raise SocialObserveRefused("post_url_invalid", str(exc)[:200]) from exc
    when_text = str(observed_at or "").strip()
    if not when_text:
        raise SocialObserveRefused("observed_at_invalid",
                                   "observed_at is required")
    try:
        moment = datetime.fromisoformat(when_text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise SocialObserveRefused("observed_at_invalid",
                                   "observed_at must be ISO-8601") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    post_id = _extract_post_id(plat, checked_url)
    if not post_id:
        raise SocialObserveRefused("post_url_invalid",
                                   "post id is unreadable from post_url")
    try:
        normalized_metrics, metrics_status, metrics_reason = _normalize_metrics(
            metrics)
    except ValueError as exc:
        raise SocialObserveRefused("metrics_invalid", str(exc)[:200]) from exc
    content_note = str(evidence_note or "").strip()
    if not content_note:
        raise SocialObserveRefused("evidence_note_invalid",
                                   "evidence_note is required")
    if len(content_note) > 2000:
        raise SocialObserveRefused("evidence_note_invalid",
                                   "evidence_note is too long")
    digest = str(screenshot_sha256 or "").strip().lower()
    if digest:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SocialObserveRefused("screenshot_invalid",
                                       "screenshot_sha256 is invalid")
    _reject_secret_fields({"actor": who, "memo": note,
                           "evidence_note": content_note})
    try:
        shares = _read_social_shares(memory_path, pid)
    except SocialObserveReadError:
        raise SocialObserveRefused("unreadable",
                                   "social shares are unreadable") from None
    matched: dict | None = None
    for share in shares:
        if not isinstance(share, dict):
            continue
        if str(share.get("channel") or "") != plat:
            continue
        if str(share.get("status") or "") != "evidence_registered":
            continue
        if str(share.get("evidence_url") or "").strip() == checked_url:
            matched = share
            break
    if matched is None:
        raise SocialObserveNotConfigured(
            "unregistered_post_url",
            "登録済みの公開投稿URLに一致しないため登録できません")
    binding = matched.get("publication_binding_id")
    try:
        binding_text = "" if binding in (None, "", 0, "0") else str(binding)
    except (TypeError, ValueError):
        binding_text = ""
    try:
        settings = get_current_settings(memory_path, pid)
    except SocialObserveReadError:
        raise SocialObserveRefused("unreadable",
                                   "social settings are unreadable") from None
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        raise SocialObserveRefused("unreadable",
                                   "social settings are unreadable") from None
    if isinstance(settings, dict) and settings:
        retention = settings.get("retention_days")
        try:
            retention_days = int(retention or 90)
        except (TypeError, ValueError):
            retention_days = 90
        purpose_text = str(settings.get("purpose") or "")
    else:
        retention_days = 90
        purpose_text = ""
    # account_or_public_id は共有行の媒体に対応する登録済みの公開IDの handle。
    account = ""
    try:
        identities = _current_platform_identities(memory_path, pid, plat)
    except SocialObserveRefused:
        identities = []
    for row in identities:
        if not isinstance(row, dict):
            continue
        handle = str(row.get("handle") or "")
        if handle:
            account = handle
            break
    saved = _save_observation(memory_path, {
        "project_id": pid, "platform": plat,
        "post_id": str(post_id),
        "post_url": checked_url,
        "account_or_public_id": account,
        "approved_binding_id": binding_text,
        "approved_binding_missing": not bool(binding_text),
        "metrics": normalized_metrics,
        "metrics_status": str(metrics_status),
        "metrics_reason": str(metrics_reason),
        "source": "manual", "run_id": "", "evidence_id": 0,
        "observed_at": when_text[:100],
        "period_start": when_text[:10], "period_end": when_text[:10],
        "purpose": purpose_text[:2000],
        "retention_days": int(retention_days),
        "excerpt": "", "evidence_note": content_note,
        "actor": who[:100], "memo": note[:2000],
        "screenshot_sha256": digest,
    })
    return dict(saved)


# ----------------------------------------------------------------------------
# 世代切替では消さない
# ----------------------------------------------------------------------------

def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    return {"project_id": str(project_id or ""), "reset": False,
            "note": "SNS観測の台帳は世代切替で変えません"}
