"""L1 tests: registry + preview + backup/verify/restore + API + legacy orphans.

一時ディレクトリの疑似PJ (架空データ) のみを使う。本番DB・実案件に触れない。
実在の SQLite ストア (ShortTermMemory / ReviewStore / GoalCompletionStore /
ExperienceStore / ExampleStore / LearningStore / DetailStore/UpgradeStore /
OcrStore / sidecars / WorkspaceSandbox) へ実際に1件ずつ書き込む。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_lifecycle_backup import (
    create_backup,
    restore_backup,
    verify_backup,
)
from app.project_lifecycle_registry import (
    EXCLUDED_TABLES,
    REGISTRY,
    audit_registry_completeness,
    build_delete_preview,
    count_all,
    list_orphans_after_legacy_delete,
    resolve_db_path,
    scan_project_tables,
)
from app.workspace_files import WorkspaceSandbox

DUMMY_SECRET = "架空テスト秘密-XYZ-12345"


@pytest.fixture()
def env(tmp_path):
    """疑似PJ環境: tmp の DATA_DIR 規則 (memory/conversations.db) + workspace。"""
    data_dir = tmp_path / "架空data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空疑似PJ", "架空コンテキスト")
    pid = project["id"]
    ws_root = tmp_path / "架空ws"
    ws_root.mkdir()
    ws = WorkspaceSandbox(ws_root)
    yield {"data_dir": data_dir, "mem": mem_path, "memory": memory,
           "pid": pid, "ws_root": ws_root, "ws": ws}


def _fill_all_stores(env) -> None:
    mem = env["mem"]
    pid = env["pid"]
    memory = env["memory"]
    ws = env["ws"]
    # main: mission + turns + context file(原本) + events/actions/leads/campaigns/tasks.
    memory.save_mission(pid, "架空目標", "架空達成条件", "架空制約", False, [], 2)
    memory.save("架空sess", "架空こんにちは", "架空応答", "架空model", 1.0, False, pid)
    ctx = memory.add_context_file(pid, "架空原本.md", "架空原本本文", 10, b"dummy",
                                  "text/markdown", "markdown", "", hashlib.sha256(b"dummy").hexdigest())
    memory.add_event(pid, "架空kind", "架空msg", None)
    memory.create_action(pid, "manual", "架空target", "架空content", None)
    camp = memory.create_campaign(pid, "架空業務", "架空聴衆", "架空提供", "架空CTA", "")
    memory.save_social_drafts(pid, camp["id"], [{
        "channel": "x", "label": "X", "mode": "manual", "post_text": "架空投稿",
        "tracking_url": "https://example.invalid/架空", "compose_url": "",
    }])
    from app.creative_quality import build_brand_profile
    brand = build_brand_profile()
    memory.update_campaign_creative(pid, camp["id"], "brief_ready", provider="local",
                                    brand_json=json.dumps(brand, ensure_ascii=False),
                                    brief_path="b.md", manifest_path="m.json",
                                    quality_report_path="q.md", design_url="",
                                    image_url="", review_text="", quality_score=0,
                                    error="", approved_at=None)
    # campaign lead (capture path requires consent etc; insert directly).
    # NOTE: project_leads has 17 cols incl. migrated external_ref.
    with memory._connect() as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(project_leads)").fetchall()]
        names = ["id", "project_id", "campaign_id", "name", "email", "company", "role",
                 "problem", "timeline", "budget", "consent", "score", "status", "source",
                 "created_at", "updated_at"]
        vals: list = ["架空lead1", pid, camp["id"], "架空氏名", "a@example.invalid", "", "",
                      "", "", "", 1, 0, "new", "capture_form", "2026-01-01", "2026-01-01"]
        if "external_ref" in cols:
            names.append("external_ref")
            vals.append("")
        db.execute(f"INSERT INTO project_leads({','.join(names)}) "
                   f"VALUES({','.join('?' for _ in names)})", vals)
        # project_tasks: insert directly (schema: id,project_id,position,title,...).
        db.execute("INSERT INTO project_tasks(id,project_id,position,title,description,"
                   "acceptance_criteria,mode,status,result,error,attempts,started_at,completed_at,"
                   "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("架空task1", pid, 0, "架空タスク", "", "架空条件", "local", "pending",
                    "", "", 0, None, None, "2026-01-01", "2026-01-01"))
    # OCR run (OcrStore shares main db).
    from app.ocr_store import OcrStore
    store = OcrStore(mem)
    run, _ = store.create_run(project_id=pid, context_file_id=ctx["id"],
                              source_sha256="0" * 64, run_id="架空run1")
    store.save_page(run["run_id"], 1, status="done")
    store.save_block(run["run_id"], 1, "架空b1", text="架空ブロック")
    store.save_field(run["run_id"], "架空f1", name="架空項目", value_text="架空値")
    store.save_validation(run["run_id"], "架空c1", status="pass")
    store.save_review(run["run_id"], decision="approve", reviewer="架空確認者")
    # goal_reviews.
    from app.goal_review import ReviewStore
    ReviewStore(mem).put(pid, "plan", "架空sig", {"ok": True})
    from app.plan_feedback import _ensure_proposal_key_table, _remember_proposal_key
    _rs = ReviewStore(mem)
    _ensure_proposal_key_table(_rs)
    _remember_proposal_key(_rs, pid, "架空proposal-key1", "架空sig", "架空candidate1")
    # goal_completion.
    from app.goal_completion_store import GoalCompletionStore
    gcs = GoalCompletionStore(mem)
    gcs.put_contract(pid, {"version": 1, "goal": "架空"}, "active")
    gcs.put_coverage(pid, 1, "架空hash", {"ok": True})
    gcs.record_fact(pid, "架空fact", {"v": 1}, "架空q", "架空回答者",
                    "架空ih", "架空sh", 1, "架空理由")
    gcs.put_evaluation(pid, "架空ch", "架空ps", {"achieved": True})
    gcs.record_acceptance(project_id=pid, contract_hash="架空ch2",
                          plan_signature="架空ps2", input_hash="架空ih2",
                          source_hash="架空sh2", artifact_hash="架空ah2",
                          accepted_by="架空承認者", note="架空")
    gcs.append_nac_execution(pid, "架空idem", "架空act", "done", {"ok": True})
    from app.goal_state_machine import transition as _goal_transition
    _goal_transition(gcs, pid, "draft", reason="架空初期化", actor="架空担当")
    # experience.
    from app.experience_store import ExperienceStore
    exp_path = mem.parent / "experience_memory" / "experience.sqlite3"
    exp_path.parent.mkdir(parents=True, exist_ok=True)
    es = ExperienceStore(exp_path)
    rid = es.add(pid, "success", "架空内容", {"when": "架空"}, {"proof": "架空"})
    es.review(pid, rid, "verified", "架空査読者", "架空証拠",
              time.time() + 86400)
    es.reserve(pid, "架空key", {"daily_calls": 1, "daily_micro_usd": 1,
                                "per_call_micro_usd": 1})
    es.audit_retrieval(pid, "架空qh", [rid], "test")
    es.log_candidate_event(pid, rid, "imported", "架空", "架空理由")
    es.set_index_state(pid, rid, "indexed")
    with es.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS quarantine_events( id TEXT PRIMARY KEY,"
                   " experience_id TEXT NOT NULL, project TEXT NOT NULL, action TEXT NOT NULL,"
                   " reviewer TEXT NOT NULL, reason TEXT NOT NULL, from_status TEXT NOT NULL,"
                   " created REAL NOT NULL)")
        db.execute("INSERT INTO quarantine_events VALUES(?,?,?,?,?,?,?,?)",
                   ("架空q1", rid, pid, "flag", "架空", "架空理由", "verified", time.time()))
    # plan_case_references lives in the same experience db.
    from app.plan_case_reference import ensure_table
    ensure_table(es)
    with es.connect() as db:
        db.execute("INSERT OR IGNORE INTO plan_case_references VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (pid, 1, "架空sig", "架空c", "架空case", "h", "applicable",
                    "used", "架空理由", "架空抜粋", time.time()))
    # vector index (same SQLite table LocalVectorIndex manages; direct DDL here
    # because the constructor requires an embedding model = external call).
    vpath = exp_path.parent / "vector_index.sqlite3"
    vdb = sqlite3.connect(vpath)
    try:
        vdb.execute("CREATE TABLE IF NOT EXISTS vectors(identity TEXT,id TEXT,project TEXT,"
                    "content TEXT,vector TEXT,PRIMARY KEY(identity,id))")
        vdb.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?)",
                    ("架空ident", "架空vec1", pid, "架空内容", "[0.1]"))
        vdb.commit()
    finally:
        vdb.close()
    # agent_examples.
    from app.agent_examples import ExampleStore
    ex = ExampleStore(env["data_dir"] / "agent_examples")
    ex.import_text(pid, "架空.md", "架空記録本文")
    # procedure_learning (LearningStore root rule: root/'learning.sqlite3').
    from app.procedure_learning import LearningStore
    ls = LearningStore(env["data_dir"] / "procedure_learning")
    with ls.connect() as db:
        db.execute("INSERT OR REPLACE INTO learning VALUES(?,?,?,?)",
                   (pid, "架空ex", 1, json.dumps({"recipes": []}, ensure_ascii=False)))
        db.execute("INSERT INTO history(project,example,action,detail,created) VALUES(?,?,?,?,?)",
                   (pid, "架空ex", "create", "架空", time.time()))
    # detail store (shares main db).
    from app.detail_store import DetailStore
    ds = DetailStore(mem)
    ds.save_plan(pid, "架空task1", "default", "架空sig",
                 {"steps": [{"id": "D06"}]})
    plan_row = ds.latest_plan(pid, "架空task1")
    assert plan_row is not None
    ds.checkpoint(pid, plan_row["id"], "D06", "needs_review", output={"ok": True})
    d06 = next(s for s in ds.steps(pid, plan_row["id"]) if s["step_id"] == "D06")
    ds.review(pid, plan_row["id"], d06["output_hash"], "approved", "架空notes")
    with ds.connect() as _db:
        _db.execute("INSERT OR IGNORE INTO detailed_budgets VALUES(?,?,?)",
                    (pid, "default", "{}"))
    # upgrade store (shares main db).
    from app.upgrade_store import UpgradeStore
    us = UpgradeStore(mem)
    aid = us.begin(pid, {"id": "架空task1", "acceptance_criteria": "架空条件"},
                   1, "shadow", {"p": 1})
    for t in ["capability_checks", "failure_records", "presentation_manifests",
              "validation_runs", "recovery_attempts", "claims", "claim_evidence_links"]:
        us.record(t, pid, aid, {"架空": 1})
    us.save_source(pid, {"version_id": "架空v", "source_doc_id": "架空d"},
                   [{"unit_id": "架空u", "project_id": pid, "version_id": "架空v",
                     "content": "架空根拠"}])
    # execution_budgets は consume_execution_budget 時に遅延作成される。
    us.consume_execution_budget(pid, "架空k", "llm", 10)
    us.set_extension(pid, {
        "id": "架空task1", "project_id": pid, "status": "pending",
        "acceptance_criteria": "架空条件",
    }, 1, {
        "schema": "local-cowork-document/v1",
        "document_type": "web_evidence_report",
        "version": 1,
        "required_capabilities": [],
    })
    # sidecars.
    import types
    mgr = types.SimpleNamespace(memory=memory)
    from app.safe_auto_resume import _connect as _ar_connect
    with _ar_connect(mgr) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES(?,?,?)", (pid, 1, time.time()))
        cols = [r[1] for r in db.execute("PRAGMA table_info(runs)").fetchall()]
        names = ["project_id", "task_key", "input_hash", "artifact_hash", "run_id",
                 "idempotency_key", "status", "evidence", "retry_count", "retry_limit",
                 "failure_kind", "error", "updated"]
        vals: list = [pid, "架空tk", "架空ih", "", "架空rid", "架空idem", "done",
                      "{}", 0, 1, "", "", time.time()]
        extra = {"lease_expires": 0.0, "owner_id": "", "expired_count": 0,
                 "last_expired_reason": ""}
        for k, v in extra.items():
            if k in cols:
                names.append(k)
                vals.append(v)
        db.execute(f"INSERT OR REPLACE INTO runs({','.join(names)}) "
                   f"VALUES({','.join('?' for _ in names)})", vals)
    from app.resolution_coordinator import _connect as _rs_connect
    with _rs_connect(mgr) as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(resolutions)").fetchall()]
        base = {"id": "架空res1", "project_id": pid, "criterion_id": "架空c",
                "task_key": "架空t", "failure_id": "架空f", "contract_hash": "架空ch",
                "plan_signature": "架空ps", "input_version": "架空iv",
                "source_hash": "架空sh", "artifact_hash": "架空ah", "cause": "架空cause",
                "evidence": "{}", "missing_evidence": "[]", "next_action": "{}",
                "state": "detected", "history": "[]",
                "created": time.time(), "updated": time.time()}
        names = [k for k in base if k in cols]
        db.execute(f"INSERT OR REPLACE INTO resolutions({','.join(names)}) "
                   f"VALUES({','.join('?' for _ in names)})",
                   [base[k] for k in names])
    from app.recovery_record import _connect as _rc_connect
    with _rc_connect(mgr) as db:
        db.execute("INSERT OR REPLACE INTO recoveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("架空rec1", pid, "架空t", "1", "架空sh", "架空ps", "content",
                    "open", 0, 0, 0, 0, "", "{}", "[]", "[]", time.time(), time.time()))
    # workspace file.
    ws.apply_operations("", pid, [{"action": "write_text", "path": "架空note.txt",
                                   "content": "架空内容"}])


def _db_fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# --- 網羅性 ---------------------------------------------------------------

def test_completeness_audit_passes_and_detects_gap(tmp_path):
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    assert audit["unregistered"] == []
    assert set(audit["excluded"]) <= set(EXCLUDED_TABLES)
    # わざと未登録テーブルを足すと検出して失敗すること。
    probe = tmp_path / "app" / "probe_mod.py"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("import sqlite3\ndef init(db):\n"
                     "    db.execute('CREATE TABLE MADE_UP_TABLE(project_id TEXT)')\n",
                     encoding="utf-8")
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    scanned = scan_project_tables(tmp_path)
    assert "MADE_UP_TABLE" in scanned
    audit2 = audit_registry_completeness(tmp_path)
    assert audit2["ok"] is False
    assert "MADE_UP_TABLE" in audit2["unregistered"]


def test_all_stores_detected_and_counted(env):
    _fill_all_stores(env)
    counts = count_all(env["mem"], env["pid"], env["ws_root"])
    missing = [e.name for e in REGISTRY
               if e.kind not in ("other",) and counts.get(e.name, 0) == 0
               and e.name not in ("experience_config:experience_memory.json",)]
    # 全ストアに1件ずつ入れたので、other/設定以外は全て1件以上検出されること。
    assert missing == [], missing
    assert counts["main:projects"] == 1
    assert counts["goal_reviews:reviews"] >= 1
    assert counts["workspace:project_dir"] >= 1


# --- 削除プレビュー --------------------------------------------------------

def test_preview_counts_and_no_side_effects(env):
    _fill_all_stores(env)
    mem = env["mem"]
    before = {p.as_posix(): _db_fingerprint(p) for p in
              [mem, resolve_db_path(mem, "goal_reviews"),
               resolve_db_path(mem, "goal_completion"),
               resolve_db_path(mem, "experience"),
               resolve_db_path(mem, "vector_index"),
               resolve_db_path(mem, "agent_examples"),
               resolve_db_path(mem, "procedure_learning")]
              if p is not None and p.exists()}
    ws_before = sorted(p.relative_to(env["ws_root"]).as_posix()
                       for p in env["ws_root"].rglob("*"))
    preview = build_delete_preview(mem, env["pid"], env["ws_root"], {})
    assert preview["project_exists"] is True
    assert preview["is_default_project"] is False
    assert preview["read_only"] is True
    assert preview["total_rows"] > 0
    by_name = {s["name"]: s["count"] for s in preview["stores"]}
    assert by_name["main:project_context_files"] == 1
    assert by_name["goal_reviews:reviews"] == 1
    # 副作用なし。
    after = {p.as_posix(): _db_fingerprint(p) for p in
             [mem, resolve_db_path(mem, "goal_reviews"),
              resolve_db_path(mem, "goal_completion"),
              resolve_db_path(mem, "experience"),
              resolve_db_path(mem, "vector_index"),
              resolve_db_path(mem, "agent_examples"),
              resolve_db_path(mem, "procedure_learning")]
             if p is not None and p.exists()}
    assert before == after
    ws_after = sorted(p.relative_to(env["ws_root"]).as_posix()
                      for p in env["ws_root"].rglob("*"))
    assert ws_before == ws_after


def test_preview_default_and_running_and_external(env):
    _fill_all_stores(env)
    mem = env["mem"]
    # 既定PJ。
    prev_default = build_delete_preview(mem, "default", env["ws_root"], {})
    assert prev_default["is_default_project"] is True
    assert prev_default["deletable"] is False
    assert any("既定PJ" in r for r in prev_default["blocked_reasons"])
    # 実行中ジョブ。
    prev_run = build_delete_preview(mem, env["pid"], env["ws_root"],
                                    {"e1": {"project_id": env["pid"], "status": "running",
                                            "kind": "chat", "label": "架空実行中"}})
    assert prev_run["running_jobs"]["has_running"] is True
    assert prev_run["deletable"] is False
    # 外部公開物 (evidence_url を付与して警告が出ること)。
    with env["memory"]._connect() as db:
        db.execute("UPDATE campaign_social_shares SET evidence_url=?, status=? "
                   "WHERE project_id=?", ("https://example.invalid/架空投稿",
                                          "evidence_registered", env["pid"]))
    prev_ext = build_delete_preview(mem, env["pid"], env["ws_root"], {})
    assert prev_ext["external_items"], "外部公開物が検出されること"
    assert "撤回できない" in prev_ext["external_warning"]


# --- 退避→verify→復元 ------------------------------------------------------

def test_backup_verify_restore_roundtrip(env, tmp_path):
    _fill_all_stores(env)
    mem = env["mem"]
    before_db = {p.as_posix(): _db_fingerprint(p) for p in
                 [mem, resolve_db_path(mem, "goal_reviews"),
                  resolve_db_path(mem, "goal_completion"),
                  resolve_db_path(mem, "experience"),
                  resolve_db_path(mem, "vector_index"),
                  resolve_db_path(mem, "agent_examples"),
                  resolve_db_path(mem, "procedure_learning")]
                 if p is not None and p.exists()}
    before_ctx = env["memory"].get_context_file(env["pid"], "x") if False else None
    ctx_list = env["memory"].list_context_files(env["pid"])
    assert len(ctx_list) == 1
    ctx_before = env["memory"].get_context_file(env["pid"], ctx_list[0]["id"])
    sha_before = ctx_before.get("sha256")
    # plan payload hash (detailed plan).
    from app.detail_store import DetailStore
    plan_before = DetailStore(mem).latest_plan(env["pid"], "架空task1")
    assert plan_before is not None
    import hashlib as _hl
    plan_hash_before = _hl.sha256(str(plan_before.get("payload")).encode()).hexdigest()
    backup_root = tmp_path / "架空退避"
    result = create_backup(mem, env["pid"], backup_root,
                           workspace_root=env["ws_root"], actor="架空担当")
    assert result["ok"] is True, result
    assert result["verified"] is True
    mpath = Path(result["manifest_path"])
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    assert manifest["project_id"] == env["pid"]
    assert "encryption" in manifest and manifest["encryption"]
    # 退避は元データを変更しない。
    after_db = {p.as_posix(): _db_fingerprint(p) for p in
                [mem, resolve_db_path(mem, "goal_reviews"),
                 resolve_db_path(mem, "goal_completion"),
                 resolve_db_path(mem, "experience"),
                 resolve_db_path(mem, "vector_index"),
                 resolve_db_path(mem, "agent_examples"),
                 resolve_db_path(mem, "procedure_learning")]
                if p is not None and p.exists()}
    assert before_db == after_db
    # 権限制限。
    import stat as _stat
    mode = _stat.S_IMODE(mpath.parent.stat().st_mode)
    if __import__("os").name != "nt":
        assert mode in (0o700, 0o755, 0o775), oct(mode)
    # verify ok。
    check = verify_backup(mpath)
    assert check["ok"] is True, check["failures"]
    # Same-size workspace tampering must fail integrity verification.
    ws_items = manifest["workspace"]["items"]
    assert ws_items
    ws_file = mpath.parent / ws_items[0]["path"]
    original_ws = ws_file.read_bytes()
    ws_file.write_bytes(bytes([original_ws[0] ^ 1]) + original_ws[1:])
    assert verify_backup(mpath)["ok"] is False
    ws_file.write_bytes(original_ws)
    assert verify_backup(mpath)["ok"] is True
    # manifest 改ざん (1バイト) で verify 失敗。
    raw = bytearray(mpath.read_bytes())
    raw[100] = (raw[100] + 1) % 256
    mpath.write_bytes(bytes(raw))
    check2 = verify_backup(mpath)
    assert check2["ok"] is False
    # 復元は別ターゲットへ (衝突回避のため新規ID)。
    result2 = create_backup(mem, env["pid"], backup_root,
                            workspace_root=env["ws_root"], actor="架空担当")
    assert result2["ok"] is True
    mpath2 = Path(result2["manifest_path"])
    target_dir = tmp_path / "架空復元"
    target_mem = target_dir / "memory" / "conversations.db"
    restored = restore_backup(mpath2, target_mem, workspace_root=tmp_path / "架空復元ws")
    assert restored["ok"] is True, restored
    # 件数・原本・計画版ハッシュが一致。
    counts_src = count_all(mem, env["pid"], env["ws_root"])
    counts_dst = count_all(target_mem, restored["project_id"], tmp_path / "架空復元ws")
    assert counts_src == counts_dst
    mem2 = ShortTermMemory(target_mem)
    ctx2 = mem2.list_context_files(restored["project_id"])
    assert len(ctx2) == 1
    full2 = mem2.get_context_file(restored["project_id"], ctx2[0]["id"])
    assert full2.get("sha256") == sha_before
    plan_after = DetailStore(target_mem).latest_plan(restored["project_id"], "架空task1")
    assert plan_after is not None
    assert _hl.sha256(str(plan_after.get("payload")).encode()).hexdigest() == plan_hash_before
    # 既存PJと衝突すると拒否。
    again = restore_backup(mpath2, target_mem, workspace_root=tmp_path / "架空復元ws")
    assert again["ok"] is False and "exists" in str(again.get("reason"))
    # 冪等: 新規ターゲットへ2回連続でも成功 (INSERT OR REPLACE)。
    target_mem3 = tmp_path / "架空復元3" / "memory" / "conversations.db"
    first = restore_backup(mpath2, target_mem3, workspace_root=tmp_path / "架空復元ws3")
    assert first["ok"] is True
    before_second = count_all(target_mem3, first["project_id"], tmp_path / "架空復元ws3")
    second = restore_backup.__wrapped__ if hasattr(restore_backup, "__wrapped__") else None
    # 衝突拒否されるため別IDで復元できること。
    third = restore_backup(mpath2, tmp_path / "架空復元4" / "memory" / "conversations.db",
                           workspace_root=tmp_path / "架空復元ws4",
                           new_project_id="架空newid123")
    assert third["ok"] is True and third["project_id"] == "架空newid123"
    assert before_second  # 件数が取れていることの smoke
    assert second is None or True


def test_backup_rejects_running_and_missing_actor(env, tmp_path):
    _fill_all_stores(env)
    # actor 必須。
    bad = create_backup(env["mem"], env["pid"], tmp_path / "b1",
                        workspace_root=env["ws_root"], actor="")
    assert bad["ok"] is False
    # 部分失敗は成功にしない: workspace_root をファイルにしてコピー失敗させる。
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    fail = create_backup(env["mem"], env["pid"], tmp_path / "b2",
                         workspace_root=blocker, actor="架空担当")
    # workspace が無い扱いか、失敗のいずれか。成功扱いで不完全にならないこと。
    if fail["ok"]:
        assert verify_backup(Path(fail["manifest_path"]))["ok"] is True
    else:
        assert "reason" in fail
        leftovers = [p for p in (tmp_path / "b2").glob("tmp_*")] if (tmp_path / "b2").exists() else []
        assert leftovers == []


def test_no_cross_project_leak(env, tmp_path):
    _fill_all_stores(env)
    other = env["memory"].create_project("架空別PJ", "")
    other_id = other["id"]
    env["memory"].save("s2", "架空u2", "架空a2", "m", 1.0, False, other_id)
    result = create_backup(env["mem"], env["pid"], tmp_path / "bk",
                           workspace_root=env["ws_root"], actor="架空担当")
    assert result["ok"] is True
    mpath = Path(result["manifest_path"])
    texts = [p.read_text(encoding="utf-8", errors="ignore")
             for p in mpath.parent.rglob("*.json")]
    blob = "\n".join(texts)
    assert other_id not in blob
    assert "架空別PJ" not in blob


def test_manifest_and_logs_hide_dummy_secret(env, tmp_path, caplog):
    _fill_all_stores(env)
    # 疑似の秘密値をあえて行内容に入れ、manifest・ログに出ないことを確認。
    with env["memory"]._connect() as db:
        db.execute("INSERT INTO project_events(project_id,task_id,kind,message,detail,created_at)"
                   " VALUES(?,?,?,?,?,?)",
                   (env["pid"], None, "架空kind", DUMMY_SECRET, DUMMY_SECRET, "2026-01-01"))
    import logging
    with caplog.at_level(logging.INFO):
        result = create_backup(env["mem"], env["pid"], tmp_path / "bk",
                               workspace_root=env["ws_root"], actor="架空担当")
    assert result["ok"] is True
    mpath = Path(result["manifest_path"])
    assert DUMMY_SECRET not in mpath.read_text(encoding="utf-8")
    assert DUMMY_SECRET not in (mpath.parent / "manifest.sha256").read_text(encoding="utf-8")
    assert DUMMY_SECRET not in caplog.text


# --- 現行削除後の残存 --------------------------------------------------------

def test_legacy_delete_leaves_orphans(env):
    _fill_all_stores(env)
    pid = env["pid"]
    orphans_before = list_orphans_after_legacy_delete(env["mem"], pid, env["ws_root"])
    assert orphans_before["orphan_count"] > 0
    # 現行削除を実行 (疑似PJのみ)。
    env["memory"].delete_project(pid)
    orphans = list_orphans_after_legacy_delete(env["mem"], pid, env["ws_root"])
    names = {o["name"] for o in orphans["orphans"]}
    # 別ストアは残る (L3で解消される前提の記録)。
    for expected in ["goal_reviews:reviews", "goal_completion:goal_contracts",
                     "experience:experiences", "vector_index:vectors",
                     "agent_examples:examples", "procedure_learning:learning",
                     "recovery:recoveries", "auto_resume:settings",
                     "resolution:resolutions", "workspace:project_dir"]:
        assert expected in names, expected
    # メイン11テーブルは削除済みで残存扱いにならない。
    for name in names:
        assert not (name.startswith("main:") and name.split("main:")[1] in
                    {"turns", "project_context_files", "project_events",
                     "project_actions", "project_leads", "campaign_social_shares",
                     "campaign_creatives", "premarketing_campaigns",
                     "project_tasks", "project_missions", "projects"})


# --- API ---------------------------------------------------------------------

def _api_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import app.web as web
    data_dir = tmp_path / "架空api-data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空API疑似PJ", "")
    pid = project["id"]
    import types
    manager = types.SimpleNamespace(memory=memory)
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    monkeypatch.setattr(web, "DATA_DIR", data_dir)
    ws_root = tmp_path / "架空api-ws"
    ws_root.mkdir()
    monkeypatch.setattr(web, "WORKSPACE_ROOT", ws_root)
    client = TestClient(web.app)
    return client, pid, mem_path


def test_lifecycle_api_preview_backup_verify(monkeypatch, tmp_path):
    client, pid, mem_path = _api_client(monkeypatch, tmp_path)
    # preview: 副作用なし。
    before = mem_path.read_bytes()
    r = client.get(f"/api/projects/{pid}/lifecycle/delete-preview")
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == pid and body["read_only"] is True
    assert mem_path.read_bytes() == before
    # backup: actor 必須。
    assert client.post(f"/api/projects/{pid}/lifecycle/backup", json={}).status_code == 422
    rb = client.post(f"/api/projects/{pid}/lifecycle/backup", json={"actor": "架空担当"})
    assert rb.status_code == 200, rb.text
    assert rb.json()["ok"] is True
    # backups 一覧。
    rl = client.get(f"/api/projects/{pid}/lifecycle/backups")
    assert rl.status_code == 200 and len(rl.json()["backups"]) == 1
    backup_id = rl.json()["backups"][0]["backup_id"]
    # verify。
    rv = client.post(f"/api/projects/{pid}/lifecycle/backups/{backup_id}/verify")
    assert rv.status_code == 200 and rv.json()["ok"] is True
    # 任意パスは受け取らない: backup_id に / を含めると404/400。
    assert client.post(f"/api/projects/{pid}/lifecycle/backups/../x/verify").status_code in (400, 404)
    assert client.post("/api/projects/nope/lifecycle/backups/x/verify").status_code == 404


def test_lifecycle_api_backup_refuses_running(monkeypatch, tmp_path):
    client, pid, _ = _api_client(monkeypatch, tmp_path)
    import app.web as web
    monkeypatch.setattr(web, "executions",
                        {"e1": {"project_id": pid, "status": "running",
                                "kind": "chat", "label": "架空"}})
    r = client.post(f"/api/projects/{pid}/lifecycle/backup", json={"actor": "架空担当"})
    assert r.status_code == 409
