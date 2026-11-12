"""A3 問い合わせ先の登録台帳接続の受入試験。疑似PJ・実関数・フェイクLLM。外部通信なし。

- 登録台帳に fictional の aozora@example.invalid を登録(internal_demo)。
  案内文にあれば PASS、メール消去/偽値/チェックリストのみは FAIL。
- 同じ fictional 値は public では使えない。registered は要確認、verified は PASS。
- 台帳空は登録コンテキストへフォールバック。読取失敗はフォールバックせず判定不能。
- 複数登録のいずれか1つで PASS(全部不要)。URL未登録の『Webフォーム優先』は入力待ち。
- expected に contact_route_id・版・ハッシュを保存する。
- 登録→項目→検証→台帳→最終検証書まで保存結果を検査する(フェイクLLM)。
"""
from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import sqlite3

from app.contact_registry import (
    RegistryReadError,
    register_contact_route,
    verify_contact_route,
)
from app.memory.short_term import ShortTermMemory
from app.requirement_matrix import (
    configure_contact_registry,
    extract_requirement_items,
    resolve_contact_values,
    set_contact_registry_provider,
    set_contact_request_context,
)
from app.requirement_verification import verify_requirement_items

CRITERION = (
    "案内文1ファイルとチェックリスト1ファイル、合計2ファイルを作る。"
    "案内文には日時・架空の問い合わせ先を含め、"
    "チェックリストには準備・終了後確認を含める。"
)
REGISTERED = (
    "開催日: 2026年11月14日 開始時刻10:00 会場: 架空ホール "
    "連絡先: aozora@example.invalid"
)


def _outputs():
    return [
        {"path": "result/sc01.md", "artifact_category": "document",
         "required_headings": ["日時・場所", "問い合わせ先"]},
        {"path": "result/sc01_checklist.md", "artifact_category": "checklist",
         "required_headings": ["準備", "終了後確認"]},
    ]


def _bind(project_id: str, registered=REGISTERED):
    return extract_requirement_items(
        "SC01", CRITERION, _outputs(), [], registered_text=registered)


def _contact_items(project_id: str, registered=REGISTERED):
    rows = _bind(project_id, registered)
    contact = [row for row in rows
               if str((row.get("expected") or {}).get("source_fact_id") or "") == "contact"]
    # 単独分解の未生成時(抽出直後の旧互換)は keyword で拾う。
    if not contact:
        registered_values = ["aozora@example.invalid"]
        if registered != REGISTERED:
            registered_values.append(str(registered))
        contact = [row for row in rows
                   if any(value in list(
                       (row.get("expected") or {}).get("keywords") or [])
                          for value in registered_values)]
    return contact


def _verify(items, files: dict, root):
    contract = {"criterion_ids": ["SC01"], "requirement_matrix": items,
                "outputs": [{"path": "result/sc01.md"},
                            {"path": "result/sc01_checklist.md"}]}
    task = {"task_key": "SC01",
            "acceptance_criteria": json.dumps(contract, ensure_ascii=False)}

    def resolve(relative: str):
        return root / relative

    return verify_requirement_items(task, resolve, semantic_judge=None)


def _write(root, files: dict):
    (root / "result").mkdir(parents=True, exist_ok=True)
    for path, body in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def _setup_registry(tmp_path, pid: str, kind: str, value: str,
                     source_kind: str, route_id: str = "架空route"):
    mem_dir = tmp_path / "架空data" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空疑似PJ", "架空コンテキスト")
    real_pid = project["id"]
    saved = register_contact_route(
        mem_path, project_id=real_pid, type=kind, public_value=value,
        source_kind=source_kind, actor="架空担当", route_id=route_id)
    return mem_path, real_pid, saved


def test_registry_fictional_internal_demo_pass_and_negatives(tmp_path):
    mem_path, pid, saved = _setup_registry(
        tmp_path, "pj", "email", "aozora@example.invalid", "fictional")
    configure_contact_registry(mem_path, default_purpose="internal_demo")
    set_contact_request_context(project_id=pid, purpose="internal_demo")
    try:
        resolved = resolve_contact_values(pid, REGISTERED)
        assert [entry["value"] for entry in resolved] == ["aozora@example.invalid"]
        assert resolved[0]["contact_route_id"] == saved["route_id"]
        assert resolved[0]["version"] == saved["version"]
        assert resolved[0]["value_sha256"] == hashlib.sha256(
            "aozora@example.invalid".encode("utf-8")).hexdigest()
        set_contact_request_context(project_id=pid, purpose="internal_demo")
        items = _contact_items(pid)
        assert items, "連絡先項目が無い"
        row = items[0]
        assert row["method"] == "phrase"
        assert (row.get("expected") or {}).get("match") == "any"
        assert (row.get("expected") or {}).get("contact_route_id") == saved["route_id"]
        assert (row.get("expected") or {}).get("contact_version") == saved["version"]
        assert (row.get("expected") or {}).get("contact_value_sha256") == hashlib.sha256(
            "aozora@example.invalid".encode("utf-8")).hexdigest()
        good = {
            "result/sc01.md": (
                "# 案内\n2026年11月14日\n## 日時・場所\n日時と場所。\n"
                "## 問い合わせ先\naozora@example.invalid へ連絡。\n"),
            "result/sc01_checklist.md": (
                "# 準備\n準備する。\n## 準備\n準備。\n## 終了後確認\n終了後確認。\n"),
        }
        root = tmp_path / "ws-good"
        _write(root, good)
        rows = _verify(items, good, root)
        contact = [r for r in rows
                   if "aozora@example.invalid" in str(
                       (r.get("expected") or {}).get("keywords") or [])]
        assert contact and all(r["result"] == "PASS" for r in contact), rows
        # メール消去は FAIL。
        bad = dict(good)
        bad["result/sc01.md"] = good["result/sc01.md"].replace(
            "aozora@example.invalid", "連絡先は未定")
        root2 = tmp_path / "ws-bad"
        _write(root2, bad)
        rows2 = _verify(items, bad, root2)
        contact2 = [r for r in rows2
                    if "aozora@example.invalid" in str(
                        (r.get("expected") or {}).get("keywords") or [])]
        assert contact2 and all(r["result"] == "FAIL" for r in contact2), rows2
        # 偽値は FAIL。
        fake = dict(good)
        fake["result/sc01.md"] = good["result/sc01.md"].replace(
            "aozora@example.invalid", "fake@example.com")
        root3 = tmp_path / "ws-fake"
        _write(root3, fake)
        rows3 = _verify(items, fake, root3)
        contact3 = [r for r in rows3
                    if "aozora@example.invalid" in str(
                        (r.get("expected") or {}).get("keywords") or [])]
        assert contact3 and all(r["result"] == "FAIL" for r in contact3), rows3
        # チェックリストのみは FAIL(対象成果物に無い)。
        other = dict(good)
        other["result/sc01.md"] = good["result/sc01.md"].replace(
            "aozora@example.invalid", "連絡先は未定")
        other["result/sc01_checklist.md"] = (
            good["result/sc01_checklist.md"] + "\naozora@example.invalid\n")
        root4 = tmp_path / "ws-other"
        _write(root4, other)
        rows4 = _verify(items, other, root4)
        contact4 = [r for r in rows4
                    if "aozora@example.invalid" in str(
                        (r.get("expected") or {}).get("keywords") or [])]
        assert contact4 and all(r["result"] == "FAIL" for r in contact4), rows4
    finally:
        set_contact_registry_provider(None)
        set_contact_request_context(project_id="", purpose=None)


def test_registry_public_purpose_gate(tmp_path):
    mem_path, pid, saved = _setup_registry(
        tmp_path, "pj", "email", "aozora@example.invalid", "fictional")
    configure_contact_registry(mem_path, default_purpose="public")
    set_contact_request_context(project_id=pid, purpose="public")
    try:
        # fictional は public で使えない。input_missing になり理由に架空不可が出る。
        items = _contact_items(pid)
        missing = [row for row in items if row["method"] == "input_missing"]
        assert missing, items
        assert "架空値" in str((missing[0].get("expected") or {}).get("reason") or "")
        # registered は要確認で PASS にならない。
        verified_path = tmp_path / "架空data2" / "memory" / "conversations.db"
        verified_path.parent.mkdir(parents=True, exist_ok=True)
        memory2 = ShortTermMemory(verified_path)
        project2 = memory2.create_project("架空疑似PJ2", "")
        pid2 = project2["id"]
        route = register_contact_route(
            verified_path, project_id=pid2, type="email",
            public_value="info-registered@example.com",
            source_kind="registered", actor="架空担当", route_id="架空reg")
        configure_contact_registry(verified_path, default_purpose="public")
        set_contact_request_context(project_id=pid2, purpose="public")
        items2 = _contact_items(pid2)
        missing2 = [row for row in items2 if row["method"] == "input_missing"]
        assert missing2 and "要確認" in str(
            (missing2[0].get("expected") or {}).get("reason") or ""), items2
        # verified は public で使える。
        confirmed = verify_contact_route(
            verified_path, pid2, route["route_id"],
            actor="架空確認者", memo="架空根拠で確認")
        configure_contact_registry(verified_path, default_purpose="public")
        set_contact_request_context(project_id=pid2, purpose="public")
        items3 = _contact_items(pid2, registered="info-registered@example.com")
        assert items3 and items3[0]["method"] == "phrase", items3
        expected = items3[0].get("expected") or {}
        assert expected.get("contact_route_id") == confirmed["route_id"]
        assert expected.get("contact_version") == confirmed["version"]
    finally:
        set_contact_registry_provider(None)
        set_contact_request_context(project_id="", purpose=None)


def test_registry_non_registry_errors_do_not_fallback(tmp_path):
    for error in (sqlite3.OperationalError("broken"), TypeError("broken")):
        set_contact_registry_provider(lambda _pid, error=error: (_ for _ in ()).throw(error))
        set_contact_request_context(project_id="別PJ", purpose="internal_demo")
        try:
            items = _contact_items("別PJ")
            missing = [row for row in items if row["method"] == "input_missing"]
            assert missing, items
            assert "読めない" in str((missing[0].get("expected") or {}).get("reason") or "")
        finally:
            set_contact_registry_provider(None)
            set_contact_request_context(project_id="", purpose=None)


def test_structured_plan_exception_always_resets_contact_context(monkeypatch):
    from app import requirement_matrix
    from app.project_manager import ProjectOrchestrator

    class BrokenMemory:
        path = "unused.db"

        def get_mission(self, _project_id):
            raise RuntimeError("planned failure")

    orchestrator = ProjectOrchestrator(
        BrokenMemory(), None, lambda _project: ("", []), None, lambda: [])
    monkeypatch.setattr(requirement_matrix, "configure_contact_registry", lambda *_a, **_k: None)
    assert requirement_matrix._CONTACT_REQUEST_CONTEXT.get()["project_id"] == ""
    try:
        asyncio.run(orchestrator._generate_structured_plan("例外PJ", ["条件"]))
    except RuntimeError as exc:
        assert str(exc) == "planned failure"
    else:
        raise AssertionError("例外経路を通っていない")
    assert requirement_matrix._CONTACT_REQUEST_CONTEXT.get()["project_id"] == ""


def test_contact_context_does_not_leak_after_reset(tmp_path):
    mem_path, pid, _saved = _setup_registry(
        tmp_path, "pj", "email", "old-project@example.invalid", "fictional")
    configure_contact_registry(mem_path, default_purpose="internal_demo")
    token = set_contact_request_context(project_id=pid, purpose="internal_demo")
    from app.requirement_matrix import reset_contact_request_context
    reset_contact_request_context(token)
    rows = extract_requirement_items(
        "SC01", CRITERION, _outputs(), [], registered_text=REGISTERED)
    keywords = [keyword for row in rows
                for keyword in list((row.get("expected") or {}).get("keywords") or [])]
    assert "old-project@example.invalid" not in keywords
    assert "aozora@example.invalid" in keywords
    set_contact_registry_provider(None)


def test_usable_for_import_failure_is_unreadable(tmp_path, monkeypatch):
    mem_path, pid, _saved = _setup_registry(
        tmp_path, "pj", "email", "aozora@example.invalid", "fictional")
    configure_contact_registry(mem_path, default_purpose="internal_demo")
    set_contact_request_context(project_id=pid, purpose="internal_demo")
    original_import = builtins.__import__

    def broken_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "app.contact_registry" and "usable_for" in tuple(fromlist or ()):
            raise ImportError("policy unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    try:
        items = _contact_items(pid)
        missing = [row for row in items if row["method"] == "input_missing"]
        assert missing, items
        assert "読めない" in str((missing[0].get("expected") or {}).get("reason") or "")
    finally:
        set_contact_registry_provider(None)
        set_contact_request_context(project_id="", purpose=None)


def test_registry_empty_fallback_and_read_failure(tmp_path):
    mem_dir = tmp_path / "空data" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空疑似PJ", "")
    pid = project["id"]
    configure_contact_registry(mem_path)
    set_contact_request_context(project_id=pid, purpose="internal_demo")
    try:
        # 台帳が空なら登録コンテキストへフォールバックして従来どおり検証される。
        items = _contact_items(pid)
        assert items and items[0]["method"] == "phrase"
        assert (items[0].get("expected") or {}).get("match") == "any"

        def _failing(_pid: str = ""):
            raise RegistryReadError("row_unreadable", path_kind="relative",
                                    sqlite_error="OperationalError")

        set_contact_registry_provider(_failing)
        set_contact_request_context(project_id=pid, purpose="internal_demo")
        items = _contact_items(pid)
        missing = [row for row in items if row["method"] == "input_missing"]
        assert missing and "読めない" in str(
            (missing[0].get("expected") or {}).get("reason") or ""), items
    finally:
        set_contact_registry_provider(None)
        set_contact_request_context(project_id="", purpose=None)


def test_registry_multiple_values_any_and_form_policy_input_missing(tmp_path):
    mem_dir = tmp_path / "複数data" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem_path = mem_dir / "conversations.db"
    memory = ShortTermMemory(mem_path)
    project = memory.create_project("架空疑似PJ", "")
    pid = project["id"]
    register_contact_route(
        mem_path, project_id=pid, type="email",
        public_value="aozora@example.invalid",
        source_kind="fictional", actor="架空担当", route_id="架空mail")
    register_contact_route(
        mem_path, project_id=pid, type="phone",
        public_value="03-0000-0000",
        source_kind="fictional", actor="架空担当", route_id="架空phone")
    register_contact_route(
        mem_path, project_id=pid, type="form",
        public_value="https://contact.example.invalid/窓口",
        source_kind="fictional", actor="架空担当", route_id="架空form")
    configure_contact_registry(mem_path, default_purpose="internal_demo")
    set_contact_request_context(project_id=pid, purpose="internal_demo")
    try:
        items = _contact_items(pid)
        assert items and items[0]["method"] == "phrase"
        assert (items[0].get("expected") or {}).get("match") == "any"
        values = list((items[0].get("expected") or {}).get("keywords") or [])
        assert len(values) == 3, values
        # いずれか1つで PASS(全部不要)。
        one = {
            "result/sc01.md": (
                "# 案内\n2026年11月14日\n## 日時・場所\n日時。\n"
                "## 問い合わせ先\n03-0000-0000 へ連絡。\n"),
            "result/sc01_checklist.md": (
                "# 準備\n準備。\n## 準備\n準備。\n## 終了後確認\n確認。\n"),
        }
        root = tmp_path / "ws-one"
        _write(root, one)
        rows = _verify(items, one, root)
        assert rows and all(r["result"] == "PASS" for r in rows
                            if str((r.get("expected") or {}).get("source_fact_id")
                                   or "") == "contact"), rows
        # 『Webフォーム優先』でURL未登録なら URL を発明せず入力待ち。
        mem_dir2 = tmp_path / "方針data" / "memory"
        mem_dir2.mkdir(parents=True, exist_ok=True)
        mem_path2 = mem_dir2 / "conversations.db"
        memory2 = ShortTermMemory(mem_path2)
        project2 = memory2.create_project("架空疑似PJ", "")
        pid2 = project2["id"]
        register_contact_route(
            mem_path2, project_id=pid2, type="email",
            public_value="aozora@example.invalid",
            source_kind="fictional", actor="架空担当", route_id="架空mail2")
        configure_contact_registry(mem_path2, default_purpose="internal_demo")
        set_contact_request_context(
            project_id=pid2, purpose="internal_demo", selected_kinds=["form"])
        try:
            items2 = extract_requirement_items(
                "SC01",
                "案内文にはWebフォーム優先で問い合わせ先を含め。",
                _outputs(), [], registered_text="案内を作成する")
            missing = [row for row in items2 if row["method"] == "input_missing"]
            assert missing and "フォーム" in str(
                missing[0].get("requirement") or ""), items2
            for row in items2:
                for keyword in list((row.get("expected") or {}).get("keywords") or []):
                    assert "http" not in str(keyword)
        finally:
            set_contact_request_context(project_id=pid, purpose="internal_demo")
    finally:
        set_contact_registry_provider(None)
        set_contact_request_context(project_id="", purpose=None)
