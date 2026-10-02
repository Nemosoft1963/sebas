"""Publishing evidence retries must not rewrite provenance timestamps."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import web


@pytest.mark.asyncio
async def test_repeated_site_registration_keeps_original_evidence(monkeypatch):
    campaign = {
        "id": "c1", "project_id": "p1", "google_form_id": "form1",
        "site_publication_approved_at": "2026-10-01T00:00:00+00:00",
        "site_publication_status": "published",
        "google_site_url": "https://sites.google.com/view/example",
        "landing_asset_version": "v1", "published_asset_version": "v1",
        "published_at": "2026-10-01T01:00:00+00:00",
    }
    monkeypatch.setattr(web, "require_project", lambda _: {"id": "p1"})
    monkeypatch.setattr(web, "memory", SimpleNamespace(get_campaign=lambda *args: campaign))
    result = await web.register_google_site(
        "p1", "c1", web.GoogleSiteResultPayload(public_url=campaign["google_site_url"]))
    assert result["published_at"] == campaign["published_at"]
    with pytest.raises(HTTPException) as exc:
        await web.register_google_site(
            "p1", "c1", web.GoogleSiteResultPayload(
                public_url="https://sites.google.com/view/different"))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_uncertain_form_creation_cannot_be_reissued(monkeypatch):
    campaign = {
        "id": "c1", "project_id": "p1", "title": "test",
        "google_form_id": "", "publication_approved_at": "2026-10-01T00:00:00+00:00",
        "publication_status": "publishing_form", "publication_attempts": 1,
    }
    class Publisher:
        async def create_and_publish(self, _):
            raise AssertionError("remote form creation must not run twice")
    monkeypatch.setattr(web, "require_project", lambda _: {"id": "p1"})
    monkeypatch.setattr(web, "memory", SimpleNamespace(get_campaign=lambda *args: campaign))
    monkeypatch.setattr(web, "google_publisher", Publisher(), raising=False)
    with pytest.raises(HTTPException) as publish:
        await web.publish_google_form("p1", "c1")
    assert publish.value.status_code == 409
    with pytest.raises(HTTPException) as request:
        await web.request_google_publication("p1", "c1")
    assert request.value.status_code == 409


@pytest.mark.asyncio
async def test_known_form_id_is_checkpointed_and_resumed_without_recreate(tmp_path, monkeypatch):
    from app.google_premarketing import GooglePremarketingError
    from app.memory.short_term import ShortTermMemory

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("checkpoint")
    campaign = memory.create_campaign(project["id"], "相談", "対象", "価値", "CTA", "")
    memory.update_campaign_publication(
        project["id"], campaign["id"], "approved",
        publication_approved_at="2026-10-01T00:00:00+00:00",
    )
    calls = []
    class Publisher:
        async def create_and_publish(self, row, on_created):
            calls.append("create")
            on_created("form-1", "https://docs.google.com/forms/d/e/form-1/viewform")
            raise GooglePremarketingError("temporary failure")
        async def resume_existing(self, row):
            calls.append("resume")
            assert row["google_form_id"] == "form-1"
            return {"form_id": "form-1",
                    "responder_uri": "https://docs.google.com/forms/d/e/form-1/viewform",
                    "question_map": {"q1": "name"}}
    monkeypatch.setattr(web, "require_project", lambda _: {"id": project["id"]})
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "google_publisher", Publisher(), raising=False)
    with pytest.raises(HTTPException) as first:
        await web.publish_google_form(project["id"], campaign["id"])
    assert first.value.status_code == 502
    failed = memory.get_campaign(project["id"], campaign["id"])
    assert failed["google_form_id"] == "form-1"
    assert failed["publication_status"] == "failed"
    result = await web.publish_google_form(project["id"], campaign["id"])
    assert result["publication_status"] == "awaiting_site"
    assert calls == ["create", "resume"]
