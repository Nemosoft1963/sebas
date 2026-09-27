import pytest

import app.web as web
from app.memory.short_term import ShortTermMemory


class WorkspaceStub:
    def __init__(self):
        self.operations = []

    def apply_operations(self, workspace_path, project_id, operations):
        self.operations.extend(operations)
        return operations


@pytest.mark.asyncio
async def test_recompose_preserves_public_site_and_creates_review_revision(tmp_path, monkeypatch):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("LP update")
    campaign = memory.create_campaign(
        project["id"], "相談LP", "担当者", "課題整理", "問い合わせる", "",
    )
    memory.update_campaign_site(
        project["id"], campaign["id"], "published",
        google_site_url="https://sites.google.com/view/existing",
        google_site_edit_url="https://sites.google.com/d/example/edit",
        site_publication_approved_at="2026-01-01T00:00:00+00:00",
    )
    workspace = WorkspaceStub()
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "workspace", workspace)

    async def build_stub(project_id, campaign_id, payload):
        return {"creative": {"status": "brief_ready"}}

    async def generate_stub(project_id, campaign_id):
        return {"creative": {"status": "reviewed"}}

    monkeypatch.setattr(web, "build_creative_package", build_stub)
    monkeypatch.setattr(web, "auto_generate_creative", generate_stub)
    result = await web.recompose_landing_page(
        project["id"], campaign["id"],
        web.LandingRecomposePayload(instruction="CTAまでを短くする", mode="local"),
    )

    updated = memory.get_campaign(project["id"], campaign["id"])
    assert updated["site_publication_status"] == "published"
    assert updated["google_site_url"] == "https://sites.google.com/view/existing"
    assert updated["landing_revision"] == 1
    assert updated["landing_revision_status"] == "review_ready"
    assert updated["site_publication_approved_at"] is None
    assert result["public_site_preserved"] is True
    assert result["human_approval_required"] is True
    assert workspace.operations[0]["path"].endswith("revisions/1/revision_request.json")
