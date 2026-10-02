"""Regression tests for evidence shared by task and goal completion gates."""
import json

from app.campaign_evidence import campaign_evidence
from app.generic_goal_checks import _check_external, collect_external_evidence
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator


def _manager(tmp_path):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("evidence regression")
    manager = ProjectOrchestrator(memory, None, lambda _: ("", []), None, lambda: [])
    return manager, project["id"]


def _task(kind):
    return {"acceptance_criteria": json.dumps({
        "schema": "local-cowork-plan/v1",
        "action_requirements": [
            {"kind": kind, "minimum_executed": 1, "evidence_required": True}
        ],
    })}


def test_published_form_without_lead_cannot_complete_lead_capture(tmp_path):
    manager, pid = _manager(tmp_path)
    campaign = manager.memory.create_campaign(pid, "test", "test", "test", "test", "")
    manager.memory.update_campaign_publication(
        pid, campaign["id"], "monitoring",
        google_form_id="form1", google_form_url="https://docs.google.com/forms/test",
        publication_approved_at="2026-09-10T09:00:00+00:00",
        published_at="2026-09-10T10:00:00+00:00",
    )
    evidence = collect_external_evidence(manager, pid)
    assert "google_form_publication" in {e["kind"] for e in evidence["operations"]}
    assert "lead_capture" not in {e["kind"] for e in evidence["operations"]}
    assert _check_external("SC1", ["lead_capture"], evidence, "result.md")["status"] == "FAIL"
    assert not manager._task_external_evidence_satisfied(
        _task("lead_capture"), manager._external_execution_evidence(pid)
    )


def test_unrelated_action_cannot_satisfy_campaign_kind_or_unknown_kind(tmp_path):
    manager, pid = _manager(tmp_path)
    action = manager.memory.create_action(pid, "manual", "example target", "actual action")
    manager.memory.update_action(pid, action["id"], "approved")
    manager.memory.update_action(pid, action["id"], "executed", evidence="verified example")
    evidence = collect_external_evidence(manager, pid)
    task_evidence = manager._external_execution_evidence(pid)
    assert _check_external("SC1", ["manual_social_post"], evidence, "result.md")["status"] == "FAIL"
    assert not manager._task_external_evidence_satisfied(_task("manual_social_post"), task_evidence)
    unknown = _check_external("SC1", ["unknown_operation"], evidence, "result.md")
    assert unknown["status"] == "UNTESTABLE"
    assert manager._task_external_evidence_satisfied(_task("approved_external_action"), task_evidence)


def test_campaign_event_cannot_satisfy_generic_approved_action(tmp_path):
    manager, pid = _manager(tmp_path)
    campaign = manager.memory.create_campaign(pid, "test", "test", "test", "test", "")
    manager.memory.update_campaign_site(
        pid, campaign["id"], "published",
        google_site_url="https://sites.google.com/view/example",
        site_publication_approved_at="2026-09-10T09:00:00+00:00",
        published_at="2026-09-10T10:00:00+00:00",
    )
    evidence = collect_external_evidence(manager, pid)
    task_evidence = manager._external_execution_evidence(pid)
    assert "google_site_publication" in {e["kind"] for e in task_evidence}
    assert _check_external("SC1", ["approved_external_action"], evidence, "result.md")["status"] == "FAIL"
    assert not manager._task_external_evidence_satisfied(_task("approved_external_action"), task_evidence)


def test_unapproved_or_invalid_site_and_post_urls_are_not_evidence():
    campaign = {
        "id": "c1", "site_publication_status": "published",
        "google_site_url": "http://example.com",
        "site_publication_approved_at": "2026-09-10T09:00:00+00:00",
    }
    shares = [{
        "id": "s1", "campaign_id": "c1", "status": "evidence_registered",
        "approved_at": "2026-09-10T10:00:00+00:00",
        "evidence_url": "not-a-url",
        "evidence_registered_at": "2026-09-10T11:00:00+00:00",
    }]
    kinds = {e["kind"] for e in campaign_evidence(campaign, shares, [])}
    assert "google_site_publication" not in kinds
    assert "social_post" not in kinds
    assert "manual_social_post" not in kinds

def test_consented_postpublication_lead_satisfies_both_gates(tmp_path):
    manager, pid = _manager(tmp_path)
    campaign = manager.memory.create_campaign(pid, "test", "test", "test", "test", "")
    manager.memory.update_campaign_publication(
        pid, campaign["id"], "monitoring",
        google_form_id="form1", google_form_url="https://docs.google.com/forms/test",
        publication_approved_at="2026-09-10T09:00:00+00:00",
        published_at="2026-09-10T10:00:00+00:00",
    )
    manager.memory.create_lead(
        campaign, "Example", "example@example.com", "Example Co", "Owner",
        "Sample need", "this month", "sample", True, 70,
    )
    evidence = collect_external_evidence(manager, pid)
    assert _check_external("SC1", ["lead_capture"], evidence, "result.md")[0] is None
    assert manager._task_external_evidence_satisfied(
        _task("lead_capture"), manager._external_execution_evidence(pid)
    )