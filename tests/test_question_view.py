import json
import sqlite3

import pytest
from fastapi import HTTPException

from app.goal_completion_store import GoalCompletionStore
from app.question_view import build_questions


class Memory:
    def __init__(self, path, mission): self.path, self.mission = path, mission
    def get_mission(self, _): return self.mission


class Manager:
    def __init__(self, path, mission): self.memory = Memory(path, mission)


def manager(tmp_path):
    mission = {'goal': '車両別損益', 'success_criteria': '', 'constraints_text': '', 'plan_version': 3, 'tasks': []}
    return Manager(tmp_path / 'memory.sqlite3', mission)


def test_qv01_max_five_sorted_and_read_only(tmp_path, monkeypatch):
    m = manager(tmp_path)
    issues = [dict(id=str(i), kind='allocation', message='m', source_ref='s', locator='l', candidates=[], amount=i, count=1, month='2026-01', allocation_subject='e') for i in range(7)]
    monkeypatch.setattr('app.question_view.applicable', lambda _: True)
    choices = [{'id': 'v1', 'company': '会社A'}]
    monkeypatch.setattr('app.question_view.state', lambda *_: {'prepared': True, 'stale': False, 'input_hash': 'h', 'questions': issues, 'vehicle_choices': choices})
    before = json.dumps(issues, sort_keys=True)
    db_path = tmp_path / 'goal_completion.sqlite3'
    assert not db_path.exists()
    result = build_questions(m, 'p', 99)
    assert [x['id'] for x in result['questions']] == ['6', '5', '4', '3', '2']
    assert result['total_open'] == 7 and result['omitted'] == 2
    assert json.dumps(issues, sort_keys=True) == before
    assert result['vehicle_choices'] == choices
    assert not db_path.exists()


def test_qv02_five_fields_no_invention(tmp_path, monkeypatch):
    m = manager(tmp_path)
    issue = dict(id='q', kind='allocation', message='誰の費用か不明', source_ref='src', locator='A1', candidates=['x'], amount=120, count=2, month='', allocation_subject='e')
    monkeypatch.setattr('app.question_view.applicable', lambda _: True)
    monkeypatch.setattr('app.question_view.state', lambda *_: {'prepared': True, 'stale': False, 'input_hash': 'h', 'questions': [issue], 'vehicle_choices': []})
    card = build_questions(m, 'p')['questions'][0]
    assert {'unknown', 'stop_condition', 'confirmed_facts', 'candidates', 'rerun_scope'} <= card.keys()
    assert card['confirmed_facts'] == [] and card['rerun_scope'] is None
    assert '120' in card['stop_condition']
    assert 'ratio' not in json.dumps(card)


def test_qv03_read_nonvehicle_unprepared_and_stale(tmp_path, monkeypatch):
    m = manager(tmp_path)
    monkeypatch.setattr('app.question_view.applicable', lambda _: False)
    assert build_questions(m, 'p')['questions'] == []
    monkeypatch.setattr('app.question_view.applicable', lambda _: True)
    monkeypatch.setattr('app.question_view.state', lambda *_: {'prepared': False, 'questions': []})
    assert not build_questions(m, 'p')['prepared']
    monkeypatch.setattr('app.question_view.state', lambda *_: {'prepared': True, 'stale': True, 'questions': [dict(id='r', kind='read'), dict(id='a', kind='allocation', amount=1)], 'vehicle_choices': []})
    result = build_questions(m, 'p')
    assert len(result['questions']) == 1 and not result['questions'][0]['answerable']


def test_fa01_append_versions_actor_unique_and_migration(tmp_path):
    store = GoalCompletionStore(tmp_path / 'memory.sqlite3')
    args = dict(project_id='p', fact_key='allocation:c:e:2026-01', value={'x': 1}, question_id='q', answered_by='human', input_hash='i', source_hash='s', plan_version=1, reason='確認済み')
    first = store.record_fact(**args); args['value'] = {'x': 2}; second = store.record_fact(**args)
    assert first['version'] == 1 and second['version'] == 2
    assert store.fact_history('p', args['fact_key'])[0]['value'] == {'x': 1}
    args['answered_by'] = ' '
    with pytest.raises(ValueError): store.record_fact(**args)
    with store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO goal_facts(project_id,fact_key,version,value_json,question_id,answered_by,answered_at,input_hash,source_hash,plan_version,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ('p', args['fact_key'], 1, '{}', 'q', 'x', 'now', '', '', 1, 'r'))


def test_fa03_history_and_latest(tmp_path):
    store = GoalCompletionStore(tmp_path / 'memory.sqlite3')
    base = dict(project_id='p', fact_key='k', question_id='q', answered_by='h', input_hash='i', source_hash='s', plan_version=1, reason='reason')
    store.record_fact(value={'v': 1}, **base); store.record_fact(value={'v': 2}, **base)
    assert [x['version'] for x in store.fact_history('p', 'k')] == [1, 2]
    assert store.latest_facts('p')['k']['value'] == {'v': 2}


def _web_setup(tmp_path, monkeypatch, flag=True):
    import app.web as web
    m = manager(tmp_path)
    monkeypatch.setattr(web, 'memory', m.memory)
    monkeypatch.setattr(web, 'orchestrator', m)
    monkeypatch.setattr(web, 'require_project', lambda _: {'id': 'p'})
    monkeypatch.setattr('app.goal_completion_flag.enabled', lambda *_: flag)
    return web, m


def _payload(answered_by='human', reason='確認済み'):
    from app.web import QuestionAnswerPayload
    return QuestionAnswerPayload(version=3, input_hash='ih', issue_id='q', vehicle_id='v1',
                                 reason=reason, answered_by=answered_by)


@pytest.mark.asyncio
async def test_fa02a_flag_off_writes_no_facts(tmp_path, monkeypatch):
    web, m = _web_setup(tmp_path, monkeypatch, flag=False)
    with pytest.raises(HTTPException) as caught:
        await web.answer_project_question('p', _payload())
    assert caught.value.status_code == 409 and caught.value.detail['code'] == 'FLAG_OFF'
    assert not (tmp_path / 'goal_completion.sqlite3').exists()


@pytest.mark.asyncio
async def test_fa02b_blank_answered_by_writes_no_facts(tmp_path, monkeypatch):
    web, m = _web_setup(tmp_path, monkeypatch)
    payload = _payload()
    payload.answered_by = '   '
    with pytest.raises(HTTPException) as caught:
        await web.answer_project_question('p', payload)
    assert caught.value.status_code == 422
    assert not (tmp_path / 'goal_completion.sqlite3').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('error,status', [(ValueError('hash mismatch'), 409)])
async def test_fa02c_decide_value_error_writes_no_facts(tmp_path, monkeypatch, error, status):
    web, m = _web_setup(tmp_path, monkeypatch)
    monkeypatch.setattr('app.vehicle_service.decide', lambda *a, **k: (_ for _ in ()).throw(error))
    with pytest.raises(HTTPException) as caught:
        await web.answer_project_question('p', _payload())
    assert caught.value.status_code == status
    assert not (tmp_path / 'goal_completion.sqlite3').exists()


@pytest.mark.asyncio
async def test_fa02c_allocation_error_is_422_and_writes_no_facts(tmp_path, monkeypatch):
    from app.vehicle_workflow import AllocationRuleError
    web, m = _web_setup(tmp_path, monkeypatch)
    monkeypatch.setattr('app.vehicle_service.decide', lambda *a, **k: (_ for _ in ()).throw(AllocationRuleError('invalid')))
    with pytest.raises(HTTPException) as caught:
        await web.answer_project_question('p', _payload())
    assert caught.value.status_code == 422
    assert not (tmp_path / 'goal_completion.sqlite3').exists()


@pytest.mark.asyncio
async def test_fa02d_success_appends_versions(tmp_path, monkeypatch):
    web, m = _web_setup(tmp_path, monkeypatch)
    envelope = {'source_hash': 'sh', 'decisions': [{'subject_key': {'company': 'c', 'employee': 'e'}, 'months': ['2026-01'], 'vehicle_id': 'v1', 'exclude': False, 'allocations': [{'vehicle_id': 'v1', 'ratio': 1}], 'apply_to': 'this_month'}]}
    monkeypatch.setattr('app.vehicle_service.decide', lambda *a, **k: {})
    monkeypatch.setattr('app.vehicle_service.state', lambda *a: {'prepared': True})
    monkeypatch.setattr('app.vehicle_workflow.load_input', lambda *a: envelope)
    first = await web.answer_project_question('p', _payload())
    second = await web.answer_project_question('p', _payload())
    assert first['fact']['version'] == 1 and second['fact']['version'] == 2
    assert len(GoalCompletionStore(m.memory.path).fact_history('p', 'allocation:c:e:2026-01')) == 2


@pytest.mark.asyncio
async def test_fa02e_legacy_decision_does_not_write_facts(tmp_path, monkeypatch):
    web, m = _web_setup(tmp_path, monkeypatch)
    monkeypatch.setattr('app.vehicle_service.decide', lambda *a, **k: {'ok': True})
    from app.web import VehicleDecisionPayload
    payload = _payload().model_dump(exclude={'answered_by'})
    result = await web.decide_vehicle_profit('p', VehicleDecisionPayload(**payload))
    assert result == {'ok': True}
    assert not (tmp_path / 'goal_completion.sqlite3').exists()


@pytest.mark.asyncio
async def test_fact_record_failure_returns_applied_state_and_error(tmp_path, monkeypatch):
    web, m = _web_setup(tmp_path, monkeypatch)
    envelope = {'source_hash': 'sh', 'decisions': [{'subject_key': {}, 'months': [], 'vehicle_id': 'v1', 'exclude': False, 'allocations': [], 'apply_to': 'this_month'}]}
    monkeypatch.setattr('app.vehicle_service.decide', lambda *a, **k: {})
    monkeypatch.setattr('app.vehicle_service.state', lambda *a: {'prepared': True})
    monkeypatch.setattr('app.vehicle_workflow.load_input', lambda *a: envelope)
    monkeypatch.setattr(GoalCompletionStore, 'record_fact', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('disk full')))
    result = await web.answer_project_question('p', _payload())
    assert result['state'] == {'prepared': True} and result['fact'] is None
    assert 'disk full' in result['fact_error']


def test_ui01_safe_dom_source():
    text = open('app/static/workflow_readiness.js', encoding='utf-8').read()
    assert 'innerHTML' not in text
    assert all(x in text for x in ['不明点', '止まる条件', '確認済み事実', '候補と影響', '再実行範囲'])
    assert "if(!answeredBy.value.trim())" in text
