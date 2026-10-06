from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import app.web as web


class Memory:
    def __init__(self, path): self.path = path
    def get_project(self, pid): return {"id": pid, "name": "P"} if pid == "p" else None
    def add_event(self, *args, **kwargs): return None


def test_auto_resume_api_disabled_enable_run_and_actor_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALSAPORTER_AUTO_RESUME_AVAILABLE", "1")
    memory = Memory(tmp_path / "api.db")
    manager = SimpleNamespace(memory=memory)
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app)

    response = client.get("/api/projects/p/auto-resume")
    assert response.status_code == 200 and response.json() == {"enabled": False, "records": []}
    with patch("app.safe_auto_resume.resume", AsyncMock(return_value={"status": "disabled"})) as mocked:
        response = client.post("/api/projects/p/auto-resume/run")
        assert response.status_code == 200 and response.json()["status"] == "disabled"
        mocked.assert_awaited_once_with(manager, "p")

    assert client.post("/api/projects/p/auto-resume/enable", json={"enabled": True}).status_code == 422
    # 計画本体を持たない案件は運用フラグがONでも有効化できない。
    denied = client.post("/api/projects/p/auto-resume/enable",
                         json={"enabled": True, "actor": "operator"})
    assert denied.status_code == 409
    assert client.get("/api/projects/p/auto-resume").json()["enabled"] is False


def test_auto_resume_api_server_flag_off_is_read_only(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALSAPORTER_AUTO_RESUME_AVAILABLE", raising=False)
    memory = Memory(tmp_path / "flag_off.db")
    manager = SimpleNamespace(memory=memory)
    monkeypatch.setattr(web, "memory", memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app)
    assert client.get("/api/projects/p/auto-resume").json() == {"enabled": False, "records": []}
    response = client.post("/api/projects/p/auto-resume/enable",
                           json={"enabled": True, "actor": "operator"})
    assert response.status_code == 409
    assert client.post("/api/projects/p/auto-resume/run").json()["status"] == "disabled"
    assert not (tmp_path / "flag_off.db.auto_resume.sqlite3").exists()
