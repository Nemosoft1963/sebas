"""Campaign milestones are evidence only after the corresponding real action."""
from app.campaign_evidence import campaign_evidence

SITE = "2026-09-10T10:00:00+00:00"
APPROVED = "2026-09-10T11:00:00+00:00"
POSTED = "2026-09-10T12:00:00+00:00"
SYNCED = "2026-09-10T13:00:00+00:00"
LEAD = "2026-09-10T14:00:00+00:00"


def campaign(**changes):
    value = {"id": "c1", "site_publication_status": "published",
             "google_site_url": "https://sites.google.com/view/example",
             "site_publication_approved_at": SITE,
             "google_form_id": "form-1", "publication_status": "monitoring",
             "published_at": SITE, "last_synced_at": SYNCED}
    value.update(changes)
    return value


def share(**changes):
    value = {"id": "s1", "campaign_id": "c1", "channel": "x",
             "status": "evidence_registered", "approved_at": APPROVED,
             "evidence_url": "https://x.com/example/status/1",
             "evidence_registered_at": POSTED}
    value.update(changes)
    return value


def kinds(campaign_value, shares, leads=(), kit_exists=False):
    return {item["kind"] for item in campaign_evidence(
        campaign_value, list(shares), list(leads), kit_exists=kit_exists)}


def test_site_and_approved_kit_do_not_claim_post_or_lead():
    draft = share(status="approved", evidence_url="", evidence_registered_at=None)
    found = kinds(campaign(last_synced_at=None), [draft], kit_exists=True)
    assert found == {"google_site_publication", "social_posting_kit_generation",
                     "social_copy_approval"}
    assert kinds(campaign(last_synced_at=None), [draft], kit_exists=False) == {
        "google_site_publication", "social_copy_approval"}


def test_opened_composer_and_unapproved_url_are_not_post_evidence():
    opened = share(status="composer_opened", evidence_url="", evidence_registered_at=None)
    unapproved = share(status="evidence_registered", approved_at=None)
    for item in (opened, unapproved):
        found = kinds(campaign(), [item])
        assert "manual_social_post" not in found
        assert "post_url_registration" not in found
        assert "form_response_sync" not in found


def test_verified_post_and_fresh_sync_and_real_lead_advance_separately():
    lead = {"id": "l1", "campaign_id": "c1", "consent": True,
            "score": 70, "created_at": LEAD}
    found = kinds(campaign(), [share()], [lead], kit_exists=True)
    assert {"google_site_publication", "social_posting_kit_generation",
            "social_copy_approval", "social_post", "manual_social_post",
            "post_url_registration", "form_response_sync", "lead_evaluation"} <= found


def test_failed_or_historical_sync_and_missing_lead_never_complete():
    lead = {"id": "l1", "campaign_id": "c1", "consent": True,
            "score": 70, "created_at": LEAD}
    for state in (
        campaign(publication_status="reauth_required"),
        campaign(last_synced_at="2026-09-09T13:00:00+00:00"),
        campaign(last_synced_at="2026-09-10T11:30:00+00:00"),
    ):
        found = kinds(state, [share()], [lead])
        assert "form_response_sync" not in found
        assert "lead_evaluation" not in found
    assert "lead_evaluation" not in kinds(campaign(), [share()])
    assert "lead_evaluation" not in kinds(campaign(), [share()], [dict(lead, consent=False)])
    assert "lead_evaluation" not in kinds(campaign(), [share()], [dict(lead, created_at=APPROVED)])
def test_approved_executed_action_keeps_its_specific_kind(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.project_manager import ProjectOrchestrator

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("外部実行証拠")
    action = memory.create_action(project["id"], "approved_customer_engagement",
                                  "example", "承認待ちの顧客接触")
    manager = ProjectOrchestrator(memory, None, lambda _: ("", []), None, lambda: [])
    memory.update_action(project["id"], action["id"], "executed", evidence="記録のみ")
    assert manager._external_execution_evidence(project["id"]) == []
    memory.update_action(project["id"], action["id"], "approved")
    memory.update_action(project["id"], action["id"], "executed", evidence="確認済みの実施証拠")
    evidence = manager._external_execution_evidence(project["id"])
    assert {item["kind"] for item in evidence} == {
        "external_action", "approved_customer_engagement",
    }
    assert all(item["reference"] == "確認済みの実施証拠" for item in evidence)
def test_published_site_guidance_points_to_missing_post_not_republication(tmp_path):
    import json
    from app.memory.short_term import ShortTermMemory
    from app.project_manager import ProjectOrchestrator

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("公開済みLP")
    memory.save_mission(project["id"], "公開後にSNS投稿する", "", "", False, [])
    contract = {"schema": "local-cowork-plan/v1", "action_requirements": [
        {"kind": "google_site_publication", "minimum_executed": 1, "evidence_required": True},
        {"kind": "manual_social_post", "minimum_executed": 1, "evidence_required": True},
    ]}
    mission = memory.replace_plan(project["id"], "外部実行", [{
        "task_key": "campaign", "title": "キャンペーン", "depends_on": [],
        "acceptance_criteria": json.dumps(contract), "mode": "local",
    }])
    campaign = memory.create_campaign(project["id"], "相談", "担当者", "課題", "相談", "")
    memory.update_campaign_site(project["id"], campaign["id"], "published",
        google_site_url="https://sites.google.com/view/example",
        site_publication_approved_at=SITE, published_at=SITE)
    manager = ProjectOrchestrator(memory, None, lambda _: ("", []), None, lambda: [])
    manager._pause_for_external_execution(project["id"], mission["tasks"][0])
    updated = memory.get_mission(project["id"])
    gate = next(item for item in updated["events"] if item["kind"] == "external_execution_gate")
    detail = json.loads(gate["detail"])
    assert detail["missing_requirements"] == ["manual_social_post"]
    assert "投稿画面" in detail["required_next_step"]
    assert "Google Sitesを手動公開" not in detail["required_next_step"]


def test_generic_action_cannot_impersonate_campaign_operation(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.project_manager import ProjectOrchestrator

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("キャンペーン証拠")
    action = memory.create_action(project["id"], "manual_social_post",
                                  "example", "投稿したとの申告")
    memory.update_action(project["id"], action["id"], "approved")
    memory.update_action(project["id"], action["id"], "executed", evidence="URLなし")
    manager = ProjectOrchestrator(memory, None, lambda _: ("", []), None, lambda: [])
    assert manager._external_execution_evidence(project["id"]) == []
