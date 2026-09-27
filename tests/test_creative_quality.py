import json

import pytest

from app.creative_quality import (
    asset_manifest, automatic_quality_review, build_brand_profile, contrast_ratio, creative_brief_markdown,
    creative_view, quality_report_markdown, validate_design_url,
)
from app.landing_page import render_landing_page_html


def campaign():
    return {
        "id": "campaign-1", "title": "機密業務向けローカルAI導入相談",
        "audience": "機密資料を扱うDX担当者", "offer": "対象業務と安全要件を整理します。",
        "call_to_action": "初回課題診断を申し込む", "google_form_id": "form-1",
        "google_form_url": "https://docs.google.com/forms/d/e/form-1/viewform",
    }


def test_creative_package_uses_campaign_facts_and_accessible_brand():
    item, brand = campaign(), build_brand_profile()
    brief = creative_brief_markdown(item, brand, "canva")
    manifest = asset_manifest(item, brand, "canva")
    report = quality_report_markdown(item, brand)
    assert item["title"] in brief and item["call_to_action"] in brief
    assert manifest["campaign_id"] == item["id"]
    assert "品質スコア" in report
    assert contrast_ratio(brand["secondary_color"], brand["background_color"]) >= 4.5


def test_canva_url_and_brand_colors_are_validated():
    assert validate_design_url("https://www.canva.com/design/example", "canva")
    with pytest.raises(ValueError):
        validate_design_url("https://example.com/design", "canva")
    with pytest.raises(ValueError):
        build_brand_profile({"primary_color": "blue"})


def test_only_approved_creative_changes_landing_page():
    item, brand = campaign(), build_brand_profile({"primary_color": "#663399"})
    item["creative"] = {"status": "reviewed", "brand": brand,
                        "image_url": "https://images.example.test/hero.png"}
    unapproved = render_landing_page_html(item)
    assert "#663399" not in unapproved
    item["creative"]["status"] = "approved"
    approved = render_landing_page_html(item)
    assert "#663399" in approved
    assert "https://images.example.test/hero.png" in approved


def test_creative_view_decodes_brand_json():
    view = creative_view({"status": "approved", "brand_json": json.dumps({"brand_name": "Example"})})
    assert view["brand"]["brand_name"] == "Example"
    assert "brand_json" not in view


def test_automatic_quality_review_is_reviewable_not_approval():
    result = automatic_quality_review(campaign(), build_brand_profile())
    assert result["score"] >= 70
    assert "最終承認は人間" in result["review"]
    assert all(result["checks"].values())
