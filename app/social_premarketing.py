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

# B1: 登録台帳の platform 表記(先頭大文字)と SNSキットの channel キー(小文字)の対応。
# 登録側の PLATFORMS は {"X","LinkedIn","Facebook","LINE","Instagram","Threads"}。
_PLATFORM_TO_CHANNEL = {
    "x": "x",
    "linkedin": "linkedin",
    "facebook": "facebook",
    "line": "line",
    "instagram": "instagram",
    "threads": "threads",
}


def _platform_to_channel(platform: object) -> str:
    return _PLATFORM_TO_CHANNEL.get(str(platform or "").strip().lower(), "")


def _scoped_selections(current: list[dict], campaign_id: str | None) -> tuple[list[dict], str]:
    """現在の版から対象の選択だけを抜く。campaign_id を持つ行があればそれを、
    なければPJ全体(空)の行を使う。純粋関数(読取も代替値の補完もしない)。"""
    rows = list(current or [])
    wanted = str(campaign_id or "")
    if wanted:
        camp_rows = [row for row in rows
                     if isinstance(row, dict) and str(row.get("campaign_id") or "") == wanted]
        if camp_rows:
            return camp_rows, "campaign"
        project_rows = [row for row in rows
                        if isinstance(row, dict) and not str(row.get("campaign_id") or "")]
        return project_rows, "project"
    project_rows = [row for row in rows
                    if isinstance(row, dict) and not str(row.get("campaign_id") or "")]
    if project_rows:
        return project_rows, "project"
    return rows, "project"


def resolve_channel_selection(memory_path: object, project_id: str,
                              campaign_id: str | None = None) -> dict:
    """台帳の channel_selection の現在の版から有効な媒体集合を求める。

    戻り値の status は selected / not_configured / none_needed / unreadable。
    読取失敗(RegistryReadError)は unreadable として返し、『媒体未設定』
    『投稿不要』に倒さない。代替値(全媒体・空)での続行はしない。
    """
    from app.contact_registry import RegistryReadError, list_channel_selections

    try:
        viewed = list_channel_selections(memory_path, project_id)
    except RegistryReadError as exc:
        return {"status": "unreadable", "channels": [], "scope": "",
                "reason_ja": "媒体の選択を読めないためSNS投稿キットを生成できません",
                "error_kind": exc.kind}
    current = viewed.get("current") if isinstance(viewed, dict) else []
    if not current:
        return {"status": "not_configured", "channels": [], "scope": "",
                "reason_ja": "媒体が未設定のため既定の全媒体で生成した"}
    scoped, scope = _scoped_selections(current, campaign_id)
    if not scoped:
        return {"status": "not_configured", "channels": [], "scope": scope,
                "reason_ja": "媒体が未設定のため既定の全媒体で生成した"}
    ordered = sorted(
        (row for row in scoped if isinstance(row, dict) and bool(row.get("enabled"))),
        key=lambda row: (int(row.get("priority") or 0),
                         str(row.get("platform") or "")),
    )
    channels: list[str] = []
    account_refs: dict[str, str] = {}
    priorities: dict[str, int] = {}
    for row in ordered:
        channel = _platform_to_channel(row.get("platform"))
        if not channel or channel not in CHANNELS or channel in channels:
            continue
        channels.append(channel)
        account_refs[channel] = str(row.get("account_ref") or "")
        try:
            priorities[channel] = int(row.get("priority") or 0)
        except (TypeError, ValueError):
            priorities[channel] = 0
    if not channels:
        return {"status": "none_needed", "channels": [], "scope": scope,
                "reason_ja": "投稿不要として登録済みのためSNS投稿キットを生成しません"}
    return {"status": "selected", "channels": channels, "scope": scope,
            "account_refs": account_refs, "priorities": priorities,
            "reason_ja": "選択した媒体だけで生成します"}


def unselected_channel_drift(selected_channels: object,
                             shares: list[dict] | None) -> list[str]:
    """登録された選択に無い媒体の共有行の channel を列挙する純粋関数。
    差分の提示用。削除はしない(呼出側で削除しないこと)。"""
    try:
        selected = {str(item) for item in (selected_channels or [])}
    except TypeError:
        selected = set()
    found: set[str] = set()
    for share in shares or []:
        if isinstance(share, dict) and str(share.get("channel") or ""):
            found.add(str(share.get("channel")))
    return sorted(found - selected)


def channel_identity_statuses(memory_path: object, project_id: str,
                              campaign_id: str | None,
                              channels: list[str] | tuple[str, ...]) -> dict:
    """選択した媒体ごとの公開IDの状態を返す。

    戻り値は {channel: {"identity_status": registered/fictional/unregistered,
    "reason_ja": ...}}。fictional の理由は『架空・公開不可』。
    台帳が読めないときは RegistryReadError を送出し、unregistered に倒さない。
    証跡の判定そのものは変えない(表示用の注記だけ)。
    """
    from app.contact_registry import list_channel_selections, list_public_identities

    wanted = [str(item) for item in (channels or []) if str(item) in CHANNELS]
    selections = list_channel_selections(memory_path, project_id)
    identities = list_public_identities(memory_path, project_id)
    sel_current = selections.get("current") if isinstance(selections, dict) else []
    id_current = identities.get("current") if isinstance(identities, dict) else []
    scoped, _scope = _scoped_selections(sel_current or [], campaign_id)
    by_channel: dict[str, dict] = {}
    for row in scoped:
        if not isinstance(row, dict):
            continue
        channel = _platform_to_channel(row.get("platform"))
        if channel and channel not in by_channel:
            by_channel[channel] = row
    id_scoped, _ = _scoped_selections(id_current or [], campaign_id)
    identities_list = [row for row in id_scoped if isinstance(row, dict)]
    by_ref = {str(row.get("identity_id") or ""): row for row in identities_list
              if str(row.get("identity_id") or "")}
    by_handle = {str(row.get("handle") or ""): row for row in identities_list
                 if str(row.get("handle") or "")}
    result: dict[str, dict] = {}
    for channel in wanted:
        ref = str((by_channel.get(channel) or {}).get("account_ref") or "")
        if ref:
            matched = by_ref.get(ref) or by_handle.get(ref)
        else:
            candidates = [row for row in identities_list
                          if _platform_to_channel(row.get("platform")) == channel]
            if len(candidates) > 1:
                result[channel] = {"identity_status": "ambiguous",
                                   "reason_ja": "公開IDが複数ある"}
                continue
            matched = candidates[0] if len(candidates) == 1 else None
        if matched is None:
            result[channel] = {"identity_status": "unregistered",
                               "reason_ja": "未登録"}
            continue
        kind = str(matched.get("source_kind") or "").lower()
        if kind == "fictional":
            result[channel] = {"identity_status": "fictional",
                               "reason_ja": "架空・公開不可"}
        elif kind == "retired":
            result[channel] = {"identity_status": "retired",
                               "reason_ja": "失効"}
        elif kind in ("registered", "verified"):
            result[channel] = {"identity_status": "registered",
                               "reason_ja": "登録済み"}
        else:
            result[channel] = {"identity_status": "unregistered",
                               "reason_ja": "未登録"}
    return result


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


def social_package(campaign: dict[str, Any],
                    channels: list[str] | tuple[str, ...] | None = None) -> list[dict[str, str]]:
    public_url = validate_public_landing_url(str(campaign.get("google_site_url", "")))
    campaign_id = str(campaign.get("id", "")).strip()
    if not campaign_id:
        raise ValueError("SNS文案にはキャンペーンIDが必要です")
    if channels is None:
        wanted = list(CHANNELS)
    else:
        wanted = list(channels)
        for name in wanted:
            if name not in CHANNELS:
                raise ValueError("Unsupported social channel")
    result = []
    for channel in wanted:
        spec = CHANNELS[channel]
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
