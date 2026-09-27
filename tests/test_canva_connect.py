import asyncio

import httpx

from app.canva_connect import CanvaConnectClient, CanvaConnectConfig, canva_status


def test_canva_status_requires_token_and_template():
    status = canva_status(CanvaConnectConfig("", "template"))
    assert status["configured"] is False
    assert status["template_configured"] is True
    assert status["token_configured"] is False
    assert status["missing"] == ["CANVA_ACCESS_TOKEN"]
    assert "CANVA_ACCESS_TOKEN" in status["message"]

    empty = canva_status(CanvaConnectConfig("", ""))
    assert empty["missing"] == ["CANVA_ACCESS_TOKEN", "CANVA_BRAND_TEMPLATE_ID"]


def test_canva_autofill_uses_only_matching_public_fields():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET" and request.url.path.endswith("/dataset"):
            return httpx.Response(200, json={
                "dataset": {
                    "TITLE": {"type": "text"},
                    "CTA": {"type": "text"},
                    "PRIVATE": {"type": "text"},
                }
            })
        if request.method == "POST":
            return httpx.Response(200, json={"job": {"id": "job-1", "status": "in_progress"}})
        return httpx.Response(200, json={
            "job": {
                "id": "job-1", "status": "success",
                "result": {"id": "design-1", "edit_url": "https://www.canva.com/design/design-1"},
            }
        })

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = CanvaConnectClient(
                CanvaConnectConfig("token", "template", api_base="https://api.canva.test"),
                client=http,
            )
            return await client.create_from_public_data({
                "title": "公開タイトル", "audience": "公開対象",
                "offer": "公開オファー", "call_to_action": "相談する",
            })

    result = asyncio.run(run())
    assert result["design_id"] == "design-1"
    assert result["filled_fields"] == ["TITLE", "CTA"]
    assert len(calls) == 3
