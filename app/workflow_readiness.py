"""統合進行状態(P1-1 第一段階)。既存のゲート・照合・キューを呼ぶだけ。合格判定は再実装しない。"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict

from app.goal_review import ReviewStore, enabled, execution_gate, plan_snapshot, active_jobs, orchestration_view, send_allowed
from app.plan_feedback import issues_for
from app.vehicle_workflow import applicable, goal_failures, load_input, source_reconciliation_report, sources

IMPLEMENTED_ACTIONS = (
    'prepare',
    'import_feedback',
    'propose_feedback',
    'apply',
    'external_review',
    'approve_plan',
    'start',
    'cancel_queue',
    'review_source_difference',
    'resolve_source_reads',
    'confirm_allocation',
    'apply_prior_answers',
    'resolve_development',
    'review_feedback',
    'review_artifacts',
    'approve_result',
    'wait_budget',
    'wait',
    'idle',
)
LOGGER = logging.getLogger(__name__)
_GATE_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_GATE_CACHE_LIMIT = 128

PIPELINE_STAGES = (
    'issues_open',
    'propose',
    'proposal_ready',
    'human_confirm',
    'confirmed',
    'apply',
    'applied',
    'external_review',
    'review_passed',
    'plan_approval',
    'approved',
    'execution_start',
)

REPLAN_FAILURE_CATEGORIES = {
    'development_required': {
        'label': '追加開発が必要',
        'next_work': '専用処理の実装または機能の追加開発を行ってください。説明文の追記では解決しません。',
    },
    'input_insufficient': {
        'label': '業務事実・原本情報の確認が必要',
        'next_work': '原本の読取結果・配賦対象・業務事実を確認し、回答または原本の修正を行ってください。',
    },
    'conflict_unresolved': {
        'label': '指摘の未解決・矛盾',
        'next_work': '保存済み修正案の対象を現行達成条件と照合し、目標との整合性を保って根拠付きで局所修復してください。',
    },
    'external_ai_failed': {
        'label': '外部AI・LLM呼び出し失敗',
        'next_work': 'ローカルLLM/外部AIの稼働状況や接続設定を確認し、復旧後に再試行してください。',
    },
    'max_attempts_exceeded': {
        'label': '再計画試行上限超過',
        'next_work': '同じ版への修正案生成が上限(3回)に達しました。人間が指摘と目標を整理して計画を直接見直してください。',
    },
}

UNIMPLEMENTED_ACTIONS = (
    ('triz_adopt', 'P2完了までTRIZ採用は blocked: framed/candidates/tried は回復成功ではありません'),
    ('triz_rerun', 'TRIZ採用再実行は業務検査と限定再実行の完了後です'),
    ('recipe_apply', '承認済みレシピは抽出時に適用判定します。条件不一致は明示拒否し、専用ボタンでは再実行しません'),
)

PHASE_LABELS = {
    'running': ('running', '実行中'),
    'accuracy_blocked': ('source_read_failed', '原本の正確性を確認してください'),
    'fact_confirm': ('allocation_unresolved', '配賦・業務事実の確認待ち'),
    'dev_blocked': ('development_blocker', '追加開発が必要な指摘があります'),
    'plan_conflict': ('plan_conflict', '修正案の対象・根拠を確認してください'),
    'plan_fact_confirm': ('plan_fact_confirm', '計画に必要な業務事実を確認してください'),
    'issues_open': ('plan_issues_open', '計画への指摘が未対応です'),
    'waiting_budget': ('waiting_budget', '外部検証の予算回復待ち'),
    'unverified': ('external_review_required', '現行版の外部検証が未完了です'),
    'connection_failed': ('connection_settings', '接続設定の問題で停止しています'),
    'provisional': ('needs_review', '暫定成果の確認待ち'),
    'complete': ('completed', '確定完了'),
    'idle': ('idle', '待機'),
}


def classify_replan_failure(revision: dict, error_text: str = '') -> dict | None:
    """修正案（再計画）が失敗または反映不可能な場合の分類と必要な作業。"""
    if not revision:
        return None
    status = revision.get('status')
    blockers = list(revision.get('blockers') or [])
    attempts = int(revision.get('attempts') or 0)
    err = str(revision.get('error') or error_text or '')

    if blockers:
        kinds = {b.get('disposition') for b in blockers if isinstance(b, dict)}
        first_reason = ''
        for b in blockers:
            if isinstance(b, dict) and b.get('reason'):
                first_reason = str(b.get('reason'))
                break
        if 'development' in kinds:
            cat = REPLAN_FAILURE_CATEGORIES['development_required']
            return {
                'code': 'development_required',
                'label': cat['label'],
                'reason': first_reason or cat['label'],
                'next_work': cat['next_work'],
                'blockers': blockers,
            }
        if 'business_fact' in kinds:
            cat = REPLAN_FAILURE_CATEGORIES['input_insufficient']
            return {
                'code': 'input_insufficient',
                'label': cat['label'],
                'reason': first_reason or cat['label'],
                'next_work': cat['next_work'],
                'blockers': blockers,
            }
        if 'unresolved' in kinds:
            cat = REPLAN_FAILURE_CATEGORIES['conflict_unresolved']
            return {
                'code': 'conflict_unresolved',
                'label': cat['label'],
                'reason': first_reason or cat['label'],
                'next_work': cat['next_work'],
                'blockers': blockers,
            }

    if attempts >= 3 and status != 'draft':
        cat = REPLAN_FAILURE_CATEGORIES['max_attempts_exceeded']
        return {
            'code': 'max_attempts_exceeded',
            'label': cat['label'],
            'reason': '同じ版に対する修正案生成が上限(3回)に達しました。',
            'next_work': cat['next_work'],
            'blockers': [],
        }

    if status == 'error' or err:
        cat = REPLAN_FAILURE_CATEGORIES['external_ai_failed']
        return {
            'code': 'external_ai_failed',
            'label': cat['label'],
            'reason': err or 'ローカルLLMでの修正案生成に失敗しました。',
            'next_work': cat['next_work'],
            'blockers': [],
        }
    return None


def resolve_pipeline_stage(mission: dict, plan: dict, revision: dict, issues: list, gate: dict, job: dict | None) -> tuple[str, str]:
    """状態遷移パイプラインにおける現在のステージと対応する next_action.id を導出する。
    issues_open → propose → proposal_ready → human_confirm → confirmed → apply → applied → external_review → review_passed → plan_approval → approved → execution_start
    """
    job_kind = (job or {}).get('kind')
    revision_status = revision.get('status')
    plan_status = plan.get('status')
    mission_status = mission.get('status')
    blockers = list(revision.get('blockers') or [])
    has_changes = bool(revision.get('changes'))
    draft_ok = revision_status == 'draft' and not blockers and has_changes

    if blockers:
        failure = classify_replan_failure(revision) or {}
        return 'proposal_ready', ('resolve_development' if failure.get('code') == 'development_required' else 'review_feedback')
    if mission_status in {'ready', 'paused'} and not gate.get('blocked'):
        return 'execution_start', 'start'
    if mission_status == 'ready':
        return 'approved', 'start'
    if mission_status == 'planning' and (plan_status == 'passed' or not gate.get('blocked')):
        return 'plan_approval', 'approve_plan'
    if plan_status == 'passed':
        return 'review_passed', 'approve_plan'

    if plan_status == 'running' or job_kind == 'plan_review':
        return 'external_review', 'wait'
    if revision_status == 'applied' or revision.get('lifecycle') == 'revalidation_pending':
        return 'applied', 'external_review'

    if revision_status == 'draft':
        if blockers:
            return 'proposal_ready', 'resolve_development'
        if draft_ok:
            return 'confirmed', 'apply'
        return 'human_confirm', 'apply'

    if revision_status in {'generating', 'running'} or job_kind == 'propose':
        return 'propose', 'wait'

    if issues:
        return 'issues_open', 'propose_feedback'

    if gate.get('blocked') or plan_status != 'passed':
        return 'external_review', 'external_review'
    return 'approved', 'start'


def _token(*parts) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _action(allowed: bool, reason: str = '') -> dict:
    return {'allowed': bool(allowed), 'reason': '' if allowed else (reason or '現在は実行できません')}


def _controls_matched(vehicle: dict) -> bool:
    recon = vehicle.get('recon') or {}
    return recon.get('passed') is True and vehicle.get('controls') == 'matched'


def _vehicle_view(manager, pid, mission) -> dict:
    view = {
        'applicable': False,
        'prepared': False,
        'stale': True,
        'unresolved_reads': 0,
        'unresolved_allocations': 0,
        'controls': 'empty',
        'envelope': None,
        'recon': None,
        'xlsx': False,
        'needs_review': False,
    }
    if not applicable(mission):
        return view
    view['applicable'] = True
    envelope = load_input(manager, pid)
    view['envelope'] = envelope
    data = (envelope or {}).get('data') or {}
    issues = list((data.get('auto_extraction') or {}).get('issues') or [])
    unresolved = [i for i in issues if i.get('status') != 'resolved']
    view['unresolved_reads'] = sum(1 for i in unresolved if i.get('kind') == 'read')
    view['unresolved_allocations'] = sum(1 for i in unresolved if i.get('kind') in {'allocation', 'adapter'})
    view['prepared'] = bool(envelope) and envelope.get('mode') == 'vehicle-auto-v1'
    if view['prepared']:
        try:
            from app.vehicle_auto import REVISION, current_ocr_adoption_signature
            from app.vehicle_workflow import digest, requirements_hash
            view['stale'] = (
                envelope.get('extractor_revision') != REVISION
                or envelope['source_hash'] != digest(sources(manager, pid))
                or envelope.get('requirements_hash') != requirements_hash(mission)
                or envelope.get('ocr_adoption_hash', '') != current_ocr_adoption_signature(manager, pid)
            )
        except Exception:
            view['stale'] = True
    if envelope:
        _, summary, rows = source_reconciliation_report(data, sources(manager, pid))
        view['recon'] = summary
        if not rows:
            view['controls'] = 'empty'
        elif summary.get('mismatched'):
            view['controls'] = 'mismatch'
        elif summary.get('unavailable') or summary.get('incomplete'):
            view['controls'] = 'unavailable'
        elif summary.get('passed') is True:
            view['controls'] = 'matched'
        else:
            view['controls'] = 'empty'
        if view['unresolved_reads'] or view['unresolved_allocations'] or summary.get('passed') is not True:
            view['needs_review'] = True
    from app.structured_planning import contract_of
    from app.vehicle_workflow import read_json, resolve
    for task in mission.get('tasks') or []:
        contract = contract_of(task) or {}
        kind = contract.get('execution_kind')
        if kind not in {'vehicle_calculate', 'vehicle_verify'}:
            continue
        for output in contract.get('outputs') or []:
            path = str(output.get('path') or '')
            try:
                target = resolve(manager, pid, path)
            except Exception:
                continue
            if not target.exists():
                continue
            if path.endswith('.xlsx') and target.is_file() and target.stat().st_size > 0:
                view['xlsx'] = True
            if path.endswith('.json'):
                try:
                    body = read_json(target)
                except Exception:
                    continue
                if body.get('status') == 'needs_review' or body.get('issues'):
                    view['needs_review'] = True
    return view


def _active_job(manager, pid, signature, plan, revision, mission):
    store = ReviewStore(manager.memory.path)
    jobs = active_jobs(store, pid)
    if jobs:
        job = max(jobs, key=lambda x: float(x.get('updated_at') or x.get('started') or 0))
        return {
            'id': job.get('id') or signature,
            'kind': job.get('kind') or 'job',
            'updated_at': float(job.get('updated_at') or job.get('started') or 0),
            'status': job.get('status'),
            'last_completed_stage': job.get('last_completed_stage') or '',
            'blocking_error': job.get('blocking_error') or '',
            'resume_from': job.get('resume_from') or '',
        }
    if plan.get('status') == 'running':
        return {'id': plan.get('job_id') or signature, 'kind': 'plan_review', 'updated_at': float(plan.get('started') or plan.get('finished') or 0)}
    if revision.get('status') in {'generating', 'running'}:
        return {
            'id': revision.get('job_id') or revision.get('candidate_id') or signature,
            'kind': 'propose',
            'updated_at': float(revision.get('started') or 0),
        }
    if pid in getattr(manager, 'planning_projects', set()):
        return {'id': pid, 'kind': 'planning', 'updated_at': time.time()}
    if mission.get('status') == 'running':
        return {'id': pid, 'kind': 'mission', 'updated_at': 0}
    return None


def _phase(mission, vehicle, revision, plan, queue, gate, issues, failures, job, review_required):
    """優先順位1〜10。complete は goal_failures 空かつ照合 matched のときだけ。"""
    if job or mission.get('status') == 'running' or plan.get('status') == 'running':
        return 'running'
    if vehicle['applicable'] and (vehicle['unresolved_reads'] or vehicle['controls'] in {'mismatch', 'unavailable'}):
        return 'accuracy_blocked'
    if vehicle['applicable'] and vehicle['unresolved_allocations']:
        return 'fact_confirm'
    if revision.get('blockers'):
        code = (classify_replan_failure(revision) or {}).get('code')
        return {'development_required': 'dev_blocked', 'input_insufficient': 'plan_fact_confirm'}.get(code, 'plan_conflict')
    plan_status = plan.get('status')
    if plan_status == 'connection_failed' or gate.get('stop_kind') == 'connection_settings':
        return 'connection_failed'
    if issues and plan_status in {None, '', 'not_passed', 'unverified', 'awaiting_external'}:
        return 'issues_open'
    if plan_status == 'waiting_budget' or queue.get('status') == 'waiting_budget':
        return 'waiting_budget'
    if review_required and (gate.get('blocked') or plan_status != 'passed'):
        return 'unverified'
    if vehicle['applicable'] and (vehicle['needs_review'] or vehicle['xlsx']) and not (
        mission.get('status') == 'completed' and not failures and _controls_matched(vehicle)
    ):
        return 'provisional'
    if mission.get('status') == 'completed' and not failures and vehicle['applicable'] and _controls_matched(vehicle):
        return 'complete'
    return 'idle'


def _endpoint(pid, suffix):
    return f'/api/projects/{pid}{suffix}' if suffix else None


def _next_action(phase, vehicle, issues, pid='', allowed_actions=None):
    allowed_actions = allowed_actions or {}
    action = None
    if phase == 'running':
        action = {'id': 'wait', 'label': '処理の完了を待つ', 'endpoint': None, 'class': 'local_safe', 'auto_executable': False}
    elif phase == 'accuracy_blocked':
        if vehicle['controls'] == 'mismatch':
            action = {'id': 'review_source_difference', 'label': '原本照合差額を確認', 'endpoint': _endpoint(pid, '/completion-gate'), 'class': 'human_fact', 'auto_executable': False}
        elif (
            vehicle['prepared'] and not vehicle['stale']
            and (vehicle['unresolved_reads'] > 0 or vehicle['controls'] == 'unavailable')
        ):
            action = {'id': 'resolve_source_reads', 'label': '原本の読取失敗・統制値不足を確認し、原本の修正または該当値の回答を行う', 'endpoint': _endpoint(pid, '/completion-gate'), 'class': 'human_fact', 'auto_executable': False}
        else:
            action = {'id': 'prepare', 'label': '原本から自動抽出を更新する', 'endpoint': _endpoint(pid, '/vehicle-profit/prepare'), 'class': 'local_safe', 'auto_executable': True}
    elif phase == 'fact_confirm':
        action = {'id': 'confirm_allocation', 'label': '未配賦の確認事項に回答する', 'endpoint': _endpoint(pid, '/vehicle-profit/decision'), 'class': 'human_fact', 'auto_executable': False}
    elif phase == 'dev_blocked':
        action = {'id': 'resolve_development', 'label': '追加開発が必要な指摘を確認する', 'endpoint': _endpoint(pid, '/goal-review'), 'class': 'development', 'auto_executable': False}
    elif phase == 'plan_conflict':
        action = {'id': 'review_feedback', 'label': '保存済み修正案の対象と根拠を確認する', 'endpoint': _endpoint(pid, '/goal-review'), 'class': 'human_approval', 'auto_executable': False}
    elif phase == 'plan_fact_confirm':
        action = {'id': 'review_feedback', 'label': '必要な業務事実と修正案を確認する', 'endpoint': _endpoint(pid, '/goal-review'), 'class': 'human_fact', 'auto_executable': False}
    elif phase == 'issues_open':
        action = {'id': 'propose_feedback', 'label': '指摘から修正案を作成する', 'endpoint': _endpoint(pid, '/goal-review/feedback/propose'), 'class': 'local_safe', 'auto_executable': False}
    elif phase == 'waiting_budget':
        action = {'id': 'wait_budget', 'label': '予算回復後の自動再開を待つ', 'endpoint': _endpoint(pid, '/goal-review/queue/cancel'), 'class': 'external', 'auto_executable': False}
    elif phase == 'connection_failed':
        action = {'id': 'external_review', 'label': '接続設定を確認して外部AI検証を再試行する', 'endpoint': _endpoint(pid, '/goal-review/plan'), 'class': 'external', 'auto_executable': False}
    elif phase == 'unverified':
        if issues:
            action = {'id': 'propose_feedback', 'label': '指摘から修正案を作成する', 'endpoint': _endpoint(pid, '/goal-review/feedback/propose'), 'class': 'local_safe', 'auto_executable': False}
        else:
            action = {'id': 'external_review', 'label': '外部AIで現行計画を検証する', 'endpoint': _endpoint(pid, '/goal-review/plan'), 'class': 'external', 'auto_executable': False}
    elif phase == 'provisional':
        action = {'id': 'review_artifacts', 'label': '暫定Excelと確認事項を確認する', 'endpoint': _endpoint(pid, '/completion-gate'), 'class': 'human_approval', 'auto_executable': False}
    elif phase == 'complete':
        action = {'id': 'approve_result', 'label': '確定結果を人間が確認する', 'endpoint': _endpoint(pid, '/completion-gate/accept'), 'class': 'human_approval', 'auto_executable': False}
    else:
        action = {'id': 'idle', 'label': '次の操作はありません', 'endpoint': None, 'class': 'local_safe', 'auto_executable': False}

    action_id = action['id']
    rule = allowed_actions.get(action_id) or {}
    is_allowed = bool(rule.get('allowed', False)) if isinstance(rule, dict) else bool(rule)
    action['executable'] = is_allowed and action_id not in {'idle', 'wait'}
    action['manual_executable'] = is_allowed and action_id not in {'idle', 'wait'}
    action['reason'] = '' if is_allowed else (rule.get('reason') if isinstance(rule, dict) else '')
    return action


def _stop_reason(phase, gate, vehicle, revision, issues, failures, plan=None):
    if phase == 'running':
        return '処理を実行中です。完了まで操作を待ってください。'
    if phase == 'accuracy_blocked':
        if vehicle['unresolved_reads']:
            return '原本の読取失敗が残っているため確定できません。抽出結果を確認してください。'
        if vehicle['controls'] == 'mismatch':
            return '原本照合が不一致のため確定できません。差額を確認してください。'
        return '原本統制値が不足しているため確定できません。'
    if phase == 'fact_confirm':
        return '未配賦または業務事実の確認が残っています。'
    if phase in {'plan_conflict', 'plan_fact_confirm'}:
        failure = classify_replan_failure(revision) or {}
        return str(failure.get('reason') or failure.get('next_work') or '修正案の未解決事項を確認してください。')
    if phase == 'dev_blocked':
        blockers = revision.get('blockers') or []
        first = blockers[0] if blockers and isinstance(blockers[0], dict) else {}
        return str(first.get('reason') or '追加開発が必要な指摘があるため反映と実行を開始できません。')
    if phase == 'issues_open':
        return '計画への指摘が未対応です。修正案を作成してください。'
    if phase == 'waiting_budget':
        return '外部検証の利用枠回復待ちです。承認済み送信内容は保持しています。'
    if phase == 'connection_failed':
        return gate.get('reason') or (plan or {}).get('stop_reason') or '必須の外部AI接続に失敗しました。計画内容の欠陥ではなく接続設定の問題です。'
    if phase == 'unverified':
        return gate.get('reason') or '現行版の外部検証が未完了または未合格です。'
    if phase == 'provisional':
        return '暫定成果があります。原本照合と確認事項を満たすまで確定完了にはしません。'
    if phase == 'complete':
        return '目標と原本照合を満たしています。'
    if failures:
        return failures[0]
    if issues:
        return '指摘が残っています。'
    return '待機中です。'


def _allowed(manager, pid, mission, vehicle, revision, plan, queue, gate, issues, job, phase=''):
    running = bool(job) or mission.get('status') == 'running' or pid in getattr(manager, 'planning_projects', set())
    busy = '実行中です' if running else ''
    blockers = list(revision.get('blockers') or [])
    failure = classify_replan_failure(revision) or {}
    blocker_label = failure.get('label') or '未解決の指摘'
    draft_ok = revision.get('status') == 'draft' and not blockers and revision.get('changes')
    start_reason = ''
    if running:
        start_reason = busy
    elif blockers:
        start_reason = blocker_label + 'があるため実行を開始できません'
    elif gate.get('blocked'):
        start_reason = gate.get('reason') or '外部検証が未完了です'
    elif mission.get('status') not in {'ready', 'paused'}:
        start_reason = '計画の承認後に実行できます'
    actions = {
        'prepare': _action(
            vehicle['applicable'] and not running,
            busy or ('車両別損益の対象ではありません' if not vehicle['applicable'] else ''),
        ),
        'import_feedback': _action(not running, busy),
        'propose_feedback': _action(bool(issues) and not running, busy or ('指摘がありません' if not issues else '')),
        'apply': _action(
            bool(draft_ok) and not running,
            busy or ('追加開発が必要な指摘があります' if blockers else '反映できる修正案がありません'),
        ),
        'external_review': _action(
            not running and send_allowed(manager, pid),
            busy or ('外部AIの許可・接続が揃っていません' if not send_allowed(manager, pid) else ''),
        ),
        'approve_plan': _action(
            mission.get('status') == 'planning' and not running and not blockers,
            busy or (blocker_label + 'を解消してから計画を承認してください' if blockers else '承認できる計画がありません'),
        ),
        'start': _action(not start_reason, start_reason),
        'cancel_queue': _action(
            queue.get('status') == 'waiting_budget' or plan.get('status') == 'waiting_budget',
            '予算待ちのキューがありません',
        ),
        'review_source_difference': _action(
            vehicle['applicable'] and vehicle['controls'] == 'mismatch' and not running,
            busy or ('原本照合の不一致はありません' if vehicle['controls'] != 'mismatch' else ''),
        ),
        'resolve_source_reads': _action(
            vehicle['applicable'] and (vehicle['unresolved_reads'] > 0 or vehicle['controls'] == 'unavailable') and not running,
            busy or ('読取失敗や統制値不足はありません' if not (vehicle['unresolved_reads'] > 0 or vehicle['controls'] == 'unavailable') else ''),
        ),
        'confirm_allocation': _action(
            vehicle['applicable'] and vehicle['unresolved_allocations'] > 0 and not running,
            busy or ('未解決の配賦はありません' if not vehicle['unresolved_allocations'] else ''),
        ),
        'apply_prior_answers': _action(
            vehicle['applicable'] and vehicle['unresolved_allocations'] > 0 and not running,
            busy or ('未解決の配賦はありません' if not vehicle['unresolved_allocations'] else ''),
        ),
        'resolve_development': _action(
            bool(blockers) and failure.get('code') == 'development_required' and not running,
            busy or '追加開発が必要な指摘はありません',
        ),
        'review_feedback': _action(
            bool(blockers) and not running,
            busy or '確認する保存済み修正案はありません',
        ),
        'review_artifacts': _action(
            bool(vehicle['needs_review'] or vehicle['xlsx']) and not running,
            busy or ('確認が必要な暫定成果物はありません' if not (vehicle['needs_review'] or vehicle['xlsx']) else ''),
        ),
        'approve_result': _action(
            phase == 'complete' and not running,
            busy or ('確定完了条件を満たしていません' if phase != 'complete' else ''),
        ),
        'wait_budget': _action(
            phase == 'waiting_budget',
            '予算回復待ちではありません',
        ),
        'wait': _action(
            bool(running),
            '実行中ではありません',
        ),
        'idle': _action(
            phase == 'idle' and not running,
            '待機状態ではありません',
        ),
    }
    blocked = [{'id': key, 'reason': value['reason']} for key, value in actions.items() if not value['allowed']]
    for ident, reason in UNIMPLEMENTED_ACTIONS:
        blocked.append({'id': ident, 'reason': reason})
    return actions, blocked


def _triz_view(manager, pid):
    """framed/candidates/tried を回復成功と表示しない。P2完了まで adopt は allowed に出さない。"""
    from app.automatic_triz import is_business_success, library_status_label
    from app.vehicle_workflow import resolve
    view = {
        'status': '',
        'label': '',
        'recovery_success': False,
        'business_recovered': False,
        'adopt_allowed': False,
        'adopt_blocked_reason': 'P2完了までTRIZ採用は blocked。framed/candidates/tried は回復成功ではありません',
    }
    try:
        root = resolve(manager, pid, 'result/triz')
    except Exception:
        return view
    if not root.exists():
        return view
    latest = None
    latest_mtime = -1
    for path in root.rglob('*.json'):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= latest_mtime:
            latest_mtime = mtime
            latest = path
    if latest is None:
        return view
    try:
        body = json.loads(latest.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return view
    status = str(body.get('status') or '')
    view['status'] = status
    view['label'] = library_status_label(status)
    recovered = is_business_success(status)
    view['recovery_success'] = recovered
    view['business_recovered'] = recovered
    if status in {'framed', 'candidate_generated', 'artifact_trial_passed', 'artifact_trials_complete', 'encoding_repaired', 'preparing'}:
        view['recovery_success'] = False
        view['label'] = library_status_label(status)
    return view


def _gate_cache_key(manager, pid: str, contract: dict, mission: dict) -> tuple:
    from app.completion_gate import _current_hashes
    from app.goal_completion_store import GoalCompletionStore

    hashes = _current_hashes(manager, pid, contract, mission)
    acceptance = GoalCompletionStore(manager.memory.path).latest_valid_acceptance(pid, **hashes)
    return (
        str(manager.memory.path), pid,
        *(str(hashes.get(name) or "") for name in (
            "contract_hash", "plan_signature", "input_hash", "source_hash", "artifact_hash"
        )),
        int((acceptance or {}).get("id") or 0),
    )


def _cached_gate(manager, pid: str, contract: dict, mission: dict) -> dict:
    from app.completion_gate import evaluate as evaluate_gate

    key = _gate_cache_key(manager, pid, contract, mission)
    cached = _GATE_CACHE.get(key)
    if cached is not None:
        _GATE_CACHE.move_to_end(key)
        return dict(cached)
    value = evaluate_gate(manager, pid)
    _GATE_CACHE[key] = dict(value)
    _GATE_CACHE.move_to_end(key)
    while len(_GATE_CACHE) > _GATE_CACHE_LIMIT:
        _GATE_CACHE.popitem(last=False)
    return dict(value)


def build_readiness(manager, pid: str) -> dict:
    """設計書 第4.1節の JSON を組み立てる。既存判定関数を呼ぶだけ。"""
    mission = manager.memory.get_mission(pid)
    _, signature = plan_snapshot(manager, pid)
    store = ReviewStore(manager.memory.path)
    plan = store.get(pid, 'plan', signature) or {}
    revision = store.get(pid, 'revision', signature) or {}
    queue = store.get(pid, 'plan_queue', signature) or {}
    gate = execution_gate(manager, pid)
    issues = issues_for(manager, pid, signature)
    vehicle = _vehicle_view(manager, pid, mission)
    failures = list(goal_failures(manager, pid) or [])
    job = _active_job(manager, pid, signature, plan, revision, mission)
    orch = orchestration_view(manager, pid)
    review_required = enabled(manager, pid)
    phase = _phase(mission, vehicle, revision, plan, queue, gate, issues, failures, job, review_required)
    final_completed = (
        phase == 'complete'
        and not failures
        and vehicle['applicable']
        and _controls_matched(vehicle)
        and mission.get('status') == 'completed'
    )
    if phase == 'complete' and not final_completed:
        phase = 'provisional' if (vehicle['xlsx'] or vehicle['needs_review']) else 'idle'
    state, state_label = PHASE_LABELS.get(phase, PHASE_LABELS['idle'])
    if vehicle['controls'] == 'mismatch' and phase == 'accuracy_blocked':
        state, state_label = 'source_reconciliation_failed', '原本照合不一致'
    allowed, blocked = _allowed(manager, pid, mission, vehicle, revision, plan, queue, gate, issues, job, phase=phase)
    if final_completed:
        artifact = 'final'
    elif vehicle['xlsx'] or vehicle['needs_review'] or vehicle['unresolved_reads'] or vehicle['unresolved_allocations']:
        artifact = 'provisional'
    else:
        artifact = 'none'
    if artifact == 'final' and not final_completed:
        artifact = 'provisional'
    contract = {}
    gate_error = ""
    try:
        from app.goal_contract import preview as preview_contract
        contract = preview_contract(manager, pid)
        gate_view = _cached_gate(manager, pid, contract, mission)
    except Exception as exc:
        gate_error = str(exc) or type(exc).__name__
        LOGGER.exception("goal_completion: readiness gate evaluation failed: %s", exc)
        gate_view = {
            'achieved': False, 'failed_criteria': [],
            'artifact_class': artifact, 'contract_hash': '', 'error': gate_error,
        }
    canonical = {
        'running': 'executing',
        'accuracy_blocked': 'needs_input',
        'fact_confirm': 'needs_input',
        'dev_blocked': 'development_required',
        'plan_conflict': 'plan_review',
        'plan_fact_confirm': 'needs_input',
        'issues_open': 'plan_review',
        'waiting_budget': 'plan_review',
        'unverified': 'plan_review',
        'connection_failed': 'plan_review',
        'provisional': 'needs_approval',
        'complete': 'achieved' if gate_view.get('achieved') else 'needs_approval',
        'idle': 'planned' if mission.get('tasks') else 'draft',
    }.get(phase, 'draft')

    pipeline_stage, pipeline_action_id = resolve_pipeline_stage(mission, plan, revision, issues, gate, job)
    replan_fail = classify_replan_failure(revision)
    next_act = _next_action(phase, vehicle, issues, pid, allowed_actions=allowed)

    return {
        'project_id': pid,
        'goal': mission.get('goal') or '',
        'plan_version': mission.get('plan_version') or 0,
        'plan_signature': signature,
        'mission_status': mission.get('status') or '',
        'phase': phase,
        'state': state,
        'state_label': state_label,
        'pipeline_stage': pipeline_stage,
        'pipeline_next_action': pipeline_action_id,
        'replan_failure': replan_fail,
        'stop_reason': _stop_reason(phase, gate, vehicle, revision, issues, failures, plan),
        'provisional': bool(not final_completed and artifact == 'provisional'),
        'final_completed': bool(final_completed),
        'next_action': next_act,
        'canonical_state': canonical,
        'contract_version': (contract or {}).get('version') or 0,
        'contract_hash': (contract or {}).get('content_hash') or gate_view.get('contract_hash') or '',
        'gate': {
            'achieved': bool(gate_view.get('achieved')),
            'failed_criteria': list(gate_view.get('failed_criteria') or []),
            'artifact_class': gate_view.get('artifact_class') or artifact,
            **({'error': gate_error} if gate_error else {}),
        },
        'allowed_actions': allowed,
        'blocked_actions': blocked,
        'active_job': job,
        'resume_from': (job or {}).get('resume_from') or orch.get('resume_from') or '',
        'last_completed_stage': orch.get('last_completed_stage') or (job or {}).get('last_completed_stage') or '',
        'blocking_error': orch.get('blocking_error') or (job or {}).get('blocking_error') or '',
        'send_allowed': send_allowed(manager, pid),
        'artifact_class': artifact,
        'vehicle': {
            'applicable': vehicle['applicable'],
            'prepared': vehicle['prepared'],
            'stale': vehicle['stale'],
            'unresolved_reads': vehicle['unresolved_reads'],
            'unresolved_allocations': vehicle['unresolved_allocations'],
            'controls': vehicle['controls'],
        },
        'triz': _triz_view(manager, pid),
        'revision_token': _token(signature, mission.get('plan_version'), vehicle.get('controls'), plan.get('status')),
    }
