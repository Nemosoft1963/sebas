import html

import pytest

from app.landing_page import (
    google_sites_automation_payload, landing_asset_version, landing_page_manifest,
    render_google_sites_copy, render_landing_page_html,
    validate_form_url, validate_google_site_url, verify_public_landing_html,
)


def campaign():
    return {
        "id": "campaign-1",
        "title": "機密業務向けローカルAI導入相談",
        "audience": "機密資料を扱うDX担当者",
        "offer": "対象業務と安全要件を30分で整理します。",
        "call_to_action": "30分の初回課題診断を申し込む",
        "google_form_id": "form-id-123",
        "google_form_url": "https://docs.google.com/forms/d/e/form-id-123/viewform",
    }


def test_landing_page_links_to_existing_google_form_and_escapes_copy():
    item = campaign()
    item["title"] = "相談 <script>alert(1)</script>"
    rendered = render_landing_page_html(item)
    assert item["google_form_url"] in rendered
    assert "id=\"inquiry-cta\"" in rendered
    assert item["call_to_action"] in rendered
    assert "<script>alert(1)</script>" not in rendered
    assert html.escape(item["title"]) in rendered


def test_google_sites_copy_and_manifest_include_required_public_content():
    item = campaign()
    manifest = landing_page_manifest(item)
    copy = render_google_sites_copy(item)
    assert manifest["inquiry_url"] == item["google_form_url"]
    assert manifest["asset_version"] == landing_asset_version(item)
    assert item["title"] in copy
    assert item["call_to_action"] in copy
    assert item["google_form_url"] in copy
    assert "情報の取扱い" in copy
    assert manifest['operator']['name'] == '弘和運輸有限会社'
    assert manifest['operator']['url'] == 'https://kouwatrsp.com/'
    assert '運営会社' in copy
    assert 'https://kouwatrsp.com/' in copy


def test_google_sites_automation_payload_requires_manual_publish():
    item = campaign()
    payload = google_sites_automation_payload(item)
    assert payload["title"] == item["title"]
    assert payload["asset_version"] == landing_asset_version(item)
    assert item["google_form_url"] in payload["embed_html"]
    assert payload["manual_publish_required"] is True
    assert item["title"] in payload["required_texts"]
    assert item["audience"] in payload["required_texts"]
    assert item["google_form_url"] in payload["required_links"]


def test_public_landing_verification_requires_exact_single_approved_asset_version():
    item = campaign()
    body = render_landing_page_html(item)
    assert all(verify_public_landing_html(body, item).values())
    assert not verify_public_landing_html(item["title"], item)["google_form_link"]
    old_version = "000000000000"
    duplicate = body + f'<div data-local-supporter-version="{old_version}"></div>'
    assert not verify_public_landing_html(duplicate, item)["single_asset_version"]
    missing_audience = body.replace(item["audience"], "")
    assert not verify_public_landing_html(missing_audience, item)["audience"]


@pytest.mark.parametrize("url", ["http://docs.google.com/forms/x", "https://example.com/form"])
def test_form_url_rejects_non_google_or_non_https(url):
    with pytest.raises(ValueError):
        validate_form_url(url)


def test_google_sites_url_requires_https_sites_host():
    assert validate_google_site_url("https://sites.google.com/view/example")
    with pytest.raises(ValueError):
        validate_google_site_url("https://example.com/site")
