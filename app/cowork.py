from __future__ import annotations

import asyncio
import time

import httpx


async def probe_service(name: str, internal_url: str, public_url: str) -> dict:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.get(internal_url)
        ready = 200 <= response.status_code < 500
        return {
            "id": name,
            "ready": ready,
            "status_code": response.status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "url": public_url,
        }
    except httpx.HTTPError as exc:
        return {
            "id": name,
            "ready": False,
            "status_code": None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "url": public_url,
            "error": str(exc),
        }


async def build_status(llm, model: str, open_webui_url: str, computer_url: str) -> dict:
    open_webui, computer = await asyncio.gather(
        probe_service("open-webui", open_webui_url, "http://127.0.0.1:3000"),
        probe_service("computer", computer_url, "http://127.0.0.1:8000"),
    )
    return {
        "status": "ok",
        "controller": {
            "ready": await llm.health(),
            "provider": "Local Ollama",
            "model": model,
        },
        "services": [open_webui, computer],
        "workspace": "/workspace",
        "security": {
            "localhost_only": True,
            "docker_socket": False,
            "host_mount": "C:/Users/example/LocalCowork/workspace",
        },
    }
