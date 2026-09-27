from __future__ import annotations

import asyncio
import json
from typing import Any

from app.external_ai import PROVIDERS, call_provider_with_metadata


COMPONENT_ROLES = (
    ("copy", "訴求文編集"),
    ("visual_direction", "ビジュアル構成"),
    ("independent_review", "独立品質批評"),
)


def public_creative_data(campaign: dict[str, Any], brand: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "title": str(campaign.get("title") or "").strip(),
        "audience": str(campaign.get("audience") or "").strip(),
        "offer": str(campaign.get("offer") or "").strip(),
        "call_to_action": str(campaign.get("call_to_action") or "").strip(),
        "brand": {
            key: str(brand.get(key) or "").strip()
            for key in (
                "brand_name", "primary_color", "secondary_color", "accent_color",
                "background_color", "tone",
            )
        },
    }
    instruction = str(campaign.get("landing_revision_instruction") or "").strip()
    if instruction:
        payload["recompose_instruction"] = instruction
    return payload


def _prompt(role: str, payload: dict[str, Any]) -> tuple[str, str]:
    system = (
        "あなたはマーケティング制作の部品担当です。与えられた公開用データだけを使い、"
        "顧客名、導入実績、受賞、保証、価格、数値効果を追加しないでください。"
        "認証情報や非公開情報を要求しないでください。日本語で簡潔に回答してください。"
    )
    instructions = {
        "copy": "見出し、補助見出し、3つの説明カード文、CTA補助文をJSON形式で提案してください。",
        "visual_direction": "16:9ヒーロー画像、図解、SNS正方形画像の構図・余白・色・画像生成指示をJSON形式で提案してください。画像内文字は使いません。",
        "independent_review": "想定デザインを事実性、視線誘導、ブランド、変換導線、アクセシビリティの観点で批評し、修正条件をJSON形式で返してください。",
    }
    return system, instructions[role] + "\n\n公開用データ:\n" + json.dumps(
        payload, ensure_ascii=False, indent=2,
    )


async def generate_creative_components(
    campaign: dict[str, Any], brand: dict[str, Any], provider_ids: list[str],
) -> list[dict[str, Any]]:
    selected = [
        provider_id for provider_id in dict.fromkeys(provider_ids)
        if provider_id in PROVIDERS and PROVIDERS[provider_id].configured
    ][:5]
    if not selected:
        return []
    payload = public_creative_data(campaign, brand)

    async def one_role(index: int) -> dict[str, Any]:
        role, label = COMPONENT_ROLES[index]
        system, prompt = _prompt(role, payload)
        ordered = selected[index % len(selected):] + selected[:index % len(selected)]
        attempts: list[dict[str, str]] = []
        for provider_id in ordered[:2]:
            provider = PROVIDERS[provider_id]
            try:
                response = await call_provider_with_metadata(
                    provider_id, prompt, system, max_tokens=1400,
                    reasoning_effort="medium" if provider_id == "chatgpt" else None,
                )
                return {
                    "role": role, "role_label": label, "provider": provider_id,
                    "provider_label": provider.label, "model": response.model,
                    "ok": True, "content": response.text[:16000], "attempts": attempts,
                }
            except Exception as exc:
                attempts.append({
                    "provider": provider_id, "provider_label": provider.label,
                    "error": str(exc)[:2000],
                })
        return {
            "role": role, "role_label": label, "provider": "",
            "provider_label": "", "model": "", "ok": False,
            "error": "利用可能な外部AIで部品を生成できませんでした",
            "attempts": attempts,
        }

    return await asyncio.gather(*(one_role(i) for i in range(len(COMPONENT_ROLES))))


def component_summary(components: list[dict[str, Any]]) -> str:
    successful = [item for item in components if item.get("ok")]
    if not successful:
        return "外部AI部品は取得できず、ローカルデザインへフォールバックしました。"
    names = "、".join(
        f"{item['provider_label']}:{item['role_label']}" for item in successful
    )
    return f"外部AIが公開用データだけから制作部品を生成しました（{names}）。"
