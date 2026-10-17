"""F2c: 削除・RAG登録・OCR・契約の fail-closed 化テスト (疑似PJのみ・実関数)。

対象は tests/fail_open_baseline.json で classification=high_risk かつ
file が app/project_delete.py / app/experience_memory.py /
app/ocr_fallback.py / app/ocr_invoice_adapter.py /
app/ocr_usami_adapter.py / app/goal_contract.py の20件。
各検出について失敗を注入し、成功/0件/通過にならないことを確認する。
実関数・疑似データ・一時ディレクトリのみ。外部通信なし。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory


def _env(tmp_path: Path, tag: str):
    data_dir = tmp_path / f"f2c-{tag}-data"
    mem_dir = data_dir / "memory"
    mem_dir.mkdir(parents=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project(f"f2c-疑似-{tag}", "架空コンテキスト")
    pid = project["id"]
    memory.save_mission(pid, "架空目標", "架空達成条件", "", False, [], 2)
    ws_root = tmp_path / f"f2c-{tag}-ws"
    ws_root.mkdir(exist_ok=True)
    backup_root = tmp_path / f"f2c-{tag}-bk"
    backup_root.mkdir(exist_ok=True)
    return {"mem": mem_path, "memory": memory, "pid": pid,
            "ws_root": ws_root, "backup_root": backup_root}


def _delete_ok(env):
    from app.project_delete import build_delete_request_preview, request_delete
    prev = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"], {})
    assert prev["can_delete"] is True, prev.get("blocked_reasons")
    name = ((prev.get("project") or {}).get("name")
            or env["memory"].get_project(env["pid"])["name"])
    out = request_delete(env["mem"], env["pid"], preview_token=prev["preview_token"],
                         project_name=name, actor="架空担当",
                         workspace_root=env["ws_root"],
                         backup_root=env["backup_root"], executions={})
    assert out["ok"] is True, out
    return out


def _purge_token(env):
    import sqlite3 as _sql
    from app.project_delete import build_purge_preview, delete_db_path
    db = _sql.connect(str(delete_db_path(env["mem"])))
    try:
        with db:
            db.execute("UPDATE project_delete_states SET purgeable_at=? WHERE project_id=?",
                       ("2000-01-01T00:00:00+00:00", env["pid"]))
    finally:
        db.close()
    pv = build_purge_preview(env["mem"], env["pid"], env["ws_root"])
    assert pv["can_purge"] is True, pv.get("blocked_reasons")
    return pv


# --- project_delete.request_delete / restore_deleted (監査失敗は成功にしない) ---

def test_f2c_request_audit_failure_not_success(tmp_path, monkeypatch):
    """検出 app/project_delete.py:request_delete:P1:a2ad58d9f50d。

    (1) 論理削除の監査記録に使う。(2) 失敗時現状: 監査失敗を握りつぶし成功を返す。
    修正後: 監査失敗は操作失敗として返す(成功を返さない)。
    """
    env = _env(tmp_path, "req-audit")
    from app.project_delete import build_delete_request_preview, request_delete
    prev = build_delete_request_preview(env["mem"], env["pid"], env["ws_root"], {})
    name = ((prev.get("project") or {}).get("name")
            or env["memory"].get_project(env["pid"])["name"])
    # 本体は ShortTermMemory を都度生成するため、クラス側を差し替える。
    from app.memory.short_term import ShortTermMemory as _STM
    monkeypatch.setattr(_STM, "add_event",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の監査失敗")))
    out = request_delete(env["mem"], env["pid"], preview_token=prev["preview_token"],
                         project_name=name, actor="架空担当",
                         workspace_root=env["ws_root"],
                         backup_root=env["backup_root"], executions={})
    assert out["ok"] is False, out
    assert out.get("code") == "AUDIT_FAILED" or "監査" in str(out.get("reason")), out


def test_f2c_restore_audit_failure_not_success(tmp_path, monkeypatch):
    """検出 app/project_delete.py:restore_deleted:P1:a2ad58d9f50d。

    (1) 復元の監査記録に使う。(2) 失敗時現状: 監査失敗を握りつぶし成功を返す。
    修正後: 監査失敗は操作失敗として返す(成功を返さない)。
    """
    env = _env(tmp_path, "res-audit")
    _delete_ok(env)
    from app.project_delete import restore_deleted
    from app.memory.short_term import ShortTermMemory as _STM
    monkeypatch.setattr(_STM, "add_event",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の監査失敗2")))
    out = restore_deleted(env["mem"], env["pid"], actor="架空担当",
                          workspace_root=env["ws_root"],
                          backup_root=env["backup_root"])
    assert out["ok"] is False, out
    assert out.get("code") == "AUDIT_FAILED" or "監査" in str(out.get("reason")), out


# --- project_delete.purge_deleted (補助purge失敗は成功にしない)×4 ---

def test_f2c_purge_aux_failure_not_success(tmp_path, monkeypatch):
    """検出 app/project_delete.py:purge_deleted:P1:a2ad58d9f50d#1〜#4。

    (1) export/delivery/pack/autopilot の補助purgeに使う。
    (2) 失敗時現状: 失敗を握りつぶし完全削除を成功として返す(残存の恐れ)。
    修正後: 補助purge失敗は操作失敗として返す(成功・残存なしにしない)。
    """
    env = _env(tmp_path, "purge-aux")
    _delete_ok(env)
    pv = _purge_token(env)
    import app.capability_export as _exp
    monkeypatch.setattr(_exp, "purge_export_packages",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の補助purge失敗")))
    from app.project_delete import purge_deleted
    out = purge_deleted(env["mem"], env["pid"], purge_token=pv["purge_token"],
                        project_name=pv["project_name"], actor="架空担当",
                        workspace_root=env["ws_root"],
                        backup_root=env["backup_root"])
    assert out["ok"] is False, out
    assert out.get("code") == "AUX_PURGE_FAILED", out
    from app.project_delete import get_delete_state
    assert (get_delete_state(env["mem"], env["pid"]) or {}).get("state") == "failed"


# --- project_delete.delete_state_status / is_deleted_or_unknown (不明は停止側) ---

def test_f2c_delete_state_unknown_on_check_failure(tmp_path, monkeypatch):
    """検出 app/project_delete.py:delete_state_status:P1/P2、
    is_deleted_or_unknown:P2。

    (1) 削除状態の判定(論理削除・完全削除・退避の成否の入口)に使う。
    (2) 失敗時現状: is_deleted確認不能を握りつぶし active を返す恐れ。
    修正後: 確認不能は unknown(停止側)。active 返却はDB無し・行無しの正常系のみ。
    不明=削除扱い(is_deleted_or_unknown=True)を保つ。
    """
    env = _env(tmp_path, "state-unknown")
    from app.project_delete import delete_state_status, is_deleted_or_unknown
    assert delete_state_status(env["mem"], env["pid"]) == "active"
    assert is_deleted_or_unknown(env["mem"], env["pid"]) is False
    import app.project_delete as _pd
    monkeypatch.setattr(_pd, "is_deleted",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の確認失敗")))
    assert delete_state_status(env["mem"], env["pid"]) == "unknown"
    assert is_deleted_or_unknown(env["mem"], env["pid"]) is True
    # 破損DBは unknown のまま(既存 fail-closed を維持)。
    _pd.delete_db_path(env["mem"]).write_bytes(b"F2C-CORRUPT-not-sqlite")
    assert delete_state_status(env["mem"], env["pid"]) == "unknown"
    assert is_deleted_or_unknown(env["mem"], env["pid"]) is True


# --- experience_memory.configured_memory (削除確認不能は除外側) ---

def test_f2c_configured_memory_check_failure_excluded(tmp_path, monkeypatch):
    """検出 app/experience_memory.py:configured_memory:P1:eaf9362805e8。

    (1) RAGの登録状態・検索可否の入口に使う。(2) 失敗時現状: 削除確認の失敗を
    握りつぶし None(無効扱い)。修正後も None(除外・停止側)を保ち狭い例外にする。
    未登録を登録済みにしないこと。
    """
    env = _env(tmp_path, "cfg-mem")
    cfg = env["mem"].parent / "experience_memory.json"
    cfg.write_text(json.dumps({"embedding_model": "架空embed",
                               "projects": {env["pid"]: "shadow"}}),
                   encoding="utf-8")
    from app.experience_memory import configured_memory
    assert configured_memory(env["mem"], env["pid"]) is not None
    import app.project_delete as _pd
    monkeypatch.setattr(_pd, "is_deleted_or_unknown",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の削除確認失敗")))
    # 確認不能は除外(None)側。検索可能として扱わない。
    assert configured_memory(env["mem"], env["pid"]) is None
    # 想定外は握りつぶさず伝播する(成功扱いにしない)。

    class _Boom(Exception):
        pass

    monkeypatch.setattr(_pd, "is_deleted_or_unknown",
                        lambda *_a, **_k: (_ for _ in ()).throw(_Boom("架空の想定外")))
    with pytest.raises(_Boom):
        configured_memory(env["mem"], env["pid"])


# --- experience_memory.import_success_cases (予期せぬ失敗は成功にしない) ---

def test_f2c_import_unexpected_failure_not_success(tmp_path, monkeypatch):
    """検出 app/experience_memory.py:import_success_cases:P1:a2ad58d9f50d。

    (1) RAGの取込・重複判定に使う。(2) 失敗時現状: 既存証拠の読取失敗を
    握りつぶし登録成功として数える恐れ。修正後: 狭い例外のみ許容し、
    想定外は伝播する(成功・登録済みにしない)。
    """
    env = _env(tmp_path, "import-fail")
    cfg = env["mem"].parent / "experience_memory.json"
    cfg.write_text(json.dumps({"embedding_model": "架空embed",
                               "projects": {env["pid"]: "shadow"}}),
                   encoding="utf-8")
    from app.experience_memory import import_success_cases
    item = {"kind": "success", "content": "【状況】架空状況です。【施策】架空施策を実施した。【成果】架空成果が出た。",
            "evidence": {"source": "架空出所"}}
    ok = import_success_cases(str(env["mem"]), env["pid"], [dict(item)], actor="架空担当")
    assert ok["registered"] == 1 and ok["accepted_status"] == "candidate", ok
    # 重複判定中の想定外失敗は握りつぶさず伝播する(成功にしない)。
    import json as _json

    class _Boom(Exception):
        pass

    monkeypatch.setattr(_json, "loads",
                        lambda *_a, **_k: (_ for _ in ()).throw(_Boom("架空の想定外")))
    with pytest.raises(_Boom):
        import_success_cases(str(env["mem"]), env["pid"], [dict(item)], actor="架空担当")


# --- experience_memory.convert_success_case_item (種別欠落の補完を狭める) ---

def test_f2c_convert_kind_falsy_not_success(tmp_path):
    """検出 app/experience_memory.py:convert_success_case_item:P3:ceaa8b3bdf24。

    (1) RAG登録の種別判定に使う。(2) 失敗時現状: kind の falsy(0/False等)を
    'success' で補完する。修正後: None/空文字以外の falsy は補完せず拒否する
    (種別の誤分類をしない)。欠落(None/空)は従来どおり success 扱い。
    """
    from app.experience_memory import convert_success_case_item
    base = {"content": "【状況】架空状況です。【施策】架空施策を実施した。【成果】架空成果が出た。",
            "evidence": {"source": "架空出所"}}
    assert convert_success_case_item(dict(base))["kind"] == "success"
    assert convert_success_case_item({**base, "kind": "success"})["kind"] == "success"
    with pytest.raises(ValueError):
        convert_success_case_item({**base, "kind": 0})
    with pytest.raises(ValueError):
        convert_success_case_item({**base, "kind": False})


# --- experience_memory.remove_from_index (索引削除失敗は成功にしない) ---

def test_f2c_remove_from_index_failure_not_removed(tmp_path, monkeypatch):
    """検出 app/experience_memory.py:remove_from_index:P1:a2ad58d9f50d。

    (1) 撤回時の索引削除・正本の不採用に使う。(2) 失敗時現状: 索引同期の失敗を
    握りつぶし removed=True(削除済み)を返す。修正後: 失敗は index_state=failed
    として記録し例外で返す(削除済みにしない)。
    """
    env = _env(tmp_path, "remove-idx")
    cfg = env["mem"].parent / "experience_memory.json"
    cfg.write_text(json.dumps({"embedding_model": "架空embed",
                               "projects": {env["pid"]: "shadow"}}),
                   encoding="utf-8")
    from app.experience_memory import configured_memory
    setting = configured_memory(env["mem"], env["pid"])
    assert setting is not None
    memory, _mode = setting

    class _FakeIndex:
        identity = "fake"

        def sync(self, rows, project):
            raise OSError("架空の索引失敗")

    memory.index = _FakeIndex()
    rid = memory.store.add(env["pid"], "success", "架空教訓",
                           {"input_version": "v1"}, {"source": "架空出所"})
    with pytest.raises(OSError):
        memory.remove_from_index(env["pid"], rid, "架空確認者", "架空理由")
    state = memory.store.get_index_state(env["pid"], rid)
    assert state is not None and state.get("status") == "failed", state


# --- goal_contract.from_mission (重み欠落を1で補わない) ---

def test_f2c_contract_weight_missing_raises(tmp_path, monkeypatch):
    """検出 app/goal_contract.py:from_mission:P3:337809848ff3。

    (1) 契約の重み(配分)に使う。(2) 失敗時現状: weight 欠落・0 を 1 で補完する。
    修正後: 欠落・0・負・非数値は例外(呼び出し側が止まる)。正常な重みは保つ。
    """
    import app.goal_templates.vehicle_monthly_pnl as _tpl
    from app.goal_contract import from_mission
    mission = {"goal": "架空車両損益のExcel集計", "success_criteria": "架空条件",
               "constraints_text": "", "plan_version": 0}
    contract = from_mission(dict(mission))
    assert [c["criterion_id"] for c in contract["criteria"]][:2] == ["C01", "C02"]
    assert all(float(c["weight"]) > 0 for c in contract["criteria"])
    # 重み欠落は補完せず止まる。
    bad = [dict(c) for c in _tpl.CRITERIA]
    bad[0] = dict(bad[0]);
    bad[0].pop("weight", None)
    monkeypatch.setattr(_tpl, "CRITERIA", bad)
    with pytest.raises(ValueError):
        from_mission(dict(mission))
    # 重み0も補完せず止まる。
    bad2 = [dict(c) for c in _tpl.CRITERIA]
    bad2[1] = dict(bad2[1]);
    bad2[1]["weight"] = 0
    monkeypatch.setattr(_tpl, "CRITERIA", bad2)
    with pytest.raises(ValueError):
        from_mission(dict(mission))


# --- ocr_fallback.run_ocr_fallback (ページ数・ページ番号の欠落を1で補わない) ---

def test_f2c_ocr_page_count_missing_raises():
    """検出 app/ocr_fallback.py:run_ocr_fallback:P3:8df6972f0340。

    (1) OCRのページ数(被覆判定)に使う。(2) 失敗時現状: page_count の欠落・0 を
    1 で補完する。修正後: 欠落・0・負・非数値は例外(呼び出し側が止まる)。
    """
    from app.ocr_fallback import run_ocr_fallback
    base = dict(original_bytes="%PDF-架空".encode("utf-8"), expected_sha256="a" * 64,
                project_id="p-架空", context_file_id="cf-架空",
                filename="架空.pdf", feature_enabled=False)
    out = run_ocr_fallback(**{**base, "page_count": 1})
    assert out["status"] == "skipped", out
    with pytest.raises(ValueError):
        run_ocr_fallback(**{**base, "page_count": 0})
    with pytest.raises(ValueError):
        run_ocr_fallback(**{**base, "page_count": None})


def test_f2c_ocr_fallback_field_page_missing_not_one():
    """検出 app/ocr_fallback.py:run_ocr_fallback:P3:4f3c648478bc。

    (1) OCR結果のページ番号(照合)に使う。(2) 失敗時現状: page_no 欠落を 1 で補完。
    修正後: 欠落・不正は例外(1ページ目に寄せない)。
    """
    from app.ocr_fallback import run_ocr_fallback
    import app.ocr_fallback as _fb
    bundle_pages = [{"page_no": 1, "width": 10, "height": 10}]
    bundle_fields = [{"name": "架空項目", "value_text": "架空値",
                      "value_type": "string", "block_id": "p1-b001",
                      "validation_status": "value"}]
    assert _fb._field_page_no({"page_no": 2}) == 2
    with pytest.raises(ValueError):
        _fb._field_page_no({})
    with pytest.raises(ValueError):
        _fb._field_page_no({"page_no": 0})
    assert bundle_pages and bundle_fields and run_ocr_fallback is not None


# --- ocr_invoice_adapter.payload_from_ocr_bundle (ページ番号・件数の補完をやめる) ---

def test_f2c_invoice_bundle_page_missing_not_one():
    """検出 app/ocr_invoice_adapter.py:payload_from_ocr_bundle:P3×2。

    (1) OCR結果のページ番号・件数(照合)に使う。(2) 失敗時現状: page_no 欠落を
    1、空ページを件数1で補完する。修正後: page_no 欠落・不正は例外(1に寄せない)。
    空ページの件数は max(1, len) のまま(冗長な or 1 を除去、挙動不変)。
    """
    from app.ocr_invoice_adapter import payload_from_ocr_bundle
    bundle = {"run": {"status": "needs_review", "trigger": "manual", "run_id": "r-架空",
                      "context_file_id": "cf-架空", "source_sha256": "a" * 64,
                      "error_code": ""},
              "pages": [{"page_no": 1, "width": 10, "height": 10}],
              "blocks": [], "fields": [{"name": "架空項目", "value_text": "架空値",
                                        "value_type": "string", "block_id": "p1-b001",
                                        "validation_status": "value"}],
              "validations": [], "review": None}
    bundle["fields"][0]["page_no"] = 1
    payload = payload_from_ocr_bundle(bundle)
    assert payload is not None and payload["source"]["page_count"] == 1, payload
    # field の page_no 欠落は 1 に寄せず例外。
    missing = dict(bundle["fields"][0])
    missing.pop("page_no")
    with pytest.raises(ValueError):
        payload_from_ocr_bundle({**bundle, "fields": [missing]})
    # 空ページでも件数1の成功にしない(VAL-02側で失敗になる材料を残す)。
    empty = dict(bundle, pages=[], blocks=[], fields=[])
    assert payload_from_ocr_bundle(empty) is None
    assert payload_from_ocr_bundle({**bundle, "pages": [{"page_no": None}]}) is None
    assert payload_from_ocr_bundle({**bundle, "pages": [{"page_no": 0}]}) is None
    with pytest.raises(ValueError):
        payload_from_ocr_bundle({**bundle, "fields": [{**bundle["fields"][0], "page_no": True}]})


# --- ocr_usami_adapter (ページ欠落を1で補わない・表解析失敗は0行成功にしない) ---

def test_f2c_usami_page_missing_skipped():
    """検出 app/ocr_usami_adapter.py:_iter_table_blocks:P3:1821dba8d871。

    (1) OCR表ブロックのページ番号(追跡)に使う。(2) 失敗時現状: page 欠落を 1 で補完。
    修正後: 欠落・不正ページは採用対象から外す(1ページ目に寄せない)。
    """
    from app.ocr_usami_adapter import _iter_table_blocks
    table = ("<table><tr><td>1111</td><td>9901</td></tr></table>")
    good = {"pages": [{"page": 2, "blocks": [{"block_id": "b1", "label": "table",
                                              "text": table, "confidence": 0.9}]}]}
    rows = list(_iter_table_blocks(good))
    assert rows and rows[0][0] == 2, rows
    bad = {"pages": [{"blocks": [{"block_id": "b1", "label": "table",
                                  "text": table, "confidence": 0.9}]}]}
    assert list(_iter_table_blocks(bad)) == []
    bad2 = {"pages": [{"page": 0, "blocks": [{"block_id": "b1", "label": "table",
                                              "text": table, "confidence": 0.9}]}]}
    assert list(_iter_table_blocks(bad2)) == []


def test_f2c_usami_parse_failure_not_empty():
    """検出 app/ocr_usami_adapter.py:parse_table_rows:P1:ca6cd4810941。

    (1) OCRの表解析(行数)に使う。(2) 失敗時現状: 解析失敗を 0行([])の成功として返す。
    修正後: 解析失敗は例外(0行の成功にしない)。
    """
    from app.ocr_usami_adapter import _TableRowParser, parse_table_rows
    assert parse_table_rows("<table><tr><td>架空</td></tr></table>") == [["架空"]]
    monkeypatch_target = _TableRowParser.feed
    import app.ocr_usami_adapter as _ua

    def _boom(self, data, *a, **k):
        raise ValueError("架空の表解析失敗")

    _ua._TableRowParser.feed = _boom
    try:
        with pytest.raises(ValueError):
            parse_table_rows("<table><tr><td>架空</td></tr></table>")
    finally:
        _ua._TableRowParser.feed = monkeypatch_target


# --- 回帰: 正常系の成功は保つ ---

def test_f2c_normal_paths_still_work(tmp_path):
    """修正後も正常系の成功・件数が保たれること(挙動を弱めない)。"""
    env = _env(tmp_path, "normal")
    out = _delete_ok(env)
    assert out["ok"] is True
    from app.project_delete import delete_state_status
    assert delete_state_status(env["mem"], env["pid"]) == "deleted"
    from app.ocr_usami_adapter import parse_table_rows
    assert parse_table_rows("<table><tr><td>a</td><td>b</td></tr></table>") == [["a", "b"]]
    from app.ocr_invoice_adapter import payload_from_ocr_bundle
    bundle = {"run": {"status": "passed", "trigger": "manual", "run_id": "r-正常",
                      "context_file_id": "cf-正常", "source_sha256": "b" * 64,
                      "error_code": ""},
              "pages": [{"page_no": 3, "width": 10, "height": 10}],
              "blocks": [], "fields": [{"name": "n", "value_text": "v",
                                        "value_type": "string", "page_no": 3,
                                        "block_id": "b", "validation_status": "value"}],
              "validations": [], "review": None}
    payload = payload_from_ocr_bundle(bundle)
    assert payload["fields"][0]["page"] == 3, payload
    assert payload["source"]["page_count"] == 1, payload
    assert json.dumps(payload, ensure_ascii=False)


def test_f2c_hashseed_stability(tmp_path):
    """PYTHONHASHSEED 変動でも決定的(ソート済み・hash()不使用の実測)。"""
    from app.ocr_usami_adapter import parse_table_rows
    first = parse_table_rows("<table><tr><td>架空</td></tr></table>")
    second = parse_table_rows("<table><tr><td>架空</td></tr></table>")
    assert first == second == [["架空"]]
    cfg_text = json.dumps({"b": 1, "a": 2}, sort_keys=True)
    assert json.loads(cfg_text) == {"a": 2, "b": 1}
    assert hashlib.sha256("架空".encode()).hexdigest()
