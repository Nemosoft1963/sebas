from __future__ import annotations

import html
import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urlparse


PRIVACY_NOTICE = (
    "送信された情報は、相談対応とサービス改善のためにのみ利用します。"
    "本人の同意なく第三者への営業連絡には利用しません。"
)
DISCLAIMER = (
    "本ページの内容は初回相談の案内です。導入効果、適合性、費用、期間を保証するものではなく、"
    "対象業務と安全要件を確認した上で個別に提案します。"
)

DEFAULT_OPERATOR_NAME = 'Example Operator'
DEFAULT_OPERATOR_URL = 'https://example.com/'
DEFAULT_OPERATOR_ADDRESS = 'Example address'


def _required(campaign: dict[str, Any], key: str) -> str:
    value = str(campaign.get(key, "")).strip()
    if not value:
        raise ValueError(f"Landing page requires campaign.{key}")
    return value


def validate_form_url(value: str) -> str:
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"docs.google.com", "forms.gle"}:
        raise ValueError("Landing page inquiry URL must be an HTTPS Google Forms URL")
    return url


def validate_google_site_url(value: str, *, edit: bool = False) -> str:
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "sites.google.com":
        kind = "edit" if edit else "public"
        raise ValueError(f"Google Sites {kind} URL must use https://sites.google.com/")
    return url


def public_operator() -> dict[str, str]:
    name = os.environ.get('PUBLIC_OPERATOR_NAME', DEFAULT_OPERATOR_NAME).strip()
    url = os.environ.get('PUBLIC_OPERATOR_URL', DEFAULT_OPERATOR_URL).strip()
    address = os.environ.get('PUBLIC_OPERATOR_ADDRESS', DEFAULT_OPERATOR_ADDRESS).strip()
    parsed = urlparse(url)
    if not name:
        raise ValueError('Public operator name is required')
    if parsed.scheme != 'https' or not parsed.hostname:
        raise ValueError('Public operator URL must use HTTPS')
    return {'name': name, 'url': url, 'address': address}


def _landing_page_manifest_core(campaign: dict[str, Any]) -> dict[str, Any]:
    creative = campaign.get("creative") if isinstance(campaign.get("creative"), dict) else {}
    approved_creative = creative if creative.get("status") == "approved" else {}
    return {
        "version": 4,
        "campaign_id": _required(campaign, "id"),
        "title": _required(campaign, "title"),
        "audience": _required(campaign, "audience"),
        "offer": _required(campaign, "offer"),
        "call_to_action": _required(campaign, "call_to_action"),
        "inquiry_url": validate_form_url(_required(campaign, "google_form_url")),
        "privacy_notice": PRIVACY_NOTICE,
        "disclaimer": DISCLAIMER,
        "operator": public_operator(),
        "creative": approved_creative,
        "required_public_checks": [
            "https", "title", "call_to_action", "google_form_link", "operator",
            "accepting_responses",
        ],
    }


def landing_asset_version(campaign: dict[str, Any]) -> str:
    canonical = json.dumps(
        _landing_page_manifest_core(campaign), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def landing_page_manifest(campaign: dict[str, Any]) -> dict[str, Any]:
    data = _landing_page_manifest_core(campaign)
    data["asset_version"] = landing_asset_version(campaign)
    return data


def render_landing_page_html(campaign: dict[str, Any]) -> str:
    data = landing_page_manifest(campaign)
    escaped = {key: html.escape(str(value), quote=True) for key, value in data.items()
               if isinstance(value, str)}
    creative = data.get("creative") or {}
    brand = creative.get("brand") if isinstance(creative.get("brand"), dict) else {}
    primary = html.escape(str(brand.get("primary_color") or "#155eef"), quote=True)
    secondary = html.escape(str(brand.get("secondary_color") or "#172033"), quote=True)
    accent = html.escape(str(brand.get("accent_color") or "#52d6a7"), quote=True)
    background = html.escape(str(brand.get("background_color") or "#f7faff"), quote=True)
    brand_name = html.escape(str(brand.get("brand_name") or "Local Supporter"), quote=True)
    operator = data['operator']
    operator_name = html.escape(operator['name'], quote=True)
    operator_url = html.escape(operator['url'], quote=True)
    operator_address = html.escape(operator['address'], quote=True)
    image_url = html.escape(str(creative.get("image_url") or ""), quote=True)
    hero_visual = (
        f'<img class="hero-image" src="{image_url}" alt="" loading="eager">'
        if image_url else
        '<div class="hero-art" aria-hidden="true"><i></i><i></i><i></i><b>LOCAL<br>AI</b></div>'
    )
    return f"""<!doctype html>
<html lang="ja" data-local-supporter-version="{data['asset_version']}"><head><meta charset="utf-8">
<meta name="local-supporter-asset-version" content="{data['asset_version']}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{escaped['offer']}">
<title>{escaped['title']}</title>
<style>
:root{{--ink:{secondary};--muted:#566176;--brand:{primary};--accent:{accent};--soft:{background};--line:#d9e1ee}}
*{{box-sizing:border-box}}body{{margin:0;color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.75;background:#fff}}
.hero{{background:linear-gradient(135deg,var(--soft),#fff);padding:72px 20px;overflow:hidden}}.wrap{{max-width:1040px;margin:auto}}.hero-grid{{display:grid;grid-template-columns:minmax(0,3fr) minmax(260px,2fr);gap:52px;align-items:center}}
.eyebrow{{color:var(--brand);font-weight:700;letter-spacing:.08em}}h1{{font-size:clamp(2rem,5vw,3.6rem);line-height:1.18;margin:.4em 0}}
.lead{{font-size:1.12rem;color:var(--muted);max-width:760px}}section{{padding:54px 20px}}.card{{border:1px solid var(--line);border-radius:18px;padding:28px;background:#fff;box-shadow:0 10px 34px rgba(24,44,80,.07)}}
.cta{{display:inline-block;margin-top:18px;padding:14px 24px;border-radius:10px;background:var(--brand);color:#fff;text-decoration:none;font-weight:700}}
.cta:focus,.cta:hover{{filter:brightness(.86)}}.notice{{font-size:.92rem;color:var(--muted)}}footer{{padding:28px 20px;border-top:1px solid var(--line)}}
.hero-image,.hero-art{{width:100%;aspect-ratio:16/10;object-fit:cover;border-radius:24px;box-shadow:0 24px 70px #17203326}}.hero-art{{position:relative;background:linear-gradient(145deg,var(--brand),var(--ink));overflow:hidden}}.hero-art i{{position:absolute;border:1px solid #ffffff44;border-radius:50%}}.hero-art i:nth-child(1){{width:260px;height:260px;right:-70px;top:-80px}}.hero-art i:nth-child(2){{width:190px;height:190px;left:-40px;bottom:-55px}}.hero-art i:nth-child(3){{width:16px;height:16px;background:var(--accent);border:0;left:22%;top:23%}}.hero-art b{{position:absolute;right:12%;bottom:12%;color:#fff;font-size:clamp(2.4rem,7vw,5rem);line-height:.83;letter-spacing:-.07em}}.quality-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:22px}}.quality-grid article{{border-left:4px solid var(--accent);padding:3px 14px}}.quality-grid h3{{margin:0 0 4px;font-size:1rem}}.quality-grid p{{margin:0;color:var(--muted);font-size:.92rem}}
@media(max-width:720px){{.hero{{padding:48px 18px}}.hero-grid{{grid-template-columns:1fr;gap:30px}}section{{padding:38px 18px}}.cta{{display:block;text-align:center}}.quality-grid{{grid-template-columns:1fr}}}}
</style></head><body>
<header class="hero"><div class="wrap hero-grid"><div><div class="eyebrow">LOCAL AI CONSULTING</div>
<h1>{escaped['title']}</h1><p class="lead">{escaped['audience']}</p>
<a id="inquiry-cta" class="cta" href="{escaped['inquiry_url']}" target="_blank" rel="noopener noreferrer">{escaped['call_to_action']}</a></div>{hero_visual}</div></header>
<main><section><div class="wrap card"><h2>この相談で整理できること</h2><p>{escaped['offer']}</p><div class="quality-grid"><article><h3>対象を整理</h3><p>業務と課題の範囲を確認します。</p></article><article><h3>条件を確認</h3><p>データと安全要件を明確にします。</p></article><article><h3>次の一歩</h3><p>検討事項を具体的な行動へ整理します。</p></article></div></div></section>
<section style="background:var(--soft)"><div class="wrap"><h2>お問い合わせ</h2><p>Googleフォームで現在の課題と検討状況をお知らせください。内容を確認し、個別相談の準備を行います。</p>
<a class="cta" href="{escaped['inquiry_url']}" target="_blank" rel="noopener noreferrer">{escaped['call_to_action']}</a></div></section>
<section><div class="wrap card"><h2>運営会社</h2><p><strong>{operator_name}</strong></p><p>{operator_address}</p>
<p><a href="{operator_url}" target="_blank" rel="noopener noreferrer">運営会社公式サイト</a></p></div></section>
<section><div class="wrap"><h2>情報の取扱い</h2><p class="notice">{escaped['privacy_notice']}</p><p class="notice">{escaped['disclaimer']}</p></div></section></main>
<footer><div class="wrap notice">{brand_name} Premarketing / 運営: <a href="{operator_url}">{operator_name}</a></div></footer></body></html>"""


def render_google_sites_copy(campaign: dict[str, Any]) -> str:
    data = landing_page_manifest(campaign)
    return f"""# {data['title']}

## 対象となる方

{data['audience']}

## この相談で整理できること

{data['offer']}

## お問い合わせ

ボタン表示: {data['call_to_action']}

リンク先: {data['inquiry_url']}

## 情報の取扱い

{data['privacy_notice']}

{data['disclaimer']}

## 運営会社

- 会社名: {data['operator']['name']}
- 所在地: {data['operator']['address']}
- 公式サイト: {data['operator']['url']}

## 公開前チェック

- タイトルとCTAが表示されている
- CTAまたは埋込みフォームから上記Googleフォームを開ける
- プライバシー文と免責文が表示されている
- 運営会社名と公式サイトが表示されている
- モバイルプレビューで文字とボタンが切れていない
- 公開URLをログアウト状態で開ける
"""


def manifest_json(campaign: dict[str, Any]) -> str:
    return json.dumps(landing_page_manifest(campaign), ensure_ascii=False, indent=2) + "\n"


def google_sites_automation_payload(campaign: dict[str, Any]) -> dict[str, Any]:
    data = landing_page_manifest(campaign)
    return {
        "version": 4,
        "campaign_id": data["campaign_id"],
        "asset_version": data["asset_version"],
        "title": data["title"],
        "embed_html": render_landing_page_html(campaign),
        "inquiry_url": data["inquiry_url"],
        "required_texts": [
            data["title"], data["audience"], data["offer"],
            data["call_to_action"], data["privacy_notice"], data["disclaimer"],
            data["operator"]["name"], data["operator"]["address"],
        ],
        "required_links": [data["inquiry_url"], data["operator"]["url"]],
        "manual_publish_required": True,
    }


def verify_public_landing_html(body: str, campaign: dict[str, Any]) -> dict[str, bool]:
    data = landing_page_manifest(campaign)
    decoded = html.unescape(body)
    versions = set(re.findall(
        r"""data-local-supporter-version\s*=\s*["']([0-9a-f]{12})["']""",
        decoded, flags=re.IGNORECASE,
    ))
    expected_version = data["asset_version"]
    required_sections = (
        "この相談で整理できること", "お問い合わせ", "運営会社", "情報の取扱い",
    )
    return {
        "asset_version": expected_version in versions,
        "single_asset_version": versions == {expected_version},
        "title": data["title"] in decoded,
        "audience": data["audience"] in decoded,
        "offer": data["offer"] in decoded,
        "call_to_action": data["call_to_action"] in decoded,
        "google_form_link": data["inquiry_url"] in decoded,
        "privacy_notice": data["privacy_notice"] in decoded,
        "disclaimer": data["disclaimer"] in decoded,
        "operator_name": data["operator"]["name"] in decoded,
        "operator_address": data["operator"]["address"] in decoded,
        "operator_url": data["operator"]["url"] in decoded,
        "section_structure": all(section in decoded for section in required_sections),
    }
