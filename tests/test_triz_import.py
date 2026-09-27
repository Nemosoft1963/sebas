import hashlib
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.agent_examples_api import install as install_examples
from app.procedure_learning_api import install as install_learning
from app.triz_import import (
    DEFAULT_HUMAN_CHECKS,
    export_item_to_values,
    load_export_items,
    source_url_from_evidence,
    verify_export_file,
)

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / 'inputs' / 'triz-problems-export.json'
META = ROOT / 'inputs' / 'triz-problems-export.meta.json'


def load_script():
    import importlib.util
    path = ROOT / 'scripts' / 'import_triz_problems.py'
    spec = importlib.util.spec_from_file_location('import_triz_problems', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_item(**overrides):
    item = {
        'domain': 'software',
        'goal': '課題を定義する',
        'improve': '読みやすさ',
        'worsens': '詳細の欠落',
        'ideal': '必要な内容を把握できる',
        'physical': '短く詳しく',
        'constraints': '事実を変えない',
        'resources': '登録テキスト',
        'evidence': '出典URL: https://example.invalid/a\n根拠: 引用',
    }
    item.update(overrides)
    return item


def test_physical_none_false_and_empty_become_blank():
    for raw in (None, 'False', 'false', '', False):
        values = export_item_to_values(sample_item(physical=raw))
        assert values['physical'] == ''


def test_physical_plain_text_is_kept():
    text = 'Floating-point drift, rotation cost, attention computation'
    values = export_item_to_values(sample_item(physical=text))
    assert values['physical'] == text


def test_rejects_overlong_and_missing_and_unknown_domain():
    with pytest.raises(ValueError):
        export_item_to_values(sample_item(goal='x' * 2501))
    with pytest.raises(ValueError):
        export_item_to_values(sample_item(goal=''))
    with pytest.raises(ValueError):
        export_item_to_values(sample_item(goal=None))
    with pytest.raises(ValueError, match='未知のdomain'):
        export_item_to_values(sample_item(domain='hardware'))
    with pytest.raises(ValueError, match='未知のdomain'):
        export_item_to_values(sample_item(domain='not-a-domain'))


def test_human_checks_state_automatic_generation():
    values = export_item_to_values(sample_item())
    assert values['human_checks'] == DEFAULT_HUMAN_CHECKS
    assert '自動生成' in values['human_checks']
    assert '人が内容を確認済みであることや承認済みであることを意味しません' in values['human_checks']
    assert values['max_seconds'] == 300


def test_verify_export_file_rejects_mismatch(tmp_path):
    export = tmp_path / 'export.json'
    meta = tmp_path / 'meta.json'
    export.write_text('{"export":{"items":[]}}', encoding='utf-8')
    meta.write_text(json.dumps({'sha256': '0' * 64}), encoding='utf-8')
    with pytest.raises(ValueError, match='SHA-256'):
        verify_export_file(export, meta)


def test_verify_export_file_accepts_real_inputs():
    verify_export_file(EXPORT, META)


def test_real_export_items_all_convert():
    items = load_export_items(EXPORT)
    assert len(items) == 145
    for item in items:
        values = export_item_to_values(item)
        assert values['physical'] != 'None'
        assert values['domain'] == item['domain']
        assert values['goal'] == str(item['goal']).strip()
        assert source_url_from_evidence(values['evidence']).startswith('http')


def test_dry_run_does_not_call_request():
    script = load_script()

    def boom(*_a, **_k):
        raise AssertionError('HTTPリクエストが発生してはいけない')

    code = script.main(
        ['--project', 'p', '--file', str(EXPORT), '--meta', str(META), '--dry-run', '--limit', '3'],
        request=boom,
        sleep=lambda _s: None,
    )
    assert code == 0


def test_dry_run_reports_conversion_failure(tmp_path):
    script = load_script()
    items = [sample_item(), sample_item(domain='nope', evidence='出典URL: https://example.invalid/b')]
    export = tmp_path / 'export.json'
    payload = json.dumps({'export': {'items': items}}, ensure_ascii=False).encode('utf-8')
    export.write_bytes(payload)
    meta = tmp_path / 'meta.json'
    meta.write_text(json.dumps({'sha256': hashlib.sha256(payload).hexdigest()}), encoding='utf-8')

    def boom(*_a, **_k):
        raise AssertionError('HTTPリクエストが発生してはいけない')

    code = script.main(
        ['--project', 'p', '--file', str(export), '--meta', str(meta), '--dry-run'],
        request=boom,
        sleep=lambda _s: None,
    )
    assert code == 1


@contextmanager
def api_client(tmp_path):
    """tests/test_triz_general.py の api fixture と同じ FastAPI + TestClient 構成。"""
    root = tmp_path / ('imp' + uuid.uuid4().hex[:6])

    def require(pid):
        if pid != 'p':
            raise HTTPException(404)

    app = FastAPI()
    env = SimpleNamespace(
        DATA_DIR=root,
        DB_PATH=root / 'memory' / 'db',
        llm=SimpleNamespace(url='http://127.0.0.1:11434'),
        require_project=require,
        memory=None,
    )
    install_examples(app, env)
    install_learning(app, env)
    with TestClient(app) as client:
        yield client


def client_request(client):
    def request(method, path, json_body=None):
        if method == 'GET':
            response = client.get(path)
        elif method == 'POST':
            response = client.post(path, json=json_body)
        else:
            raise ValueError(method)
        try:
            body = response.json()
        except Exception:
            body = {'detail': response.text}
        return response.status_code, body

    return request


def test_api_import_few_items_is_idempotent(tmp_path):
    script = load_script()
    items = load_export_items(EXPORT)[:3]
    export = tmp_path / 'triz-problems-export.json'
    raw = json.dumps({'export': {'items': items}}, ensure_ascii=False).encode('utf-8')
    export.write_bytes(raw)
    meta = tmp_path / 'meta.json'
    meta.write_text(json.dumps({'sha256': hashlib.sha256(raw).hexdigest()}), encoding='utf-8')
    argv = ['--project', 'p', '--file', str(export), '--meta', str(meta)]
    with api_client(tmp_path) as client:
        sender = client_request(client)
        assert script.main(argv, request=sender, sleep=lambda _s: None) == 0
        listing = client.get('/api/projects/p/agent-examples').json()
        assert len(listing) == 1
        eid = listing[0]['id']
        general = client.get('/api/projects/p/agent-examples/%s/learning/general' % eid).json()
        assert len(general['sessions']) == 3
        assert script.main(argv, request=sender, sleep=lambda _s: None) == 0
        listing_again = client.get('/api/projects/p/agent-examples').json()
        assert len(listing_again) == 1
        assert listing_again[0]['id'] == eid
        general_again = client.get('/api/projects/p/agent-examples/%s/learning/general' % eid).json()
        assert len(general_again['sessions']) == 3
        urls = [source_url_from_evidence(s['problem']['evidence']) for s in general_again['sessions']]
        assert len(urls) == len(set(urls)) == 3


def test_conflict_is_retried_then_succeeds(tmp_path):
    script = load_script()
    item = sample_item()
    export = tmp_path / 'export.json'
    raw = json.dumps({'export': {'items': [item]}}, ensure_ascii=False).encode('utf-8')
    export.write_bytes(raw)
    meta = tmp_path / 'meta.json'
    meta.write_text(json.dumps({'sha256': hashlib.sha256(raw).hexdigest()}), encoding='utf-8')
    calls = []
    sleeps = []

    def fake(method, path, json_body=None):
        calls.append((method, path))
        if method == 'POST' and path.endswith('/agent-examples'):
            return 200, {'id': 'eid1'}
        if method == 'GET' and path.endswith('/general'):
            return 200, {'revision': 0, 'sessions': []}
        if method == 'POST' and path.endswith('/general'):
            if sum(1 for m, p in calls if m == 'POST' and p.endswith('/general')) == 1:
                return 409, {'detail': '学習・検証中です。完了または取消後に実行してください'}
            return 200, {'revision': 1, 'general_inventions': [{'problem': {'evidence': item['evidence']}}]}
        raise AssertionError((method, path))

    code = script.main(
        ['--project', 'p', '--file', str(export), '--meta', str(meta)],
        request=fake,
        sleep=lambda s: sleeps.append(s),
    )
    assert code == 0
    assert sleeps == [script.RETRY_WAIT]
    assert sum(1 for m, p in calls if m == 'POST' and p.endswith('/general')) == 2
