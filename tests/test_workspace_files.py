from pathlib import Path

import pytest

from app.workspace_files import WorkspaceSandbox, normalize_workspace_path


def test_workspace_supports_safe_operations_and_backups(tmp_path):
    service = WorkspaceSandbox(tmp_path / "workspace")
    project_id = "project-1"
    configured = "projects/project-1"

    audit = service.apply_operations(configured, project_id, [
        {"action": "mkdir", "path": "output/reports"},
        {"action": "write_text", "path": "output/reports/result.md", "content": "初版"},
        {"action": "copy", "source": "output/reports/result.md", "path": "output/result-copy.md"},
        {"action": "move", "source": "output/result-copy.md", "path": "output/result-final.md"},
    ])
    assert [item["action"] for item in audit] == ["mkdir", "write_text", "copy", "move"]
    scope = tmp_path / "workspace" / configured
    assert (scope / "output/result-final.md").read_text(encoding="utf-8") == "初版"
    assert not (scope / "output/result-copy.md").exists()

    second = service.apply_operations(configured, project_id, [
        {"action": "write_text", "path": "output/reports/result.md", "content": "改訂"},
    ])
    assert second[0]["backup"]
    assert (scope / second[0]["backup"]).read_text(encoding="utf-8") == "初版"
    assert (scope / "output/reports/result.md").read_text(encoding="utf-8") == "改訂"

    with pytest.raises(ValueError, match="Allowed operations"):
        service.apply_operations(configured, project_id, [{"action": "delete", "path": "output/result-final.md"}])
    assert (scope / "output/result-final.md").exists()


@pytest.mark.parametrize("value", [
    "../escape", "projects/../../escape", "/etc/passwd", "C:/Windows/file",
    "D:\\private\\file", "\\\\server\\share\\file", "projects\\..\\..\\escape",
])
def test_workspace_rejects_escape_paths(tmp_path, value):
    service = WorkspaceSandbox(tmp_path / "workspace")
    with pytest.raises(ValueError):
        normalize_workspace_path(value)
    with pytest.raises(ValueError):
        service.project_path(value, "project-1")


def test_workspace_rejects_symlink_escape(tmp_path):
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    scope = root / "projects/project-1"
    scope.mkdir(parents=True)
    link = scope / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    service = WorkspaceSandbox(root)
    with pytest.raises(ValueError, match="escaped"):
        service.apply_operations("projects/project-1", "project-1", [
            {"action": "write_text", "path": "linked/escape.txt", "content": "blocked"},
        ])
    assert not (outside / "escape.txt").exists()


def test_workspace_snapshot_can_return_manifest_without_duplicate_contents(tmp_path):
    service = WorkspaceSandbox(tmp_path / "workspace")
    service.apply_operations("projects/project-1", "project-1", [
        {"action": "write_text", "path": "input/payroll.csv",
         "content": "PRIVATE_PAYROLL_VALUE"},
    ])

    manifest = service.snapshot(
        "projects/project-1", "project-1", include_contents=False,
    )
    full = service.snapshot(
        "projects/project-1", "project-1", include_contents=True,
    )

    assert "input/payroll.csv" in manifest
    assert "PRIVATE_PAYROLL_VALUE" not in manifest
    assert "PRIVATE_PAYROLL_VALUE" in full


def test_registered_original_is_stored_under_project_scope(tmp_path):
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    stored = workspace.store_original(
        "projects/vehicle", "project-id", "fuel/annual.xlsx", b"original"
    )
    assert stored["path"] == "originals/fuel/annual.xlsx"
    assert (tmp_path / "workspace/projects/vehicle/originals/fuel/annual.xlsx").read_bytes() == b"original"


def test_project_scoped_absolute_workspace_path_is_accepted(tmp_path):
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    audit = workspace.apply_operations("projects/vehicle", "project-id", [{
        "action": "write_text",
        "path": "/workspace/projects/vehicle/output/report.md",
        "content": "ok",
    }])
    assert audit[0]["path"] == "output/report.md"
    assert (tmp_path / "workspace/projects/vehicle/output/report.md").read_text() == "ok"


def test_rejected_ai_write_is_quarantined_and_previous_file_is_restored(tmp_path):
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    configured = "projects/review"
    project_id = "review"
    workspace.apply_operations(configured, project_id, [{
        "action": "write_text", "path": "result/report.md", "content": "承認済み",
    }])
    candidate = workspace.apply_operations(configured, project_id, [{
        "action": "write_text", "path": "result/report.md", "content": "不合格候補",
    }])

    rejected = workspace.reject_operations(configured, project_id, candidate)

    scope = tmp_path / "workspace" / configured
    assert (scope / "result/report.md").read_text(encoding="utf-8") == "承認済み"
    assert rejected[0]["status"] == "restored"
    rejected_path = scope / rejected[0]["rejected_path"]
    assert rejected_path.read_text(encoding="utf-8") == "不合格候補"
