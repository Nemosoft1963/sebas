import hashlib
import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI

from app.system_state import (
    build_release_manifest, build_system_state, render_state_markdown,
    verify_release_manifest, write_release_manifest, write_system_state,
)


def fake_app():
    app = FastAPI()
    for path in (
        "/api/projects/{project_id}/next-action",
        "/api/projects/{project_id}/goal-state",
        "/api/projects/{project_id}/evidence/trace",
        "/api/projects/{project_id}/questions",
    ):
        app.get(path)(lambda: None)
    return app


def minimal_root(tmp_path):
    for name in ("app", "scripts", "tests", "docs"):
        (tmp_path / name).mkdir()
    (tmp_path / "app" / "vehicle_auto.py").write_text("REVISION='x'\n", encoding="utf-8")
    return tmp_path


def stable(value):
    copied = dict(value)
    copied.pop("generated_at", None)
    return copied


def test_ss01_required_json_and_markdown_from_json(tmp_path):
    root = minimal_root(tmp_path)
    state = build_system_state(root, app=fake_app())
    out = tmp_path / "out"
    json_path, md_path = write_system_state(out, state)
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert {"generated_at", "generator_version", "revisions", "git", "api", "feature_flags", "goal_completion_db", "tests", "verification", "safety_invariants", "docs"} <= loaded.keys()
    markdown = md_path.read_text(encoding="utf-8")
    assert markdown == render_state_markdown(loaded)
    assert "known_unverified" in markdown and "verified_in_production: `false`" in markdown


def test_ss02_unknown_and_explicit_verification_only(tmp_path):
    root = minimal_root(tmp_path)
    default = build_system_state(root, app=fake_app())
    explicit = build_system_state(root, tests_result="9 passed", verified_in_production=True, app=fake_app())
    assert default["tests"] == "unknown" and default["verification"]["verified_in_production"] is False
    assert explicit["tests"] == "9 passed" and explicit["verification"]["verified_in_production"] is True


def test_ss03_deterministic_except_timestamp(tmp_path):
    root = minimal_root(tmp_path)
    first = build_system_state(root, app=fake_app(), generated_at="one")
    second = build_system_state(root, app=fake_app(), generated_at="two")
    assert stable(first) == stable(second)
    assert json.dumps(first, sort_keys=True, ensure_ascii=False) != json.dumps(second, sort_keys=True, ensure_ascii=False)


def test_ss04_memory_files_read_only_and_absent_db_not_created(tmp_path):
    root = minimal_root(tmp_path)
    memory = tmp_path / "memory" / "conversations.db"
    memory.parent.mkdir()
    memory.write_bytes(b"memory")
    flag = memory.parent / "goal_completion.json"
    flag.write_text('{"enabled":false,"projects":{"p":true}}', encoding="utf-8")
    db_path = memory.parent / "goal_completion.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE human_acceptances(id INTEGER)")
        db.execute("CREATE TABLE goal_facts(id INTEGER)")
    before = {path: path.read_bytes() for path in (memory, flag, db_path)}
    state = build_system_state(root, memory_path=memory, app=fake_app())
    assert state["feature_flags"] == {"default": False, "projects": {"p": True}}
    assert state["goal_completion_db"]["counts"] == {"goal_facts": 0, "human_acceptances": 0}
    assert before == {path: path.read_bytes() for path in before}
    db_path.unlink()
    state = build_system_state(root, memory_path=memory, app=fake_app())
    assert state["goal_completion_db"] is None and not db_path.exists()


def test_ss05_real_api_goal_routes_sorted():
    from app.web import app
    api = build_system_state(Path(__file__).resolve().parents[1], app=app)["api"]
    routes = api["goal_completion_routes"]
    assert routes == sorted(routes, key=lambda item: (item["path"], item["method"]))
    paths = {item["path"] for item in routes}
    assert any("next-action" in path for path in paths)
    assert any("goal-state" in path for path in paths)
    assert any("evidence/trace" in path for path in paths)
    assert any("questions" in path for path in paths)


def test_ss06_manifest_exclusions_hash_and_determinism(tmp_path):
    root = minimal_root(tmp_path)
    target = root / "app" / "main.py"
    target.write_bytes(b"hello")
    (root / "app" / "__pycache__").mkdir()
    (root / "app" / "__pycache__" / "x.pyc").write_bytes(b"bad")
    (root / "docs" / "generated").mkdir()
    (root / "docs" / "generated" / "old.json").write_text("{}", encoding="utf-8")
    first = build_release_manifest(root, generated_at="one")
    second = build_release_manifest(root, generated_at="two")
    paths = [item["path"] for item in first["files"]]
    item = next(item for item in first["files"] if item["path"] == "app/main.py")
    assert not any("__pycache__" in path or "docs/generated" in path for path in paths)
    assert item["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert first["aggregate_sha256"] == second["aggregate_sha256"]
    assert first["deployed"] is False and first["counts"]["files"] == len(first["files"])


def test_ss07_verify_detects_each_difference_without_writes(tmp_path):
    root = minimal_root(tmp_path)
    a = root / "app" / "a.py"; b = root / "tests" / "b.py"
    a.write_text("a", encoding="utf-8"); b.write_text("b", encoding="utf-8")
    manifest_path = write_release_manifest(tmp_path / "manifest", build_release_manifest(root))
    manifest_before = manifest_path.read_bytes()
    assert verify_release_manifest(manifest_path, root)["ok"] is True
    a.write_text("changed", encoding="utf-8")
    b.unlink()
    extra = root / "scripts" / "extra.py"; extra.write_text("extra", encoding="utf-8")
    result = verify_release_manifest(manifest_path, root)
    assert result["ok"] is False
    assert "app/a.py" in result["mismatched"]
    assert "tests/b.py" in result["missing"]
    assert "scripts/extra.py" in result["extra"]
    assert manifest_path.read_bytes() == manifest_before


def test_ss08_generator_does_not_modify_protected_documents(tmp_path):
    root = minimal_root(tmp_path)
    protected = {}
    for name in ("MANIFEST.json", "VERSIONS.md", "BUILD_REPORT.md", "AGENTS.md"):
        path = root / name
        path.write_bytes((name + "\n").encode())
        protected[path] = path.read_bytes()
    out = root / "docs" / "generated"
    write_system_state(out, build_system_state(root, app=fake_app()))
    write_release_manifest(out, build_release_manifest(root))
    assert protected == {path: path.read_bytes() for path in protected}
