import json

import httpx
import pytest

from app.google_premarketing import (
    CONSENT_VALUE, GoogleFormsPublisher, GooglePremarketingConfig,
    sync_campaign_responses,
)
from app.memory.short_term import ShortTermMemory


@pytest.mark.asyncio
async def test_google_form_is_created_only_after_publisher_is_called():
    calls = []
    items = []
    published = {"isPublished": False, "isAcceptingResponses": False}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.url.host == "oauth.test":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        if request.method == "POST" and str(request.url).endswith("/forms?unpublished=true"):
            return httpx.Response(200, json={"formId": "form-1"})
        if request.method == "GET":
            return httpx.Response(200, json={
                "formId": "form-1", "info": {"title": "相談"},
                "responderUri": "https://docs.google.com/forms/d/e/form-1/viewform",
                "items": list(items), "publishSettings": {"publishState": dict(published)},
            })
        if str(request.url).endswith(":batchUpdate"):
            for entry in json.loads(request.content)["requests"]:
                if "createItem" in entry:
                    title = entry["createItem"]["item"]["title"]
                    items.append({"title": title, "questionItem": {
                        "question": {"questionId": "q-" + str(len(items))}
                    }})
            return httpx.Response(200, json={})
        if str(request.url).endswith(":setPublishSettings"):
            published.update(isPublished=True, isAcceptingResponses=True)
            return httpx.Response(200, json={})
        raise AssertionError(str(request.url))

    config = GooglePremarketingConfig(
        "client", "secret", "refresh", "marketing@example.test",
        forms_base_url="https://forms.test/v1", token_url="https://oauth.test/token",
    )
    checkpoint = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = GoogleFormsPublisher(config, client)
        result = await publisher.create_and_publish({
            "title": "相談", "audience": "DX担当者", "offer": "30分診断",
        }, on_created=lambda form_id, uri: checkpoint.append(form_id))

    assert result["form_id"] == "form-1"
    assert checkpoint == ["form-1"]
    assert "name" in result["question_map"].values()
    assert any(url.endswith("form-1:batchUpdate") for _, url in calls)
    assert any(url.endswith("form-1:setPublishSettings") for _, url in calls)


class FakePublisher:
    async def list_responses(self, form_id, since=""):
        assert form_id == "form-1"
        return [{
            "responseId": "response-1",
            "lastSubmittedTime": "2026-09-07T01:02:03Z",
            "answers": {
                "q-name": {"textAnswers": {"answers": [{"value": "山田"}]}},
                "q-email": {"textAnswers": {"answers": [{"value": "lead@example.test"}]}},
                "q-company": {"textAnswers": {"answers": [{"value": "Example株式会社"}]}},
                "q-role": {"textAnswers": {"answers": [{"value": "DX責任者"}]}},
                "q-problem": {"textAnswers": {"answers": [{"value": "機密資料を外部へ出さずに分析し、現場業務を改善する方法を検討しています。"}]}},
                "q-timeline": {"textAnswers": {"answers": [{"value": "1か月以内"}]}},
                "q-budget": {"textAnswers": {"answers": [{"value": "100万円程度"}]}},
                "q-consent": {"textAnswers": {"answers": [{"value": CONSENT_VALUE}]}},
            },
        }]


@pytest.mark.asyncio
async def test_google_response_sync_is_idempotent_and_queues_approval(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("google lead loop")
    campaign = memory.create_campaign(
        project["id"], "ローカルAI相談", "DX担当者", "30分診断", "相談予約", "",
    )
    mapping = {
        "q-name": "name", "q-email": "email", "q-company": "company",
        "q-role": "role", "q-problem": "problem", "q-timeline": "timeline",
        "q-budget": "budget", "q-consent": "consent",
    }
    campaign = memory.update_campaign_publication(
        project["id"], campaign["id"], "awaiting_site",
        google_form_id="form-1", google_question_map=json.dumps(mapping),
    )

    first = await sync_campaign_responses(memory, campaign, FakePublisher())
    second = await sync_campaign_responses(memory, campaign, FakePublisher())

    assert first["imported"] == 1
    assert second["duplicates"] == 1
    assert len(memory.list_leads(project["id"])) == 1
    assert memory.list_leads(project["id"])[0]["source"] == "google_forms"
    assert len(memory.list_actions(project["id"])) == 1
    assert memory.list_actions(project["id"])[0]["status"] == "pending_approval"


def test_campaign_publication_state_is_persisted(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("publication")
    campaign = memory.create_campaign(project["id"], "相談", "対象", "価値", "CTA", "")
    updated = memory.update_campaign_publication(
        project["id"], campaign["id"], "awaiting_approval",
        publication_attempts=0,
    )
    assert updated["publication_status"] == "awaiting_approval"
    assert memory.get_campaign(project["id"], campaign["id"])["id"] == campaign["id"]


def test_form_creation_claim_is_single_use(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("claim")
    campaign = memory.create_campaign(project["id"], "相談", "対象", "価値", "CTA", "")
    memory.update_campaign_publication(
        project["id"], campaign["id"], "approved",
        publication_approved_at="2026-10-01T00:00:00+00:00",
    )
    claimed = memory.claim_form_publication(project["id"], campaign["id"])
    assert claimed["publication_status"] == "publishing_form"
    assert claimed["publication_attempts"] == 1
    with pytest.raises(ValueError, match="承認済み"):
        memory.claim_form_publication(project["id"], campaign["id"])
    assert memory.get_campaign(project["id"], campaign["id"])["publication_attempts"] == 1


@pytest.mark.asyncio
async def test_resume_existing_form_reuses_id_and_adds_only_missing_questions():
    from app.google_premarketing import FORM_FIELDS, CONSENT_TITLE
    calls = []
    items = [{
        "title": FORM_FIELDS[0][1],
        "questionItem": {"question": {"questionId": "q-existing"}},
    }]
    published = {"isPublished": False, "isAcceptingResponses": False}
    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.url.host == "oauth.test":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        if request.method == "GET":
            return httpx.Response(200, json={
                "formId": "form-1", "info": {"title": "相談"},
                "responderUri": "https://docs.google.com/forms/d/e/form-1/viewform",
                "items": list(items), "publishSettings": {"publishState": dict(published)},
            })
        if str(request.url).endswith(":batchUpdate"):
            body = json.loads(request.content)
            for entry in body["requests"]:
                if "createItem" in entry:
                    title = entry["createItem"]["item"]["title"]
                    assert title != FORM_FIELDS[0][1]
                    items.append({"title": title, "questionItem": {
                        "question": {"questionId": "q-" + str(len(items))}
                    }})
            return httpx.Response(200, json={})
        if str(request.url).endswith(":setPublishSettings"):
            published.update(isPublished=True, isAcceptingResponses=True)
            return httpx.Response(200, json={})
        raise AssertionError("forms.create must not run on resume")
    config = GooglePremarketingConfig(
        "client", "secret", "refresh", "marketing@example.test",
        forms_base_url="https://forms.test/v1", token_url="https://oauth.test/token",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = GoogleFormsPublisher(config, client)
        campaign = {"title": "相談", "audience": "DX", "offer": "診断",
                    "google_form_id": "form-1"}
        first = await publisher.resume_existing(campaign)
        second = await publisher.resume_existing(campaign)
    assert first["form_id"] == second["form_id"] == "form-1"
    assert set(first["question_map"].values()) == {key for key, *_ in FORM_FIELDS} | {"consent"}
    assert any(item["title"] == CONSENT_TITLE for item in items)
    assert not any("forms?unpublished=true" in url for _, url in calls)


def test_restart_marks_interrupted_form_without_losing_known_id(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("restart")
    campaign = memory.create_campaign(project["id"], "相談", "対象", "価値", "CTA", "")
    memory.update_campaign_publication(
        project["id"], campaign["id"], "publishing_form",
        publication_approved_at="2026-10-01T00:00:00+00:00",
        google_form_id="form-1",
    )
    assert memory.recover_interrupted_form_publications() == 1
    recovered = memory.get_campaign(project["id"], campaign["id"])
    assert recovered["publication_status"] == "failed"
    assert recovered["google_form_id"] == "form-1"
    claimed = memory.claim_form_resume(project["id"], campaign["id"])
    assert claimed["publication_status"] == "publishing_form"
    assert memory.recover_interrupted_form_publications() == 1
