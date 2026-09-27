from __future__ import annotations

from typing import Any


def _stage(key: str, label: str, state: str, detail: str) -> dict[str, str]:
    return {"key": key, "label": label, "state": state, "detail": detail}


def campaign_execution_monitor(campaign: dict[str, Any], shares: list[dict],
                               leads: list[dict]) -> dict[str, Any]:
    campaign_leads = [lead for lead in leads if lead.get("campaign_id") == campaign.get("id")]
    form_status = str(campaign.get("publication_status") or "local_only")
    site_status = str(campaign.get("site_publication_status") or "not_requested")
    error = str(campaign.get("site_publication_error") or campaign.get("publication_error") or "")
    creative = campaign.get("creative") if isinstance(campaign.get("creative"), dict) else {}
    creative_status = str(creative.get("status") or "not_started")

    stages = [_stage("campaign", "キャンペーン", "complete", "訴求内容を保存済み")]
    if creative_status == "approved":
        stages.append(_stage("creative", "デザイン品質", "complete",
                             f"品質スコア {int(creative.get('quality_score') or 0)}・承認済み"))
    elif creative_status == "reviewed":
        stages.append(_stage("creative", "デザイン品質", "running",
                             f"品質スコア {int(creative.get('quality_score') or 0)}・承認待ち"))
    elif creative_status == "brief_ready":
        stages.append(_stage("creative", "デザイン品質", "waiting", "Canva等で制作・レビューが必要"))
    elif creative_status == "failed":
        stages.append(_stage("creative", "デザイン品質", "attention",
                             str(creative.get("error") or "再制作が必要")))
    else:
        stages.append(_stage("creative", "デザイン品質", "waiting", "品質向上資材の生成が必要"))
    if campaign.get("google_form_id"):
        stages.append(_stage("form", "Googleフォーム", "complete", "公開・回答受付設定済み"))
    elif form_status in {"failed", "reauth_required"}:
        stages.append(_stage("form", "Googleフォーム", "attention", error or "再設定が必要"))
    elif form_status in {"awaiting_approval", "approved", "publishing_form"}:
        stages.append(_stage("form", "Googleフォーム", "running", form_status))
    else:
        stages.append(_stage("form", "Googleフォーム", "waiting", "公開申請が必要"))

    if site_status == "published" and campaign.get("google_site_url"):
        stages.append(_stage("landing", "公開LP", "complete", "公開URL検証済み"))
    elif site_status in {"failed", "reauth_required"}:
        stages.append(_stage("landing", "公開LP", "attention", error or "再確認が必要"))
    elif site_status in {"awaiting_approval", "approved"}:
        stages.append(_stage("landing", "公開LP", "running", site_status))
    elif campaign.get("landing_assets_path"):
        stages.append(_stage("landing", "公開LP", "waiting", "資材生成済み・公開待ち"))
    else:
        stages.append(_stage("landing", "公開LP", "waiting", "LP資材生成が必要"))

    prepared = [share for share in shares if share.get("status") in {
        "approved", "composer_opened", "evidence_registered",
    }]
    awaiting = [share for share in shares if share.get("status") == "awaiting_approval"]
    if shares and len(prepared) == len(shares):
        stages.append(_stage("social", "SNS文案", "complete", f"{len(shares)}媒体を承認済み"))
    elif awaiting:
        stages.append(_stage("social", "SNS文案", "running", f"{len(awaiting)}媒体が承認待ち"))
    elif shares:
        stages.append(_stage("social", "SNS文案", "waiting", f"{len(shares)}媒体の文案確認が必要"))
    else:
        stages.append(_stage("social", "SNS文案", "blocked" if not campaign.get("google_site_url") else "waiting",
                             "公開LP登録後に生成可能" if not campaign.get("google_site_url") else "投稿キット未生成"))

    open_count = sum(int(share.get("open_count") or 0) for share in shares)
    evidence_count = sum(1 for share in shares if share.get("evidence_url"))
    if evidence_count:
        stages.append(_stage("outreach", "SNS発信", "complete", f"公開投稿 {evidence_count}件を確認"))
    elif open_count:
        stages.append(_stage("outreach", "SNS発信", "running", f"投稿操作 {open_count}回・公開証拠待ち"))
    else:
        stages.append(_stage("outreach", "SNS発信", "waiting", "投稿操作は未開始"))

    if campaign_leads:
        stages.append(_stage("leads", "リード獲得", "complete", f"同意付きリード {len(campaign_leads)}件"))
    else:
        stages.append(_stage("leads", "リード獲得", "waiting", "回答・問い合わせを監視中"))

    if not campaign.get("google_form_id"):
        next_action = "Googleフォームの公開処理を進める"
    elif creative_status != "approved":
        next_action = "デザイン品質向上を実施し、レビュー結果を承認する"
    elif site_status != "published" or not campaign.get("google_site_url"):
        next_action = "Google SitesのLPを公開し、公開URLを検証・登録する"
    elif not shares:
        next_action = "SNS投稿キットを生成する"
    elif any(share.get("status") == "draft_ready" for share in shares):
        next_action = "SNS文案を確認して承認申請する"
    elif awaiting:
        next_action = "SNS文案と計測URLを承認する"
    elif not open_count:
        next_action = "承認済みSNSの投稿画面を開く"
    elif not evidence_count:
        next_action = "実際に公開した投稿URLを証拠登録する"
    elif not campaign_leads:
        next_action = "Googleフォーム回答と問い合わせを継続監視する"
    else:
        next_action = "有望リードの連絡承認待ちアクションを確認する"

    updated_values = [str(campaign.get("updated_at") or "")]
    updated_values.extend(str(item.get("updated_at") or "") for item in shares)
    updated_values.extend(str(item.get("updated_at") or "") for item in campaign_leads)
    completed = sum(1 for stage in stages if stage["state"] == "complete")
    return {
        "progress_percent": round(completed * 100 / len(stages)),
        "stages": stages,
        "next_action": next_action,
        "attention": error,
        "counts": {
            "social_channels": len(shares),
            "composer_opens": open_count,
            "published_posts": evidence_count,
            "leads": len(campaign_leads),
            "qualified_leads": sum(1 for lead in campaign_leads if int(lead.get("score") or 0) >= 60),
        },
        "last_activity_at": max(updated_values),
    }
