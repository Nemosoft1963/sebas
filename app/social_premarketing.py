from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


CHANNELS: dict[str, dict[str, str]] = {
    "x": {"label": "X", "mode": "composer"},
    "linkedin": {"label": "LinkedIn", "mode": "composer_copy"},
    "facebook": {"label": "Facebook", "mode": "composer_copy"},
    "line": {"label": "LINE", "mode": "composer"},
    "instagram": {"label": "Instagram", "mode": "copy"},
    "threads": {"label": "Threads", "mode": "copy"},
}

EVIDENCE_HOSTS = {
    "x": {"x.com", "twitter.com", "www.x.com", "www.twitter.com"},
    "linkedin": {"linkedin.com", "www.linkedin.com"},
    "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com"},
    "line": {"line.me", "timeline.line.me", "www.line.me"},
    "instagram": {"instagram.com", "www.instagram.com"},
    "threads": {"threads.net", "www.threads.net"},
}


def validate_public_landing_url(value: str) -> str:
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("SNS共有には公開済みのHTTPSランディングページURLが必要です")
    return url


def tracking_url(public_url: str, campaign_id: str, channel: str,
                 variant: str = "primary") -> str:
    if channel not in CHANNELS:
        raise ValueError("Unsupported social channel")
    parsed = urlparse(validate_public_landing_url(public_url))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"utm_source": channel, "utm_medium": "social",
                  "utm_campaign": campaign_id, "utm_content": variant})
    return urlunparse(parsed._replace(query=urlencode(query)))


def post_text(campaign: dict[str, Any], channel: str) -> str:
    if channel not in CHANNELS:
        raise ValueError("Unsupported social channel")
    title = str(campaign.get("title", "")).strip()
    audience = str(campaign.get("audience", "")).strip()
    offer = str(campaign.get("offer", "")).strip()
    cta = str(campaign.get("call_to_action", "")).strip()
    if not all((title, audience, offer, cta)):
        raise ValueError("SNS文案にはキャンペーンのタイトル、対象、提供価値、CTAが必要です")
    if channel == "x":
        # Leave room for X's shortened link representation and separator.
        return f"{title}\n\n{audience}\n{offer}\n\n{cta}\n#ローカルAI #業務改善"[:250].rstrip()
    if channel == "linkedin":
        return f"{title}\n\n対象となる方\n{audience}\n\nご提供内容\n{offer}\n\n次のステップ\n{cta}\n\n#ローカルAI #DX #業務改善"
    if channel == "facebook":
        return f"{title}\n\n{audience}\n\n{offer}\n\n{cta}"
    if channel == "line":
        return f"{title}\n{offer}\n{cta}"
    return f"{title}\n\n{audience}\n{offer}\n\n{cta}\n\n#ローカルAI #業務改善"


def compose_url(channel: str, text: str, url: str) -> str:
    if channel == "x":
        return "https://twitter.com/intent/tweet?" + urlencode({"text": text, "url": url})
    if channel == "linkedin":
        return "https://www.linkedin.com/sharing/share-offsite/?" + urlencode({"url": url})
    if channel == "facebook":
        return "https://www.facebook.com/sharer/sharer.php?" + urlencode({"u": url})
    if channel == "line":
        return "https://social-plugins.line.me/lineit/share?" + urlencode({"url": url, "text": text})
    return ""


def social_package(campaign: dict[str, Any]) -> list[dict[str, str]]:
    public_url = validate_public_landing_url(str(campaign.get("google_site_url", "")))
    campaign_id = str(campaign.get("id", "")).strip()
    if not campaign_id:
        raise ValueError("SNS文案にはキャンペーンIDが必要です")
    result = []
    for channel, spec in CHANNELS.items():
        url = tracking_url(public_url, campaign_id, channel)
        text = post_text(campaign, channel)
        result.append({"channel": channel, "label": spec["label"],
                       "mode": spec["mode"], "variant": "primary",
                       "post_text": text, "tracking_url": url,
                       "compose_url": compose_url(channel, text, url)})
    return result


def social_package_markdown(campaign: dict[str, Any], items: list[dict[str, str]]) -> str:
    sections = []
    for item in items:
        action = item["compose_url"] or "投稿本文と計測URLをコピーして手動投稿"
        sections.append(f"## {item['label']}\n\n### 投稿文案\n\n{item['post_text']}\n\n"
                        f"### 計測URL\n\n{item['tracking_url']}\n\n### 操作\n\n{action}")
    return (f"# {campaign['title']} SNS投稿キット\n\n"
            "自動投稿は行いません。承認後に投稿画面を開き、最終編集と投稿は利用者が行います。\n\n"
            + "\n\n".join(sections) + "\n")


def social_package_json(items: list[dict[str, str]]) -> str:
    return json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=2) + "\n"


def validate_evidence_url(channel: str, value: str) -> str:
    if channel not in CHANNELS:
        raise ValueError("Unsupported social channel")
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in EVIDENCE_HOSTS[channel]:
        raise ValueError(f"{CHANNELS[channel]['label']}の公開投稿HTTPS URLを指定してください")
    return url
