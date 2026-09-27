from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.memory.short_term import ShortTermMemory
from app.social_premarketing import (
    CHANNELS, social_package, tracking_url, validate_evidence_url,
    validate_public_landing_url,
)


def campaign():
    return {
        "id": "campaign-1",
        "title": "機密業務向けローカルAI導入相談",
        "audience": "機密資料を扱うDX担当者",
        "offer": "対象業務と安全要件を30分で整理します。",
        "call_to_action": "30分の初回課題診断を申し込む",
        "google_site_url": "https://sites.google.com/view/local-ai-consulting",
    }


def test_social_package_has_safe_manual_channels_and_utm():
    items = social_package(campaign())
    assert {item["channel"] for item in items} == set(CHANNELS)
    x = next(item for item in items if item["channel"] == "x")
    assert x["compose_url"].startswith("https://twitter.com/intent/tweet?")
    instagram = next(item for item in items if item["channel"] == "instagram")
    assert instagram["mode"] == "copy" and instagram["compose_url"] == ""
    params = parse_qs(urlparse(x["tracking_url"]).query)
    assert params == {
        "utm_source": ["x"], "utm_medium": ["social"],
        "utm_campaign": ["campaign-1"], "utm_content": ["primary"],
    }


def test_tracking_url_preserves_existing_query_and_rejects_local_http():
    url = tracking_url("https://example.test/lp?ref=kept", "c1", "line")
    assert parse_qs(urlparse(url).query)["ref"] == ["kept"]
    with pytest.raises(ValueError):
        validate_public_landing_url("http://127.0.0.1:8099/preview")


def test_evidence_url_is_restricted_to_the_selected_social_network():
    assert validate_evidence_url("x", "https://x.com/example/status/1")
    with pytest.raises(ValueError):
        validate_evidence_url("x", "https://example.test/fake-post")


def test_social_approval_open_and_evidence_are_persistent(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("social")
    saved_campaign = memory.create_campaign(
        project["id"], "相談", "DX担当者", "課題を整理", "問い合わせ", "",
    )
    item_campaign = {**saved_campaign, **campaign(), "id": saved_campaign["id"]}
    drafts = memory.save_social_drafts(project["id"], saved_campaign["id"],
                                        social_package(item_campaign))
    assert len(drafts) == 6 and {item["status"] for item in drafts} == {"draft_ready"}
    memory.update_social_campaign_status(
        project["id"], saved_campaign["id"], "draft_ready", "awaiting_approval",
    )
    memory.update_social_campaign_status(
        project["id"], saved_campaign["id"], "awaiting_approval", "approved",
    )
    opened = memory.mark_social_opened(project["id"], saved_campaign["id"], "x")
    assert opened["status"] == "composer_opened" and opened["open_count"] == 1
    recorded = memory.register_social_evidence(
        project["id"], saved_campaign["id"], "x", "https://x.com/example/status/1",
    )
    assert recorded["status"] == "evidence_registered"
    assert recorded["evidence_url"].endswith("/status/1")


def test_unapproved_social_draft_cannot_be_opened(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("social")
    saved_campaign = memory.create_campaign(
        project["id"], "相談", "DX担当者", "課題を整理", "問い合わせ", "",
    )
    item_campaign = {**saved_campaign, **campaign(), "id": saved_campaign["id"]}
    memory.save_social_drafts(project["id"], saved_campaign["id"],
                              social_package(item_campaign))
    with pytest.raises(ValueError):
        memory.mark_social_opened(project["id"], saved_campaign["id"], "x")


def test_social_regeneration_resets_working_state_after_history_is_captured(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("social")
    saved_campaign = memory.create_campaign(
        project["id"], "相談", "DX担当者", "課題を整理", "問い合わせ", "",
    )
    item_campaign = {**saved_campaign, **campaign(), "id": saved_campaign["id"]}
    items = social_package(item_campaign)
    memory.save_social_drafts(project["id"], saved_campaign["id"], items)
    memory.update_social_campaign_status(
        project["id"], saved_campaign["id"], "draft_ready", "awaiting_approval",
    )
    memory.update_social_campaign_status(
        project["id"], saved_campaign["id"], "awaiting_approval", "approved",
    )
    memory.mark_social_opened(project["id"], saved_campaign["id"], "x")
    regenerated = memory.save_social_drafts(project["id"], saved_campaign["id"], items)
    assert {item["status"] for item in regenerated} == {"draft_ready"}
    assert {item["open_count"] for item in regenerated} == {0}
    assert {item["evidence_url"] for item in regenerated} == {""}


def test_social_regeneration_route_archives_and_guards_evidence():
    source = (Path(__file__).resolve().parents[1] / "app" / "web.py").read_text(
        encoding="utf-8"
    )
    assert "social_package_regenerated" in source
    assert "archive_paths" in source
    assert "公開投稿URLが登録済みのため既存キットは再生成できません" in source
