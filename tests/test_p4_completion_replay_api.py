from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
import app.web as web

class Memory:
    path='unused.db'
    def get_project(self,p): return {'id':p} if p=='p' else None

def test_completion_replay_api(monkeypatch):
    memory=Memory(); manager=SimpleNamespace(memory=memory)
    monkeypatch.setattr(web,'memory',memory);monkeypatch.setattr(web,'orchestrator',manager)
    with patch('app.completion_replay.preview',return_value={'read_only':True,'crossed_approval_boundary':False}) as fn:
        response=TestClient(web.app).get('/api/projects/p/completion-replay')
    assert response.status_code==200 and response.json()['read_only'] is True
    fn.assert_called_once_with(manager,'p')
