"""L1 回帰: 退避元に存在しなかったテーブルを含む復元.

一時ディレクトリの疑似PJ (架空データ) のみ。本番・実案件に触れない。
外部通信なし。秘密値なし。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from app.memory.short_term import ShortTermMemory
from app.project_lifecycle_backup import (
    create_backup,
    restore_backup,
    verify_backup,
)
from app.project_lifecycle_registry import count_all


def _make_minimal_env(tmp_path: Path, tag: str = "min"):
    data_dir = tmp_path / f"架空data-{tag}"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空最小PJ", "架空ctx")
    pid = project["id"]
    ws_root = tmp_path / f"架空ws-{tag}"
    ws_root.mkdir(parents=True, exist_ok=True)
    return {"data_dir": data_dir, "mem": mem_path, "memory": memory,
            "pid": pid, "ws_root": ws_root}


def _fill_minimal(env) -> dict:
    mem = env["mem"]
    pid = env["pid"]
    memory = env["memory"]
    body = b"dummy-original-bytes-min"
    sha = hashlib.sha256(body).hexdigest()
    ctx = memory.add_context_file(pid, "架空原本.md", "架空原本本文", 10, body,
                                  "text/markdown", "markdown", "", sha)
    memory.add_event(pid, "架空kind1", "架空msg1", None)
    memory.add_event(pid, "架空kind2", "架空msg2", None)
    # 遅延作成テーブルが本当に存在しないこと (未作成)。
    db = sqlite3.connect(mem)
    try:
        existing = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        db.close()
    for t in ("detailed_plans", "detailed_steps", "detailed_reviews",
              "detailed_budgets", "task_attempts", "execution_budgets",
              "source_versions"):
        assert t not in existing, f"前提崩れ: {t} が存在する"
    return {"ctx": ctx, "sha": sha}


def _entry_by_name(manifest: dict, name: str) -> dict:
    for e in manifest["entries"]:
        if e["name"] == name:
            return e
    raise AssertionError(f"entry not found: {name}")


def _rehash_manifest(backup_dir: Path) -> None:
    import hashlib as _hl
    mpath = backup_dir / "manifest.json"
    raw = mpath.read_bytes()
    (backup_dir / "manifest.sha256").write_text(
        _hl.sha256(raw).hexdigest() + "\n", encoding="utf-8")


def _copy_backup(src_dir: Path, dst_parent: Path, name: str) -> Path:
    dst = dst_parent / name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src_dir, dst)
    return dst


def test_absent_tables_restore_ok_and_counts_hash_match(tmp_path):
    env = _make_minimal_env(tmp_path, "a")
    filled = _fill_minimal(env)
    backup_root = tmp_path / "架空退避-a"
    res = create_backup(env["mem"], env["pid"], backup_root,
                        workspace_root=env["ws_root"], actor="架空担当")
    assert res["ok"] is True, res
    mpath = Path(res["manifest_path"])
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    # 要件1: 各対象に table_present/行数/(存在時)スキーマが記録される。
    absent_names = ["main:detailed_plans", "main:detailed_steps",
                    "main:detailed_reviews", "main:detailed_budgets",
                    "main:task_attempts", "main:execution_budgets",
                    "main:source_versions"]
    for name in absent_names:
        e = _entry_by_name(manifest, name)
        assert e["table_present"] is False, name
        assert e["count"] == 0, name
        assert e.get("schema", "") == "", name
    present = _entry_by_name(manifest, "main:projects")
    assert present["table_present"] is True
    assert present["count"] == 1
    assert present.get("schema", "").strip() != ""
    assert verify_backup(mpath)["ok"] is True
    # (a) 空の復元先への復元が ok=True。
    target_mem = tmp_path / "架空復元-a" / "memory" / "conversations.db"
    out = restore_backup(mpath, target_mem,
                         workspace_root=tmp_path / "架空復元ws-a")
    assert out["ok"] is True, out
    # (b) 件数・原本ハッシュが一致。
    src_counts = count_all(env["mem"], env["pid"], env["ws_root"])
    dst_counts = count_all(target_mem, out["project_id"],
                           tmp_path / "架空復元ws-a")
    assert src_counts == dst_counts
    mem2 = ShortTermMemory(target_mem)
    rows = mem2.list_context_files(out["project_id"])
    assert len(rows) == 1
    full = mem2.get_context_file(out["project_id"], rows[0]["id"])
    assert full.get("sha256") == filled["sha"]
    with mem2._connect() as db:
        n_ev = db.execute(
            "SELECT COUNT(*) FROM project_events WHERE project_id=?",
            (out["project_id"],)).fetchone()[0]
    assert int(n_ev) == 2


def test_tamper_present_to_absent_detected(tmp_path):
    env = _make_minimal_env(tmp_path, "c")
    _fill_minimal(env)
    backup_root = tmp_path / "架空退避-c"
    res = create_backup(env["mem"], env["pid"], backup_root,
                        workspace_root=env["ws_root"], actor="架空担当")
    assert res["ok"] is True
    src_dir = Path(res["backup_dir"])
    tampered = _copy_backup(src_dir, tmp_path, "改ざん-c")
    mpath = tampered / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    e = _entry_by_name(manifest, "main:projects")
    assert e["table_present"] is True and int(e["count"]) > 0
    # 「存在した」を「存在しなかった」に書き換え (manifest のみ改ざん)。
    e["table_present"] = False
    e["schema"] = ""
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2,
                                sort_keys=True) + "\n", encoding="utf-8")
    _rehash_manifest(tampered)
    # verify は通っても (ハッシュ再計算済み)、復元は失敗すること。
    assert verify_backup(mpath)["ok"] is True
    out = restore_backup(mpath, tmp_path / "架空復元-c" / "memory" / "conversations.db",
                         workspace_root=tmp_path / "架空復元ws-c")
    assert out["ok"] is False, out


def test_rows_without_schema_fails(tmp_path):
    env = _make_minimal_env(tmp_path, "d")
    _fill_minimal(env)
    backup_root = tmp_path / "架空退避-d"
    res = create_backup(env["mem"], env["pid"], backup_root,
                        workspace_root=env["ws_root"], actor="架空担当")
    assert res["ok"] is True
    src_dir = Path(res["backup_dir"])
    manifest = json.loads((src_dir / "manifest.json").read_text(encoding="utf-8"))
    target_entry = _entry_by_name(manifest, "main:projects")
    assert int(target_entry["count"]) > 0
    tampered = _copy_backup(src_dir, tmp_path, "改ざん-d")
    mpath = tampered / "manifest.json"
    tmanifest = json.loads(mpath.read_text(encoding="utf-8"))
    te = _entry_by_name(tmanifest, "main:projects")
    # ファイル側の schema を空にし、行は残す。
    fpath = tampered / te["file"]
    payload = json.loads(fpath.read_text(encoding="utf-8"))
    assert len(payload.get("rows", [])) > 0
    payload["schema"] = ""
    fdata = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    fpath.write_bytes(fdata)
    te["schema"] = ""
    te["sha256"] = hashlib.sha256(fdata).hexdigest()
    te["bytes"] = len(fdata)
    mpath.write_text(json.dumps(tmanifest, ensure_ascii=False, indent=2,
                                sort_keys=True) + "\n", encoding="utf-8")
    _rehash_manifest(tampered)
    assert verify_backup(mpath)["ok"] is True
    out = restore_backup(mpath, tmp_path / "架空復元-d" / "memory" / "conversations.db",
                         workspace_root=tmp_path / "架空復元ws-d")
    assert out["ok"] is False, out


def test_legacy_manifest_compat(tmp_path):
    env = _make_minimal_env(tmp_path, "o")
    filled = _fill_minimal(env)
    backup_root = tmp_path / "架空退避-o"
    res = create_backup(env["mem"], env["pid"], backup_root,
                        workspace_root=env["ws_root"], actor="架空担当")
    assert res["ok"] is True
    src_dir = Path(res["backup_dir"])
    # 旧形式化: table_present と manifest schema を除去。
    legacy = _copy_backup(src_dir, tmp_path, "旧形式-o")
    mpath = legacy / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    for e in manifest["entries"]:
        e.pop("table_present", None)
        e.pop("schema", None)
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2,
                                sort_keys=True) + "\n", encoding="utf-8")
    _rehash_manifest(legacy)
    assert verify_backup(mpath)["ok"] is True
    # 旧形式で 0行・スキーマ無しは成功扱い。
    out = restore_backup(mpath, tmp_path / "架空復元-o" / "memory" / "conversations.db",
                         workspace_root=tmp_path / "架空復元ws-o")
    assert out["ok"] is True, out
    mem2 = ShortTermMemory(tmp_path / "架空復元-o" / "memory" / "conversations.db")
    rows = mem2.list_context_files(out["project_id"])
    assert len(rows) == 1
    full = mem2.get_context_file(out["project_id"], rows[0]["id"])
    assert full.get("sha256") == filled["sha"]
    # 旧形式で行あり・スキーマ無しは失敗。
    legacy2 = _copy_backup(src_dir, tmp_path, "旧形式-o2")
    mpath2 = legacy2 / "manifest.json"
    manifest2 = json.loads(mpath2.read_text(encoding="utf-8"))
    for e in manifest2["entries"]:
        e.pop("table_present", None)
        e.pop("schema", None)
    mpath2.write_text(json.dumps(manifest2, ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n", encoding="utf-8")
    # main:projects のファイル側 schema を空に (行は残す) しハッシュ更新。
    te = _entry_by_name(manifest2, "main:projects")
    fpath = legacy2 / te["file"]
    payload = json.loads(fpath.read_text(encoding="utf-8"))
    assert len(payload.get("rows", [])) > 0
    payload["schema"] = ""
    payload.pop("table_present", None)
    fdata = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    fpath.write_bytes(fdata)
    manifest2b = json.loads(mpath2.read_text(encoding="utf-8"))
    te2 = _entry_by_name(manifest2b, "main:projects")
    te2["sha256"] = hashlib.sha256(fdata).hexdigest()
    te2["bytes"] = len(fdata)
    mpath2.write_text(json.dumps(manifest2b, ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n", encoding="utf-8")
    _rehash_manifest(legacy2)
    assert verify_backup(mpath2)["ok"] is True
    out2 = restore_backup(mpath2, tmp_path / "架空復元-o2" / "memory" / "conversations.db",
                          workspace_root=tmp_path / "架空復元ws-o2")
    assert out2["ok"] is False, out2


def test_rerestore_idempotent_content(tmp_path):
    env = _make_minimal_env(tmp_path, "e")
    filled = _fill_minimal(env)
    backup_root = tmp_path / "架空退避-e"
    res = create_backup(env["mem"], env["pid"], backup_root,
                        workspace_root=env["ws_root"], actor="架空担当")
    assert res["ok"] is True
    mpath = Path(res["manifest_path"])
    ws_a = tmp_path / "架空復元ws-e1"
    ws_b = tmp_path / "架空復元ws-e2"
    mem_a = tmp_path / "架空復元-e1" / "memory" / "conversations.db"
    mem_b = tmp_path / "架空復元-e2" / "memory" / "conversations.db"
    out_a = restore_backup(mpath, mem_a, workspace_root=ws_a)
    out_b = restore_backup(mpath, mem_b, workspace_root=ws_b)
    assert out_a["ok"] is True, out_a
    assert out_b["ok"] is True, out_b
    counts_a = count_all(mem_a, out_a["project_id"], ws_a)
    counts_b = count_all(mem_b, out_b["project_id"], ws_b)
    assert counts_a == counts_b
    for mem_p, ws_p, out in ((mem_a, ws_a, out_a), (mem_b, ws_b, out_b)):
        mem = ShortTermMemory(mem_p)
        rows = mem.list_context_files(out["project_id"])
        assert len(rows) == 1
        full = mem.get_context_file(out["project_id"], rows[0]["id"])
        assert full.get("sha256") == filled["sha"]
    assert counts_a == count_all(env["mem"], env["pid"], env["ws_root"])
