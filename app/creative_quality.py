from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse


DEFAULT_BRAND = {
    "brand_name": "Local Supporter",
    "primary_color": "#155eef",
    "secondary_color": "#172033",
    "accent_color": "#52d6a7",
    "background_color": "#f7faff",
    "tone": "信頼感があり、具体的で、過度な効果を約束しない",
    "font_family": "system-ui, -apple-system, Segoe UI, sans-serif",
}
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
ALLOWED_PROVIDERS = {"canva", "local", "other"}


def _color(value: str, fallback: str) -> str:
    candidate = str(value or fallback).strip()
    if not HEX_COLOR.fullmatch(candidate):
        raise ValueError(f"Invalid brand color: {candidate}")
    return candidate.lower()


def build_brand_profile(values: dict[str, Any] | None = None) -> dict[str, str]:
    values = values or {}
    return {
        "brand_name": str(values.get("brand_name") or DEFAULT_BRAND["brand_name"]).strip()[:120],
        "primary_color": _color(values.get("primary_color", ""), DEFAULT_BRAND["primary_color"]),
        "secondary_color": _color(values.get("secondary_color", ""), DEFAULT_BRAND["secondary_color"]),
        "accent_color": _color(values.get("accent_color", ""), DEFAULT_BRAND["accent_color"]),
        "background_color": _color(values.get("background_color", ""), DEFAULT_BRAND["background_color"]),
        "tone": str(values.get("tone") or DEFAULT_BRAND["tone"]).strip()[:500],
        "font_family": DEFAULT_BRAND["font_family"],
    }


def _luminance(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
              for value in channels]
    return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]


def contrast_ratio(first: str, second: str) -> float:
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return round((high + .05) / (low + .05), 2)


def validate_design_url(value: str, provider: str) -> str:
    url = value.strip()
    if not url and provider == "local":
        return ""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Design URL must be HTTPS")
    if provider == "canva" and not (parsed.hostname == "canva.com" or parsed.hostname.endswith(".canva.com")):
        raise ValueError("Canva design URL must use https://www.canva.com/")
    return url


def validate_image_url(value: str) -> str:
    url = value.strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Creative image URL must be HTTPS")
    return url


def creative_brief_markdown(campaign: dict[str, Any], brand: dict[str, str], provider: str) -> str:
    return f"""# クリエイティブ制作仕様

## 基本データ

- キャンペーン: {campaign['title']}
- 対象: {campaign['audience']}
- 提供価値: {campaign['offer']}
- CTA: {campaign['call_to_action']}
- 制作サービス: {provider}

## ブランド

- 名称: {brand['brand_name']}
- 基調色: {brand['primary_color']}
- 文字色: {brand['secondary_color']}
- アクセント: {brand['accent_color']}
- 背景色: {brand['background_color']}
- 表現: {brand['tone']}

## 制作物

1. LPのファーストビュー用ビジュアル（16:9、文字を画像へ焼き込まない）
2. 提供内容を説明する図解（3要素以内、モバイルでも読める）
3. SNS告知用正方形画像（1080 x 1080）

## 構成基準

- 対象者、課題、提供価値、CTAの順で理解できる視線誘導にする
- CTAは背景とのコントラストを確保し、ページ内で表現を統一する
- 実在しない顧客、受賞、数値実績、導入効果、推薦文を追加しない
- 個人情報、認証情報、非公開の顧客資料を制作サービスへ送らない
- 装飾より読みやすさ、信頼感、問い合わせ導線を優先する

## Canva等への制作指示

「{campaign['title']}」のランディングページ用ビジュアルを作成してください。
対象は「{campaign['audience']}」、提供価値は「{campaign['offer']}」、CTAは
「{campaign['call_to_action']}」です。上記ブランド設定を使用し、誇張や未確認の
実績を加えず、落ち着いたB2B向けデザインにしてください。
"""


def asset_manifest(campaign: dict[str, Any], brand: dict[str, str], provider: str) -> dict[str, Any]:
    return {
        "version": 1,
        "campaign_id": campaign["id"],
        "provider": provider,
        "brand": brand,
        "deliverables": [
            {"key": "hero", "size": "1600x900", "required": False, "status": "pending"},
            {"key": "explanation", "size": "1200x800", "required": False, "status": "pending"},
            {"key": "social_square", "size": "1080x1080", "required": False, "status": "pending"},
        ],
        "public_copy_source": ["title", "audience", "offer", "call_to_action"],
    }


def quality_report_markdown(campaign: dict[str, Any], brand: dict[str, str], *,
                            score: int | None = None, review: str = "") -> str:
    primary_contrast = contrast_ratio(brand["primary_color"], "#ffffff")
    text_contrast = contrast_ratio(brand["secondary_color"], brand["background_color"])
    checks = [
        ("対象顧客", len(str(campaign.get("audience", "")).strip()) >= 3),
        ("提供価値", len(str(campaign.get("offer", "")).strip()) >= 3),
        ("CTA", len(str(campaign.get("call_to_action", "")).strip()) >= 2),
        ("本文コントラスト 4.5以上", text_contrast >= 4.5),
        ("CTAコントラスト 3.0以上", primary_contrast >= 3.0),
    ]
    passed = sum(1 for _, ok in checks if ok)
    baseline = round(passed * 70 / len(checks)) + 15
    current_score = max(0, min(100, int(score if score is not None else baseline)))
    lines = [
        "# クリエイティブ品質レポート", "", f"品質スコア: {current_score} / 100", "",
        f"CTA色と白文字のコントラスト: {primary_contrast}",
        f"本文色と背景色のコントラスト: {text_contrast}", "", "## 自動確認", "",
    ]
    lines.extend(f"- {'PASS' if ok else '要修正'}: {name}" for name, ok in checks)
    lines.extend(["", "## デザインレビュー", "", review.strip() or "Canva等で制作後にレビューを登録してください。", "",
                  "## 承認条件", "", "- 品質スコア70以上", "- CTAと問い合わせ先が基本データと一致",
                  "- 未確認の実績・保証表現がない", "- PC・モバイルで可読", ""])
    return "\n".join(lines)


def automatic_quality_review(campaign: dict[str, Any], brand: dict[str, str]) -> dict[str, Any]:
    """Score the deterministic local visual without claiming human review."""
    text_contrast = contrast_ratio(brand["secondary_color"], brand["background_color"])
    cta_contrast = contrast_ratio(brand["primary_color"], "#ffffff")
    required_copy = all(
        len(str(campaign.get(key, "")).strip()) >= minimum
        for key, minimum in (
            ("title", 2), ("audience", 3), ("offer", 3), ("call_to_action", 2),
        )
    )
    checks = {
        "公開用基本データが揃っている": required_copy,
        "本文コントラストが4.5以上": text_contrast >= 4.5,
        "CTAコントラストが3.0以上": cta_contrast >= 3.0,
        "文字を画像へ焼き込まずHTMLで保持する": True,
        "PC・モバイル用レスポンシブ構成を使う": True,
        "未確認の実績・保証・顧客名を追加しない": True,
    }
    score = round(sum(1 for ok in checks.values() if ok) * 90 / len(checks))
    details = "、".join(f"{name}:{'PASS' if ok else '要修正'}" for name, ok in checks.items())
    review = (
        "Local Supporterの自動レイアウトエンジンが、登録済み公開データとブランド色から"
        "16:9ヒーロー、説明カード、CTA、モバイル表示を生成しました。"
        f" 自動確認={details}。これは自動評価であり、最終承認は人間が行います。"
    )
    return {"score": score, "review": review, "checks": checks}


def creative_view(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not record:
        return None
    result = dict(record)
    try:
        result["brand"] = json.loads(result.pop("brand_json", "{}") or "{}")
    except json.JSONDecodeError:
        result["brand"] = {}
    return result
