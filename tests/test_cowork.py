from pathlib import Path

import pytest
import yaml

import app.cowork as cowork


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_cowork_status_reports_local_security_boundary(monkeypatch):
    class HealthyLlm:
        async def health(self):
            return True

    async def healthy_probe(name, internal_url, public_url):
        return {
            "id": name,
            "ready": True,
            "status_code": 200,
            "latency_ms": 1.0,
            "url": public_url,
        }

    monkeypatch.setattr(cowork, "probe_service", healthy_probe)
    result = await cowork.build_status(
        HealthyLlm(), "gpt-oss:20b", "http://open-webui:8080", "http://cptr:8000"
    )

    assert result["controller"] == {
        "provider": "Local Ollama",
        "ready": True,
        "model": "gpt-oss:20b",
    }
    assert result["workspace"] == "/workspace"
    assert result["security"] == {
        "localhost_only": True,
        "docker_socket": False,
        "host_mount": "C:/Users/example/LocalCowork/workspace",
    }
    assert {service["id"] for service in result["services"]} == {
        "open-webui",
        "computer",
    }


def test_cowork_routes_are_declared():
    source = (ROOT / "app" / "web.py").read_text(encoding="utf-8")
    assert '@app.get("/cowork")' in source
    assert '@app.get("/api/cowork/status")' in source


def test_compose_limits_ports_and_workspace_mount():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    assert compose["services"]["web"]["ports"] == ["127.0.0.1:8099:8000"]
    assert compose["services"]["open-webui"]["ports"] == [
        "127.0.0.1:${OPEN_WEBUI_PORT:-3000}:8080"
    ]
    assert compose["services"]["cptr"]["ports"] == [
        "127.0.0.1:${CPTR_PORT:-8000}:8000"
    ]
    publisher = compose["services"]["google-publisher-browser"]
    assert publisher["ports"] == [
        "127.0.0.1:${GOOGLE_BROWSER_PORT:-8010}:8000"
    ]
    assert publisher["build"]["dockerfile"] == "docker/google-browser.Dockerfile"
    assert publisher["shm_size"] == "1gb"
    assert "chromium" in publisher["healthcheck"]["test"][1]
    assert "/tmp/.X11-unix/X99" in publisher["healthcheck"]["test"][1]

    mounts = compose["services"]["cptr"]["volumes"]
    assert "${WORKSPACE_PATH:-C:/Users/example/LocalCowork/workspace}:/workspace" in mounts
    serialized = yaml.safe_dump(compose)
    assert "/var/run/docker.sock" not in serialized
    assert "C:\\" not in serialized
    assert "D:\\" not in serialized


def test_google_publisher_image_contains_managed_chrome_runtime():
    dockerfile = (ROOT / "docker" / "google-browser.Dockerfile").read_text(encoding="utf-8")
    bootstrap = (ROOT / "docker" / "google_browser_bootstrap.py").read_text(encoding="utf-8")
    wrapper = (ROOT / "docker" / "google-chrome-wrapper.sh").read_text(encoding="utf-8")
    compatibility_patch = (ROOT / "docker" / "patch_cptr_chromium.py").read_text(
        encoding="utf-8"
    )

    assert "chromium" in dockerfile
    assert "xvfb-run -n 99" in dockerfile
    assert "1024x720x24" in dockerfile
    assert 'app_config["browser.tab_default_mode"] = "chrome"' in bootstrap
    assert 'app_config["browser.quality.default"] = "low"' in bootstrap
    assert 'app_config["browser.quality.max_resolution"] = 720' in bootstrap
    assert "remove_invalid_quality_profile_override" in bootstrap
    assert "--no-sandbox" in wrapper
    assert "/usr/bin/chromium" in wrapper
    assert "patch_cptr_chromium.py" in dockerfile
    assert 'max(1, int(profile["max_touch_points"] or 0))' in compatibility_patch
    assert "https://sites.google.com/new" in compatibility_patch
    assert "remove_stale_chromium_locks" in bootstrap
    assert "SingletonLock" in bootstrap
    assert "remove_stale_display_locks" in bootstrap
    assert "/tmp/.X99-lock" in bootstrap
    manifest = (ROOT / "docker" / "google-sites-extension" / "manifest.json").read_text(
        encoding="utf-8"
    )
    assistant = (ROOT / "docker" / "google-sites-extension" / "content.js").read_text(
        encoding="utf-8"
    )
    assert "sites.google.com" in manifest
    assert "Local Supporter：下書きを配置" in assistant
    assert 'location.assign("https://sites.new")' in assistant
    assert "localSupporterSitesDraftPendingAt" in assistant
    assert '"[role=textbox],[contenteditable=true]"' in assistant
    assert "textEditable(pageTitle)" in assistant
    assert "activate(pageTitleAnchor)" in assistant
    assert "inserted_pending_preview" in assistant
    assert "await waitFor(driveSaved" in assistant
    assert "function previewOpen()" in assistant
    assert "syncPanelWithPreview" in assistant
    assert "byTextWithin(dialog" in assistant
    assert "embedDialog()" in assistant
    assert "previewReady()" in assistant
    assert 'byText(["公開", "publish"])' not in assistant
