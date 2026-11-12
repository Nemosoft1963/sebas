"""B2: 公開前の版照合(束縛の記録・照合・完全削除)。

- 保存先は項目別台帳と同じSQLite(item_ledger_db_path)への追記型の表
  publication_bindings。A2の表と同じ作法(_connect_rw/_connect_ro、
  RegistryReadError、版は追記、消さない)。
- パスワード・トークンなどの秘密は保存しない。値そのものは入れず、
  IDと版とSHA-256だけ。
- 読取失敗は『未登録』に倒さない。台帳DBが無い・表がまだ無い場合だけ未登録。
- PJ初期化(世代切替)では束縛を消さない。完全削除・バックアップは
  project_lifecycle_registry の汎用経路(sqlite_table)で行う。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from app.requirement_item_ledger import (
    _ensure_tables as _ensure_item_tables,
    classify_ledger_path_kind,
    item_ledger_db_path,
    ledger_readonly_uri,
)

LOGGER = logging.getLogger(__name__)

BINDING_TABLE_DDL = """CREATE TABLE IF NOT EXISTS publication_bindings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    binding_json TEXT NOT NULL DEFAULT '{}',
    binding_sha256 TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL)"""

BINDING_STATUSES = frozenset({"ok", "stale", "no_binding", "blocked", "unreadable"})

# 選択媒体ごとの計数ハッシュに使う正規化の版。上げるときは新旧の両方で照合する。
BINDING_SCHEMA_VERSION = 1


class BindingReadError(RuntimeError):
    """束縛台帳の読取失敗。『束縛なし』とは区別する。"""

    def __init__(self, kind: str, *, path_kind: str = "",
                 sqlite_error: str = "", detail: str = "") -> None:
        super().__init__(f"publication binding read failed: {kind}")
        self.kind = str(kind or "")
        self.path_kind = str(path_kind or "")
        self.sqlite_error = str(sqlite_error or "")
        self.detail = str(detail or "")[:500]


def _type_name(exc: BaseException) -> str:
    try:
        return str(type(exc).__name__ or "Error")
    except Exception:
        return "Error"


def ensure_binding_tables(db: sqlite3.Connection) -> None:
    """束縛表と索引を作成する。失敗は呼出側へ送出する(握りつぶさない)。"""
    _ensure_item_tables(db)
    db.execute(BINDING_TABLE_DDL)
    db.execute("""CREATE INDEX IF NOT EXISTS idx_publication_bindings_project
        ON publication_bindings(project_id, campaign_id, stage, id DESC)""")


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
            ensure_binding_tables(db)
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
        raise BindingReadError(
            "connect_failed", path_kind=kind,
            sqlite_error=_type_name(exc)) from exc
    try:
        yield db
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_binding(payload: dict) -> tuple[str, str]:
    """束縛の正規化JSONとSHA-256。正規化は sort_keys・区切り固定。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n"
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _table_names(db: sqlite3.Connection) -> set[str]:
    try:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.Error as exc:
        raise BindingReadError(
            "table_unreadable", sqlite_error=_type_name(exc)) from exc
    return {str(row[0]) for row in rows}


def _binding_view(row: dict) -> dict:
    try:
        payload = json.loads(str(row.get("binding_json") or "{}"))
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "id": int(row.get("id") or 0),
        "project_id": str(row.get("project_id") or ""),
        "campaign_id": str(row.get("campaign_id") or ""),
        "stage": str(row.get("stage") or ""),
        "binding": payload,
        "binding_sha256": str(row.get("binding_sha256") or ""),
        "approved_at": str(row.get("approved_at") or ""),
        "actor": str(row.get("actor") or ""),
        "created_at": row.get("created_at"),
    }


def list_bindings(memory_path: str | Path, project_id: str,
                  campaign_id: str) -> dict:
    """束縛の一覧。読取失敗は例外。DB無し・表無しは束縛なし。"""
    pid = str(project_id or "")
    cid = str(campaign_id or "")
    if not pid or not cid:
        raise ValueError("project_id and campaign_id are required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return {"project_id": pid, "campaign_id": cid,
                "bindings": [], "status": "no_binding"}
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "publication_bindings" not in names:
            return {"project_id": pid, "campaign_id": cid,
                    "bindings": [], "status": "no_binding"}
        try:
            rows = db.execute(
                "SELECT * FROM publication_bindings WHERE project_id=? AND campaign_id=? "
                "ORDER BY id DESC",
                (pid, cid)).fetchall()
        except sqlite3.Error as exc:
            raise BindingReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
    return {"project_id": pid, "campaign_id": cid,
            "bindings": [_binding_view(dict(r)) for r in rows],
            "status": "ok" if rows else "no_binding"}


def record_binding(memory_path: str | Path, *, project_id: str,
                   campaign_id: str, stage: str, binding: dict,
                   approved_at: str = "", actor: str = "") -> dict:
    """束縛を追記する。消さない。版の上書きはしない。"""
    pid = str(project_id or "")
    cid = str(campaign_id or "")
    phase = str(stage or "")
    if not pid or not cid:
        raise ValueError("project_id and campaign_id are required")
    if phase not in ("kit", "approved"):
        raise ValueError("stage must be kit or approved")
    if not isinstance(binding, dict):
        raise ValueError("binding is required")
    who = str(actor or "")[:100]
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            canonical, digest = _canonical_binding(binding)
            now = time.time()
            cursor = db.execute(
                """INSERT INTO publication_bindings(
                    project_id, campaign_id, stage, binding_json,
                    binding_sha256, approved_at, actor, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (pid, cid, phase, canonical, digest,
                 str(approved_at or ""), who, now),
            )
            db.commit()
            saved = db.execute(
                "SELECT * FROM publication_bindings WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    return _binding_view(dict(saved))


def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    """PJ初期化では束縛の記録は変えない。証拠として残す。"""
    return {"project_id": str(project_id or ""), "reset": False,
            "note": "公開前の版照合の束縛は世代切替で変えません"}


def purge_project_bindings(memory_path: str | Path, project_id: str) -> dict:
    """完全削除時は当該PJの束縛行だけ消す。他PJに影響しない。"""
    pid = str(project_id or "")
    path = item_ledger_db_path(memory_path)
    if not path.exists() or not pid:
        return {"publication_bindings": 0}
    removed = {"publication_bindings": 0}
    try:
        db = sqlite3.connect(str(path), timeout=30)
    except (OSError, sqlite3.Error) as exc:
        LOGGER.warning("binding purge connect failed: %s", _type_name(exc))
        raise RuntimeError(
            f"binding purge connect failed: {_type_name(exc)}") from exc
    try:
        with db:
            try:
                names = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"binding purge table inspection failed: {_type_name(exc)}") from exc
            if "publication_bindings" not in names:
                # 束縛をまだ一度も作っていないPJ(表が無い)は削除対象なし(0件)。
                return removed
            try:
                cursor = db.execute(
                    "DELETE FROM publication_bindings WHERE project_id=?", (pid,))
                removed["publication_bindings"] = int(cursor.rowcount or 0)
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"binding purge delete failed: {_type_name(exc)}") from exc
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass
    return removed


# ----------------------------------------------------------------------------
# 束縛の内容の構築(版・ハッシュの収集。値そのものは入れない)
# ----------------------------------------------------------------------------

def _wrap_registry_error(exc: BaseException) -> BindingReadError:
    try:
        from app.contact_registry import RegistryReadError as _RegistryReadError
        if isinstance(exc, _RegistryReadError):
            return BindingReadError(
                "row_unreadable", path_kind=str(getattr(exc, "path_kind", "")),
                sqlite_error=str(getattr(exc, "sqlite_error", "")),
                detail=str(getattr(exc, "detail", ""))[:500])
    except (ImportError, AttributeError, ValueError):
        pass
    return BindingReadError("row_unreadable", sqlite_error=_type_name(exc))


def _scope_rows(rows: list[dict], campaign_id: str) -> tuple[list[dict], str]:
    """campaign_idを持つ行があればそれを、なければPJ全体(空)の行を使う。"""
    wanted = str(campaign_id or "")
    cleaned = [r for r in rows if isinstance(r, dict)]
    if wanted:
        camp_rows = [r for r in cleaned if str(r.get("campaign_id") or "") == wanted]
        if camp_rows:
            return camp_rows, "campaign"
        project_rows = [r for r in cleaned if not str(r.get("campaign_id") or "")]
        return project_rows, "project"
    project_rows = [r for r in cleaned if not str(r.get("campaign_id") or "")]
    if project_rows:
        return project_rows, "project"
    return cleaned, "project"


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
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _identity_value_sha(row: dict) -> str:
    """公開IDの値の指紋。handle と profile_url から作る(値そのものは保存しない)。"""
    handle = str(row.get("handle") or "")
    profile_url = str(row.get("profile_url") or "")
    return _sha256_text(handle + "|" + profile_url)


def collect_registry_snapshot(memory_path: str | Path, project_id: str,
                               campaign_id: str) -> dict:
    """現在の登録の版・ハッシュだけを集める。値そのものは入れない。

    読取失敗は BindingReadError(『束縛なし』に倒さない)。
    """
    from app.contact_registry import (
        list_channel_selections,
        list_contact_routes,
        list_public_identities,
    )
    pid = str(project_id or "")
    cid = str(campaign_id or "")
    if not pid or not cid:
        raise ValueError("project_id and campaign_id are required")
    try:
        routes_viewed = list_contact_routes(memory_path, pid)
        channels_viewed = list_channel_selections(memory_path, pid)
        identities_viewed = list_public_identities(memory_path, pid)
    except Exception as exc:
        raise _wrap_registry_error(exc) from exc
    route_rows = routes_viewed.get("current") if isinstance(routes_viewed, dict) else []
    channel_rows = channels_viewed.get("current") if isinstance(channels_viewed, dict) else []
    identity_rows = identities_viewed.get("current") if isinstance(identities_viewed, dict) else []
    scoped_routes, _ = _scope_rows(list(route_rows or []), cid)
    scoped_channels, _ = _scope_rows(list(channel_rows or []), cid)
    scoped_identities, _ = _scope_rows(list(identity_rows or []), cid)
    contacts: list[dict] = []
    for row in scoped_routes:
        value = str(row.get("public_value") or "")
        try:
            version = int(row.get("version"))
        except (TypeError, ValueError):
            raise BindingReadError(
                "row_unreadable", detail="contact route version unreadable")
        if version < 1:
            raise BindingReadError(
                "row_unreadable", detail="contact route version out of range")
        contacts.append({
            "route_id": str(row.get("route_id") or ""),
            "version": version,
            "value_sha256": _sha256_text(value),
            "source_kind": str(row.get("source_kind") or ""),
            "expires_at": row.get("expires_at"),
        })
    contacts.sort(key=lambda e: str(e.get("route_id") or ""))
    channels: list[dict] = []
    for row in scoped_channels:
        try:
            version = int(row.get("version"))
        except (TypeError, ValueError):
            raise BindingReadError(
                "row_unreadable", detail="channel selection version unreadable")
        if version < 1:
            raise BindingReadError(
                "row_unreadable", detail="channel selection version out of range")
        channels.append({
            "platform": str(row.get("platform") or ""),
            "version": version,
            "enabled": bool(row.get("enabled")),
            "account_ref": str(row.get("account_ref") or ""),
        })
    channels.sort(key=lambda e: str(e.get("platform") or ""))
    identities: list[dict] = []
    for row in scoped_identities:
        handle = str(row.get("handle") or "")
        profile_url = str(row.get("profile_url") or "")
        try:
            identity_version = int(row.get("version"))
        except (TypeError, ValueError):
            raise BindingReadError(
                "row_unreadable", detail="public identity version unreadable")
        if identity_version < 1:
            raise BindingReadError(
                "row_unreadable", detail="public identity version out of range")
        identities.append({
            "identity_id": str(row.get("identity_id") or ""),
            "platform": str(row.get("platform") or ""),
            "version": identity_version,
            "source_kind": str(row.get("source_kind") or ""),
            "value_sha256": _identity_value_sha(row),
            "expires_at": row.get("expires_at"),
        })
    identities.sort(key=lambda e: str(e.get("identity_id") or ""))
    # 媒体は SNSキットの channel キー(小文字)で持つ。登録台帳の表記(X等)のままだと
    # 承認時と照合時の比較が食い違うため、正規化して保存する。
    from app.social_premarketing import _platform_to_channel as _to_channel
    enabled_platforms = sorted({
        _to_channel(r.get("platform")) for r in scoped_channels
        if isinstance(r, dict) and bool(r.get("enabled"))
        and _to_channel(r.get("platform"))})
    versions = {}
    for row in scoped_channels:
        if isinstance(row, dict) and _to_channel(row.get("platform")):
            try:
                channel_version = int(row.get("version"))
            except (TypeError, ValueError):
                raise BindingReadError(
                    "row_unreadable",
                    detail="channel selection version unreadable for snapshot")
            if channel_version < 1:
                raise BindingReadError(
                    "row_unreadable",
                    detail="channel selection version out of range for snapshot")
            versions[_to_channel(row.get("platform"))] = channel_version
    return {
        "contact_routes": contacts,
        "channel_selection": {
            "channels": enabled_platforms,
            "versions": versions,
        },
        "identities": identities,
    }


def build_binding_payload(registry_snapshot: dict, packet_sha256: str,
                          kit_sha256: str) -> dict:
    """束縛の正規化対象を作る。秘密・値そのものは入れない。"""
    snap = dict(registry_snapshot or {})
    return {
        "schema_version": BINDING_SCHEMA_VERSION,
        "contact_routes": list(snap.get("contact_routes") or []),
        "channel_selection": dict(snap.get("channel_selection") or {}),
        "identities": list(snap.get("identities") or []),
        "packet_sha256": str(packet_sha256 or ""),
        "kit_sha256": str(kit_sha256 or ""),
    }


def _manifest_items_from_shares(shares: list[dict]) -> list[dict]:
    items: list[dict] = []
    for share in shares or []:
        if not isinstance(share, dict):
            continue
        items.append({
            "channel": str(share.get("channel") or ""),
            "label": str(share.get("label") or ""),
            "mode": str(share.get("mode") or ""),
            "variant": str(share.get("variant") or "primary"),
            "post_text": str(share.get("post_text") or ""),
            "tracking_url": str(share.get("tracking_url") or ""),
            "compose_url": str(share.get("compose_url") or ""),
        })
    items.sort(key=lambda e: str(e.get("channel") or ""))
    return items


def _load_campaign_and_shares(memory_path: str | Path, project_id: str,
                               campaign_id: str) -> tuple[dict | None, list[dict]]:
    """メインDBからキャンペーンと共有行を読む。読取失敗は BindingReadError。"""
    from app.requirement_item_ledger import ledger_readonly_uri as _ro_uri
    mem = Path(memory_path)
    if not mem.exists():
        raise BindingReadError("connect_failed", detail="no main db")
    try:
        db = sqlite3.connect(_ro_uri(mem), uri=True, timeout=15)
        db.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise BindingReadError(
            "connect_failed", sqlite_error=_type_name(exc)) from exc
    try:
        try:
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error as exc:
            raise BindingReadError(
                "table_unreadable", sqlite_error=_type_name(exc)) from exc
        if "premarketing_campaigns" not in names:
            return None, []
        try:
            campaign_row = db.execute(
                "SELECT * FROM premarketing_campaigns WHERE project_id=? AND id=?",
                (str(project_id or ""), str(campaign_id or ""))).fetchone()
            campaign = dict(campaign_row) if campaign_row is not None else None
        except sqlite3.Error as exc:
            raise BindingReadError(
                "row_unreadable", sqlite_error=_type_name(exc)) from exc
        shares: list[dict] = []
        if "campaign_social_shares" in names:
            try:
                rows = db.execute(
                    "SELECT * FROM campaign_social_shares WHERE project_id=? AND campaign_id=? "
                    "ORDER BY channel, variant",
                    (str(project_id or ""), str(campaign_id or ""))).fetchall()
                shares = [dict(r) for r in rows]
            except sqlite3.Error as exc:
                raise BindingReadError(
                    "row_unreadable", sqlite_error=_type_name(exc)) from exc
        return campaign, shares
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _workspace_kit_hashes(memory_path: str | Path, project_id: str,
                          campaign_id: str,
                          workspace_root: str | Path | None) -> tuple[str | None, str | None]:
    """実キットファイルのハッシュ。ファイルが無ければ None。読取失敗は例外。"""
    root = None
    if workspace_root is not None:
        root = Path(workspace_root)
    else:
        import os
        env_root = str(os.getenv("WORKSPACE_ROOT") or "").strip()
        if env_root:
            root = Path(env_root)
    if root is None:
        return None, None
    # プロジェクトの workspace_path を読む。
    from app.requirement_item_ledger import ledger_readonly_uri as _ro_uri
    mem = Path(memory_path)
    configured = ""
    try:
        db = sqlite3.connect(_ro_uri(mem), uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            try:
                names = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.Error:
                names = set()
            if "projects" in names:
                try:
                    row = db.execute(
                        "SELECT workspace_path FROM projects WHERE id=?",
                        (str(project_id or ""),)).fetchone()
                    if row is not None:
                        try:
                            configured = str(row["workspace_path"] or "")
                        except (KeyError, IndexError):
                            configured = ""
                except sqlite3.Error:
                    configured = ""
        finally:
            try:
                db.close()
            except sqlite3.Error:
                pass
    except (OSError, sqlite3.Error, ValueError):
        return None, None
    rel = (configured or "").strip().replace("\\", "/").strip("/")
    if not rel:
        rel = f"projects/{project_id}"
    rel = rel.strip("/")
    if not rel or ".." in rel.split("/"):
        return None, None
    base = root / rel / f"premarketing/{campaign_id}/social"
    manifest_path = base / "social_post_manifest.json"
    kit_path = base / "social_post_kit.md"
    packet = None
    kit = None
    if manifest_path.is_file():
        try:
            packet = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise BindingReadError(
                "row_unreadable", sqlite_error=_type_name(exc),
                detail="kit manifest unreadable") from exc
    if kit_path.is_file():
        try:
            kit = hashlib.sha256(kit_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise BindingReadError(
                "row_unreadable", sqlite_error=_type_name(exc),
                detail="kit markdown unreadable") from exc
    return packet, kit


def current_packet_kit_hashes(memory_path: str | Path, project_id: str,
                               campaign_id: str,
                               workspace_root: str | Path | None = None) -> tuple[str, str]:
    """現在のキットのハッシュを再計算する。実ファイルがあれば実ファイルを優先する。"""
    from app.social_premarketing import social_package_json, social_package_markdown
    campaign, shares = _load_campaign_and_shares(memory_path, project_id, campaign_id)
    items = _manifest_items_from_shares(shares)
    manifest_text = social_package_json(items)
    kit_text = ""
    if campaign is not None:
        try:
            kit_text = social_package_markdown(campaign, items)
        except (ValueError, TypeError, AttributeError, KeyError):
            kit_text = ""
    try:
        db_packet = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, AttributeError):
        raise BindingReadError(
            "row_unreadable", detail="packet manifest hash unreadable")
    db_kit = ""
    if kit_text:
        try:
            db_kit = hashlib.sha256(kit_text.encode("utf-8")).hexdigest()
        except (TypeError, ValueError, AttributeError):
            raise BindingReadError(
                "row_unreadable", detail="kit markdown hash unreadable")
    try:
        file_packet, file_kit = _workspace_kit_hashes(
            memory_path, project_id, campaign_id, workspace_root)
    except BindingReadError:
        raise
    packet = file_packet if file_packet is not None else db_packet
    kit = file_kit if file_kit is not None else db_kit
    return packet, kit


def _match_identity_for_channel(identities: list[dict], selections: list[dict],
                                channel: str) -> tuple[dict | None, str]:
    """選択媒体に対応する公開ID行を探す。戻り値 (行 or None, 状態)。"""
    from app.social_premarketing import _platform_to_channel  # type: ignore
    wanted = str(channel or "")
    by_channel: dict[str, dict] = {}
    for row in selections or []:
        if not isinstance(row, dict):
            continue
        mapped = _platform_to_channel(row.get("platform"))
        if mapped and mapped not in by_channel:
            by_channel[mapped] = row
    ref = str((by_channel.get(wanted) or {}).get("account_ref") or "")
    scoped = [r for r in (identities or []) if isinstance(r, dict)]
    # account_ref での紐付けを優先する。
    if ref:
        for row in scoped:
            if str(row.get("identity_id") or "") == ref:
                return row, "matched_ref"
        for row in scoped:
            # ハンドルでの紐付けも許す(B1の表示と合わせる)。
            if str(row.get("handle") or "") == ref:
                return row, "matched_ref"
        return None, "unregistered"
    candidates = []
    for row in scoped:
        try:
            from app.social_premarketing import _platform_to_channel as _map
            mapped = _map(row.get("platform"))
        except (ImportError, AttributeError, ValueError):
            mapped = ""
        if mapped == wanted:
            candidates.append(row)
    if len(candidates) == 1:
        return candidates[0], "matched_single"
    if len(candidates) > 1:
        return None, "ambiguous"
    return None, "unregistered"


def _selection_channels(registry_snapshot: dict) -> list[str]:
    sel = registry_snapshot.get("channel_selection") if isinstance(registry_snapshot, dict) else {}
    if isinstance(sel, dict) and isinstance(sel.get("channels"), list):
        return [str(c) for c in sel.get("channels") if str(c)]
    return []


def check_publication_binding(memory_path: str | Path, project_id: str,
                              campaign_id: str,
                              workspace_root: str | Path | None = None) -> dict:
    """現在の登録の版・実際のキットのハッシュを再計算し、approved の束縛と比べる。

    戻り値の status: ok / stale / no_binding / blocked / unreadable。
    比較は『値の存在』でなく版・ハッシュで行う。読取失敗は ok/stale に倒さない。
    """
    pid = str(project_id or "")
    cid = str(campaign_id or "")
    if not pid or not cid:
        raise ValueError("project_id and campaign_id are required")
    try:
        viewed = list_bindings(memory_path, pid, cid)
    except BindingReadError as exc:
        return {"status": "unreadable", "reasons": ["束縛の台帳を読めない"],
                "failure_kind": exc.kind, "path_kind": exc.path_kind,
                "sqlite_error": exc.sqlite_error,
                "project_id": pid, "campaign_id": cid}
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "unreadable", "reasons": ["束縛の台帳を読めない"],
                "failure_kind": "row_unreadable", "sqlite_error": _type_name(exc),
                "project_id": pid, "campaign_id": cid}
    bindings = viewed.get("bindings") if isinstance(viewed, dict) else []
    approved = None
    for row in bindings or []:
        if isinstance(row, dict) and str(row.get("stage") or "") == "approved":
            approved = row
            break
    if approved is None:
        return {"status": "no_binding",
                "reasons": ["承認の束縛が無いため公開できない。古い承認は使えない"],
                "project_id": pid, "campaign_id": cid,
                "approved_binding": None}
    approved_payload = approved.get("binding") if isinstance(approved, dict) else {}
    if not isinstance(approved_payload, dict):
        approved_payload = {}
    try:
        registry_snapshot = collect_registry_snapshot(memory_path, pid, cid)
    except BindingReadError as exc:
        return {"status": "unreadable", "reasons": ["登録の版を読めない"],
                "failure_kind": exc.kind, "path_kind": exc.path_kind,
                "sqlite_error": exc.sqlite_error,
                "project_id": pid, "campaign_id": cid,
                "approved_binding": approved}
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "unreadable", "reasons": ["登録の版を読めない"],
                "failure_kind": "row_unreadable", "sqlite_error": _type_name(exc),
                "project_id": pid, "campaign_id": cid,
                "approved_binding": approved}
    try:
        packet_sha, kit_sha = current_packet_kit_hashes(
            memory_path, pid, cid, workspace_root)
    except BindingReadError as exc:
        return {"status": "unreadable", "reasons": ["キットのハッシュを読めない"],
                "failure_kind": exc.kind, "path_kind": exc.path_kind,
                "sqlite_error": exc.sqlite_error,
                "project_id": pid, "campaign_id": cid,
                "approved_binding": approved}
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "unreadable", "reasons": ["キットのハッシュを読めない"],
                "failure_kind": "row_unreadable", "sqlite_error": _type_name(exc),
                "project_id": pid, "campaign_id": cid,
                "approved_binding": approved}
    current_payload = build_binding_payload(registry_snapshot, packet_sha, kit_sha)
    reasons: list[str] = []
    blocked_reasons: list[str] = []
    now = time.time()
    # 対象媒体は承認時の選択を基準にする。承認時に選択が無ければ現在の選択を使う。
    approved_channels = []
    try:
        sel = (approved_payload.get("channel_selection")
               if isinstance(approved_payload, dict) else {})
        if isinstance(sel, dict) and isinstance(sel.get("channels"), list):
            approved_channels = [str(c) for c in sel.get("channels") if str(c)]
    except (AttributeError, TypeError):
        approved_channels = []
    target_channels = list(approved_channels) if approved_channels else _selection_channels(
        registry_snapshot)
    # 公開IDの照合。fictional は stale ではなく blocked。
    try:
        from app.contact_registry import list_channel_selections as _list_sel
        from app.contact_registry import list_public_identities as _list_ids
        sel_viewed = _list_sel(memory_path, pid)
        ids_viewed = _list_ids(memory_path, pid)
        sel_current = sel_viewed.get("current") if isinstance(sel_viewed, dict) else []
        ids_current = ids_viewed.get("current") if isinstance(ids_viewed, dict) else []
        scoped_sel, _ = _scope_rows(list(sel_current or []), cid)
        scoped_ids, _ = _scope_rows(list(ids_current or []), cid)
    except Exception as exc:
        return {"status": "unreadable", "reasons": ["登録の版を読めない"],
                "failure_kind": "row_unreadable", "sqlite_error": _type_name(exc),
                "project_id": pid, "campaign_id": cid,
                "approved_binding": approved}
    approved_identities = approved_payload.get("identities") if isinstance(
        approved_payload, dict) else []
    if not isinstance(approved_identities, list):
        approved_identities = []
    approved_by_channel: dict[str, dict] = {}
    # 承認時の媒体ごとの公開IDを identities から platform で対応付ける。
    for channel in target_channels:
        candidates = [r for r in approved_identities
                      if isinstance(r, dict) and _channel_of_identity(r) == channel]
        if len(candidates) == 1:
            approved_by_channel[channel] = candidates[0]
    for channel in target_channels:
        current_row, match_state = _match_identity_for_channel(
            list(scoped_ids), list(scoped_sel), channel)
        approved_row = approved_by_channel.get(channel)
        if current_row is not None and str(current_row.get("source_kind") or "") == "fictional":
            blocked_reasons.append(f"{channel}の公開IDが架空のため公開不可")
            continue
        if match_state == "ambiguous":
            reasons.append(f"{channel}の公開IDが複数あり特定できない")
            continue
        if current_row is None:
            reasons.append(f"{channel}の公開IDが未登録または紐付けが変わった")
            continue
        # 失効の判定。
        if str(current_row.get("source_kind") or "") == "retired":
            reasons.append(f"{channel}の公開IDが失効した")
            continue
        expires = _expires_at_value(current_row.get("expires_at"))
        if expires is not None and now >= expires:
            reasons.append(f"{channel}の公開IDが失効した")
            continue
        if approved_row is None:
            reasons.append(f"{channel}の公開IDの束縛が無く照合できない")
            continue
        if (str(current_row.get("identity_id") or "")
                != str(approved_row.get("identity_id") or "")):
            reasons.append(f"{channel}の公開IDが変わった")
            continue
        try:
            current_version = int(current_row.get("version"))
            approved_version = int(approved_row.get("version"))
        except (TypeError, ValueError):
            return {"status": "unreadable",
                    "reasons": [f"{channel}の公開IDの版を読めない"],
                    "failure_kind": "row_unreadable",
                    "project_id": pid, "campaign_id": cid,
                    "approved_binding": approved}
        if current_version != approved_version:
            reasons.append(f"{channel}の公開IDの版が変わった")
            continue
        # 現在の行は表示用の行(handle/profile_url を持つ)。集計と同じ式で指紋を作って比べる。
        if _identity_value_sha(current_row) != str(
                approved_row.get("value_sha256") or ""):
            reasons.append(f"{channel}の公開IDが変わった")
            continue
        if str(current_row.get("source_kind") or "") != str(
                approved_row.get("source_kind") or ""):
            reasons.append(f"{channel}の公開IDの状態が変わった")
            continue
    # 媒体選択の照合。
    approved_sel = approved_payload.get("channel_selection") if isinstance(
        approved_payload, dict) else {}
    current_sel = registry_snapshot.get("channel_selection") if isinstance(
        registry_snapshot, dict) else {}
    if not isinstance(approved_sel, dict):
        approved_sel = {}
    if not isinstance(current_sel, dict):
        current_sel = {}
    if (sorted(str(c) for c in (approved_sel.get("channels") or []))
            != sorted(str(c) for c in (current_sel.get("channels") or []))):
        reasons.append("媒体選択が変わった")
    else:
        approved_versions = approved_sel.get("versions") if isinstance(
            approved_sel.get("versions"), dict) else {}
        current_versions = current_sel.get("versions") if isinstance(
            current_sel.get("versions"), dict) else {}
        if dict(approved_versions or {}) != dict(current_versions or {}):
            reasons.append("媒体選択の版が変わった")
    # 連絡先の照合。
    approved_contacts = approved_payload.get("contact_routes") if isinstance(
        approved_payload, dict) else []
    current_contacts = registry_snapshot.get("contact_routes") if isinstance(
        registry_snapshot, dict) else []
    if not isinstance(approved_contacts, list):
        approved_contacts = []
    if not isinstance(current_contacts, list):
        current_contacts = []
    approved_by_id = {str(r.get("route_id") or ""): r for r in approved_contacts
                      if isinstance(r, dict) and str(r.get("route_id") or "")}
    current_by_id = {str(r.get("route_id") or ""): r for r in current_contacts
                     if isinstance(r, dict) and str(r.get("route_id") or "")}
    if set(approved_by_id) != set(current_by_id):
        reasons.append("連絡先の版が変わった")
    else:
        for route_id, approved_row in approved_by_id.items():
            current_row = current_by_id.get(route_id)
            if not isinstance(current_row, dict):
                reasons.append(f"連絡先{route_id}の版が変わった")
                continue
            # 承認時点で既に失効していた連絡先は『変わった』ではないので止めない。
            # 承認後に失効へ変わったときだけ止める(版の比較で別途検出)。
            if (str(current_row.get("source_kind") or "") == "retired"
                    and str(approved_row.get("source_kind") or "") != "retired"):
                reasons.append(f"連絡先{route_id}が失効した")
                continue
            expires = _expires_at_value(current_row.get("expires_at"))
            if expires is not None and now >= expires:
                reasons.append(f"連絡先{route_id}が失効した")
                continue
            try:
                current_contact_version = int(current_row.get("version"))
                approved_contact_version = int(approved_row.get("version"))
            except (TypeError, ValueError):
                return {"status": "unreadable",
                        "reasons": [f"連絡先{route_id}の版を読めない"],
                        "failure_kind": "row_unreadable",
                        "project_id": pid, "campaign_id": cid,
                        "approved_binding": approved}
            if current_contact_version != approved_contact_version:
                reasons.append(f"連絡先{route_id}の版が変わった")
                continue
            if str(current_row.get("value_sha256") or "") != str(
                    approved_row.get("value_sha256") or ""):
                reasons.append("連絡先の版が変わった")
                continue
            if str(current_row.get("source_kind") or "") != str(
                    approved_row.get("source_kind") or ""):
                reasons.append(f"連絡先{route_id}の状態が変わった")
                continue
    # packet・成果物の照合。
    if str(approved_payload.get("packet_sha256") or "") != str(packet_sha or ""):
        reasons.append("公開packetが変わった")
    if str(approved_payload.get("kit_sha256") or "") != str(kit_sha or ""):
        reasons.append("承認後に成果物が変わった")
    if blocked_reasons:
        return {"status": "blocked", "reasons": sorted(set(blocked_reasons + reasons)),
                "project_id": pid, "campaign_id": cid,
                "approved_binding": approved,
                "current_binding": current_payload,
                "approved_binding_id": int(approved.get("id") or 0),
                "approved_binding_sha256": str(approved.get("binding_sha256") or "")}
    if reasons:
        return {"status": "stale", "reasons": sorted(set(reasons)),
                "project_id": pid, "campaign_id": cid,
                "approved_binding": approved,
                "current_binding": current_payload,
                "approved_binding_id": int(approved.get("id") or 0),
                "approved_binding_sha256": str(approved.get("binding_sha256") or "")}
    # 版が一致していても「登録済み」は公開用の確認済みではない。
    # 最終操作の直前に公開用途の入力適格性を再検査する。
    try:
        from app.contact_registry import preflight as _contact_preflight
        public_check = _contact_preflight(
            memory_path, pid, campaign_id=cid, purposes=["public"])
    except Exception as exc:
        return {"status": "unreadable", "reasons": ["公開用の登録を読めない"],
                "failure_kind": "row_unreadable", "sqlite_error": _type_name(exc),
                "project_id": pid, "campaign_id": cid,
                "approved_binding": approved}
    readiness = public_check.get("public_input_ready") if isinstance(public_check, dict) else None
    if not isinstance(readiness, dict) or readiness.get("ready") is not True:
        missing = (readiness or {}).get("missing_fields") if isinstance(readiness, dict) else []
        fields = [str(field) for field in (missing or []) if str(field)]
        detail = "、".join(fields) if fields else "登録の確認結果"
        return {"status": "blocked", "reasons": [f"公開用途の確認済み入力が不足: {detail}"],
                "project_id": pid, "campaign_id": cid,
                "approved_binding": approved,
                "current_binding": current_payload,
                "approved_binding_id": int(approved.get("id") or 0),
                "approved_binding_sha256": str(approved.get("binding_sha256") or "")}
    return {"status": "ok", "reasons": [],
            "project_id": pid, "campaign_id": cid,
            "approved_binding": approved,
            "current_binding": current_payload,
            "approved_binding_id": int(approved.get("id") or 0),
            "approved_binding_sha256": str(approved.get("binding_sha256") or "")}


def _channel_of_identity(row: dict) -> str:
    try:
        from app.social_premarketing import _platform_to_channel as _map
        return str(_map(row.get("platform")) or "")
    except (ImportError, AttributeError, ValueError):
        return ""
