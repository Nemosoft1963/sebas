import time

from app.automatic_triz import approved_rag_for_failure
from app.experience_memory import ExperienceMemory, memory_scope


class FixedIndex:
    def __init__(self, ids):
        self.ids = ids

    def search(self, *args):
        return self.ids


def test_triz_rag_uses_only_approved_current_project_and_input(tmp_path):
    memory = ExperienceMemory(tmp_path, {})
    store = memory.store
    ids = {}
    for key, project, kind, applicability in (
        ('good', 'p', 'success', {'input_version': 'current'}),
        ('pending', 'p', 'success', {}),
        ('revoked', 'p', 'success', {}),
        ('other_project', 'other', 'success', {}),
        ('old_input', 'p', 'success', {'input_version': 'old'}),
        ('raw_external', 'p', 'external', {}),
    ):
        rid = store.add(project, kind, key, applicability, {'validation': 'test'})
        ids[key] = rid
        if key != 'pending':
            store.review(project, rid, 'verified', 'reviewer', 'verified evidence', time.time() + 3600)
    store.review('p', ids['revoked'], 'revoked', 'reviewer', 'retracted', 0)
    memory.index = FixedIndex(list(ids.values()))
    problem = {'goal': 'goal', 'improve': 'improve', 'evidence': 'failed'}
    with memory_scope(memory, 'p', 'current', mode='enforce'):
        result = approved_rag_for_failure(problem)
    assert result == {'status': 'matched', 'references': [ids['good']], 'lessons': ['good']}
    with memory_scope(memory, 'p', 'current', mode='shadow'):
        assert approved_rag_for_failure(problem)['status'] == 'off'
    assert approved_rag_for_failure(problem)['references'] == []


def test_triz_rag_unavailable_does_not_claim_recovery(tmp_path):
    memory = ExperienceMemory(tmp_path, {})
    rid = memory.store.add('p', 'success', 'lesson', {}, {})
    memory.store.review('p', rid, 'verified', 'reviewer', 'proof', time.time() + 3600)
    class BrokenIndex:
        def search(self, *args):
            raise RuntimeError('embedding offline')
    memory.index = BrokenIndex()
    with memory_scope(memory, 'p', 'v', mode='enforce'):
        result = approved_rag_for_failure({'goal': 'test'})
    assert result['status'] == 'unavailable'
    assert result['references'] == []
    assert result['lessons'] == []


def test_readiness_reports_rag_ready_before_first_triz_failure(tmp_path):
    from types import SimpleNamespace
    from app.workflow_readiness import _triz_view
    memory_path = tmp_path / 'conversations.db'
    (tmp_path / 'experience_memory.json').write_text(
        '{"projects":{"p":"enforce"},"embedding_model":"nomic-embed-text:latest"}',
        encoding='utf-8',
    )
    manager = SimpleNamespace(memory=SimpleNamespace(path=memory_path))
    view = _triz_view(manager, 'p')
    assert view['rag_status'] == 'ready'
    assert view['recovery_success'] is False
    assert view['rag_reference_count'] == 0


def test_triz_generation_requires_explicit_project_switch(tmp_path):
    from app.automatic_triz import triz_generation_enabled
    memory_path = tmp_path / 'conversations.db'
    assert triz_generation_enabled(memory_path, 'p') is False
    (tmp_path / 'automatic_triz.json').write_text(
        '{"projects":{"p":true,"other":false}}', encoding='utf-8'
    )
    assert triz_generation_enabled(memory_path, 'p') is True
    assert triz_generation_enabled(memory_path, 'other') is False
    assert triz_generation_enabled(memory_path, 'unlisted') is False
