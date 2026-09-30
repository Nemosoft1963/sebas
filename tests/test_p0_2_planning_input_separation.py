"""P0-2: 計画生成入力の分離。指摘全文を計画工程へ混ぜず、生成後検査で重複を拒否する。"""
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.core import Ollama
from app.goal_review import ReviewStore, plan_snapshot
from app.plan_feedback import (
    PLANNING_FEEDBACK, PLANNING_ISSUE_FIELDS, import_feedback, issues_for,
    normalize_planning_feedback, normalize_planning_issue,
)
from app.project_manager import ProjectOrchestrator
from app.structured_planning import (
    compile_plan, inspect_plan_structure, normalize_title, validate_plan,
)
from test_goal_review import setup
from test_structured_planning import task as make_structured_task


UNIQUE_REVIEW = (
    "UNIQUE_REVIEW_PHRASE_ALPHA_FOR_PLANNING_DELTA_TEST "
    "はタスク名やタスク本文へコピーしてはいけない長い評価レポートです。"
)


def _normalized_issues(signature='sig'):
    issues = [
        {
            'id': 'iss-alpha',
            'provider': 'provider_alpha',
            'criterion': 'SC01',
            'text': '照合手順を具体化してください。' + UNIQUE_REVIEW,
            'origin': 'api',
        },
        {
            'id': 'iss-beta',
            'provider': 'provider_beta',
            'criterion': 'SC01',
            'text': '成果物の判定基準を明記してください。' + UNIQUE_REVIEW,
            'origin': 'api',
        },
        {
            'id': 'iss-gamma',
            'provider': 'provider_gamma',
            'criterion': 'SC02',
            'text': '計画草案の評価と改善提案を各社分だけ追加してください。' + UNIQUE_REVIEW,
            'origin': 'api',
        },
    ]
    return issues, normalize_planning_feedback(issues, signature)


def test_normalize_planning_feedback_is_delta_not_full_review():
    issues, deltas = _normalized_issues('plan-sig-1')
    assert len(deltas) == 3
    for row in deltas:
        for field in PLANNING_ISSUE_FIELDS:
            assert field in row
        assert 'text' not in row
        assert UNIQUE_REVIEW not in json.dumps(row, ensure_ascii=False)
        assert row['source_signature'] == 'plan-sig-1'
        assert isinstance(row['criterion_ids'], list)
        assert isinstance(row['required_change'], str)
        assert row['evidence']
    eval_rows = [row for row in deltas if row['category'] == 'evaluation']
    assert len(eval_rows) == 1
    assert '評価専用タスクは追加しない' in eval_rows[0]['required_change']
    compact = normalize_planning_issue(issues[0], 'plan-sig-1')
    assert compact['criterion_ids'] == ['SC01']
    assert compact['required_change'] == '照合手順を具体化してください'
    assert UNIQUE_REVIEW not in compact['required_change']


@pytest.mark.asyncio
async def test_planning_feedback_excludes_full_review_during_generation(tmp_path):
    manager, pid, task = setup(tmp_path, True)
    manager.llm = Mock(spec=Ollama)
    sig = plan_snapshot(manager, pid)[1]
    store = ReviewStore(manager.memory.path)
    store.put(pid, 'plan', sig, {
        'status': 'fail',
        'reviews': [
            {
                'provider': 'provider_alpha',
                'status': 'fail',
                'issues': ['照合手順を具体化してください。' + UNIQUE_REVIEW],
            },
            {
                'provider': 'provider_beta',
                'status': 'fail',
                'issues': ['成果物の判定基準を明記してください。' + UNIQUE_REVIEW],
            },
            {
                'provider': 'provider_gamma',
                'status': 'fail',
                'issues': ['計画草案の評価と改善提案を各社分だけ追加してください。' + UNIQUE_REVIEW],
            },
        ],
    })
    raw = issues_for(manager, pid, sig)
    assert len(raw) >= 3
    captured = {}

    async def generate(_):
        feedback = PLANNING_FEEDBACK.get()
        captured['feedback'] = deepcopy(feedback)
        captured['task_count_before'] = len(manager.memory.get_mission(pid)['tasks'])
        mission = manager.memory.get_mission(pid)
        manager.memory.replace_plan(
            pid, 'P0-2 draft', mission['tasks'], expected_version=mission['plan_version'],
        )

    manager._generate_plan_impl = generate

    async def local(system, prompt, schema=None):
        assert UNIQUE_REVIEW not in prompt
        assert '満たすべき差分' in prompt
        issue = issues_for(manager, pid, plan_snapshot(manager, pid)[1])
        return json.dumps({'actions': [
            {
                'issue_id': item['id'],
                'disposition': 'unresolved',
                'target': task['task_key'],
                'change': '現行工程の検証方法を差分として具体化する。',
                'reason': '指摘は差分として扱い、評価専用タスクは増やさない。',
            }
            for item in issue
        ]})

    manager._local_complete = local
    result = await manager.generate_plan(pid)
    assert captured['feedback']
    assert all('text' not in row for row in captured['feedback'])
    assert all(UNIQUE_REVIEW not in json.dumps(row, ensure_ascii=False) for row in captured['feedback'])
    assert len(result['tasks']) == captured['task_count_before']
    blob = json.dumps(result['tasks'], ensure_ascii=False)
    assert UNIQUE_REVIEW not in blob
    assert all(UNIQUE_REVIEW not in (t.get('title') or '') for t in result['tasks'])
    assert all(UNIQUE_REVIEW not in (t.get('description') or '') for t in result['tasks'])


@pytest.mark.asyncio
async def test_local_complete_does_not_append_planning_feedback(tmp_path):
    manager, pid, _ = setup(tmp_path)
    seen = []

    class Llm:
        async def stream(self, messages):
            seen.append(messages[-1]['content'])
            yield '{"ok":true}'

    manager.llm = Llm()
    token = PLANNING_FEEDBACK.set(normalize_planning_feedback([
        {'id': 'x', 'text': UNIQUE_REVIEW, 'criterion': 'SC01'},
    ], 'sig'))
    try:
        text = await manager._local_complete('sys', '計画を1件だけ作る')
    finally:
        PLANNING_FEEDBACK.reset(token)
    assert text
    assert UNIQUE_REVIEW not in seen[0]
    assert '過去の外部AI指摘' not in seen[0]


def test_inspect_plan_structure_rejects_duplicate_task_key_and_title():
    plan = compile_plan(['条件A', '条件B'], [make_structured_task(1), make_structured_task(2)])
    broken = deepcopy(plan)
    broken['tasks'][1]['task_key'] = broken['tasks'][2]['task_key']
    result = inspect_plan_structure(broken, {'SC01', 'SC02'})
    assert not result['passed']
    assert any('duplicate task_key' in item for item in result['issues'])

    titled = deepcopy(plan)
    titled['tasks'][1]['title'] = '市場 整理'
    titled['tasks'][2]['title'] = '市場整理'
    assert normalize_title(titled['tasks'][1]['title']) == normalize_title(titled['tasks'][2]['title'])
    result = inspect_plan_structure(titled, {'SC01', 'SC02'})
    assert not result['passed']
    assert any('duplicate normalized title' in item for item in result['issues'])


def test_inspect_plan_structure_rejects_duplicate_criterion_and_final_verification():
    plan = compile_plan(['条件A', '条件B'], [make_structured_task(1), make_structured_task(2)])
    broken = deepcopy(plan)
    from app.structured_planning import contract_of
    extra = deepcopy(broken['tasks'][1])
    extra['task_key'] = 'SC01b'
    extra['title'] = '条件Aの重複実行'
    contract = contract_of(extra)
    contract['criterion_ids'] = ['SC01']
    extra['acceptance_criteria'] = json.dumps(contract, ensure_ascii=False)
    broken['tasks'].insert(-1, extra)
    result = inspect_plan_structure(broken, {'SC01', 'SC02'})
    assert not result['passed']
    assert any('multiple primary execution tasks' in item for item in result['issues'])

    finals = deepcopy(plan)
    clone = deepcopy(finals['tasks'][-1])
    clone['task_key'] = 'final_verification_2'
    clone['title'] = '最終検証の再実施'
    finals['tasks'].append(clone)
    result = inspect_plan_structure(finals, {'SC01', 'SC02'})
    assert not result['passed']
    assert 'final_verification_duplicate' in result['issues']


def test_inspect_plan_structure_does_not_scale_evaluation_tasks_with_reviews():
    plan = compile_plan(['条件A'], [make_structured_task(1)])
    extra = {
        'task_key': 'eval_1',
        'title': '計画草案の評価と改善提案',
        'description': '外部レビューを工程として繰り返す',
        'depends_on': ['SC00'],
        'acceptance_criteria': '',
    }
    extra2 = dict(extra, task_key='eval_2', title='計画草案の評価と改善提案 2')
    bloated = deepcopy(plan)
    bloated['tasks'][1:1] = [extra, extra2]
    result = inspect_plan_structure(bloated, {'SC01'}, review_count=3)
    assert not result['passed']
    assert 'evaluation_task_limit' in result['issues']
    assert 'evaluation_tasks_scale_with_reviews' in result['issues']
    healthy = inspect_plan_structure(plan, {'SC01'}, review_count=5)
    assert healthy['passed']
    assert healthy['evaluation_tasks'] == []
    assert len(healthy['final_verification_tasks']) == 1
    coverage = healthy['coverage']['SC01']
    assert coverage['exec_task_keys'] == ['SC01']
    assert coverage['artifact_paths']
    assert coverage['verify_task_keys'] == ['final_verification']


def test_validate_plan_still_accepts_normal_compiled_plan():
    plan = compile_plan(['条件A', '条件B'], [make_structured_task(1), make_structured_task(2)])
    assert validate_plan(plan, {'SC01', 'SC02'})['passed']


@pytest.mark.asyncio
async def test_multiple_reviews_do_not_increase_structured_task_count(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    class Llm:
        async def stream(self, messages):
            yield json.dumps({
                'title': '販売資料', 'scope': '架空の販売準備資料の内容を整理する',
                'headings': ['目的', '実施内容'], 'depends_on': [],
            }, ensure_ascii=False)

    memory = ShortTermMemory(tmp_path / 'memory.db')
    project = memory.create_project('P0-2計画')
    pid = project['id']
    memory.save_mission(pid, '## 達成条件\n1. 販売資料を作成', '', '', False, [])
    manager = ProjectOrchestrator(
        memory, Llm(), lambda _: ('', []), None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / 'workspace'),
    )
    baseline = await manager.generate_plan(pid)
    baseline_count = len(baseline['tasks'])
    sig = plan_snapshot(manager, pid)[1]
    for provider, text in (
        ('alpha', '照合手順を具体化してください。' + UNIQUE_REVIEW),
        ('beta', '成果物の判定基準を明記してください。' + UNIQUE_REVIEW),
        ('gamma', '依存関係を明示してください。' + UNIQUE_REVIEW),
        ('delta', '検証方法を1件にまとめてください。' + UNIQUE_REVIEW),
    ):
        import_feedback(manager, pid, sig, provider, text)
    assert len(issues_for(manager, pid, sig)) >= 3
    manager.llm = Mock(spec=Ollama)

    async def generate(_):
        feedback = PLANNING_FEEDBACK.get() or []
        assert len(feedback) >= 3
        assert all('text' not in row for row in feedback)
        assert all(UNIQUE_REVIEW not in json.dumps(row, ensure_ascii=False) for row in feedback)
        mission = manager.memory.get_mission(pid)
        manager.memory.replace_plan(
            pid, mission.get('plan_summary') or 'P0-2', mission['tasks'],
            expected_version=mission['plan_version'],
        )

    async def local(system, prompt, schema=None):
        assert UNIQUE_REVIEW not in prompt
        issue = issues_for(manager, pid, plan_snapshot(manager, pid)[1])
        return json.dumps({'actions': [
            {
                'issue_id': item['id'],
                'disposition': 'unresolved',
                'target': 'SC01',
                'change': '現行の実行タスクで差分を満たす。',
                'reason': '評価専用タスクは増やさず既存工程で対応する。',
            }
            for item in issue
        ]})

    manager._generate_plan_impl = generate
    manager._local_complete = local
    updated = await manager.generate_plan(pid)
    assert len(updated['tasks']) == baseline_count
    assert UNIQUE_REVIEW not in json.dumps(updated['tasks'], ensure_ascii=False)
    eval_titles = [t['title'] for t in updated['tasks'] if '計画草案の評価' in t['title']]
    assert eval_titles == []
