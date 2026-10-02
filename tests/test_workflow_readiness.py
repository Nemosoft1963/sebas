"""P1-1 第一段階: workflow-readiness の受入条件 1〜6。"""
import json
import time

import pytest
from fastapi import HTTPException

from app.goal_review import ReviewStore, plan_snapshot
from app.plan_feedback import import_feedback
from app.workflow_readiness import UNIMPLEMENTED_ACTIONS, build_readiness
from app.vehicle_workflow import digest, resolve, sources, write_json, INPUT_PATH
from test_goal_review import setup as goal_setup
from test_vehicle_workflow import fixture, setup as vehicle_setup


def policy_required(tmp_path):
    (tmp_path / 'goal_review_policy.json').write_text('{"required":true}', encoding='utf-8')


def vehicle_plan(tmp_path, required=True):
    manager, pid = vehicle_setup(tmp_path)
    if required:
        policy_required(tmp_path)
    return manager, pid


@pytest.mark.asyncio
async def test_unverified_vehicle_plan_blocks_start(tmp_path):
    manager, pid = vehicle_plan(tmp_path)
    await manager.generate_plan(pid)
    manager.approve(pid)
    row = build_readiness(manager, pid)
    assert row['phase'] == 'unverified'
    assert row['next_action']['id'] in {'propose_feedback', 'external_review'}
    assert row['allowed_actions']['start']['allowed'] is False
    assert '外部' in row['allowed_actions']['start']['reason'] or '検証' in row['allowed_actions']['start']['reason']
    assert row['final_completed'] is False
    assert row['artifact_class'] != 'final'


@pytest.mark.asyncio
async def test_revision_blockers_dev_blocked_and_apply_start(tmp_path):
    manager, pid = vehicle_plan(tmp_path)
    await manager.generate_plan(pid)
    sig = plan_snapshot(manager, pid)[1]
    import_feedback(manager, pid, sig, '検証', '専用器の動作を変える指摘です。実装と受入テストが必要です。')
    store = ReviewStore(manager.memory.path)
    store.put(pid, 'revision', sig, {
        'status': 'draft',
        'candidate_id': 'c1',
        'changes': [{'target': 'vehicle_extract', 'before': 'a', 'after': 'b'}],
        'blockers': [{'disposition': 'development', 'reason': '追加開発が必要です。専用実行器は説明文では変わりません。'}],
        'actions': [],
        'issues': [],
    })
    row = build_readiness(manager, pid)
    assert row['phase'] == 'dev_blocked'
    assert row['allowed_actions']['apply']['allowed'] is False
    assert row['allowed_actions']['start']['allowed'] is False
    assert any(x['id'] == 'apply' for x in row['blocked_actions'])
    assert any(x['id'] == 'start' for x in row['blocked_actions'])


@pytest.mark.asyncio
async def test_waiting_budget_then_tick_changes_phase(tmp_path, monkeypatch):
    from app.goal_review import review_plan, require_review
    from app.goal_review_queue import tick
    manager, pid, _ = goal_setup(tmp_path, True)
    calls = []

    async def runner(packet, providers):
        calls.append(packet)
        return [dict(id=p, ok=True, review=json.dumps({'verdict': 'pass', 'issues': []})) for p in providers]

    manager.plan_review_runner = runner
    monkeypatch.setattr('app.goal_review.review_budget', lambda *a: dict(managed=True, remaining_calls=0, reset_at=time.time() - 1))
    sig = plan_snapshot(manager, pid)[1]
    row = await review_plan(manager, pid, sig, '秘密情報を含まない目標と計画の説明です。原本と結果の対応を確認します。', True)
    assert row['status'] == 'waiting_budget'
    ready = build_readiness(manager, pid)
    assert ready['phase'] == 'waiting_budget'
    assert ready['next_action']['id'] == 'wait_budget'
    monkeypatch.setattr('app.goal_review.review_budget', lambda *a: dict(managed=True, remaining_calls=4))
    await tick(manager)
    after = build_readiness(manager, pid)
    assert after['phase'] != 'waiting_budget'
    require_review(manager, pid)
    assert after['phase'] in {'idle', 'unverified', 'complete', 'provisional'}


@pytest.mark.asyncio
async def test_needs_review_input_is_provisional_not_complete(tmp_path):
    manager, pid = vehicle_plan(tmp_path, required=False)
    item = manager.memory.add_context_file(pid, 'source.txt', '原本', 6, source='upload')
    mission = await manager.generate_plan(pid)
    data = fixture()
    data['auto_extraction'] = {
        'issues': [{
            'id': 'r1', 'kind': 'read', 'source_ref': 'context:' + item['id'],
            'locator': 'Sheet!C1', 'message': '金額を数値として読み取れません',
            'status': 'unresolved', 'raw_text': '要確認', 'reason_code': 'non_numeric',
            'month': '2026-01', 'category': 'fuel',
        }],
        'recoveries': [],
    }
    for row in data['records'] + data['source_controls'] + data['source_dispositions']:
        row['source_ref'] = 'context:' + item['id']
    snapshot = sources(manager, pid)
    write_json(resolve(manager, pid, INPUT_PATH), {
        'data': data, 'confirmed': True, 'source_hash': digest(snapshot),
        'mode': 'vehicle-auto-v1',
    })
    xlsx = resolve(manager, pid, 'result/vehicle/v' + str(mission['plan_version']) + '/profit.xlsx')
    xlsx.parent.mkdir(parents=True, exist_ok=True)
    xlsx.write_bytes(b'xlsx')
    calc = resolve(manager, pid, 'result/vehicle/v' + str(mission['plan_version']) + '/calculation.json')
    write_json(calc, {'status': 'needs_review', 'issues': ['読取失敗'], 'version': mission['plan_version']})
    manager.memory.set_mission_status(pid, 'completed', '途中版', 'vehicle')
    row = build_readiness(manager, pid)
    assert row['artifact_class'] == 'provisional'
    assert row['phase'] != 'complete'
    assert row['final_completed'] is False


def test_unimplemented_actions_are_not_allowed(tmp_path):
    manager, pid = vehicle_plan(tmp_path)
    row = build_readiness(manager, pid)
    unimplemented = {ident for ident, _ in UNIMPLEMENTED_ACTIONS}
    assert unimplemented.isdisjoint(row['allowed_actions'])
    blocked_ids = {x['id'] for x in row['blocked_actions']}
    assert unimplemented <= blocked_ids


@pytest.mark.asyncio
async def test_api_does_not_mix_200_and_4xx(tmp_path, monkeypatch):
    import app.web as web
    manager, pid = vehicle_plan(tmp_path)
    await manager.generate_plan(pid)
    monkeypatch.setattr(web, 'memory', manager.memory)
    monkeypatch.setattr(web, 'orchestrator', manager)
    body = await web.get_workflow_readiness(pid)
    assert body['project_id'] == pid
    assert 'phase' in body and 'allowed_actions' in body
    with pytest.raises(HTTPException) as error:
        await web.get_workflow_readiness('missing-project')
    assert error.value.status_code == 404
    assert error.value.status_code != 200


@pytest.mark.asyncio
async def test_unresolved_repair_target_is_not_called_development_or_approvable(tmp_path):
    manager, pid = vehicle_plan(tmp_path)
    await manager.generate_plan(pid)
    sig = plan_snapshot(manager, pid)[1]
    ReviewStore(manager.memory.path).put(pid, 'revision', sig, {
        'status': 'draft', 'candidate_id': 'saved-draft',
        'blockers': [{'disposition': 'unresolved', 'reason': '旧SC番号の修正対象が不明です'}],
        'changes': [], 'actions': [], 'issues': [],
    })
    row = build_readiness(manager, pid)
    assert row['replan_failure']['code'] == 'conflict_unresolved'
    assert row['phase'] == 'plan_conflict'
    assert row['next_action']['id'] == 'review_feedback'
    assert row['next_action']['manual_executable'] is True
    assert row['allowed_actions']['approve_plan']['allowed'] is False
    assert row['allowed_actions']['resolve_development']['allowed'] is False
    with pytest.raises(ValueError, match='未解決'):
        manager.approve(pid)


@pytest.mark.asyncio
async def test_business_fact_blocker_has_distinct_guidance(tmp_path):
    manager, pid = vehicle_plan(tmp_path)
    await manager.generate_plan(pid)
    sig = plan_snapshot(manager, pid)[1]
    ReviewStore(manager.memory.path).put(pid, 'revision', sig, {
        'status': 'draft', 'candidate_id': 'saved-draft',
        'blockers': [{'disposition': 'business_fact', 'reason': '配賦対象の業務判断が必要です'}],
        'changes': [], 'actions': [], 'issues': [],
    })
    row = build_readiness(manager, pid)
    assert row['phase'] == 'plan_fact_confirm'
    assert row['next_action']['id'] == 'review_feedback'
    assert row['allowed_actions']['approve_plan']['allowed'] is False
    assert row['allowed_actions']['start']['allowed'] is False