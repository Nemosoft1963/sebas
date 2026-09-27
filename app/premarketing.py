"""Local, consent-based lead qualification and sales-action queuing."""
from __future__ import annotations

import json
from email.utils import parseaddr


def lead_score(company: str, role: str, problem: str,
               timeline: str, budget: str, consent: bool) -> int:
    score = 20 if consent else 0
    score += 10 if company.strip() else 0
    score += 10 if role.strip() else 0
    score += 25 if len(problem.strip()) >= 30 else 0
    timeline_text = timeline.lower()
    score += 20 if any(value in timeline_text for value in (
        "すぐ", "1か月", "1ヶ月", "30日", "今月",
    )) else (10 if timeline.strip() else 0)
    score += 15 if budget.strip() and budget.strip() not in {"未定", "不明", "なし"} else 0
    return min(score, 100)


def capture_and_queue(memory, campaign: dict, *, name: str, email: str,
                      company: str, role: str, problem: str, timeline: str,
                      budget: str, consent: bool, source: str = "capture_form",
                      external_ref: str = "") -> tuple[dict, dict | None]:
    existing = memory.get_lead_by_external_ref(campaign["id"], source, external_ref)
    if existing:
        return existing, None
    recipient = parseaddr(email.strip())[1]
    if not consent or not recipient or "@" not in recipient:
        raise ValueError("有効なメールアドレスと利用同意が必要です")
    fields = [name, recipient, company, role, problem, timeline, budget]
    if any(len(value) > limit for value, limit in zip(
        fields, (120, 320, 200, 200, 4000, 200, 200), strict=True,
    )):
        raise ValueError("入力が長すぎます")
    score = lead_score(company, role, problem, timeline, budget, consent)
    lead = memory.create_lead(
        campaign, name.strip(), recipient, company.strip(), role.strip(),
        problem.strip(), timeline.strip(), budget.strip(), consent, score,
        source=source, external_ref=external_ref,
    )
    memory.add_event(
        campaign["project_id"], "premarketing_lead_captured",
        f"同意付きリードを獲得: score={score}",
        detail=json.dumps({
            "lead_id": lead["id"], "campaign_id": campaign["id"],
            "score": score, "company": company.strip(),
        }, ensure_ascii=False),
    )
    action = None
    if score >= 60:
        action = memory.create_action(
            campaign["project_id"], "email", recipient,
            f"件名: {campaign['title']}へのお申し込みありがとうございます\n\n"
            f"{name.strip()} 様\n\nお申し込みありがとうございます。\n"
            f"ご相談内容を確認し、{campaign['call_to_action']}についてご案内します。\n\n"
            "このメールは送信前に担当者が内容を確認・承認します。",
        )
        memory.update_lead_status(campaign["project_id"], lead["id"], "contact_queued")
        memory.add_event(
            campaign["project_id"], "qualified_lead_action_queued",
            f"有望リードを承認待ちメールへ変換: score={score}",
            detail=json.dumps({"lead_id": lead["id"], "action_id": action["id"]}),
        )
        lead = memory.get_lead(campaign["project_id"], lead["id"]) or lead
    return lead, action
