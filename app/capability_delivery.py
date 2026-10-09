"""Stage 2-D: 開発結果の取込みと再判定 (一般機構)。

2-A (事前照合・提案)・2-B (指示パッケージ) の後段。一般機構であり、特定の案件・
件数・ID に依存しない。Jenkinsへの接続・送信は一切しない (人がパッケージを
渡したことの記録と、返却物の受領・検証だけを扱う。ジョブ名・URL・認証情報を
推測・保持・送信しない。外部通信なし)。

原則 (最重要): Jenkins の成功表示・ビルドPASS・テスト成功だけで能力を有効化
しない。verified は、元の案件・同じ計画署名 (または同じ入力) で事前照合
(capability_preflight) を再生し、元の不足 (gap_code / 能力) が消え、必要な
証拠が揃ったときだけ付く。能力レジストリ (app/capability_registry.py) は
コード所有であり、本機能は登録簿を書き換えない (納品された実装を人が本番へ
反映した後の、現行コードの状態を再判定するだけ)。別案件・別入力の成功で
代理しない。

- record_submitted(): export_ready -> submitted (人が渡したことの記録)。
  実際には何も送信しない。再記録は冪等。
- record_delivery(): 返却物の受領・検査。受理したら delivered
  (「実装が納品された」ことのみ。能力は有効になっていない)。納品記録は
  新テーブル gap_deliveries に追記のみで保存する。
- verify_proposal(): delivered の提案について元の計画署名の入力で事前照合を
  再生する。不足が消えていれば verified、残っていれば delivered のまま残件を
  返す。verified への遷移はこの関数からの専用経路でのみ行い、2-A の
  set_proposal_status() の「verified 遷移を拒否」する保護は弱めない
  (本モジュールは set_proposal_status() を経由せず直接SQLで遷移させる。
  外部から任意に verified を設定できる経路は作らない)。

確認者名の扱い: actor は作業記録用の表示名であり、本人認証ではない
(既存の確認者名の扱いに準じる。認証は未実装)。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

#: 納品内容の保持上限 (全文保存はしない。ハッシュと短い参照だけ残す)。
SHORT_LOG_CHARS = 500
MAX_TEXT_CHARS = 200000
MAX_FILES = 50
MAX_FILE_CHARS = 20000

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_hash(value: object) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _full_hash_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_short(text: object, limit: int = SHORT_LOG_CHARS) -> str:
    """保存・応答用の短い引用。秘密混入の疑いは種別ラベルのみ残し値を落とす。"""
    blob = str(text or "")
    try:
        from app.plan_review_loop import contains_secret

        label = contains_secret(blob)
    except Exception:
        label = ""
    if label:
        return f"[秘密混入の疑いのため保持しません:{label}]"
    return " ".join(blob.split())[: max(1, int(limit))]


def _sanitize_json(value: object, depth: int = 0) -> object:
    """テスト結果等の保存用に正規化する。長い文字列は切り詰め、秘密値は落とす。"""
    if depth > 4:
        return "[深い階層のため省略]"
    if isinstance(value, dict):
        out: dict = {}
        for key in sorted(str(k) for k in value.keys())[:50]:
            out[str(key)[:80]] = _sanitize_json(value[key], depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize_json(v, depth + 1) for v in list(value)[:50]]
    if isinstance(value, str):
        return _safe_short(value, 140)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _safe_short(str(value), 140)


def _lock_for(db_path: object, proposal_id: str) -> threading.Lock:
    key = f"{db_path}|{proposal_id}"
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _ensure_delivery_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS gap_deliveries(
        id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        base_commit TEXT NOT NULL DEFAULT '',
        result_commit TEXT NOT NULL DEFAULT '',
        diff_ref TEXT NOT NULL DEFAULT '',
        content_hash TEXT NOT NULL DEFAULT '',
        artifact_sha256 TEXT NOT NULL DEFAULT '',
        test_summary TEXT NOT NULL DEFAULT '{}',
        capability_ids TEXT NOT NULL DEFAULT '[]',
        failure_log TEXT NOT NULL DEFAULT '',
        verdict TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_gap_deliveries_proposal
        ON gap_deliveries(proposal_id, created_at)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_gap_deliveries_project
        ON gap_deliveries(project_id)""")
    db.execute("""CREATE TABLE IF NOT EXISTS gap_verifications(
        id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        plan_signature TEXT NOT NULL DEFAULT '',
        replay_hash TEXT NOT NULL DEFAULT '',
        result TEXT NOT NULL DEFAULT '',
        remaining TEXT NOT NULL DEFAULT '[]',
        checked_at TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_gap_verifications_proposal
        ON gap_verifications(proposal_id, checked_at)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_gap_verifications_project
        ON gap_verifications(project_id)""")


@contextmanager
def _connect(manager_or_path):
    from app import capability_gap as _gap

    path = _gap.gap_db_path(manager_or_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            _gap._ensure_tables(db)
            _ensure_delivery_tables(db)
        yield db
    finally:
        db.close()


def _read_connection(manager_or_path):
    from app import capability_gap as _gap

    path = _gap.gap_db_path(manager_or_path)
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def _decode_delivery(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in ("test_summary",):
        try:
            item[key] = json.loads(item.get(key) or "{}")
        except (ValueError, TypeError):
            item[key] = {}
    for key in ("capability_ids", "remaining"):
        if key in item:
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except (ValueError, TypeError):
                item[key] = []
    return item


def list_deliveries(manager_or_path, proposal_id: str) -> list[dict]:
    """納品履歴 (追記のみ。旧版は残す)。"""
    db = _read_connection(manager_or_path)
    if db is None:
        return []
    try:
        with db:
            try:
                rows = db.execute(
                    "SELECT * FROM gap_deliveries WHERE proposal_id=? "
                    "ORDER BY created_at",
                    (str(proposal_id),)).fetchall()
            except sqlite3.OperationalError:
                return []
    finally:
        db.close()
    return [_decode_delivery(r) for r in rows]


def list_verifications(manager_or_path, proposal_id: str) -> list[dict]:
    """再判定の記録。同じ入力・同じ結果の再呼出しでは増やさない (冪等)。"""
    db = _read_connection(manager_or_path)
    if db is None:
        return []
    try:
        with db:
            try:
                rows = db.execute(
                    "SELECT * FROM gap_verifications WHERE proposal_id=? "
                    "ORDER BY checked_at",
                    (str(proposal_id),)).fetchall()
            except sqlite3.OperationalError:
                return []
    finally:
        db.close()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["remaining"] = json.loads(item.get("remaining") or "[]")
        except (ValueError, TypeError):
            item["remaining"] = []
        out.append(item)
    return out


# ----------------------------------------------------------------------------
# submitted (人の記録。実際には何も送信しない)
# ----------------------------------------------------------------------------

def record_submitted(manager, project_id: str, proposal_id: str, *,
                     actor: str, memo: str = "") -> dict:
    """export_ready -> submitted。人がパッケージをJenkinsへ渡したことの記録。

    実際には何も送信しない (接続・送信コードは持たない)。export_ready からのみ。
    再記録は冪等 (状態を進めず既存を返す)。
    """
    from app import capability_gap as _gap

    pid = str(project_id or "")
    target = str(proposal_id or "")
    actor_name = str(actor or "").strip()
    if not actor_name:
        raise ValueError("actor is required")
    if not pid:
        raise ValueError("project_id is required")
    proposal = _gap.get_proposal(manager, target)
    if proposal is None or str(proposal.get("project_id") or "") != pid:
        raise ValueError("提案が見つかりません")
    status = str(proposal.get("status") or "")
    if status == "submitted":
        return {
            "ok": True,
            "idempotent": True,
            "proposal_id": target,
            "project_id": pid,
            "status": "submitted",
            "content_hash": str(proposal.get("content_hash") or ""),
        }
    if status != "export_ready":
        raise ValueError(f"状態 {status} では送付記録できません")
    reason = _safe_short(memo or "パッケージをJenkinsへ渡したことの記録", 500)
    reason = f"{reason} (実際には何も送信していません)"
    updated = _gap.set_proposal_status(manager, pid, target, "submitted",
                                       actor=actor_name, reason=reason)
    return {
        "ok": True,
        "idempotent": False,
        "proposal_id": target,
        "project_id": pid,
        "status": str(updated.get("status") or ""),
        "content_hash": str(updated.get("content_hash") or ""),
    }


# ----------------------------------------------------------------------------
# 開発結果の取込み
# ----------------------------------------------------------------------------

def _canonical_delivery_content(diff_text: object = "",
                                artifact_text: object = "",
                                files: object = None) -> bytes:
    """申告ハッシュと照合する正規形。提出された差分/ファイル内容だけから作る。"""
    norm_files: dict[str, str] = {}
    if isinstance(files, dict):
        for key in sorted(str(k) for k in files.keys())[:MAX_FILES]:
            norm_files[str(key)[:120]] = str(files[key])[:MAX_FILE_CHARS]
    material = {
        "diff_text": str(diff_text or "")[:MAX_TEXT_CHARS],
        "artifact_text": str(artifact_text or "")[:MAX_TEXT_CHARS],
        "files": norm_files,
    }
    return (json.dumps(material, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _evaluate_tests(test_results: object) -> tuple[str, str]:
    """テスト結果の検査。成功なら delivered、失敗・欠落・0件なら理由付きで返す。

    戻り値: (verdict, reason_ja)。verdict は delivered / rejected /
    needs_evidence のいずれか。
    """
    if not isinstance(test_results, dict) or not test_results:
        return ("needs_evidence", "テスト結果がありません。証拠をそろえてください")
    tests = test_results.get("tests")
    total_raw = test_results.get("total")
    failed_raw = test_results.get("failed", 0)
    passed_flag = test_results.get("passed")
    passed_count_raw = test_results.get("passed_count")
    try:
        total_n = int(total_raw) if total_raw is not None else 0
    except (TypeError, ValueError):
        total_n = 0
    try:
        failed_n = int(failed_raw) if failed_raw is not None else 0
    except (TypeError, ValueError):
        failed_n = 0
    bad = 0
    if isinstance(tests, list) and tests:
        total_n = max(total_n, len(tests))
        for item in tests:
            if isinstance(item, dict) and item.get("passed") is False:
                bad += 1
    failed_n += bad
    if passed_flag is False or failed_n > 0:
        return ("rejected", "テスト結果が失敗を示しています。修正のうえ再納品してください")
    if passed_count_raw is not None:
        try:
            if int(passed_count_raw) < total_n:
                return ("rejected", "テスト結果が失敗を示しています。修正のうえ再納品してください")
        except (TypeError, ValueError):
            return ("needs_evidence", "テスト結果が判読できません。証拠をそろえてください")
    if total_n <= 0:
        return ("needs_evidence", "テスト結果の件数が0です。証拠をそろえてください")
    return ("delivered", "")


def _scan_secret(*blobs: object) -> str:
    """秘密・個人情報らしい文字列の検査。検出時は種別ラベルのみ返す。"""
    from app.plan_review_loop import contains_secret

    for blob in blobs:
        label = contains_secret(str(blob or ""))
        if label:
            return str(label)
    return ""


def _expected_base_ref(manager, project_id: str, proposal_id: str) -> str:
    """照合すべき基準版。エクスポート時の base_ref があればそれ、無ければ現行。"""
    from app import capability_export as _export

    try:
        exported = _export.get_export(manager, proposal_id)
    except Exception:
        exported = None
    if exported is not None and str(exported.get("base_ref") or ""):
        return str(exported.get("base_ref") or "")
    try:
        return str(_export.get_base_ref() or "unknown")
    except Exception:
        return "unknown"


def record_delivery(manager, project_id: str, proposal_id: str,
                    delivery: dict, *, actor: str) -> dict:
    """開発結果の取込み。submitted (または export_ready) の提案にだけ受け付ける。

    検査して拒否または stale / rejected / needs_evidence にする:
    - proposal_id の不一致・存在しない提案は ValueError。
    - 同一の納品 (同一ハッシュ) の重複投入は冪等 (二重に状態を進めない)。
    - 別内容の再納品は履歴として追記する (旧版は残す)。
    - 基準版が古い (納品の基準コミットがエクスポート時の base_ref と異なる)
      場合は stale とし、日本語で理由を返す。
    - 成果物ハッシュの不一致は拒否 (rejected)。ハッシュが計算できない納品
      (参照のみ) は delivered にせず needs_evidence。
    - テスト結果が失敗・欠落・件数0 の場合は delivered にせず rejected か
      needs_evidence (理由付き)。
    - 秘密・個人情報らしい文字列は保存前に拒否する (値は応答・記録に出さない)。
    - 受理したら delivered (「実装が納品された」ことのみ。能力は有効に
      なっていない)。
    """
    from app import capability_gap as _gap

    pid = str(project_id or "")
    target = str(proposal_id or "")
    actor_name = str(actor or "").strip()
    if not actor_name:
        raise ValueError("actor is required")
    if not isinstance(delivery, dict):
        raise ValueError("delivery is required")
    if not pid:
        raise ValueError("project_id is required")
    if delivery.get("proposal_id") not in (None, "", target):
        raise ValueError("proposal_id が一致しません")
    proposal = _gap.get_proposal(manager, target)
    if proposal is None or str(proposal.get("project_id") or "") != pid:
        raise ValueError("提案が見つかりません")
    status = str(proposal.get("status") or "")
    if status not in ("export_ready", "submitted", "delivered"):
        raise ValueError(f"状態 {status} への納品は受け付けません")

    base_commit = str(delivery.get("base_commit") or "").strip()
    result_commit = str(delivery.get("result_commit") or "").strip()
    diff_ref = str(delivery.get("diff_ref") or "")
    diff_text = str(delivery.get("diff_text") or "")
    artifact_text = str(delivery.get("artifact_text") or "")
    files = delivery.get("files")
    declared = str(delivery.get("artifact_sha256") or "").strip().lower()
    test_results = delivery.get("test_results")
    capability_ids = delivery.get("capability_ids")
    failure_logs = str(delivery.get("failure_logs") or "")
    if not isinstance(capability_ids, list):
        capability_ids = [capability_ids] if capability_ids else []
    capability_ids = [str(x)[:80] for x in capability_ids[:50] if str(x)]

    # 秘密検査 (保存・状態変更の前に行い、値は残さない)。
    secret_label = _scan_secret(failure_logs, diff_text, artifact_text, diff_ref,
                                *(str(files[k]) for k in files)
                                if isinstance(files, dict) else ())
    if secret_label:
        return {
            "ok": False,
            "proposal_id": target,
            "project_id": pid,
            "status": status,
            "verdict": "rejected",
            "reason": f"秘密混入の疑いのため納品を拒否しました(種別: {secret_label})",
        }

    # 同一ハッシュの重複投入は冪等 (二重に状態を進めない)。
    if declared:
        for row in list_deliveries(manager, target):
            if str(row.get("artifact_sha256") or "").strip().lower() == declared:
                return {
                    "ok": True,
                    "idempotent": True,
                    "proposal_id": target,
                    "project_id": pid,
                    "status": status,
                    "verdict": str(row.get("verdict") or ""),
                    "delivery_id": str(row.get("id") or ""),
                    "reason": str(row.get("reason") or ""),
                }

    # 基準版の照合 (古ければ stale)。
    expected = _expected_base_ref(manager, pid, target)
    if not base_commit:
        verdict: str = "needs_evidence"
        reason = "基準コミットがありません。再同期のうえ再評価が必要です"
    elif expected not in ("", "unknown") and base_commit != expected:
        verdict = "stale"
        reason = "納品の基準版がエクスポート時と異なります。再同期のうえ再評価が必要です"
    else:
        verdict = ""
        reason = ""

    # 成果物ハッシュの照合。
    content_bytes = _canonical_delivery_content(diff_text, artifact_text, files)
    has_content = bool(diff_text.strip() or artifact_text.strip()
                       or (isinstance(files, dict) and files))
    computed = _full_hash_hex(content_bytes) if has_content else ""
    if verdict == "":
        if not has_content:
            verdict = "needs_evidence"
            reason = "成果物ハッシュを検証できません(参照のみ・内容なし)。証拠をそろえてください"
        elif not declared:
            verdict = "needs_evidence"
            reason = "成果物ハッシュがありません。証拠をそろえてください"
        elif declared != computed:
            verdict = "rejected"
            reason = "申告された成果物ハッシュと提出内容から計算したハッシュが一致しません"

    # テスト結果の検査 (ハッシュ不一致より重い失敗があれば上書きしない。
    # 既に rejected の場合は rejected のまま、delivered 候補のみ検査する)。
    if verdict in ("", "delivered"):
        test_verdict, test_reason = _evaluate_tests(test_results)
        if test_verdict != "delivered":
            verdict = test_verdict
            reason = test_reason
    if verdict == "":
        verdict = "delivered"
        reason = "実装が納品されました。まだ能力は有効ではありません"

    lock = _lock_for(_gap.gap_db_path(manager), target)
    with lock:
        # 施錠後に重複を再確認する (同時投入でも1件)。
        if declared:
            for row in list_deliveries(manager, target):
                if str(row.get("artifact_sha256") or "").strip().lower() == declared:
                    return {
                        "ok": True,
                        "idempotent": True,
                        "proposal_id": target,
                        "project_id": pid,
                        "status": str((_gap.get_proposal(manager, target) or {})
                                      .get("status") or status),
                        "verdict": str(row.get("verdict") or ""),
                        "delivery_id": str(row.get("id") or ""),
                        "reason": str(row.get("reason") or ""),
                    }
        fresh = _gap.get_proposal(manager, target)
        cur_status = str((fresh or {}).get("status") or status)
        if cur_status not in ("export_ready", "submitted", "delivered"):
            raise ValueError(f"状態 {cur_status} への納品は受け付けません")
        delivery_id = hashlib.sha256(
            f"{target}|{declared}|{time.time_ns()}".encode("utf-8")).hexdigest()[:32]
        with _connect(manager) as db:
            with db:
                db.execute(
                    """INSERT INTO gap_deliveries(
                           id, proposal_id, project_id, base_commit, result_commit,
                           diff_ref, content_hash, artifact_sha256, test_summary,
                           capability_ids, failure_log, verdict, reason, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (delivery_id, target, pid, base_commit[:256],
                     result_commit[:256], _safe_short(diff_ref, 200),
                     computed, declared,
                     json.dumps(_sanitize_json(test_results if isinstance(
                         test_results, dict) else {}), ensure_ascii=False),
                     json.dumps(capability_ids, ensure_ascii=False),
                     _safe_short(failure_logs, SHORT_LOG_CHARS),
                     verdict, _safe_short(reason, 500), _utcnow()))
        # 状態の進行 (delivered の再納品は履歴追記のみで状態を進めない)。
        new_status = cur_status
        if cur_status in ("export_ready", "submitted"):
            if verdict == "delivered":
                if cur_status == "export_ready":
                    _gap.set_proposal_status(
                        manager, pid, target, "submitted", actor=actor_name,
                        reason="納品受付に伴う送付記録の補完 (実際には何も送信していません)")
                new_status = _gap.set_proposal_status(
                    manager, pid, target, "delivered", actor=actor_name,
                    reason=reason).get("status") or "delivered"
            else:
                new_status = _gap.set_proposal_status(
                    manager, pid, target, verdict, actor=actor_name,
                    reason=reason).get("status") or verdict
        # delivered のまま履歴追記した場合も監査に残す (状態は変えない)。
        if cur_status == "delivered":
            _gap._audit_event(manager, pid, target, actor=actor_name,
                              from_status="delivered", to_status="delivered",
                              reason=f"再納品の履歴追記: {verdict}",
                              content_hash=str((fresh or {}).get("content_hash") or ""))
    return {
        "ok": True,
        "idempotent": False,
        "proposal_id": target,
        "project_id": pid,
        "status": str(new_status),
        "verdict": verdict,
        "delivery_id": delivery_id,
        "reason": reason,
    }


# ----------------------------------------------------------------------------
# 再判定 (verified はこの関数からの専用経路でのみ付与する)
# ----------------------------------------------------------------------------

def _transition_to_verified_internal(manager, project_id: str, proposal_id: str, *,
                                     actor: str, reason: str,
                                     content_hash: str) -> dict:
    """verified への専用遷移 (2-D の再判定だけが使う内部経路)。

    2-A の set_proposal_status() は verified を拒否したままにする
    (既存の保護を弱めない)。delivered からのみ遷移し、それ以外は拒否する。
    """
    from app import capability_gap as _gap

    pid = str(project_id or "")
    target = str(proposal_id or "")
    with _lock_for(_gap.gap_db_path(manager), target):
        with _connect(manager) as db:
            with db:
                row = db.execute("SELECT * FROM function_proposals WHERE proposal_id=?",
                                 (target,)).fetchone()
                if row is None:
                    raise ValueError("提案が見つかりません")
                current = _gap._decode_proposal(row)
                if str(current.get("project_id") or "") != pid:
                    raise ValueError("別案件の提案は操作できません")
                cur_status = str(current.get("status") or "")
                if cur_status == "verified":
                    return current
                if cur_status != "delivered":
                    raise ValueError(f"状態 {cur_status} からは検証済みにできません")
                db.execute("UPDATE function_proposals SET status='verified', "
                           "updated_at=? WHERE proposal_id=?",
                           (_utcnow(), target))
    _gap._audit_event(manager, pid, target, actor=actor,
                      from_status="delivered", to_status="verified",
                      reason=reason, content_hash=content_hash)
    updated = _gap.get_proposal(manager, target)
    if updated is None:
        raise ValueError("提案の再読込に失敗しました")
    return updated


def _replay_remaining(replay: dict, proposal: dict) -> tuple[list[dict], bool]:
    """元の不足の残件と、必要状態の充足を調べる。

    戻り値: (remaining, required_ok)。remaining は対象の達成条件に残る不足の列。
    required_ok は該当操作が implemented かつ validated を含む必要状態を満たすか。
    """
    criterion_ids = [str(x) for x in (proposal.get("criterion_ids") or []) if str(x)]
    if not criterion_ids:
        return ([{"criterion_id": "", "gap_code": "unknown",
                   "required_capability_id": "",
                   "unmet_states": ["criterion"],
                   "note": "対象の達成条件がありません"}], False)
    gaps = [g for g in (replay.get("gaps") or [])
            if isinstance(g, dict) and str(g.get("criterion_id") or "") in criterion_ids]
    ops_by_criterion: dict[str, list[dict]] = {}
    for row in (replay.get("criteria") or []):
        if not isinstance(row, dict):
            continue
        cid = str(row.get("criterion_id") or "")
        if cid in criterion_ids:
            ops_by_criterion[cid] = [o for o in (row.get("operations") or [])
                                     if isinstance(o, dict)]
    remaining: list[dict] = []
    for gap in gaps:
        cid = str(gap.get("criterion_id") or "")
        op_id = str(gap.get("operation_id") or "")
        unmet: list[str] = []
        for op in ops_by_criterion.get(cid, []):
            if str(op.get("operation_id") or "") == op_id:
                for key in ("implemented", "configured", "input_compatible",
                            "validated", "permitted"):
                    if not op.get(key):
                        unmet.append(key)
                break
        remaining.append({
            "criterion_id": cid,
            "gap_code": str(gap.get("gap_code") or ""),
            "required_capability_id": str(gap.get("required_capability_id") or ""),
            "operation_id": op_id,
            "unmet_states": unmet,
        })
    required_ok = True
    total_ops = 0
    for ops in ops_by_criterion.values():
        for op in ops:
            total_ops += 1
            for key in ("implemented", "configured", "input_compatible",
                        "validated", "permitted"):
                if not op.get(key):
                    required_ok = False
    if total_ops == 0:
        required_ok = False
    return (remaining, required_ok)


def verify_proposal(manager, project_id: str, proposal_id: str, *,
                    actor: str) -> dict:
    """再判定。delivered の提案について元の計画署名の入力で事前照合を再生する。

    - 元の不足 (同じ criterion・capability・gap_code) が消え、かつ該当能力が
      implemented かつ validated を含む必要状態を満たす -> verified
      (根拠として再生結果のハッシュ・検査時刻・計画署名を保存)。
    - 不足がまだ残る -> verified にせず、delivered のまま残件を返す。
    - 計画署名が変わっている -> stale (新規提案は勝手に作らない)。
    - 何度呼んでも冪等 (同じ入力・同じ結果で状態を増殖させない)。
    """
    from app import capability_gap as _gap

    pid = str(project_id or "")
    target = str(proposal_id or "")
    actor_name = str(actor or "").strip()
    if not actor_name:
        raise ValueError("actor is required")
    proposal = _gap.get_proposal(manager, target)
    if proposal is None or str(proposal.get("project_id") or "") != pid:
        raise ValueError("提案が見つかりません")
    status = str(proposal.get("status") or "")
    if status == "verified":
        rows = list_verifications(manager, target)
        latest = rows[-1] if rows else {}
        return {
            "ok": True,
            "idempotent": True,
            "proposal_id": target,
            "project_id": pid,
            "status": "verified",
            "verified": True,
            "plan_signature": str(proposal.get("plan_signature") or ""),
            "replay_hash": str((latest or {}).get("replay_hash") or ""),
            "checked_at": str((latest or {}).get("checked_at") or ""),
        }
    if status != "delivered":
        raise ValueError("delivered の提案だけを再判定できます")

    # 計画署名の照合 (変わっていれば stale。新規提案は作らない)。
    try:
        from app.goal_review import plan_snapshot

        _, current_signature = plan_snapshot(manager, pid)
    except Exception:
        current_signature = ""
    current_signature = str(current_signature or "")
    proposal_signature = str(proposal.get("plan_signature") or "")
    if current_signature and proposal_signature != current_signature:
        updated = _gap.set_proposal_status(
            manager, pid, target, "stale", actor=actor_name,
            reason="計画が更新されたため古い版です。新しい計画署名で事前照合し、"
                   "必要なら不足機能の提案を確認してください")
        return {
            "ok": False,
            "stale": True,
            "proposal_id": target,
            "project_id": pid,
            "status": str(updated.get("status") or "stale"),
            "verified": False,
            "reason": "計画が更新されたためこの提案は古い版です。新しい計画署名で"
                      "事前照合し、必要なら不足機能の提案を確認してください",
        }

    # 元の案件・元の計画署名の入力で事前照合を再生する (現行コード・現行登録簿)。
    replay = _gap.capability_preflight(manager, pid)
    replay_hash = _short_hash({
        "gaps": replay.get("gaps"),
        "criteria": replay.get("criteria"),
        "plan_signature": replay.get("plan_signature"),
    })
    checked_at = _utcnow()
    remaining, required_ok = _replay_remaining(replay, proposal)
    verified = (not remaining) and required_ok

    lock = _lock_for(_gap.gap_db_path(manager), target)
    with lock:
        rows = list_verifications(manager, target)
        latest = rows[-1] if rows else None
        if (latest is not None
                and str(latest.get("replay_hash") or "") == replay_hash
                and str(latest.get("result") or "") == ("verified" if verified else "delivered")):
            return {
                "ok": True,
                "idempotent": True,
                "proposal_id": target,
                "project_id": pid,
                "status": status,
                "verified": bool(verified),
                "plan_signature": proposal_signature,
                "replay_hash": replay_hash,
                "checked_at": str(latest.get("checked_at") or ""),
                "remaining": list(latest.get("remaining") or []),
            }
        verification_id = hashlib.sha256(
            f"{target}|{replay_hash}|{time.time_ns()}".encode("utf-8")).hexdigest()[:32]
        with _connect(manager) as db:
            with db:
                db.execute(
                    """INSERT INTO gap_verifications(
                           id, proposal_id, project_id, plan_signature, replay_hash,
                           result, remaining, checked_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (verification_id, target, pid, proposal_signature, replay_hash,
                     "verified" if verified else "delivered",
                     json.dumps(_sanitize_json(remaining), ensure_ascii=False),
                     checked_at))
    if verified:
        updated = _transition_to_verified_internal(
            manager, pid, target, actor=actor_name,
            reason="元の計画署名の入力で事前照合を再生し、元の不足が消えたことを確認",
            content_hash=str(proposal.get("content_hash") or ""))
        return {
            "ok": True,
            "idempotent": False,
            "proposal_id": target,
            "project_id": pid,
            "status": str(updated.get("status") or "verified"),
            "verified": True,
            "plan_signature": proposal_signature,
            "replay_hash": replay_hash,
            "checked_at": checked_at,
            "remaining": [],
            "note": "元の停止が再判定で解消しました",
        }
    return {
        "ok": True,
        "idempotent": False,
        "proposal_id": target,
        "project_id": pid,
        "status": "delivered",
        "verified": False,
        "plan_signature": proposal_signature,
        "replay_hash": replay_hash,
        "checked_at": checked_at,
        "remaining": remaining,
        "reason": "元の停止が残っています。テスト成功だけでは能力は有効になりません",
    }


def purge_delivery_records(memory_path: object, project_id: str) -> dict[str, int]:
    """完全削除時の後始末。当該PJの行だけ消し、他PJに影響しない。"""
    from app import capability_gap as _gap

    pid = str(project_id or "")
    path = _gap.gap_db_path(memory_path)
    if not path.exists():
        return {"gap_deliveries": 0, "gap_verifications": 0}
    removed: dict[str, int] = {"gap_deliveries": 0, "gap_verifications": 0}
    with _lock_for(path, f"purge:{pid}"):
        db = sqlite3.connect(str(path), timeout=30)
        try:
            with db:
                for table in ("gap_deliveries", "gap_verifications"):
                    try:
                        cur = db.execute(
                            f"DELETE FROM {table} WHERE project_id=?", (pid,))
                        removed[table] = int(cur.rowcount or 0)
                    except sqlite3.OperationalError:
                        pass
        finally:
            db.close()
    return removed
