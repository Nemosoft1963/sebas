"""P0-6: 外部AIの接続失敗を計画指摘から分離し、合格ポリシーで次工程可否を決める。"""
import json
from pathlib import Path

import pytest

from app.external_ai import (
    ProviderHTTPError, ProviderResponse, classify_provider_failure,
    connection_error_from_payload, invoke_provider_isolated,
)
from app.goal_review import (
    ReviewStore, evaluate_external_review_status, execution_gate, plan_snapshot,
    require_review, review_pass_policy, review_plan,
)
from app.plan_feedback import issues_for
from test_goal_review import setup


def write_policy(tmp_path, **fields):
    body = {'required': True}
    body.update(fields)
    (tmp_path / 'goal_review_policy.json').write_text(
        json.dumps(body, ensure_ascii=False), encoding='utf-8',
    )


def pass_review(provider):
    return {
        'id': provider, 'ok': True,
        'review': json.dumps({'verdict': 'pass', 'issues': []}),
    }


SUMMARY = '目標と各工程を比較し、入力から出力まで不足がないか検証します。'


@pytest.mark.asyncio
async def test_gemini_429_is_connection_error_not_plan_issue(tmp_path):
    manager, pid, _ = setup(tmp_path, True)
    write_policy(tmp_path, min_success_count=2)
    sig = plan_snapshot(manager, pid)[1]

    async def runner(text, providers):
        out = []
        for provider in providers:
            if provider == 'a':
                out.append({
                    'id': 'a', 'ok': False, 'error': 'HTTP 429: RESOURCE_EXHAUSTED rate limit',
                    'outcome': 'connection_error', 'status_code': 429,
                    'error_category': 'connection_error',
                })
            else:
                out.append(pass_review(provider))
        return out

    manager.plan_review_runner = runner
    result = await review_plan(manager, pid, sig, SUMMARY, True)
    issues = issues_for(manager, pid, sig)
    assert all('429' not in (item.get('text') or '') for item in issues)
    assert all(item.get('origin') != 'api' or 'RESOURCE_EXHAUSTED' not in item.get('text', '') for item in issues)
    assert result['connection_errors']
    assert any(err['provider'] == 'a' and err['status_code'] == 429 for err in result['connection_errors'])
    assert any(row['id'] == 'a' and row['outcome'] == 'connection_error' for row in result['provider_outcomes'])
    assert any(row['id'] == 'b' and row['outcome'] == 'success' for row in result['provider_outcomes'])


@pytest.mark.asyncio
async def test_meta_401_is_connection_error_not_plan_issue(tmp_path):
    manager, pid, _ = setup(tmp_path, True)
    write_policy(tmp_path, min_success_count=2)
    sig = plan_snapshot(manager, pid)[1]

    async def runner(text, providers):
        out = []
        for provider in providers:
            if provider == 'b':
                out.append({
                    'id': 'b', 'ok': False, 'error': 'HTTP 401: unauthenticated',
                    'outcome': 'connection_error', 'status_code': 401,
                    'error_category': 'authentication_error',
                })
            else:
                out.append(pass_review(provider))
        return out

    manager.plan_review_runner = runner
    result = await review_plan(manager, pid, sig, SUMMARY, True)
    issues = issues_for(manager, pid, sig)
    assert issues == []
    assert any(err['provider'] == 'b' and err['status_code'] == 401 for err in result['connection_errors'])
    assert result['stop_kind'] != 'plan'


@pytest.mark.asyncio
async def test_min_success_count_allows_next_stage_without_unanimous_pass(tmp_path):
    manager, pid, _ = setup(tmp_path, True)
    manager.memory.save_mission(pid, '機密会社の作業結果を整理する', '根拠を示す', '', True, ['a', 'b', 'c'])
    manager.provider_statuses = lambda: [
        {'id': 'a', 'configured': True}, {'id': 'b', 'configured': True}, {'id': 'c', 'configured': True},
    ]
    write_policy(tmp_path, min_success_count=2)
    sig = plan_snapshot(manager, pid)[1]

    async def runner(text, providers):
        return [
            pass_review('a'),
            pass_review('b'),
            {
                'id': 'c', 'ok': False, 'error': 'HTTP 429: Too Many Requests',
                'outcome': 'connection_error', 'status_code': 429,
            },
        ]

    manager.plan_review_runner = runner
    result = await review_plan(manager, pid, sig, SUMMARY, True)
    assert result['status'] == 'passed'
    assert result['success_count'] == 2
    require_review(manager, pid)
    assert execution_gate(manager, pid)['blocked'] is False
    assert issues_for(manager, pid, sig) == []


@pytest.mark.asyncio
async def test_required_provider_connection_failure_stops_as_settings_not_plan(tmp_path):
    manager, pid, _ = setup(tmp_path, True)
    write_policy(tmp_path, min_success_count=1, required_providers=['a'])
    sig = plan_snapshot(manager, pid)[1]

    async def runner(text, providers):
        return [
            {
                'id': 'a', 'ok': False, 'error': 'HTTP 401: invalid api key',
                'outcome': 'connection_error', 'status_code': 401,
                'error_category': 'authentication_error',
            },
            pass_review('b'),
        ]

    manager.plan_review_runner = runner
    result = await review_plan(manager, pid, sig, SUMMARY, True)
    assert result['status'] == 'connection_failed'
    assert result['stop_kind'] == 'connection_settings'
    assert '接続設定' in result['stop_reason']
    assert '計画内容' in result['stop_reason'] or '計画' in result['stop_reason']
    with pytest.raises(ValueError, match='接続設定'):
        require_review(manager, pid)
    gate = execution_gate(manager, pid)
    assert gate['blocked'] is True
    assert gate.get('stop_kind') == 'connection_settings'
    assert '接続' in gate['reason']
    assert issues_for(manager, pid, sig) == []


@pytest.mark.asyncio
async def test_one_provider_retry_does_not_rerun_successful_providers(tmp_path):
    manager, pid, _ = setup(tmp_path, True)
    sig = plan_snapshot(manager, pid)[1]
    calls = []

    async def runner(text, providers):
        calls.append(list(providers))
        return [
            pass_review(p) if p == 'a' else {
                'id': p, 'ok': False, 'error': 'HTTP 429: rate limit',
                'outcome': 'connection_error', 'status_code': 429,
            }
            for p in providers
        ]

    manager.plan_review_runner = runner
    await review_plan(manager, pid, sig, SUMMARY, True)
    await review_plan(manager, pid, sig, SUMMARY, True)
    assert calls[0] == ['a', 'b']
    assert calls[1] == ['b']
    stored = ReviewStore(manager.memory.path).get(pid, 'plan', sig)
    a_row = next(x for x in stored['reviews'] if x['provider'] == 'a')
    assert a_row['status'] == 'pass'
    b_row = next(x for x in stored['reviews'] if x['provider'] == 'b')
    assert b_row['status'] == 'connection_error'


@pytest.mark.asyncio
async def test_invoke_provider_retries_only_that_provider(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')
    monkeypatch.setenv('GEMINI_MODEL', 'gemini-test')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-test')
    monkeypatch.setenv('EXTERNAL_AI_CONNECTION_RETRIES', '1')
    counts = {'gemini': 0, 'chatgpt': 0}

    async def fake_call(provider_id, prompt, system, max_tokens=2200, reasoning_effort=None):
        counts[provider_id] = counts.get(provider_id, 0) + 1
        if provider_id == 'gemini':
            raise ProviderHTTPError(429, 'RESOURCE_EXHAUSTED')
        return ProviderResponse(text='ok', model='gpt-test')

    monkeypatch.setattr('app.external_ai.call_provider_with_metadata', fake_call)
    gemini = await invoke_provider_isolated('gemini', 'p', 's', retries=1)
    chatgpt = await invoke_provider_isolated('chatgpt', 'p', 's', retries=1)
    assert gemini['ok'] is False
    assert gemini['outcome'] == 'connection_error'
    assert gemini['status_code'] == 429
    assert chatgpt['ok'] is True
    assert counts['gemini'] == 2
    assert counts['chatgpt'] == 1


def test_classify_and_payload_connection_errors():
    http429 = classify_provider_failure(ProviderHTTPError(429, 'rate'))
    assert http429['kind'] == 'connection' and http429['status_code'] == 429
    http401 = classify_provider_failure(ProviderHTTPError(401, 'auth'))
    assert http401['kind'] == 'connection' and http401['category'] == 'authentication_error'
    timeout = classify_provider_failure(TimeoutError('timed out'))
    assert timeout['kind'] == 'connection' and timeout['retryable'] is True
    payload = connection_error_from_payload({'ok': False, 'error': 'HTTP 429: Too Many Requests'})
    assert payload['status_code'] == 429
    assert connection_error_from_payload({'ok': True, 'review': 'pass'}) is None


def test_pass_policy_defaults_require_all_and_can_be_configured(tmp_path):
    manager, pid, _ = setup(tmp_path, True)
    default = review_pass_policy(manager, pid, 5)
    assert default['min_success_count'] == 5
    assert default['required_providers'] == []
    write_policy(tmp_path, min_success_count=3, required_providers=['gemini', 'claude'])
    configured = review_pass_policy(manager, pid, 5)
    assert configured['min_success_count'] == 3
    assert configured['required_providers'] == ['gemini', 'claude']


def test_evaluate_status_separates_connection_stop_from_plan_fail():
    parsed = [
        {'provider': 'a', 'status': 'pass', 'issues': []},
        {'provider': 'b', 'status': 'connection_error', 'issues': [],
         'connection_error': {'status_code': 429, 'category': 'connection_error', 'error': 'HTTP 429'}},
        {'provider': 'c', 'status': 'pass', 'issues': []},
    ]
    majority = evaluate_external_review_status(
        parsed, ['a', 'b', 'c'], {'min_success_count': 2, 'required_providers': []},
    )
    assert majority['status'] == 'passed'
    required_fail = evaluate_external_review_status(
        parsed, ['a', 'b', 'c'], {'min_success_count': 2, 'required_providers': ['b']},
    )
    assert required_fail['status'] == 'connection_failed'
    assert required_fail['stop_kind'] == 'connection_settings'
    content = [
        {'provider': 'a', 'status': 'fail', 'issues': [{'reason': '工程不足'}]},
        {'provider': 'b', 'status': 'fail', 'issues': [{'reason': '検証なし'}]},
    ]
    plan_fail = evaluate_external_review_status(
        content, ['a', 'b'], {'min_success_count': 2, 'required_providers': []},
    )
    assert plan_fail['status'] == 'not_passed'
    assert plan_fail['stop_kind'] == 'plan'


def test_ui_separates_provider_outcomes_and_connection_errors():
    root = Path(__file__).resolve().parents[1]
    js = (root / 'app' / 'static' / 'goal_review.js').read_text(encoding='utf-8')
    wr = (root / 'app' / 'static' / 'workflow_readiness.js').read_text(encoding='utf-8')
    py = (root / 'app' / 'workflow_readiness.py').read_text(encoding='utf-8')
    assert 'goalProviderOutcomes' in js
    assert '接続エラー（計画への指摘ではありません）' in js
    assert 'connection_failed' in js
    assert '接続設定の問題で停止' in js
    assert '合格ポリシー' in js
    assert 'connection_failed' in py
    assert '接続設定の問題' in py
    assert wr.count('innerHTML') == 0
    assert "planDetails.open=true" in js
    assert "box.querySelectorAll('button').forEach(x=>x.disabled=false)" in js
    assert "el('goalPlanSend').disabled=!state.send_allowed" in js
    assert "finally{el('goalReviewRefresh').disabled=false;}" in js
