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
