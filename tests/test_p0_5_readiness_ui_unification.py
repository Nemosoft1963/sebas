"""P0-5: readinessとUIの単一判定、状態遷移パイプライン、失敗分類のテスト。"""
from unittest.mock import patch
import pytest

from app.workflow_readiness import (
    PIPELINE_STAGES,
    REPLAN_FAILURE_CATEGORIES,
    build_readiness,
    classify_replan_failure,
    resolve_pipeline_stage,
)
from app.next_action_controller import compute
from test_workflow_readiness import vehicle_plan


def test_p0_5_allowed_actions_single_source_of_truth(tmp_path):
    """allowed_actions が next-action と UI 判定の唯一の判定元であること。"""
    manager, pid = vehicle_plan(tmp_path)
    ready = build_readiness(manager, pid)
    allowed_actions = ready['allowed_actions']
    next_action = ready['next_action']

    action_id = next_action['id']
    rule = allowed_actions.get(action_id)
    if rule is not None:
        assert next_action['manual_executable'] == (rule['allowed'] and action_id not in {'idle', 'wait'})
        if not rule['allowed']:
            assert next_action['reason'] == rule['reason']

    computed = compute(manager, pid)
    assert computed['action_id'] == action_id
    assert computed['allowed'] == (rule['allowed'] if rule else False)
    assert computed['manual_executable'] == next_action['manual_executable']


def test_p0_5_manual_vs_auto_executable_separation(tmp_path):
    """人が操作すれば実行できるとシステムが自動実行できるの分離。"""
    manager, pid = vehicle_plan(tmp_path)
    ready = build_readiness(manager, pid)
    allowed_actions = ready['allowed_actions']

    # propose_feedback: 人は押せる (allowed) が自動実行ではない (auto_executable == False)
    # 指摘が存在する場合
    revision = {'status': 'none', 'blockers': [], 'changes': []}
    issues = [{'id': 'i1', 'text': '修正が必要', 'provider': 'api'}]
    gate = {'blocked': True, 'reason': '外部検証未完了'}
    mission = {'status': 'planning', 'tasks': [{'task_key': 't1'}]}
    job = None

    stage, next_id = resolve_pipeline_stage(mission, {}, revision, issues, gate, job)
    assert stage == 'issues_open'
    assert next_id == 'propose_feedback'

    # next_action の auto_executable と manual_executable の分離
    with patch('app.workflow_readiness.issues_for', return_value=issues):
        r = build_readiness(manager, pid)
        assert r['allowed_actions']['propose_feedback']['allowed'] is True
        assert r['next_action']['id'] == 'propose_feedback'
        assert r['next_action']['manual_executable'] is True
        assert r['next_action']['auto_executable'] is False

        nac_row = compute(manager, pid)
        assert nac_row['action_id'] == 'propose_feedback'
        assert nac_row['executable'] is True
        assert nac_row['manual_executable'] is True
        assert nac_row['auto_executable'] is False


def test_p0_5_no_active_job_does_not_show_running(tmp_path):
    """実行中ジョブが無い場合に「実行中」と表示されないこと。"""
    manager, pid = vehicle_plan(tmp_path)
    ready = build_readiness(manager, pid)
    assert ready['active_job'] is None
    assert ready['phase'] != 'running'
    assert '実行中です' not in ready['stop_reason']
    assert ready['next_action']['id'] != 'wait'


def test_p0_5_pipeline_stages_and_next_actions():
    """状態遷移パイプライン(12段階)の各段階で対応する next_action が一貫して決まること。"""
    # 1. issues_open -> propose_feedback
    stage, next_id = resolve_pipeline_stage(
        {'status': 'planning'}, {}, {}, [{'id': '1'}], {'blocked': True}, None
    )
    assert stage == 'issues_open' and next_id == 'propose_feedback'

    # 2. propose (generating) -> wait
    stage, next_id = resolve_pipeline_stage(
        {'status': 'planning'}, {}, {'status': 'generating'}, [{'id': '1'}], {'blocked': True}, None
    )
    assert stage == 'propose' and next_id == 'wait'

    # 3. proposal_ready (blockersあり) -> resolve_development
    stage, next_id = resolve_pipeline_stage(
        {'status': 'planning'}, {}, {'status': 'draft', 'blockers': [{'disposition': 'development', 'reason': 'dev'}]}, [], {'blocked': True}, None
    )
    assert stage == 'proposal_ready' and next_id == 'resolve_development'

    # 4. confirmed (draft_ok: blockersなし、changesあり) -> apply
    stage, next_id = resolve_pipeline_stage(
        {'status': 'planning'}, {}, {'status': 'draft', 'blockers': [], 'changes': [{'target': 't1'}]}, [], {'blocked': True}, None
    )
    assert stage == 'confirmed' and next_id == 'apply'

    # 5. applied (revalidation_pending) -> external_review
    stage, next_id = resolve_pipeline_stage(
        {'status': 'planning'}, {}, {'status': 'applied', 'lifecycle': 'revalidation_pending'}, [], {'blocked': True}, None
    )
    assert stage == 'applied' and next_id == 'external_review'

    # 6. external_review (plan running) -> wait
    stage, next_id = resolve_pipeline_stage(
        {'status': 'planning'}, {'status': 'running'}, {}, [], {'blocked': True}, {'kind': 'plan_review'}
    )
    assert stage == 'external_review' and next_id == 'wait'

    # 7. review_passed -> approve_plan
    stage, next_id = resolve_pipeline_stage(
        {'status': 'planning'}, {'status': 'passed'}, {}, [], {'blocked': False}, None
    )
    assert stage in {'review_passed', 'plan_approval'} and next_id == 'approve_plan'

    # 8. approved / execution_start -> start
    stage, next_id = resolve_pipeline_stage(
        {'status': 'ready'}, {'status': 'passed'}, {}, [], {'blocked': False}, None
    )
    assert stage in {'approved', 'execution_start'} and next_id == 'start'


def test_p0_5_replan_failure_classification():
    """再計画失敗時の分類と具体的次作業の検証。"""
    # 1. development_required
    rev_dev = {
        'status': 'draft',
        'blockers': [{'disposition': 'development', 'reason': '専用器の実装が必要です'}],
    }
    fail_dev = classify_replan_failure(rev_dev)
    assert fail_dev is not None
    assert fail_dev['code'] == 'development_required'
    assert '追加開発' in fail_dev['label']
    assert '専用処理の実装' in fail_dev['next_work']

    # 2. input_insufficient (business_fact)
    rev_fact = {
        'status': 'draft',
        'blockers': [{'disposition': 'business_fact', 'reason': '所属車両の確認が必要です'}],
    }
    fail_fact = classify_replan_failure(rev_fact)
    assert fail_fact is not None
    assert fail_fact['code'] == 'input_insufficient'
    assert '原本' in fail_fact['next_work'] or '確認' in fail_fact['next_work']

    # 3. conflict_unresolved
    rev_unres = {
        'status': 'draft',
        'blockers': [{'disposition': 'unresolved', 'reason': '指摘内容に根拠がありません'}],
    }
    fail_unres = classify_replan_failure(rev_unres)
    assert fail_unres is not None
    assert fail_unres['code'] == 'conflict_unresolved'
    assert '整合性' in fail_unres['next_work']

    # 4. max_attempts_exceeded
    rev_max = {
        'status': 'error',
        'attempts': 3,
        'error': '同じ版の修正案生成は3回までです',
    }
    fail_max = classify_replan_failure(rev_max)
    assert fail_max is not None
    assert fail_max['code'] == 'max_attempts_exceeded'
    assert '直接見直して' in fail_max['next_work']

    # 5. external_ai_failed
    rev_err = {
        'status': 'error',
        'attempts': 1,
        'error': 'Ollama connection refused',
    }
    fail_err = classify_replan_failure(rev_err)
    assert fail_err is not None
    assert fail_err['code'] == 'external_ai_failed'
    assert '稼働状況' in fail_err['next_work']


def test_p0_5_stop_reason_and_next_action_consistency(tmp_path):
    """停止理由と次の操作が矛盾しないこと。"""
    manager, pid = vehicle_plan(tmp_path)
    ready = build_readiness(manager, pid)

    phase = ready['phase']
    stop_reason = ready['stop_reason']
    next_action = ready['next_action']

    if phase == 'complete':
        assert '満たしています' in stop_reason
        assert next_action['id'] == 'approve_result'
    elif phase == 'issues_open':
        assert '指摘' in stop_reason
        assert next_action['id'] == 'propose_feedback'
    elif phase == 'dev_blocked':
        assert '開発' in stop_reason or '指摘' in stop_reason
        assert next_action['id'] == 'resolve_development'
