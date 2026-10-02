from app.premarketing_monitor import campaign_execution_monitor


def campaign(**overrides):
    item = {
        "id": "campaign-1", "publication_status": "awaiting_site",
        "site_publication_status": "draft_ready", "google_form_id": "form-1",
        "google_site_url": "", "landing_assets_path": "premarketing/c1/landing",
        "publication_error": "", "site_publication_error": "",
        "creative": {"status": "approved", "quality_score": 85},
        "updated_at": "2026-09-07T01:00:00+00:00",
    }
    item.update(overrides)
    return item


def test_monitor_reports_current_lp_block_and_next_action():
    monitor = campaign_execution_monitor(campaign(), [], [])
    assert monitor["progress_percent"] == 29
    assert next(stage for stage in monitor["stages"] if stage["key"] == "form")["state"] == "running"
    assert next(stage for stage in monitor["stages"] if stage["key"] == "social")["state"] == "blocked"
    assert "Google Sites" in monitor["next_action"]


def test_monitor_counts_social_execution_evidence_and_qualified_leads():
    shares = [
        {"id": "share-1", "campaign_id": "campaign-1", "channel": "x",
         "status": "evidence_registered", "open_count": 2,
         "approved_at": "2026-09-07T01:30:00+00:00",
         "evidence_registered_at": "2026-09-07T02:00:00+00:00",
         "evidence_url": "https://x.com/example/status/1", "updated_at": "2026-09-07T02:00:00+00:00"},
        {"id": "share-2", "campaign_id": "campaign-1", "status": "approved",
         "approved_at": "2026-09-07T01:30:00+00:00",
         "open_count": 0, "evidence_url": "",
         "updated_at": "2026-09-07T02:01:00+00:00"},
    ]
    leads = [
        {"id": "lead-1", "campaign_id": "campaign-1", "consent": True,
         "score": 80, "created_at": "2026-09-07T03:00:00+00:00",
         "updated_at": "2026-09-07T03:00:00+00:00"},
        {"campaign_id": "other", "score": 99, "updated_at": "2026-09-07T04:00:00+00:00"},
    ]
    monitor = campaign_execution_monitor(
        campaign(site_publication_status="published", publication_status="monitoring",
                 google_site_url="https://sites.google.com/view/example",
                 google_form_url="https://docs.google.com/forms/d/e/example/viewform",
                 site_publication_approved_at="2026-09-07T00:00:00+00:00",
                 publication_approved_at="2026-09-07T00:00:00+00:00",
                 published_at="2026-09-07T01:00:00+00:00",
                 last_synced_at="2026-09-07T02:30:00+00:00"),
        shares, leads,
    )
    assert monitor["progress_percent"] == 100
    assert monitor["counts"] == {
        "social_channels": 2, "composer_opens": 2,
        "published_posts": 1, "leads": 1, "qualified_leads": 1,
    }
    assert monitor["last_activity_at"] == "2026-09-07T03:00:00+00:00"
    assert "有望リード" in monitor["next_action"]


def test_monitor_surfaces_publication_error():
    monitor = campaign_execution_monitor(
        campaign(publication_status="reauth_required", google_form_id="",
                 publication_error="OAuth再認証が必要"), [], [],
    )
    assert monitor["attention"] == "OAuth再認証が必要"
    assert any(stage["state"] == "attention" for stage in monitor["stages"])


def test_monitor_blocks_landing_work_until_creative_is_approved():
    monitor = campaign_execution_monitor(
        campaign(creative={"status": "brief_ready", "quality_score": 0}), [], [],
    )
    stage = next(item for item in monitor["stages"] if item["key"] == "creative")
    assert stage["state"] == "waiting"
    assert "デザイン品質向上" in monitor["next_action"]


def test_monitor_does_not_count_url_or_lead_without_provenance():
    shares = [{"id": "share-1", "campaign_id": "campaign-1",
               "status": "evidence_registered", "open_count": 1,
               "evidence_url": "https://x.com/example/status/1",
               "updated_at": "2026-09-07T02:00:00+00:00"}]
    leads = [{"id": "lead-1", "campaign_id": "campaign-1", "consent": False,
              "score": 90, "updated_at": "2026-09-07T03:00:00+00:00"}]
    monitor = campaign_execution_monitor(campaign(), shares, leads)
    assert monitor["counts"]["published_posts"] == 0
    assert monitor["counts"]["leads"] == 0
    assert monitor["counts"]["qualified_leads"] == 0
    assert next(stage for stage in monitor["stages"] if stage["key"] == "outreach")["state"] != "complete"
    assert next(stage for stage in monitor["stages"] if stage["key"] == "leads")["state"] != "complete"
