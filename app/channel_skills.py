"""C1: 共通外部チャネルスキルの契約・状態・成熟度・証拠・追記型実行台帳。

- 実通信は作らない。アダプタは登録口だけ(初期は空)。
- 保存先は項目別台帳と同じSQLite(item_ledger_db_path)への追記型3表。
- 外部本文は未信頼データ。証拠行に untrusted:true を常に付ける。
- パスワード・Cookie・APIキー・トークンは証拠・台帳・応答に入れない。
- 読取失敗は ChannelSkillReadError(『実行なし』『接続なし』『成功』に倒さない)。
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
from pathlib import Path
from typing import Any

from app.requirement_item_ledger import (
    _ensure_tables as _ensure_item_tables,
    classify_ledger_path_kind,
    item_ledger_db_path,
    ledger_readonly_uri,
)

LOGGER = logging.getLogger(__name__)

SKILL_STATES = frozenset({
    "ready", "needs_configuration", "needs_approval", "running",
    "succeeded", "no_results", "failed", "unknown",
})
RUN_STATUSES = frozenset({"running", "succeeded", "no_results", "failed", "unknown"})
VERIFICATION_KINDS = frozenset({"permission", "field"})
VERIFICATION_ACTIONS = frozenset({"verify", "revoke"})
OPERATING_MODES = frozenset({"demo_test", "production"})

CHANNEL_SKILL_IDS = frozenset({
    "web.search_public", "mail.receive", "social.observe", "mail.send_approved",
})


class ChannelSkillReadError(RuntimeError):
    """台帳の読取失敗。『実行なし』『接続なし』とは区別する。"""

    def __init__(self, kind: str, *, path_kind: str = "",
                 sqlite_error: str = "", detail: str = "") -> None:
        super().__init__(f"channel skill read failed: {kind}")
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
# スキル定義(契約)
# ----------------------------------------------------------------------------

def validate_skill_definition(payload: dict) -> dict:
    """スキル定義の検証。不足・不正は ValueError。"""
    if not isinstance(payload, dict):
        raise ValueError("skill definition is required")
    capability_id = str(payload.get("capability_id") or "").strip()
    if not capability_id:
        raise ValueError("capability_id is required")
    version = str(payload.get("version") or "").strip()
    if not version:
        raise ValueError("version is required")
    purpose = str(payload.get("purpose") or "").strip()
    if not purpose:
        raise ValueError("purpose is required")
    provider_kind = str(payload.get("provider_kind") or "").strip()
    if not provider_kind:
        raise ValueError("provider_kind is required")
    required_permissions = payload.get("required_permissions")
    if not isinstance(required_permissions, list) or not required_permissions:
        raise ValueError("required_permissions is required")
    for item in required_permissions:
        if not str(item or "").strip():
            raise ValueError("required_permissions must be non-empty strings")
    input_refs = payload.get("input_refs")
    if not isinstance(input_refs, list) or not input_refs:
        raise ValueError("input_refs is required")
    output_format = str(payload.get("output_format") or "").strip()
    if not output_format:
        raise ValueError("output_format is required")
    retry_policy = str(payload.get("retry_policy") or "").strip()
    if not retry_policy:
        raise ValueError("retry_policy is required")
    # 自動再試行しない場合は none と明記する(空を許さない)。
    if retry_policy not in ("none", "manual_only"):
        raise ValueError("retry_policy must be none or manual_only")
    requires_approval = payload.get("requires_approval")
    if not isinstance(requires_approval, bool):
        raise ValueError("requires_approval is required")
    allowed_modes = payload.get("allowed_operating_modes")
    if not isinstance(allowed_modes, list) or not allowed_modes:
        raise ValueError("allowed_operating_modes is required")
    for mode in allowed_modes:
        if str(mode) not in OPERATING_MODES:
            raise ValueError("allowed_operating_modes must be demo_test/production")
    try:
        retention = int(payload.get("data_retention_days"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("data_retention_days is required") from None
    if retention < 0:
        raise ValueError("data_retention_days is out of range")
    allowed_viewers = payload.get("allowed_viewers")
    if not isinstance(allowed_viewers, list) or not allowed_viewers:
        raise ValueError("allowed_viewers is required")
    reads_external = payload.get("reads_external_content")
    if not isinstance(reads_external, bool):
        raise ValueError("reads_external_content is required")
    return {
        "capability_id": capability_id,
        "version": version,
        "purpose": purpose,
        "provider_kind": provider_kind,
        "required_permissions": [str(x) for x in required_permissions],
        "input_refs": [str(x) for x in input_refs],
        "output_format": output_format,
        "retry_policy": retry_policy,
        "requires_approval": requires_approval,
        "allowed_operating_modes": [str(x) for x in allowed_modes],
        "data_retention_days": retention,
        "allowed_viewers": [str(x) for x in allowed_viewers],
        "reads_external_content": reads_external,
    }


_SKILL_DEFINITIONS_RAW: list[dict] = [
    {
        "capability_id": "web.search_public",
        "version": "1",
        "purpose": "許可された公開Web調査だけを行い、一次資料のURL・取得時刻・ハッシュを計画の根拠へ結ぶ",
        "provider_kind": "public_web",
        "required_permissions": ["public_read"],
        "input_refs": ["query", "period", "purpose"],
        "output_format": "evidence_rows",
        "retry_policy": "none",
        "requires_approval": False,
        "allowed_operating_modes": ["demo_test", "production"],
        "data_retention_days": 90,
        "allowed_viewers": ["operator"],
        "reads_external_content": True,
    },
    {
        "capability_id": "mail.receive",
        "version": "1",
        "purpose": "許可された専用メールボックスの対象期間・フォルダだけを差分取得する",
        "provider_kind": "imap",
        "required_permissions": ["mail_read"],
        "input_refs": ["mailbox", "folder", "period"],
        "output_format": "evidence_rows",
        "retry_policy": "none",
        "requires_approval": False,
        "allowed_operating_modes": ["demo_test", "production"],
        "data_retention_days": 90,
        "allowed_viewers": ["operator"],
        "reads_external_content": True,
    },
    {
        "capability_id": "social.observe",
        "version": "1",
        "purpose": "登録済み公開ID・投稿IDから実投稿と取得可能な反応を確認する",
        "provider_kind": "social_x",
        "required_permissions": ["social_read"],
        "input_refs": ["public_id", "post_id", "period"],
        "output_format": "evidence_rows",
        "retry_policy": "none",
        "requires_approval": False,
        "allowed_operating_modes": ["demo_test", "production"],
        "data_retention_days": 90,
        "allowed_viewers": ["operator"],
        "reads_external_content": True,
    },
    {
        "capability_id": "mail.send_approved",
        "version": "1",
        "purpose": "承認済みメールだけを送信直前に再照合して送る",
        "provider_kind": "smtp",
        "required_permissions": ["mail_send"],
        "input_refs": ["to", "subject", "body", "approved_binding", "operating_mode"],
        "output_format": "send_receipt",
        "retry_policy": "none",
        "requires_approval": True,
        "allowed_operating_modes": ["demo_test", "production"],
        "data_retention_days": 90,
        "allowed_viewers": ["operator"],
        "reads_external_content": False,
    },
]

SKILL_DEFINITIONS: dict[str, dict] = {}
for _raw in _SKILL_DEFINITIONS_RAW:
    _validated = validate_skill_definition(_raw)
    SKILL_DEFINITIONS[_validated["capability_id"]] = _validated


def get_skill_definition(capability_id: str) -> dict:
    """定義を返す。未知は ValueError。"""
    key = str(capability_id or "").strip()
    if key not in SKILL_DEFINITIONS:
        raise ValueError(f"unknown capability_id: {key}")
    return dict(SKILL_DEFINITIONS[key])


def list_skill_definitions() -> list[dict]:
    return [dict(SKILL_DEFINITIONS[key]) for key in sorted(SKILL_DEFINITIONS)]


# ----------------------------------------------------------------------------
# アダプタ・接続設定・承認(実通信は作らない。登録口だけ)
# ----------------------------------------------------------------------------

_ADAPTERS: dict[str, str] = {}
_CONNECTIONS: dict[tuple[str, str], dict] = {}
_APPROVALS: dict[tuple[str, str], bool] = {}
_registry_guard = threading.Lock()


def register_adapter(capability_id: str, adapter: str) -> dict:
    key = str(capability_id or "").strip()
    if key not in SKILL_DEFINITIONS:
        raise ValueError(f"unknown capability_id: {key}")
    name = str(adapter or "").strip()
    if not name:
        raise ValueError("adapter is required")
    with _registry_guard:
        _ADAPTERS[key] = name
    return {"capability_id": key, "adapter": name}


def clear_adapters() -> None:
    with _registry_guard:
        _ADAPTERS.clear()
        _CONNECTIONS.clear()
        _APPROVALS.clear()


def has_adapter(capability_id: str) -> bool:
    with _registry_guard:
        return str(capability_id or "") in _ADAPTERS


def set_connection_config(project_id: str, capability_id: str, config: dict) -> dict:
    pid = str(project_id or "").strip()
    key = str(capability_id or "").strip()
    if not pid or key not in SKILL_DEFINITIONS:
        raise ValueError("project_id and known capability_id are required")
    if not isinstance(config, dict) or not config:
        raise ValueError("config is required")
    with _registry_guard:
        _CONNECTIONS[(pid, key)] = dict(config)
    return {"project_id": pid, "capability_id": key}


def has_connection(project_id: str, capability_id: str) -> bool:
    with _registry_guard:
        return (str(project_id or ""), str(capability_id or "")) in _CONNECTIONS


def set_approval_present(project_id: str, capability_id: str, present: bool) -> dict:
    pid = str(project_id or "").strip()
    key = str(capability_id or "").strip()
    if not pid or key not in SKILL_DEFINITIONS:
        raise ValueError("project_id and known capability_id are required")
    with _registry_guard:
        _APPROVALS[(pid, key)] = bool(present)
    return {"project_id": pid, "capability_id": key, "present": bool(present)}


def has_approval(project_id: str, capability_id: str) -> bool:
    with _registry_guard:
        return bool(_APPROVALS.get((str(project_id or ""), str(capability_id or ""))))


# ----------------------------------------------------------------------------
# 公開Web調査の許可(承認済みの計画の契約だけ。語の一致では許可しない)
# ----------------------------------------------------------------------------

# 計画の状態(app/project_manager.py・app/memory/short_term.py で使われる値)。
# 承認前を表すもの。ここに無い・承認前でもない未知の値は許可しない(閉じた側)。
PLAN_PRE_APPROVAL_STATUSES = frozenset({"draft", "planning"})
# 承認後の状態。承認の記録(kind='approved')があり、その後に計画が作り直されて
# いない場合にだけ許可の根拠になる。
PLAN_APPROVED_STATUSES = frozenset({
    "ready", "running", "paused", "completed", "failed", "cancelled", "needs_review",
})
# 計画の中身が変わったことを表すイベント。承認の記録より新しければ、承認は
# 現在の計画に対するものではないので許可しない(作り直し・目標追補・台帳診断の追加)。
PLAN_CHANGE_EVENT_KINDS = ("plan_generated", "plan_extended")


def web_search_permission(memory_path: str | Path,
                          project_id: str) -> dict:
    """公開Web調査の許可を判定する。

    許可 = 次の全部が成り立つ。
    (1) そのPJの状態が承認前(draft/planning)でない。
    (2) 人の承認の記録(project_events の kind='approved')がある。
    (3) その承認の後に計画が作り直されず・追補されていない(kind='plan_generated'
        と kind='plan_extended' のどちらも承認より新しくない)。
    (4) その計画に `public_web_research.required` が真の契約を持つタスクが
        1つ以上ある。
    語の一致や外部アクションの文字列では許可しない。
    戻り値は {"allowed", "reason"}。
    reason は allowed/no_plan/not_approved/no_web_task/unreadable。
    読めない(例外)は『許可なし』と『読取不能』を区別する(unreadable)。
    握りつぶし(pass・空の代替値)は使わない。
    """
    pid = str(project_id or "").strip()
    if not pid:
        return {"allowed": False, "reason": "no_plan"}
    try:
        import sqlite3 as _sqlite3

        from app.memory.short_term import ShortTermMemory
        resolved = Path(str(memory_path or ""))
        db_path = resolved if resolved.suffix else resolved.parent / "conversations.db"
        # 素のDB(テーブル未作成)は『計画なし』として扱う(旧来の needs_configuration)。
        # 読取中の例外だけ unreadable。握りつぶしは使わない。
        if not db_path.exists():
            return {"allowed": False, "reason": "no_plan"}
        memory = ShortTermMemory(db_path, read_only=True)
        try:
            mission = memory.get_mission(pid)
        except _sqlite3.Error:
            return {"allowed": False, "reason": "no_plan"}
        except (OSError, ValueError, TypeError,
                AttributeError, KeyError) as exc:
            return {"allowed": False, "reason": "unreadable",
                    "error_type": type(exc).__name__}
        if not isinstance(mission, dict) or not mission:
            return {"allowed": False, "reason": "no_plan"}
        status = str(mission.get("status") or "")
        if status in PLAN_PRE_APPROVAL_STATUSES or status not in PLAN_APPROVED_STATUSES:
            # 承認前(draft/planning)と未知の状態は許可しない。
            return {"allowed": False, "reason": "not_approved",
                    "status": status}
        # 承認後の状態でも、人の承認の記録が要る。状態の値だけでは判定しない。
        # get_mission() は直近の一部イベントしか返さないため、最新IDを直接読む。
        try:
            latest = memory.latest_event_ids(
                pid, ("approved",) + PLAN_CHANGE_EVENT_KINDS)
        except (_sqlite3.Error, OSError, ValueError, TypeError,
                AttributeError) as exc:
            return {"allowed": False, "reason": "unreadable",
                    "error_type": type(exc).__name__}
        approved_id = int(latest.get("approved") or 0)
        if approved_id <= 0:
            # 承認の記録が無い(状態だけ running/ready などになっている)。
            return {"allowed": False, "reason": "not_approved",
                    "status": status}
        latest_change = max(int(latest.get(kind) or 0)
                            for kind in PLAN_CHANGE_EVENT_KINDS)
        if latest_change > approved_id:
            # 承認の後に計画が作り直された・追補された。再承認まで許可しない。
            return {"allowed": False, "reason": "not_approved",
                    "status": status}
        tasks = mission.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return {"allowed": False, "reason": "no_web_task"}
        from app.structured_planning import contract_of
        for task in tasks:
            if not isinstance(task, dict):
                continue
            try:
                contract = contract_of(task)
            except (ImportError, AttributeError, ValueError, TypeError,
                    KeyError, _sqlite3.Error) as exc:
                return {"allowed": False, "reason": "unreadable",
                        "error_type": type(exc).__name__}
            if not isinstance(contract, dict):
                continue
            web = contract.get("public_web_research")
            if isinstance(web, dict) and bool(web.get("required")):
                return {"allowed": True, "reason": "allowed"}
        return {"allowed": False, "reason": "no_web_task"}
    except (OSError, ValueError, TypeError, AttributeError, ImportError,
            _sqlite3.Error) as exc:
        return {"allowed": False, "reason": "unreadable",
                "error_type": type(exc).__name__}


def _persistent_web_allowed(memory_path: str | Path, project_id: str) -> bool:
    """後方互換の真偽値。読取不能・無しは False(厳しい側)。"""
    try:
        viewed = web_search_permission(memory_path, project_id)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    if not isinstance(viewed, dict):
        return False
    return bool(viewed.get("allowed"))


def is_web_search_allowed(memory_path: str | Path, project_id: str) -> bool:
    """公開Web調査の許可。API・実行層が使う。読取不能は False。"""
    return bool(_persistent_web_allowed(memory_path, project_id))


# ----------------------------------------------------------------------------
# 台帳3表
# ----------------------------------------------------------------------------

RUN_TABLE_DDL = """CREATE TABLE IF NOT EXISTS channel_skill_runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    status TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    operating_mode TEXT NOT NULL DEFAULT '',
    adapter TEXT NOT NULL DEFAULT '',
    params_summary TEXT NOT NULL DEFAULT '{}',
    started_at REAL NOT NULL,
    finished_at REAL,
    error_kind TEXT NOT NULL DEFAULT '',
    evidence_count INTEGER NOT NULL DEFAULT 0,
    completed_ok INTEGER NOT NULL DEFAULT 0)"""

EVIDENCE_TABLE_DDL = """CREATE TABLE IF NOT EXISTS channel_skill_evidence(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL,
    account_or_public_id TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    attachment_sha256s_json TEXT NOT NULL DEFAULT '[]',
    plan_item_id TEXT NOT NULL,
    approved_binding_id TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL,
    retention_days INTEGER NOT NULL DEFAULT 0,
    excerpt TEXT NOT NULL DEFAULT '',
    excerpt_redacted INTEGER NOT NULL DEFAULT 0,
    redact_reason TEXT NOT NULL DEFAULT '',
    untrusted INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL)"""

VERIFICATION_TABLE_DDL = """CREATE TABLE IF NOT EXISTS channel_skill_verifications(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    verification_kind TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'verify',
    actor TEXT NOT NULL,
    memo TEXT NOT NULL,
    created_at REAL NOT NULL)"""


def ensure_channel_skill_tables(db: sqlite3.Connection) -> None:
    _ensure_item_tables(db)
    db.execute(RUN_TABLE_DDL)
    db.execute(EVIDENCE_TABLE_DDL)
    db.execute(VERIFICATION_TABLE_DDL)
    db.execute("""CREATE INDEX IF NOT EXISTS idx_channel_runs_project
        ON channel_skill_runs(project_id, capability_id, id DESC)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_channel_evidence_project
        ON channel_skill_evidence(project_id, capability_id, id DESC)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_channel_verification_project
        ON channel_skill_verifications(project_id, capability_id, id DESC)""")


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
            ensure_channel_skill_tables(db)
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
        raise ChannelSkillReadError(
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
        raise ChannelSkillReadError(
            "table_unreadable", sqlite_error=_type_name(exc)) from exc
    return {str(row[0]) for row in rows}


# ----------------------------------------------------------------------------
# 状態の決定規則(a)〜(h) 純粋関数 + DB解決
# ----------------------------------------------------------------------------

def resolve_skill_state(*, adapter_present: bool, connection_configured: bool,
                        approval_required: bool, approval_present: bool,
                        running: bool, latest_status: str = "",
                        latest_readable: bool = True) -> str:
    """状態の決定規則の純粋関数。決定できないとき unknown。

    (a)アダプタ無し・接続設定無し → needs_configuration
    (b)承認要・無し → needs_approval
    (c)直近失敗 → failed
    (d)直近不明 → unknown
    (e)正常0件 → no_results
    (f)正常1件以上 → succeeded
    (g)実行中 → running
    (h)準備済み → ready
    """
    if not adapter_present or not connection_configured:
        return "needs_configuration"
    if approval_required and not approval_present:
        return "needs_approval"
    if running:
        return "running"
    status = str(latest_status or "")
    if not status:
        # 実行が無い: 準備できている
        return "ready"
    if not latest_readable:
        return "unknown"
    if status == "failed":
        return "failed"
    if status == "unknown":
        return "unknown"
    if status == "no_results":
        return "no_results"
    if status == "succeeded":
        return "succeeded"
    if status == "running":
        return "running"
    return "unknown"


def _latest_run_status(db: sqlite3.Connection, project_id: str,
                       capability_id: str) -> tuple[str, bool, dict | None]:
    """直近の実行を読む。行が無ければ ("", True, None)。読取失敗は例外。"""
    try:
        row = db.execute(
            "SELECT * FROM channel_skill_runs WHERE project_id=? AND capability_id=? "
            "ORDER BY id DESC LIMIT 1",
            (project_id, capability_id)).fetchone()
    except sqlite3.Error as exc:
        raise ChannelSkillReadError(
            "row_unreadable", sqlite_error=_type_name(exc)) from exc
    if row is None:
        return "", True, None
    saved = dict(row)
    status = str(saved.get("status") or "")
    if status not in SKILL_STATES:
        return status, False, saved
    return status, True, saved


def skill_state(memory_path: str | Path, project_id: str,
                capability_id: str) -> str:
    """DB参照の状態解決。読取失敗は例外(『接続なし』等に倒さない)。"""
    pid = str(project_id or "").strip()
    key = str(capability_id or "").strip()
    if not pid or key not in SKILL_DEFINITIONS:
        raise ValueError("project_id and known capability_id are required")
    definition = SKILL_DEFINITIONS[key]
    adapter_present = has_adapter(key)
    if key == "mail.receive":
        # C3: 接続設定は永続の保存先だけから求める。
        # メモリ内の辞書では上書きできない(C2の公開Webと同じ作り)。
        # 読取不能は ready/needs_configuration に倒さず unknown を返す。
        try:
            from app import channel_skill_mail as _mail_skill
            _mail_current = _mail_skill.get_current_settings(
                memory_path, pid)
        except Exception:
            return "unknown"
        if _mail_current is None:
            connection_configured = False
        elif not isinstance(_mail_current, dict):
            return "unknown"
        else:
            # 仕様の(a): 接続済みはアダプタ登録と環境変数の存在がそろったときだけ。
            # 環境変数が無ければ接続できないため needs_configuration(厳しい側)。
            # 値は見ない(存在だけ)。
            _prefix = str(_mail_current.get("credential_env_prefix") or "")
            connection_configured = bool(
                _mail_skill.mail_env_ready(_prefix))
    elif key == "web.search_public":
        # 公開Webの接続設定は承認済みの計画の契約だけから求める。
        # メモリ内の辞書では上書きできない(後続のメール・SNS用に辞書は残す)。
        permission = web_search_permission(memory_path, pid)
        if str(permission.get("reason") or "") == "unreadable":
            # 読取不能は ready に倒さない。unknown を返す。
            return "unknown"
        connection_configured = bool(permission.get("allowed"))
    elif key == "social.observe":
        # C4: 接続設定は永続の保存先だけから求める。
        # メモリ内の辞書では上書きできない(C2・C3と同じ作り)。
        # 読取不能は ready/needs_configuration に倒さず unknown を返す。
        try:
            from app import channel_skill_social as _social_skill
            _social_current = _social_skill.get_current_settings(
                memory_path, pid)
        except Exception:
            return "unknown"
        if _social_current is None:
            connection_configured = False
        elif not isinstance(_social_current, dict):
            return "unknown"
        else:
            connection_configured = True
    elif key == "mail.send_approved":
        # C5: 接続設定は (a)SMTPアダプタの登録(起動時の register_adapter)、
        # (b)SMTP設定の存在(環境変数の存在だけ)、の両方が真のときだけ真。
        # メモリ内の辞書では上書きできない(C2〜C4と同じ作り)。
        # 読取不能は ready/needs_configuration に倒さず unknown を返す。
        try:
            from app import channel_skill_mail_send as _send_skill
            _send_view = _send_skill.mail_send_connection_view(
                memory_path, pid)
        except Exception:
            return "unknown"
        if not isinstance(_send_view, dict):
            return "unknown"
        if not bool(_send_view.get("readable", True)):
            return "unknown"
        connection_configured = bool(_send_view.get("configured"))
    else:
        connection_configured = has_connection(pid, key)
    if not adapter_present or not connection_configured:
        return resolve_skill_state(
            adapter_present=adapter_present,
            connection_configured=connection_configured,
            approval_required=bool(definition.get("requires_approval")),
            approval_present=False, running=False, latest_status="",
            latest_readable=True)
    approval_present = True
    if definition.get("requires_approval"):
        approval_present = has_approval(pid, key)
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return resolve_skill_state(
            adapter_present=True, connection_configured=True,
            approval_required=bool(definition.get("requires_approval")),
            approval_present=approval_present, running=False,
            latest_status="", latest_readable=True)
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "channel_skill_runs" not in names:
            return resolve_skill_state(
                adapter_present=True, connection_configured=True,
                approval_required=bool(definition.get("requires_approval")),
                approval_present=approval_present, running=False,
                latest_status="", latest_readable=True)
        status, readable, _saved = _latest_run_status(db, pid, key)
        running = (status == "running")
        # 実行中行があっても最新の確定行が別にある場合は最新を優先する
        # (追記型のため running のまま残る行より新しい確定行を正とする)。
        # _latest_run_status は最新1行だけを見るため、そのまま使う。
        return resolve_skill_state(
            adapter_present=True, connection_configured=True,
            approval_required=bool(definition.get("requires_approval")),
            approval_present=approval_present, running=running,
            latest_status=status, latest_readable=readable)


def capability_registry_state(capability_id: str) -> tuple[str, str]:
    """CapabilityRegistry.check 用。アダプタ無しは unavailable。availableにしない。"""
    key = str(capability_id or "").strip()
    if key not in SKILL_DEFINITIONS:
        return "unknown", "unregistered"
    if not has_adapter(key):
        return "unavailable", "adapter_not_connected"
    # 接続設定はPJ単位のため、登録の有無だけでは configured にしない。
    # PJ文脈なしでは未接続として扱う(availableにしない)。
    return "unavailable", "not_configured"


# ----------------------------------------------------------------------------
# 成熟度4区分(別表示)
# ----------------------------------------------------------------------------

def _verification_flags(db: sqlite3.Connection, project_id: str,
                        capability_id: str) -> tuple[bool, bool]:
    try:
        rows = db.execute(
            "SELECT verification_kind, action FROM channel_skill_verifications "
            "WHERE project_id=? AND capability_id=? ORDER BY id",
            (project_id, capability_id)).fetchall()
    except sqlite3.Error as exc:
        raise ChannelSkillReadError(
            "row_unreadable", sqlite_error=_type_name(exc)) from exc
    permission = False
    field = False
    for row in rows:
        try:
            item = dict(row)
        except (TypeError, ValueError):
            continue
        kind = str(item.get("verification_kind") or "")
        action = str(item.get("action") or "verify")
        if kind == "permission":
            permission = (action == "verify")
        elif kind == "field":
            field = (action == "verify")
    return permission, field


def maturity(memory_path: str | Path, project_id: str,
             capability_id: str) -> dict:
    """成熟度4区分。読取失敗は例外。定義・接続だけでは確認済みにしない。"""
    pid = str(project_id or "").strip()
    key = str(capability_id or "").strip()
    if not pid or key not in SKILL_DEFINITIONS:
        raise ValueError("project_id and known capability_id are required")
    defined = True
    if key == "mail.receive":
        # C3: 接続済みは (a)標準IMAPアダプタが登録されている(起動時の
        # register_adapter)、(b)そのPJの接続設定がある、の両方が真のときだけ真。
        # 資格情報の環境変数の存在だけを確認し、値は見ない。
        # メモリ内の辞書では上書きできない(C2の公開Webと同じ作り)。
        # 読取不能は偽(unknown は skill_state 側)。
        try:
            from app import channel_skill_mail as _mail_skill
        except (ImportError, AttributeError, TypeError):
            return {"defined": defined, "connected": False,
                    "permission_verified": permission, "field_verified": field}
        try:
            _mail_current = _mail_skill.get_current_settings(
                memory_path, pid)
        except Exception:
            _mail_current = None
        if not isinstance(_mail_current, dict) or not _mail_current:
            connected = False
        else:
            _prefix = str(_mail_current.get("credential_env_prefix") or "")
            connected = bool(has_adapter(key) and _mail_skill.mail_env_ready(_prefix))
    elif key == "web.search_public":
        # 公開Webの接続済みは承認済みの計画の契約だけから求める。
        # メモリ内の辞書では真にできない。読取不能は偽(unknown は skill_state 側)。
        try:
            permission = web_search_permission(memory_path, pid)
        except (OSError, ValueError, TypeError, AttributeError):
            permission = {"allowed": False, "reason": "unreadable"}
        connected = bool(isinstance(permission, dict)
                         and permission.get("allowed"))
    elif key == "social.observe":
        # C4: 接続済みは (a)その媒体のアダプタが登録されている、
        # (b)そのPJの接続設定がある、の両方が真のときだけ真。
        # メモリ内の辞書では上書きできない(C2・C3と同じ作り)。
        # 読取不能は偽(unknown は skill_state 側)。
        try:
            from app import channel_skill_social as _social_skill
        except (ImportError, AttributeError, TypeError):
            return {"defined": defined, "connected": False,
                    "permission_verified": False, "field_verified": False}
        try:
            _social_current = _social_skill.get_current_settings(
                memory_path, pid)
        except Exception:
            _social_current = None
        connected = bool(
            isinstance(_social_current, dict) and bool(_social_current)
            and has_adapter(key))
    elif key == "mail.send_approved":
        # C5: 接続済みは (a)SMTPアダプタの登録と (b)SMTP設定の存在(値は見ない)の
        # 両方が真のときだけ真。skill_state と同じ判定 (mail_send_connection_view)。
        # メモリ内の辞書では真にできない。読取不能は偽。
        try:
            from app import channel_skill_mail_send as _send_skill
            _send_view = _send_skill.mail_send_connection_view(memory_path, pid)
        except Exception:
            _send_view = None
        connected = bool(isinstance(_send_view, dict)
                         and _send_view.get("readable", True)
                         and _send_view.get("configured"))
    else:
        connected = bool(has_adapter(key) and has_connection(pid, key))
    permission = False
    field = False
    path = item_ledger_db_path(memory_path)
    if path.exists():
        with _connect_ro(memory_path) as db:
            names = _table_names(db)
            if "channel_skill_verifications" in names:
                permission, field = _verification_flags(db, pid, key)
    return {"defined": defined, "connected": connected,
            "permission_verified": permission, "field_verified": field}


def record_verification(memory_path: str | Path, *, project_id: str,
                        capability_id: str, verification_kind: str,
                        actor: str, memo: str,
                        action: str = "verify") -> dict:
    """確認記録を追記する。担当者・根拠メモ・日時が必須。取消は revoke 行。"""
    pid = str(project_id or "").strip()
    key = str(capability_id or "").strip()
    kind = str(verification_kind or "").strip()
    act = str(action or "verify").strip() or "verify"
    who = str(actor or "").strip()
    note = str(memo or "").strip()
    if not pid or key not in SKILL_DEFINITIONS:
        raise ValueError("project_id and known capability_id are required")
    if kind not in VERIFICATION_KINDS:
        raise ValueError("verification_kind must be permission or field")
    if act not in VERIFICATION_ACTIONS:
        raise ValueError("action must be verify or revoke")
    if not who:
        raise ValueError("actor is required")
    if not note:
        raise ValueError("memo is required")
    _reject_secret_text({"actor": who, "memo": note})
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            now = time.time()
            cursor = db.execute(
                """INSERT INTO channel_skill_verifications(
                    project_id, capability_id, verification_kind, action,
                    actor, memo, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (pid, key, kind, act, who[:100], note[:2000], now),
            )
            db.commit()
            saved = db.execute(
                "SELECT * FROM channel_skill_verifications WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    return _verification_view(dict(saved))


def _verification_view(row: dict) -> dict:
    return {
        "id": int(row.get("id") or 0),
        "project_id": str(row.get("project_id") or ""),
        "capability_id": str(row.get("capability_id") or ""),
        "verification_kind": str(row.get("verification_kind") or ""),
        "action": str(row.get("action") or ""),
        "actor": str(row.get("actor") or ""),
        "memo": str(row.get("memo") or ""),
        "created_at": row.get("created_at"),
    }


def list_verifications(memory_path: str | Path, project_id: str,
                       capability_id: str) -> list[dict]:
    pid = str(project_id or "").strip()
    key = str(capability_id or "").strip()
    if not pid or key not in SKILL_DEFINITIONS:
        raise ValueError("project_id and known capability_id are required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return []
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "channel_skill_verifications" not in names:
            return []
        try:
            rows = db.execute(
                "SELECT * FROM channel_skill_verifications "
                "WHERE project_id=? AND capability_id=? ORDER BY id",
                (pid, key)).fetchall()
        except sqlite3.Error as exc:
            raise ChannelSkillReadError(
                "row_unreadable", sqlite_error=_type_name(exc)) from exc
    return [_verification_view(dict(r)) for r in rows]


# ----------------------------------------------------------------------------
# 証拠の共通形式(伏せ・ハッシュ・未信頼)
# ----------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<![0-9A-Za-z])(?:\+81[\s\-]*0?\d{1,4}[\s\-]*\d{1,4}[\s\-]*\d{3,4}"
    r"|0\d{1,3}[\s\-]+\d{1,4}[\s\-]+\d{3,4}"
    r"|0[5789]0\d{7,8})(?![0-9A-Za-z])")


def _reject_secret_text(fields: dict[str, str]) -> None:
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


def make_excerpt(body: str) -> dict:
    """本文から伏せ済み抜粋(最大500字)を作る。秘密の形は抜粋ごと保存しない。"""
    from app.plan_review_loop import contains_secret
    text = str(body or "")
    if not text.strip():
        return {"excerpt": "", "excerpt_redacted": False, "redact_reason": ""}
    try:
        label = contains_secret(text)
    except (AttributeError, TypeError, ValueError):
        label = ""
    if label not in ("", "メールアドレス", "電話番号様", "電話番号様(語付き)"):
        return {"excerpt": "", "excerpt_redacted": True,
                "redact_reason": f"secret suspected: {label}"}
    redacted = _EMAIL_RE.sub("[伏せ]", text)
    redacted = _PHONE_RE.sub("[伏せ]", redacted)
    # 伏せ後に秘密の形が残っていないか再確認する。
    try:
        label2 = contains_secret(redacted)
    except (AttributeError, TypeError, ValueError):
        label2 = ""
    if label2 not in ("", "メールアドレス", "電話番号様", "電話番号様(語付き)"):
        # メール・電話の伏せで消えるはずの検出だけ許す。残る秘密は保存しない。
        if label2 not in ("メールアドレス", "電話番号様", "電話番号様(語付き)"):
            return {"excerpt": "", "excerpt_redacted": True,
                    "redact_reason": f"secret suspected: {label2}"}
        # 検出が伏せ済み表記由来の誤検出なら、そのまま残さず伏せ扱いにする。
        if "[伏せ]" in redacted:
            cleaned = redacted.replace("[伏せ]", " ")
            try:
                label3 = contains_secret(cleaned)
            except (AttributeError, TypeError, ValueError):
                label3 = ""
            if label3 not in ("", "メールアドレス", "電話番号様", "電話番号様(語付き)"):
                return {"excerpt": "", "excerpt_redacted": True,
                        "redact_reason": f"secret suspected: {label3}"}
    return {"excerpt": redacted[:500], "excerpt_redacted": False, "redact_reason": ""}


def validate_evidence(payload: dict) -> dict:
    """証拠の検証。本文そのものは受け取らない(ハッシュと伏せ抜粋だけ)。"""
    if not isinstance(payload, dict):
        raise ValueError("evidence is required")
    provider = str(payload.get("provider") or "").strip()
    account = str(payload.get("account_or_public_id") or "").strip()
    external_id = str(payload.get("external_id") or "").strip()
    url = str(payload.get("url") or "").strip()
    fetched_at = str(payload.get("fetched_at") or "").strip()
    period_start = str(payload.get("period_start") or "").strip()
    period_end = str(payload.get("period_end") or "").strip()
    body_sha256 = str(payload.get("body_sha256") or "").strip().lower()
    attachment = payload.get("attachment_sha256s")
    project_id = str(payload.get("project_id") or "").strip()
    plan_item_id = str(payload.get("plan_item_id") or "").strip()
    purpose = str(payload.get("purpose") or "").strip()
    try:
        retention = int(payload.get("retention_days"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("retention_days is required") from None
    if not provider:
        raise ValueError("provider is required")
    if not account:
        raise ValueError("account_or_public_id is required")
    if not external_id and not url:
        raise ValueError("external_id or url is required")
    if not fetched_at or not period_start or not period_end:
        raise ValueError("fetched_at and period are required")
    if not re.fullmatch(r"[0-9a-f]{64}", body_sha256 or ""):
        raise ValueError("body_sha256 is required")
    if not isinstance(attachment, list):
        raise ValueError("attachment_sha256s is required")
    for item in attachment:
        if not re.fullmatch(r"[0-9a-f]{64}", str(item or "").strip().lower()):
            raise ValueError("attachment_sha256s must be sha256 hex")
    if not project_id:
        raise ValueError("project_id is required")
    if not plan_item_id:
        raise ValueError("plan_item_id is required")
    if not purpose:
        raise ValueError("purpose is required")
    if retention < 0:
        raise ValueError("retention_days is out of range")
    if "body" in payload:
        raise ValueError("body must not be stored")
    excerpt = str(payload.get("excerpt") or "")
    if len(excerpt) > 500:
        raise ValueError("excerpt must be 500 chars or fewer")
    # C5: Message-ID形式(<...@...>)の external_id は秘密検査の対象外にする。
    # make_msgid のID部分は数字ドット列で、マイナンバー検出に誤検出される。
    # Message-IDは送信記録のIDであり秘密ではない。既存の秘密検出の期待値は変えない。
    _secret_external_id = external_id
    if (len(external_id) >= 3 and external_id.startswith("<")
            and external_id.endswith(">") and "@" in external_id):
        _secret_external_id = ""
    _reject_secret_text({
        "provider": provider, "account_or_public_id": account,
        "external_id": _secret_external_id, "url": url, "purpose": purpose,
        "excerpt": excerpt,
    })
    return {
        "provider": provider,
        "account_or_public_id": account,
        "external_id": external_id,
        "url": url,
        "fetched_at": fetched_at,
        "period_start": period_start,
        "period_end": period_end,
        "body_sha256": body_sha256,
        "attachment_sha256s": [str(x).strip().lower() for x in attachment],
        "project_id": project_id,
        "plan_item_id": plan_item_id,
        "approved_binding_id": str(payload.get("approved_binding_id") or ""),
        "purpose": purpose,
        "retention_days": retention,
        "excerpt": excerpt,
        "excerpt_redacted": bool(payload.get("excerpt_redacted")),
        "redact_reason": str(payload.get("redact_reason") or "")[:500],
        "untrusted": True,
    }


def build_evidence(*, provider: str, account_or_public_id: str,
                   external_id: str = "", url: str = "",
                   fetched_at: str, period_start: str, period_end: str,
                   body: str, attachment_bodies: list[bytes] | None = None,
                   project_id: str, plan_item_id: str,
                   approved_binding_id: str = "", purpose: str,
                   retention_days: int) -> dict:
    """本文からハッシュと伏せ抜粋だけを作る。命令文でも実行せず保存だけする。"""
    text = str(body or "")
    body_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    attachment_digests: list[str] = []
    for blob in attachment_bodies or []:
        data = blob if isinstance(blob, (bytes, bytearray)) else str(blob).encode("utf-8")
        attachment_digests.append(hashlib.sha256(bytes(data)).hexdigest())
    excerpt_info = make_excerpt(text)
    return validate_evidence({
        "provider": provider, "account_or_public_id": account_or_public_id,
        "external_id": external_id, "url": url,
        "fetched_at": fetched_at, "period_start": period_start,
        "period_end": period_end, "body_sha256": body_digest,
        "attachment_sha256s": attachment_digests,
        "project_id": project_id, "plan_item_id": plan_item_id,
        "approved_binding_id": approved_binding_id, "purpose": purpose,
        "retention_days": retention_days,
        "excerpt": excerpt_info["excerpt"],
        "excerpt_redacted": excerpt_info["excerpt_redacted"],
        "redact_reason": excerpt_info["redact_reason"],
    })


def record_evidence(memory_path: str | Path, evidence: dict,
                    run_id: str = "", capability_id: str = "") -> dict:
    """証拠を追記する。本文は保存しない。秘密は入れない。"""
    viewed = validate_evidence(evidence)
    pid = viewed["project_id"]
    key = str(capability_id or viewed.get("capability_id") or "").strip()
    if not key:
        # 証拠に対応する能力が不明なら保存しない(他PJ混入防止のため明示を要求)。
        raise ValueError("capability_id is required")
    if key not in SKILL_DEFINITIONS:
        raise ValueError(f"unknown capability_id: {key}")
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            now = time.time()
            cursor = db.execute(
                """INSERT INTO channel_skill_evidence(
                    project_id, capability_id, run_id, provider,
                    account_or_public_id, external_id, url, fetched_at,
                    period_start, period_end, body_sha256,
                    attachment_sha256s_json, plan_item_id,
                    approved_binding_id, purpose, retention_days, excerpt,
                    excerpt_redacted, redact_reason, untrusted, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, key, str(run_id or ""),
                 viewed["provider"], viewed["account_or_public_id"],
                 viewed["external_id"], viewed["url"],
                 viewed["fetched_at"], viewed["period_start"],
                 viewed["period_end"], viewed["body_sha256"],
                 json.dumps(viewed["attachment_sha256s"], ensure_ascii=False),
                 viewed["plan_item_id"], viewed["approved_binding_id"],
                 viewed["purpose"], int(viewed["retention_days"]),
                 viewed["excerpt"], 1 if viewed["excerpt_redacted"] else 0,
                 viewed["redact_reason"], 1, now),
            )
            db.commit()
            saved = db.execute(
                "SELECT * FROM channel_skill_evidence WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    return _evidence_view(dict(saved))


def _evidence_view(row: dict) -> dict:
    try:
        attachments = json.loads(str(row.get("attachment_sha256s_json") or "[]"))
    except (TypeError, ValueError):
        attachments = []
    if not isinstance(attachments, list):
        attachments = []
    return {
        "id": int(row.get("id") or 0),
        "project_id": str(row.get("project_id") or ""),
        "capability_id": str(row.get("capability_id") or ""),
        "run_id": str(row.get("run_id") or ""),
        "provider": str(row.get("provider") or ""),
        "account_or_public_id": str(row.get("account_or_public_id") or ""),
        "external_id": str(row.get("external_id") or ""),
        "url": str(row.get("url") or ""),
        "fetched_at": str(row.get("fetched_at") or ""),
        "period_start": str(row.get("period_start") or ""),
        "period_end": str(row.get("period_end") or ""),
        "body_sha256": str(row.get("body_sha256") or ""),
        "attachment_sha256s": [str(x) for x in attachments],
        "plan_item_id": str(row.get("plan_item_id") or ""),
        "approved_binding_id": str(row.get("approved_binding_id") or ""),
        "purpose": str(row.get("purpose") or ""),
        "retention_days": int(row.get("retention_days") or 0),
        "excerpt": str(row.get("excerpt") or ""),
        "excerpt_redacted": bool(row.get("excerpt_redacted")),
        "redact_reason": str(row.get("redact_reason") or ""),
        "untrusted": True,
        "created_at": row.get("created_at"),
    }


def list_evidence(memory_path: str | Path, project_id: str,
                  capability_id: str = "", run_id: str = "",
                  plan_item_id: str = "") -> list[dict]:
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return []
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "channel_skill_evidence" not in names:
            return []
        query = "SELECT * FROM channel_skill_evidence WHERE project_id=?"
        args: list[Any] = [pid]
        if str(capability_id or "").strip():
            query += " AND capability_id=?"
            args.append(str(capability_id).strip())
        if str(run_id or "").strip():
            query += " AND run_id=?"
            args.append(str(run_id).strip())
        if str(plan_item_id or "").strip():
            query += " AND plan_item_id=?"
            args.append(str(plan_item_id).strip())
        query += " ORDER BY id"
        try:
            rows = db.execute(query, tuple(args)).fetchall()
        except sqlite3.Error as exc:
            raise ChannelSkillReadError(
                "row_unreadable", sqlite_error=_type_name(exc)) from exc
    return [_evidence_view(dict(r)) for r in rows]


def list_evidence_by_plan_item(memory_path: str | Path, project_id: str,
                               plan_item_id: str) -> list[dict]:
    """計画項目に結び付いた全スキルの証拠(他PJの行を返さない)。"""
    pid = str(project_id or "").strip()
    item = str(plan_item_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    if not item:
        raise ValueError("plan_item_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return []
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "channel_skill_evidence" not in names:
            return []
        try:
            rows = db.execute(
                "SELECT * FROM channel_skill_evidence WHERE project_id=? "
                "AND plan_item_id=? ORDER BY id",
                (pid, item)).fetchall()
        except sqlite3.Error as exc:
            raise ChannelSkillReadError(
                "row_unreadable", sqlite_error=_type_name(exc)) from exc
    return [_evidence_view(dict(r)) for r in rows]


# ----------------------------------------------------------------------------
# C2: web.search_public の検索語検証・計画項目ID確認・実行前検査
# ----------------------------------------------------------------------------

MAX_SEARCH_QUERY_CHARS = 200

_PHONE_TEXT_RE = re.compile(
    r"(?:\+?81[\s\-]*\(?0?\d{1,4}\)?[\s\-]*\d{1,4}[\s\-]*\d{3,4}"
    r"|0\d{1,3}[\s\-]*\d{1,4}[\s\-]*\d{3,4}"
    r"|0[5789]0[\s\-]*\d{4}[\s\-]*\d{4})")


def validate_search_query(query: str,
                          *,
                          project_sources: list[str] | None = None) -> dict:
    """公開検索語の純粋な検証。実行・書込・外部呼び出しなし。

    次のいずれかを含む検索語は拒否(ValueError、理由つき):
    (a)contains_secret で検出される秘密の形(メール・電話の形は(b)で扱い除外)、
    (b)メールアドレス・電話番号の形、(c)200字超過、
    (d)PJ原本・登録コンテキストの文章の20文字以上の長い引用。
    拒否の理由は返すが、検索語そのものは台帳に保存しない。
    """
    text = str(query or "")
    if not text.strip():
        raise ValueError("query is required")
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) > MAX_SEARCH_QUERY_CHARS:
        raise ValueError(
            f"query is too long: {len(normalized)} chars "
            f"(limit {MAX_SEARCH_QUERY_CHARS})")
    from app.plan_review_loop import contains_secret
    try:
        label = contains_secret(normalized)
    except (AttributeError, TypeError, ValueError):
        label = ""
    if label and label not in ("", "メールアドレス", "電話番号様", "電話番号様(語付き)"):
        raise ValueError(f"secret suspected: {label}")
    if _EMAIL_RE.search(normalized):
        raise ValueError("email address must not be in query")
    if _PHONE_TEXT_RE.search(normalized):
        raise ValueError("phone number must not be in query")
    for source in project_sources or []:
        blob = re.sub(r"\s+", " ", str(source or "")).strip()
        if len(blob) < 20:
            continue
        # 原本テキストと20文字以上連続で一致する部分があれば拒否。
        for start in range(0, len(normalized) - 19):
            fragment = normalized[start:start + 20]
            if fragment and fragment in blob:
                raise ValueError("query quotes project source text")
    return {"query": normalized, "chars": len(normalized)}


def project_plan_item_ids(memory_path: str | Path, project_id: str) -> set[str]:
    """そのPJの最新の計画の項目ID(要求契約・項目別台帳)。

    実在する項目IDの集合。読めない・無いときは空集合(『確認できない』)。
    例外は送出しない(呼出側が『確認できない』として拒否する)。
    """
    pid = str(project_id or "").strip()
    if not pid:
        return set()
    found: set[str] = set()
    # (a)項目別台帳の最新の台帳の item_id。
    try:
        from app.requirement_item_ledger import latest_ledger
        saved = latest_ledger(memory_path, pid)
        if isinstance(saved, dict) and isinstance(saved.get("rows"), list):
            for row in saved.get("rows") or []:
                if not isinstance(row, dict):
                    continue
                text = str(row.get("item_id") or "").strip()
                if text:
                    found.add(text)
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        pass
    if found:
        return set(found)
    # (b)計画の requirement_matrix(構造化計画の契約)。
    try:
        from app.memory.short_term import ShortTermMemory
        resolved = Path(str(memory_path or ""))
        db_path = resolved if resolved.suffix else resolved.parent / "conversations.db"
        if db_path.exists():
            memory = ShortTermMemory(db_path)
            try:
                mission = memory.get_mission(pid)
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                mission = {}
            tasks = (mission or {}).get("tasks") if isinstance(mission, dict) else []
            for task in tasks or []:
                if not isinstance(task, dict):
                    continue
                try:
                    from app.structured_planning import contract_of
                    contract = contract_of(task)
                except (ImportError, AttributeError, ValueError, TypeError, KeyError):
                    continue
                if not isinstance(contract, dict):
                    continue
                for row in contract.get("requirement_matrix") or []:
                    if not isinstance(row, dict):
                        continue
                    text = str(row.get("item_id") or "").strip()
                    if text:
                        found.add(text)
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        return set(found)
    return set(found)


def validate_plan_item_id(memory_path: str | Path, project_id: str,
                          plan_item_id: str) -> str:
    """計画項目IDの実在確認。実在しない・読めないときは ValueError。"""
    pid = str(project_id or "").strip()
    item = str(plan_item_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    if not item:
        raise ValueError("plan_item_id is required")
    ids = project_plan_item_ids(memory_path, pid)
    if not ids:
        raise ValueError("plan items are unreadable: cannot confirm item")
    if item not in ids:
        raise ValueError(f"unknown plan_item_id: {item}")
    return item


def project_registered_context_texts(memory_path: str | Path,
                                     project_id: str) -> list[str]:
    """そのPJの登録コンテキスト・原本の文章(検索語の引用検査用)。

    目標・達成条件・登録資料の本文だけ。資格情報・原本バイト列は含めない。
    読取不能は空列。例外を送出しない。
    """
    pid = str(project_id or "").strip()
    if not pid:
        return []
    texts: list[str] = []
    try:
        from app.memory.short_term import ShortTermMemory
        resolved = Path(str(memory_path or ""))
        db_path = resolved if resolved.suffix else resolved.parent / "conversations.db"
        if not db_path.exists():
            return []
        memory = ShortTermMemory(db_path)
        try:
            mission = memory.get_mission(pid)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            mission = {}
        for key in ("goal", "success_criteria", "constraints_text"):
            value = (mission or {}).get(key) if isinstance(mission, dict) else ""
            if isinstance(value, str) and len(value.strip()) >= 20:
                texts.append(value)
        try:
            files = memory.list_context_files(pid, include_content=True)
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            files = []
        for item in files or []:
            if not isinstance(item, dict):
                continue
            if item.get("source") == "memo":
                continue
            content = str(item.get("content") or "")
            if len(content) >= 20:
                texts.append(content[:20000])
    except (OSError, ValueError, TypeError, AttributeError, ImportError):
        return list(texts)
    return list(texts)

def _validate_run_status(status: str, evidence_count: int,
                         completed_ok: bool) -> None:
    if status not in RUN_STATUSES:
        raise ValueError("status must be running/succeeded/no_results/failed/unknown")
    try:
        count = int(evidence_count)
    except (TypeError, ValueError):
        raise ValueError("evidence_count is required") from None
    if count < 0:
        raise ValueError("evidence_count is out of range")
    if status == "succeeded" and count < 1:
        raise ValueError("succeeded requires at least one evidence")
    if status == "no_results" and not completed_ok:
        raise ValueError("no_results requires completed_ok")
    if status in ("running", "failed", "unknown") and False:
        raise ValueError("unreachable")


def record_run(memory_path: str | Path, *, project_id: str,
               capability_id: str, status: str, purpose: str = "",
               operating_mode: str = "", adapter: str = "",
               params_summary: dict | None = None,
               started_at: float | None = None,
               finished_at: float | None = None,
               error_kind: str = "", evidence_count: int = 0,
               completed_ok: bool = False,
               run_id: str = "") -> dict:
    """実行を追記する。succeeded/no_results の条件を満たさなければ ValueError。"""
    pid = str(project_id or "").strip()
    key = str(capability_id or "").strip()
    if not pid or key not in SKILL_DEFINITIONS:
        raise ValueError("project_id and known capability_id are required")
    state = str(status or "").strip()
    _validate_run_status(state, int(evidence_count or 0), bool(completed_ok))
    mode = str(operating_mode or "").strip()
    if mode and mode not in OPERATING_MODES:
        raise ValueError("operating_mode must be demo_test or production")
    summary = dict(params_summary or {})
    _reject_secret_text({f"params.{k}": str(v) for k, v in summary.items()})
    try:
        summary_text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raise ValueError("params_summary is not serializable") from None
    rid = str(run_id or "").strip() or f"csr_{hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]}"
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            now = time.time()
            cursor = db.execute(
                """INSERT INTO channel_skill_runs(
                    run_id, project_id, capability_id, status, purpose,
                    operating_mode, adapter, params_summary, started_at,
                    finished_at, error_kind, evidence_count, completed_ok)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, pid, key, state, str(purpose or "")[:2000],
                 mode, str(adapter or "")[:200],
                 summary_text[:8000],
                 float(started_at) if started_at is not None else now,
                 finished_at, str(error_kind or "")[:500],
                 int(evidence_count or 0), 1 if completed_ok else 0),
            )
            db.commit()
            saved = db.execute(
                "SELECT * FROM channel_skill_runs WHERE id=?",
                (int(cursor.lastrowid),)).fetchone()
    return _run_view(dict(saved))


def _run_view(row: dict) -> dict:
    try:
        summary = json.loads(str(row.get("params_summary") or "{}"))
    except (TypeError, ValueError):
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    return {
        "id": int(row.get("id") or 0),
        "run_id": str(row.get("run_id") or ""),
        "project_id": str(row.get("project_id") or ""),
        "capability_id": str(row.get("capability_id") or ""),
        "status": str(row.get("status") or ""),
        "purpose": str(row.get("purpose") or ""),
        "operating_mode": str(row.get("operating_mode") or ""),
        "adapter": str(row.get("adapter") or ""),
        "params_summary": summary,
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "error_kind": str(row.get("error_kind") or ""),
        "evidence_count": int(row.get("evidence_count") or 0),
        "completed_ok": bool(row.get("completed_ok")),
    }


def list_runs(memory_path: str | Path, project_id: str,
              capability_id: str = "", limit: int = 50) -> list[dict]:
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    try:
        capped = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        capped = 50
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return []
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "channel_skill_runs" not in names:
            return []
        query = "SELECT * FROM channel_skill_runs WHERE project_id=?"
        args: list[Any] = [pid]
        if str(capability_id or "").strip():
            if str(capability_id).strip() not in SKILL_DEFINITIONS:
                raise ValueError("unknown capability_id")
            query += " AND capability_id=?"
            args.append(str(capability_id).strip())
        query += " ORDER BY id DESC LIMIT ?"
        args.append(capped)
        try:
            rows = db.execute(query, tuple(args)).fetchall()
        except sqlite3.Error as exc:
            raise ChannelSkillReadError(
                "row_unreadable", sqlite_error=_type_name(exc)) from exc
    return [_run_view(dict(r)) for r in rows]


def get_latest_run(memory_path: str | Path, project_id: str,
                   capability_id: str) -> dict | None:
    pid = str(project_id or "").strip()
    key = str(capability_id or "").strip()
    if not pid or key not in SKILL_DEFINITIONS:
        raise ValueError("project_id and known capability_id are required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return None
    with _connect_ro(memory_path) as db:
        names = _table_names(db)
        if "channel_skill_runs" not in names:
            return None
        _status, _readable, saved = _latest_run_status(db, pid, key)
        if saved is None:
            return None
        return _run_view(saved)


# ----------------------------------------------------------------------------
# 一覧表示用(定義・状態・成熟度・直近の要約)
# ----------------------------------------------------------------------------

def describe_skill(memory_path: str | Path, project_id: str,
                   capability_id: str) -> dict:
    """定義・状態・成熟度4区分・直近の実行要約。読取失敗は例外。"""
    definition = get_skill_definition(capability_id)
    state = skill_state(memory_path, project_id, capability_id)
    maturity_view = maturity(memory_path, project_id, capability_id)
    latest: dict | None = None
    try:
        latest = get_latest_run(memory_path, project_id, capability_id)
    except ChannelSkillReadError:
        raise
    summary: dict | None = None
    if isinstance(latest, dict):
        evidence_total = 0
        try:
            evidence_total = len(list_evidence(
                memory_path, str(project_id or ""),
                str(capability_id or "")))
        except ChannelSkillReadError:
            raise
        summary = {
            "run_id": str(latest.get("run_id") or ""),
            "status": str(latest.get("status") or ""),
            "finished_at": latest.get("finished_at"),
            "error_kind": str(latest.get("error_kind") or ""),
            "evidence_count": int(latest.get("evidence_count") or 0),
            "evidence_total": int(evidence_total),
        }
    return {"definition": definition, "state": state,
            "maturity": maturity_view, "latest": summary}


def list_skills(memory_path: str | Path, project_id: str) -> list[dict]:
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    return [describe_skill(memory_path, pid, key)
            for key in sorted(SKILL_DEFINITIONS)]


# ----------------------------------------------------------------------------
# ライフサイクル(世代切替では消さない)
# ----------------------------------------------------------------------------

def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    """PJ初期化(世代切替)では実行台帳を消さない(B3の運用区分と同じ扱い)。"""
    return {"project_id": str(project_id or ""), "reset": False,
            "note": "チャネルスキルの実行台帳は世代切替で変えません"}
