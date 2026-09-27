from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx


class CanvaConnectError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CanvaConnectConfig:
    access_token: str
    brand_template_id: str
    api_base: str = "https://api.canva.com/rest/v1"
    timeout_seconds: float = 30
    max_attempts: int = 4

    @classmethod
    def from_env(cls) -> "CanvaConnectConfig":
        return cls(
            access_token=os.getenv("CANVA_ACCESS_TOKEN", "").strip(),
            brand_template_id=os.getenv("CANVA_BRAND_TEMPLATE_ID", "").strip(),
            api_base=os.getenv("CANVA_API_BASE", "https://api.canva.com/rest/v1").rstrip("/"),
            timeout_seconds=float(os.getenv("CANVA_API_TIMEOUT", "30")),
            max_attempts=max(1, min(8, int(os.getenv("CANVA_API_MAX_ATTEMPTS", "4")))),
        )

    @property
    def configured(self) -> bool:
        return bool(self.access_token and self.brand_template_id)


def canva_status(config: CanvaConnectConfig | None = None) -> dict[str, Any]:
    config = config or CanvaConnectConfig.from_env()
    missing = []
    if not config.access_token:
        missing.append("CANVA_ACCESS_TOKEN")
    if not config.brand_template_id:
        missing.append("CANVA_BRAND_TEMPLATE_ID")
    return {
        "configured": config.configured,
        "template_configured": bool(config.brand_template_id),
        "token_configured": bool(config.access_token),
        "mode": "connect_api_autofill",
        "missing": missing,
        "message": "Canva Connect API接続済み" if not missing else
                   "Canva連携に必要な設定が不足しています: " + ", ".join(missing),
    }


class CanvaConnectClient:
    def __init__(self, config: CanvaConnectConfig | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.config = config or CanvaConnectConfig.from_env()
        self.client = client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        if not self.config.configured:
            raise CanvaConnectError("Canva Connect API is not configured")
        headers = dict(kwargs.pop("headers", {}))
        headers.update({
            "Authorization": f"Bearer {self.config.access_token}",
            "Content-Type": "application/json",
        })
        last_error = ""
        for attempt in range(self.config.max_attempts):
            response = await self.client.request(
                method, f"{self.config.api_base}{path}", headers=headers, **kwargs,
            )
            if response.status_code < 400:
                return response.json()
            last_error = response.text.replace("\n", " ")[:1000]
            if response.status_code not in {408, 429, 500, 502, 503, 504}:
                break
            await asyncio.sleep(min(8, 2 ** attempt))
        raise CanvaConnectError(f"Canva API failed: {last_error or 'unknown error'}")

    async def _poll(self, path: str) -> dict[str, Any]:
        for attempt in range(self.config.max_attempts + 2):
            data = await self._request("GET", path)
            job = data.get("job") or {}
            status = str(job.get("status") or "")
            if status in {"success", "failed"}:
                if status == "failed":
                    raise CanvaConnectError(f"Canva job failed: {job.get('error') or 'unknown'}")
                return data
            await asyncio.sleep(min(10, 2 ** attempt))
        raise CanvaConnectError("Canva job did not complete within the retry budget")

    async def create_from_public_data(self, public_data: dict[str, str]) -> dict[str, Any]:
        template_id = self.config.brand_template_id
        schema_response = await self._request(
            "GET", f"/brand-templates/{template_id}/dataset",
        )
        schema = schema_response.get("dataset") or {}
        aliases = {
            "title": ("title", "headline", "campaign"),
            "audience": ("audience", "target"),
            "offer": ("offer", "body", "description", "value"),
            "call_to_action": ("call_to_action", "cta", "button"),
        }
        data: dict[str, dict[str, str]] = {}
        for field_name, definition in schema.items():
            if not isinstance(definition, dict) or definition.get("type") != "text":
                continue
            normalized = field_name.casefold().replace(" ", "_")
            source_key = next(
                (key for key, names in aliases.items() if normalized in names), None,
            )
            if source_key and public_data.get(source_key):
                data[field_name] = {"type": "text", "text": public_data[source_key]}
        if not data:
            raise CanvaConnectError(
                "Canva template has no matching text fields: TITLE, AUDIENCE, OFFER, CTA"
            )
        created = await self._request("POST", "/autofills", json={
            "type": "create_from_brand_template",
            "brand_template_id": template_id,
            "data": data,
        })
        job_id = str((created.get("job") or {}).get("id") or "")
        if not job_id:
            raise CanvaConnectError("Canva did not return an autofill job id")
        completed = await self._poll(f"/autofills/{job_id}")
        job = completed.get("job") or {}
        design = job.get("result") or job.get("design") or {}
        return {
            "job_id": job_id,
            "design_id": str(design.get("id") or job.get("design_id") or ""),
            "design_url": str(
                design.get("edit_url") or design.get("url") or job.get("design_url") or ""
            ),
            "filled_fields": list(data),
        }
