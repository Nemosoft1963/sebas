"""Stage 2-B: Jenkins指示パッケージのエクスポート (一般機構)。

2-A の FunctionProposal (reviewed/export_ready) から、Jenkins向け指示パッケージ
3点 (instruction.json / REQUEST.md / manifest.json) を PJごとのプライベートな
development_instructions/<proposal_id>/ (PJのWorkspace配下。公開・送信先にしない)
へ原子的出力する。承認前はローカル保存のみ。Jenkinsへの送信・接続は一切しない
(ジョブ名・URL・認証情報を推測したり、指示データへ入れたりしない。外部通信なし)。

- 秘密検査は app/plan_review_loop.py::contains_secret を再利用し、出力前に3ファイル
  全文へかける。検出したら何も書かず状態も変えない (理由は種別ラベルのみ)。
- 原本の本文全文は入れない (内部参照ID・ハッシュ・短い引用だけ)。
- 冪等: 同一内容ハッシュの再送は同一パッケージを返す (再生成しない)。
  訂正で内容ハッシュが変わったら再生成し、旧版は残して superseded を記録する。
- 成功時に reviewed -> export_ready へ (既存の状態遷移関数を使う)。
  export_ready でも「送信済み」ではない (submitted は付けない。2-Dで扱う)。
- /delivery と /verify は作らない (2-Dで追加する)。

確認者名の扱い: actor は作業記録用の表示名であり、本人認証ではない
(既存の確認者名の扱いに準じる。認証は未実装)。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_INSTRUCTION = "sebas.development-instruction/v1"
SCHEMA_MANIFEST = "sebas.development-manifest/v1"

EXPORT_DIRNAME = "development_instructions"

#: proposal_id は安全な文字種・長さに制限し、パストラバーサルを拒否する。
PROPOSAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: 変更禁止境界 (指示データに明記する固定文言。一般機構であり特定PJに依存しない)。
FORBIDDEN_BOUNDARIES = (
    ".env を読まない・書かない・転送しない",
    "/data 配下を読まない・書かない・転送しない",
    "PJ原本の本文全文を取り込まない (内部参照ID・ハッシュ・短い引用だけ)",
    "認証情報 (鍵・トークン・パスワード・接続文字列) を扱わない・出力しない",
    "docker-compose*.yml を変更しない",
    "docker/proxy/ 配下を変更しない",
    "既存の安全ゲート (承認・検証・公開境界) を弱体化しない",
)

#: 期待する返却物 (固定文言)。
EXPECTED_RETURNS = (
    "proposal_id (指示との突合用)",
    "基準コミット (base_ref) と成果コミット",
    "差分またはPR (別ブランチ・隔離環境での実装)",
    "テスト結果 (既存テスト + 提案固有の受入テスト)",
    "検査済み能力ID",
    "失敗ログ",
    "成果物ハッシュ",
)

#: テスト用の失敗注入フック。製品コードでは常に None。
#: {"stage": "write", "after_files": int} で指定ファイル数書込み後に例外を送る。
FAIL_INJECT: dict | None = None

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def is_safe_proposal_id(value: object) -> bool:
    """proposal_id の入力検証。安全な文字種・長さのみ通し、経路指定を拒否する。"""
    text = str(value or "")
    if not PROPOSAL_ID_RE.fullmatch(text):
        return False
    if text in (".", ".."):
        return False
    if "/" in text or "\\" in text:
        return False
    return True


def get_base_ref(app_root: str | Path | None = None) -> str:
    """基準ソース版の決定的な識別子 (秘密を含まない)。

    本番ソースのコミットを取得する手段が無いため、app/ 配下の主要ファイルの
    ハッシュから作る。値はハッシュのみであり、ファイル内容・秘密値を含まない。
    照合 (Jenkins作業コピーとの突合) は 2-D で行う。
    """
    root = Path(app_root) if app_root else Path(__file__).resolve().parents[1]
    app_dir = root / "app"
    digest = hashlib.sha256()
    try:
        files = sorted(p for p in app_dir.glob("*.py") if p.is_file())
    except OSError:
        files = []
    count = 0
    for path in files[:200]:
        try:
            h = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(b":")
        digest.update(h.encode("utf-8"))
        digest.update(b"\n")
        count += 1
    if count == 0:
        return "unknown"
    return f"app-content-{digest.hexdigest()[:16]}-files{count}"


def _resolve_mem_path(manager_or_path) -> Path:
    if hasattr(manager_or_path, "memory"):
        return Path(manager_or_path.memory.path)
    return Path(manager_or_path)


def _gap_db_path(manager_or_path) -> Path:
    from app.capability_gap import gap_db_path as _gap_path

    return _gap_path(manager_or_path)


def _ensure_export_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS export_packages(
        proposal_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        base_ref TEXT NOT NULL,
        package_rel TEXT NOT NULL,
        instruction_sha256 TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_export_packages_project
        ON export_packages(project_id)""")
    db.execute("""CREATE TABLE IF NOT EXISTS export_history(
        id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        package_rel TEXT NOT NULL,
        instruction_sha256 TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        superseded_at TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_export_history_proposal
        ON export_history(proposal_id, superseded_at)""")


@contextmanager
def _connect(manager_or_path):
    from app.capability_gap import gap_db_path as _gap_path

    path = _gap_path(manager_or_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            # 2-A の表は capability_gap 側で作られる。ここでは追加のみ (壊さない)。
            try:
                from app.capability_gap import _ensure_tables as _ensure_gap

                _ensure_gap(db)
            except Exception:
                pass
            _ensure_export_tables(db)
        yield db
    finally:
        db.close()


def _read_connection(manager_or_path):
    path = _gap_db_path(manager_or_path)
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def get_export(manager_or_path, proposal_id: str) -> dict | None:
    db = _read_connection(manager_or_path)
    if db is None:
        return None
    try:
        with db:
            try:
                row = db.execute("SELECT * FROM export_packages WHERE proposal_id=?",
                                 (str(proposal_id),)).fetchone()
            except sqlite3.OperationalError:
                return None
    finally:
        db.close()
    return dict(row) if row is not None else None


def list_exports(manager_or_path, project_id: str) -> list[dict]:
    db = _read_connection(manager_or_path)
    if db is None:
        return []
    try:
        with db:
            try:
                rows = db.execute("SELECT * FROM export_packages WHERE project_id=? "
                                  "ORDER BY updated_at, proposal_id",
                                  (str(project_id),)).fetchall()
            except sqlite3.OperationalError:
                return []
    finally:
        db.close()
    return [dict(r) for r in rows]


def list_export_history(manager_or_path, proposal_id: str) -> list[dict]:
    db = _read_connection(manager_or_path)
    if db is None:
        return []
    try:
        with db:
            try:
                rows = db.execute("SELECT * FROM export_history WHERE proposal_id=? "
                                  "ORDER BY superseded_at",
                                  (str(proposal_id),)).fetchall()
            except sqlite3.OperationalError:
                return []
    finally:
        db.close()
    return [dict(r) for r in rows]


def _resolve_scope_dir(manager, project_id: str) -> Path:
    """PJのWorkspaceスコープ (私用)。公開・送信先にしない。"""
    pid = str(project_id or "")
    configured = ""
    try:
        project = manager.memory.get_project(pid)
        if project:
            configured = str(project.get("workspace_path") or "")
    except Exception:
        configured = ""
    workspace = getattr(manager, "workspace", None)
    if workspace is None:
        raise ValueError("workspace が無いためエクスポートできません")
    _, scope = workspace.project_path(configured or f"projects/{pid}", pid)
    return Path(scope)


def _package_paths(scope: Path, proposal_id: str) -> tuple[Path, Path, str]:
    parent = scope / EXPORT_DIRNAME
    final = parent / proposal_id
    rel = f"{EXPORT_DIRNAME}/{proposal_id}"
    return parent, final, rel


def _target_candidates(proposal: dict) -> list[str]:
    """対象ファイル候補 (一般的・決定的。特定PJの語・IDに依存しない)。"""
    cap = str(proposal.get("required_capability_id") or "")
    short = re.sub(r"[^A-Za-z0-9_]", "_", cap).strip("_")[:40] or "new_capability"
    return [
        f"app/{short}.py (新規実装。既存ファイルの直接改変は最小限)",
        f"tests/test_{short}.py (提案固有の受入テスト)",
        "docs/<capability>_design.md (必要なら設計メモ)",
    ]


def _build_instruction(proposal: dict, base_ref: str) -> dict:
    op = str(proposal.get("required_operation") or "")
    contract = proposal.get("interface_contract") or {}
    if not isinstance(contract, dict):
        contract = {}
    tests = proposal.get("acceptance_tests") or []
    if not isinstance(tests, list):
        tests = [tests]
    alternatives = proposal.get("existing_alternatives") or []
    if not isinstance(alternatives, list):
        alternatives = []
    observed = proposal.get("observed_failure") or {}
    if not isinstance(observed, dict):
        observed = {"code": str(observed)}
    evidence = observed.get("evidence_refs") or {}
    if not isinstance(evidence, dict):
        evidence = {}
    return {
        "schema": SCHEMA_INSTRUCTION,
        "proposal_id": str(proposal.get("proposal_id") or ""),
        "project_id": str(proposal.get("project_id") or ""),
        "plan_signature": str(proposal.get("plan_signature") or ""),
        "criterion_ids": list(proposal.get("criterion_ids") or []),
        "task_keys": list(proposal.get("task_keys") or []),
        "gap_code": str(proposal.get("gap_code") or "missing_capability"),
        "required_capability_id": str(proposal.get("required_capability_id") or ""),
        "required_operation": op,
        "base_ref": str(base_ref or "unknown"),
        "target_files": _target_candidates(proposal),
        "change_forbidden": list(FORBIDDEN_BOUNDARIES),
        "interface_contract": {
            "inputs": list(contract.get("inputs") or []),
            "outputs": list(contract.get("outputs") or []),
            "failure_states": list(contract.get("failure_states") or []),
        },
        "acceptance_tests": [str(x) for x in tests],
        "reproduction_steps": [
            "同一の計画署名・同一入力で事前照合を再生し、不足が残ることを確認する",
            "提案固有の受入テストを実行し、失敗の再現と成功条件を記録する",
            "既存テストを実行し、安全ゲートの弱体化が無いことを確認する",
        ],
        "expected_returns": {
            "proposal_id": str(proposal.get("proposal_id") or ""),
            "base_commit": str(base_ref or "unknown"),
            "result_commit": "成果コミット (開発側で記録)",
            "diff_or_pr": "差分またはPR (別ブランチ・隔離環境)",
            "test_results": "テスト結果 (既存 + 受入)",
            "capability_ids": "検査済み能力ID",
            "failure_logs": "失敗ログ",
            "artifact_hashes": "成果物ハッシュ",
        },
        "existing_alternatives": alternatives,
        "observed_failure": {
            "code": str(observed.get("code") or ""),
            "evidence_refs": dict(evidence),
        },
        "permission_class": str(proposal.get("permission_class") or "local_reversible"),
        "content_hash": str(proposal.get("content_hash") or ""),
        "generated_at": _utcnow(),
        "note": "承認前のローカル保存のみ。送信はしない",
    }


def _build_request_md(proposal: dict, instruction: dict) -> str:
    pid = str(proposal.get("proposal_id") or "")
    lines = [
        f"# 開発依頼 {pid}",
        "",
        "この文書は人が読むための依頼文です。詳細な機械可読条件は instruction.json を見てください。",
        "",
        "## 目標との関係",
        "",
        f"- 対象の達成条件: {', '.join(str(x) for x in (proposal.get('criterion_ids') or [])) or '-'}",
        f"- 対象工程: {', '.join(str(x) for x in (proposal.get('task_keys') or [])) or '-'}",
        f"- 必要な操作: {proposal.get('required_operation') or '-'}",
        "",
        "## 現行機能で足りない理由",
        "",
        f"- 観測された失敗: {((proposal.get('observed_failure') or {}).get('code')) or '-'}",
        "- 既存機能の再利用範囲:",
    ]
    alternatives = proposal.get("existing_alternatives") or []
    if isinstance(alternatives, list) and alternatives:
        for alt in alternatives:
            if isinstance(alt, dict):
                lines.append(f"  - {alt.get('capability_id') or '-'}: {alt.get('why_insufficient') or '-'}")
    else:
        lines.append("  - (なし)")
    lines += [
        "",
        "## 新機能の入出力・失敗時挙動",
        "",
        f"- 入力: {', '.join(str(x) for x in ((instruction.get('interface_contract') or {}).get('inputs') or [])) or '-'}",
        f"- 出力: {', '.join(str(x) for x in ((instruction.get('interface_contract') or {}).get('outputs') or [])) or '-'}",
        f"- 失敗時: {', '.join(str(x) for x in ((instruction.get('interface_contract') or {}).get('failure_states') or [])) or '-'}",
        "",
        "## 承認境界",
        "",
        f"- 許可区分: {proposal.get('permission_class') or '-'}",
        "- 変更禁止:",
    ]
    for boundary in FORBIDDEN_BOUNDARIES:
        lines.append(f"  - {boundary}")
    lines += [
        "",
        "## 受入テスト",
        "",
    ]
    tests = instruction.get("acceptance_tests") or []
    if tests:
        for item in tests:
            lines.append(f"- {item}")
    else:
        lines.append("- (なし)")
    lines += [
        "",
        "## 期待する返却物",
        "",
    ]
    for item in EXPECTED_RETURNS:
        lines.append(f"- {item}")
    lines += [
        "",
        f"基準版: {instruction.get('base_ref') or 'unknown'}",
        "",
        "承認前のローカル保存のみであり、送信はしません。",
        "",
    ]
    return "\n".join(lines)


def _build_manifest(proposal_id: str, instruction_bytes: bytes,
                    request_bytes: bytes) -> dict:
    return {
        "schema": SCHEMA_MANIFEST,
        "proposal_id": str(proposal_id),
        "files": {
            "instruction.json": _sha256_bytes(instruction_bytes),
            "REQUEST.md": _sha256_bytes(request_bytes),
        },
        "generated_at": _utcnow(),
    }


def _check_no_secret(*blobs: str) -> str:
    """3ファイル全文へ秘密検査。検出時は種別ラベルのみ返す (値は出さない)。"""
    from app.plan_review_loop import contains_secret

    for blob in blobs:
        label = contains_secret(str(blob or ""))
        if label:
            return str(label)
    return ""


def _read_file_bytes(path: Path) -> bytes | None:
    try:
        if not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _cleanup_tmp(tmp: Path) -> None:
    try:
        if tmp.is_dir():
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
        elif tmp.exists():
            tmp.unlink()
    except OSError:
        pass


def export_proposal(manager, project_id: str, proposal_id: str, *,
                    actor: str, base_ref: str = "") -> dict:
    """指示パッケージを原子的出力する (一般機構)。

    状態が reviewed/export_ready の提案だけ対象。draft/rejected/stale/
    needs_evidence は拒否し理由を返す。秘密検出時は書かず状態も変えない。
    同一内容ハッシュの再送は既存を返す。同時実行でも1つ。訂正でハッシュが
    変わったら再生成し旧版を残して superseded を記録する。
    """
    from app.capability_gap import get_proposal

    pid = str(project_id or "")
    target_id = str(proposal_id or "")
    actor_name = str(actor or "").strip()
    if not actor_name:
        raise ValueError("actor is required")
    if not pid:
        raise ValueError("project_id is required")
    if not is_safe_proposal_id(target_id):
        return {"ok": False, "reason": "proposal_id が不正です"}
    proposal = get_proposal(manager, target_id)
    if proposal is None or str(proposal.get("project_id") or "") != pid:
        return {"ok": False, "reason": "提案が見つかりません"}
    status = str(proposal.get("status") or "")
    if status not in ("reviewed", "export_ready"):
        return {"ok": False,
                "reason": f"状態 {status} ではエクスポートできません"}
    content_hash = str(proposal.get("content_hash") or "")
    ref = str(base_ref or "").strip() or "unknown"
    # 秘密を含まない識別子に正規化する (長すぎる値は受けない)。
    if ref != "unknown" and len(ref) > 128:
        ref = ref[:128]
    if ref != "unknown" and not re.fullmatch(r"[A-Za-z0-9_.:+@#-]{1,128}", ref):
        # 版識別子として安全な文字種でなければ unknown 扱いにする
        # (値をそのまま残さず、stale 判定の材料にする)。
        ref = "unknown"

    lock = _lock_for(str(_gap_db_path(manager)) + "|" + target_id)
    with lock:
        with _connect(manager) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM export_packages WHERE proposal_id=?",
                             (target_id,)).fetchone()
            existing = dict(row) if row else None
            if (existing is not None
                    and str(existing.get("project_id") or "") != pid):
                db.commit()
                return {"ok": False, "reason": "提案が見つかりません"}
            db.commit()

        # 既存パッケージの検証 (同一ハッシュなら再生成しない)。
        if existing is not None and str(existing.get("content_hash") or "") == content_hash:
            try:
                scope = _resolve_scope_dir(manager, pid)
            except ValueError as exc:
                return {"ok": False, "reason": str(exc)}
            _, final, rel = _package_paths(scope, target_id)
            inst = _read_file_bytes(final / "instruction.json")
            req = _read_file_bytes(final / "REQUEST.md")
            man_raw = _read_file_bytes(final / "manifest.json")
            if inst is not None and req is not None and man_raw is not None:
                try:
                    manifest = json.loads(man_raw.decode("utf-8"))
                    files = manifest.get("files") or {}
                    ok_hash = (
                        files.get("instruction.json") == _sha256_bytes(inst)
                        and files.get("REQUEST.md") == _sha256_bytes(req)
                        and str(manifest.get("proposal_id") or "") == target_id
                    )
                except (ValueError, UnicodeDecodeError, AttributeError):
                    ok_hash = False
                if ok_hash:
                    # ハッシュ同一のまま返す (再生成して変えない)。
                    return {
                        "ok": True,
                        "idempotent": True,
                        "proposal_id": target_id,
                        "project_id": pid,
                        "status": status,
                        "content_hash": content_hash,
                        "base_ref": str(existing.get("base_ref") or ""),
                        "package_rel": str(existing.get("package_rel") or rel),
                        "instruction_sha256": str(existing.get("instruction_sha256") or ""),
                        "request_sha256": str(existing.get("request_sha256") or ""),
                        "manifest_sha256": str(existing.get("manifest_sha256") or ""),
                    }
            # ファイル欠損・破損時は再生成へ進む (下へ落ちる)。

        # 新規生成する内容を組み立てる (まだ書かない)。
        instruction = _build_instruction(proposal, ref)
        instruction_bytes = (json.dumps(instruction, ensure_ascii=False,
                                        indent=2, sort_keys=True) + "\n").encode("utf-8")
        request_text = _build_request_md(proposal, instruction)
        request_bytes = request_text.encode("utf-8")
        manifest = _build_manifest(target_id, instruction_bytes, request_bytes)
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False,
                                     indent=2, sort_keys=True) + "\n").encode("utf-8")
        # 秘密検査 (出力前に全文へ。検出したら書かず状態も変えない)。
        secret_label = _check_no_secret(instruction_bytes.decode("utf-8", "ignore"),
                                        request_text,
                                        manifest_bytes.decode("utf-8", "ignore"))
        if secret_label:
            return {"ok": False,
                    "reason": f"秘密混入の疑いのためエクスポートを拒否しました(種別: {secret_label})"}

        try:
            scope = _resolve_scope_dir(manager, pid)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        parent, final, rel = _package_paths(scope, target_id)
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "reason": f"出力先を作成できません: {type(exc).__name__}"}

        # 旧版の保全: 内容が変わり既存があれば、旧ディレクトリを残して記録する。
        superseded: list[dict] = []
        if existing is not None and str(existing.get("content_hash") or "") != content_hash:
            if final.is_dir():
                old_hash = str(existing.get("content_hash") or "")[:8] or "old"
                archive = parent / f"{target_id}.superseded.{old_hash}"
                suffix = 0
                while archive.exists():
                    suffix += 1
                    archive = parent / f"{target_id}.superseded.{old_hash}.{suffix}"
                try:
                    final.rename(archive)
                except OSError as exc:
                    return {"ok": False,
                            "reason": f"旧版の保全に失敗したため何も変更しません: {type(exc).__name__}"}
                superseded.append({
                    "proposal_id": target_id,
                    "content_hash": str(existing.get("content_hash") or ""),
                    "package_rel": f"{EXPORT_DIRNAME}/{archive.name}",
                })
            # DBの旧行は history へ移す (ファイルは残す)。
            with _connect(manager) as db:
                with db:
                    db.execute(
                        """INSERT OR REPLACE INTO export_history(
                               id, proposal_id, project_id, content_hash, package_rel,
                               instruction_sha256, request_sha256, manifest_sha256,
                               superseded_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (f"sup_{target_id}_{uuid.uuid4().hex[:8]}", target_id, pid,
                         str(existing.get("content_hash") or ""),
                         str(existing.get("package_rel") or rel),
                         str(existing.get("instruction_sha256") or ""),
                         str(existing.get("request_sha256") or ""),
                         str(existing.get("manifest_sha256") or ""),
                         _utcnow()))

        # 原子的出力 (一時名で書いて rename。途中失敗で半端を残さない)。
        tmp = parent / f".tmp.{target_id}.{uuid.uuid4().hex[:8]}"
        try:
            tmp.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            return {"ok": False, "reason": f"一時出力先を作成できません: {type(exc).__name__}"}
        try:
            (tmp / "instruction.json").write_bytes(instruction_bytes)
            if FAIL_INJECT and FAIL_INJECT.get("stage") == "write":
                after = int(FAIL_INJECT.get("after_files", 1))
                if after <= 1:
                    raise OSError("injected write failure")
            (tmp / "REQUEST.md").write_bytes(request_bytes)
            if FAIL_INJECT and FAIL_INJECT.get("stage") == "write":
                after = int(FAIL_INJECT.get("after_files", 1))
                if after <= 2:
                    raise OSError("injected write failure")
            (tmp / "manifest.json").write_bytes(manifest_bytes)
            if FAIL_INJECT and FAIL_INJECT.get("stage") == "write":
                raise OSError("injected write failure")
            # 書いた内容の再照合 (破損があれば公開しない)。
            check_inst = (tmp / "instruction.json").read_bytes()
            check_req = (tmp / "REQUEST.md").read_bytes()
            check_man = (tmp / "manifest.json").read_bytes()
            check_manifest = json.loads(check_man.decode("utf-8"))
            check_files = check_manifest.get("files") or {}
            if (check_files.get("instruction.json") != _sha256_bytes(check_inst)
                    or check_files.get("REQUEST.md") != _sha256_bytes(check_req)):
                raise OSError("hash verification failed")
            if final.exists():
                # 同時実行で先に作られた場合: 内容が同一なら破棄して既存を返す。
                # 内容が異なれば一時側を残さず失敗にする (半端を残さない)。
                _cleanup_tmp(tmp)
                reread = get_export(manager, target_id)
                if (reread is not None
                        and str(reread.get("content_hash") or "") == content_hash):
                    return {
                        "ok": True,
                        "idempotent": True,
                        "proposal_id": target_id,
                        "project_id": pid,
                        "status": str(get_proposal(manager, target_id).get("status") or ""),
                        "content_hash": content_hash,
                        "base_ref": str(reread.get("base_ref") or ""),
                        "package_rel": str(reread.get("package_rel") or rel),
                        "instruction_sha256": str(reread.get("instruction_sha256") or ""),
                        "request_sha256": str(reread.get("request_sha256") or ""),
                        "manifest_sha256": str(reread.get("manifest_sha256") or ""),
                    }
                return {"ok": False, "reason": "同時実行の競合のため再実行してください"}
            tmp.rename(final)
        except Exception as exc:
            _cleanup_tmp(tmp)
            # 旧版を移した直後の失敗では、半端な final を残さない。
            # (旧版は archive に残る。新規は作り直せる。)
            if isinstance(exc, OSError) and "injected" in str(exc):
                return {"ok": False, "reason": "書込み中断のため何も変更しません"}
            return {"ok": False,
                    "reason": f"出力に失敗したため何も変更しません: {type(exc).__name__}"}

        instruction_sha = _sha256_bytes(instruction_bytes)
        request_sha = _sha256_bytes(request_bytes)
        manifest_sha = _sha256_bytes(manifest_bytes)
        now = _utcnow()
        with _connect(manager) as db:
            with db:
                db.execute(
                    """INSERT OR REPLACE INTO export_packages(
                           proposal_id, project_id, content_hash, base_ref, package_rel,
                           instruction_sha256, request_sha256, manifest_sha256,
                           created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,
                              COALESCE((SELECT created_at FROM export_packages
                                        WHERE proposal_id=?), ?))""",
                    (target_id, pid, content_hash, ref, rel,
                     instruction_sha, request_sha, manifest_sha,
                     now, target_id, now))
                # updated_at を現在にそろえる (作成時刻は保持)。
                db.execute("UPDATE export_packages SET updated_at=?, content_hash=?, "
                           "base_ref=?, package_rel=?, instruction_sha256=?, "
                           "request_sha256=?, manifest_sha256=? WHERE proposal_id=?",
                           (now, content_hash, ref, rel,
                            instruction_sha, request_sha, manifest_sha, target_id))

        # 成功時に reviewed -> export_ready へ (既存の遷移関数を使う)。
        from app.capability_gap import set_proposal_status

        try:
            fresh = get_proposal(manager, target_id)
            cur_status = str((fresh or {}).get("status") or "")
            if cur_status == "reviewed":
                fresh = set_proposal_status(manager, pid, target_id, "export_ready",
                                            actor=actor_name, reason="指示パッケージを出力")
                cur_status = "export_ready"
        except ValueError as exc:
            # 不正な遷移は拒否のまま (ファイルは残るが状態は変えない)。
            # 既に export_ready の場合はここに来ない。
            return {"ok": False, "reason": f"状態遷移に失敗しました: {exc}"}
        return {
            "ok": True,
            "idempotent": False,
            "proposal_id": target_id,
            "project_id": pid,
            "status": cur_status,
            "content_hash": content_hash,
            "base_ref": ref,
            "package_rel": rel,
            "instruction_sha256": instruction_sha,
            "request_sha256": request_sha,
            "manifest_sha256": manifest_sha,
            "superseded": superseded,
        }


def purge_export_packages(memory_path: str | Path, project_id: str,
                          workspace_root: str | Path | None = None) -> dict[str, int]:
    """完全削除時の後始末。当該PJの行と私用パッケージだけ消し、他PJに影響しない。"""
    from app.capability_gap import gap_db_path as _gap_path

    pid = str(project_id or "")
    removed: dict[str, int] = {"export_packages": 0, "export_history": 0,
                               "export_files": 0}
    path = _gap_path(memory_path)
    if path.exists():
        with _lock_for(str(path) + "|purge"):
            try:
                db = sqlite3.connect(str(path), timeout=30)
            except sqlite3.Error:
                db = None
            if db is not None:
                try:
                    with db:
                        for table in ("export_packages", "export_history"):
                            try:
                                cur = db.execute(
                                    f"DELETE FROM {table} WHERE project_id=?", (pid,))
                                removed[table] = int(cur.rowcount or 0)
                            except sqlite3.OperationalError:
                                pass
                finally:
                    db.close()
    # 私用パッケージの削除 (当該PJのスコープだけ)。
    if workspace_root is not None:
        from app.project_lifecycle_registry import resolve_workspace_dir

        configured = ""
        mem = Path(memory_path)
        if mem.exists():
            try:
                db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
                try:
                    try:
                        r = db.execute("SELECT workspace_path FROM projects WHERE id=?",
                                       (pid,)).fetchone()
                        if r:
                            configured = str(r[0] or "")
                    except sqlite3.Error:
                        pass
                finally:
                    db.close()
            except sqlite3.Error:
                pass
        scope = resolve_workspace_dir(memory_path, pid, workspace_root, configured)
        candidates: list[Path] = []
        if scope is not None:
            candidates.append(scope / EXPORT_DIRNAME)
        # 既定配置のフォールバック (設定が読めない場合)。
        fallback = resolve_workspace_dir(memory_path, pid, workspace_root,
                                         f"projects/{pid}")
        if fallback is not None and fallback not in candidates:
            # projects/{pid} 直下ではなく、その development_instructions を見る。
            candidates.append(fallback / EXPORT_DIRNAME)
        for base in candidates:
            if base is None or not base.is_dir():
                continue
            # 当該PJのベース配下だけを消す (他PJのベースには触れない)。
            # export_packages の package_rel が無くても、ベースごと消すのは
            # 当該PJのスコープに限られるため安全 (他PJのスコープは別ディレクトリ)。
            try:
                import shutil

                # package_rel の無い古い残存も含め、ベース配下を数えて消す。
                # ただしベース自体が他PJと共有されることは無い (スコープはPJ専用)。
                count = 0
                for child in list(base.iterdir()):
                    try:
                        if child.is_dir() and not child.is_symlink():
                            shutil.rmtree(child)
                            count += 1
                        elif child.is_file() or child.is_symlink():
                            child.unlink()
                            count += 1
                    except OSError:
                        continue
                removed["export_files"] += count
                # 空になったベースは残してもよいが、数えない。
            except OSError:
                continue
    return removed
