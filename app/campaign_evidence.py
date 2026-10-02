"""Evidence from completed, approved premarketing operations.

No draft, composer-open, failed sync, or missing lead is promoted to execution.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


CAMPAIGN_OPERATION_KINDS = frozenset({
    "google_site_publication", "social_posting_kit_generation",
    "social_copy_approval", "social_post", "manual_social_post",
    "post_url_registration", "form_response_sync", "lead_evaluation",
    "google_form_publication", "lead_capture",
})


def _https_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme == "https" and bool(parsed.hostname)


def campaign_evidence(campaign: dict, shares: list[dict], leads: list[dict],
                      *, kit_exists: bool = False) -> list[dict]:
    campaign_id = str(campaign.get("id") or "")
    if not campaign_id:
        return []
    evidence: list[dict] = []
    site_url = str(campaign.get("google_site_url") or "").strip()
    site_ready = (campaign.get("site_publication_status") == "published" and _https_url(site_url)
                  and bool(campaign.get("site_publication_approved_at")))
    if site_ready:
        evidence.append({"kind": "google_site_publication", "id": campaign_id,
                         "campaign_id": campaign_id, "reference": site_url})

    form_url = str(campaign.get("google_form_url") or "").strip()
    form_ready = (
        bool(campaign.get("google_form_id")) and _https_url(form_url)
        and campaign.get("publication_status") in {"published", "monitoring"}
        and bool(campaign.get("publication_approved_at"))
        and _time(campaign.get("published_at")) is not None
    )
    if form_ready:
        evidence.append({"kind": "google_form_publication", "id": campaign_id,
                         "campaign_id": campaign_id, "reference": form_url})
        published_at = _time(campaign.get("published_at"))
        for lead in leads:
            if lead.get("campaign_id") != campaign_id or not lead.get("consent"):
                continue
            created = _time(lead.get("created_at"))
            if created is not None and created >= published_at:
                evidence.append({"kind": "lead_capture", "id": lead.get("id"),
                                 "campaign_id": campaign_id,
                                 "reference": str(lead.get("id"))})
    if kit_exists and shares:
        evidence.append({"kind": "social_posting_kit_generation", "id": campaign_id,
                         "campaign_id": campaign_id,
                         "reference": f"premarketing/{campaign_id}/social/social_post_kit.md"})

    posted_at: list[datetime] = []
    for share in shares:
        if share.get("campaign_id") != campaign_id:
            continue
        approved = bool(share.get("approved_at")) and share.get("status") in {
            "approved", "composer_opened", "evidence_registered",
        }
        if approved:
            evidence.append({"kind": "social_copy_approval", "id": share.get("id"),
                             "campaign_id": campaign_id,
                             "reference": str(share.get("approved_at"))})
        url = str(share.get("evidence_url") or "").strip()
        registered = _time(share.get("evidence_registered_at"))
        if approved and share.get("status") == "evidence_registered" and _https_url(url) and registered:
            posted_at.append(registered)
            for kind in ("social_post", "manual_social_post", "post_url_registration"):
                event = {"kind": kind, "id": share.get("id"),
                         "campaign_id": campaign_id, "reference": url}
                if kind == "social_post":
                    event["channel"] = share.get("channel")
                evidence.append(event)

    # A historical sync does not satisfy the current campaign after OAuth fails,
    # after a newer site publication, or before any approved post URL exists.
    synced = _time(campaign.get("last_synced_at"))
    published = _time(campaign.get("published_at"))
    sync_ready = (
        site_ready and bool(campaign.get("google_form_id"))
        and campaign.get("publication_status") == "monitoring"
        and synced is not None and published is not None
        and synced >= published and any(synced >= post for post in posted_at)
    )
    if sync_ready:
        evidence.append({"kind": "form_response_sync", "id": campaign_id,
                         "campaign_id": campaign_id,
                         "reference": str(campaign["last_synced_at"])})
        for lead in leads:
            if lead.get("campaign_id") != campaign_id or not lead.get("consent"):
                continue
            created = _time(lead.get("created_at"))
            score = lead.get("score")
            if (created is None or created < synced or isinstance(score, bool)
                    or not isinstance(score, int) or not 0 <= score <= 100):
                continue
            evidence.append({"kind": "lead_evaluation", "id": lead.get("id"),
                             "campaign_id": campaign_id,
                             "reference": str(lead.get("id"))})
    return evidence