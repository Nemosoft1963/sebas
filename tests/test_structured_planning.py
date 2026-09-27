import json
import asyncio
from copy import deepcopy

import pytest

from app.structured_planning import (
    compile_task, compile_plan, validate_plan, verify_outputs, extract_criteria, contract_of,
    sanitize_proposal,
)
from app.project_manager import ProjectOrchestrator
from app.memory.short_term import ShortTermMemory
from app.workspace_files import WorkspaceSandbox


def task(index, depends=None):
    return compile_task(index, f'条件{index}', {
        'title': f'資料{index}', 'scope': '条件に対応する具体的な本文を作る',
        'headings': ['目的', '実施内容'], 'depends_on': depends or [],
    }, [])


def test_extract_criteria_stops_at_next_section_and_keeps_additional_success():
    assert extract_criteria('## 達成条件\n1. 市場を定義\n2. 資料を作る\n## 次\n1. 別項目', '仮説検証') == ['市場を定義', '資料を作る', '仮説検証']


def test_sanitize_proposal_removes_backend_owned_scope_tokens():
    proposal = sanitize_proposal({
        'title': '会社情報の反映',
        'scope': 'https://example.com/ と result/sc02.md を pandas で読み report.pdf に反映',
        'headings': ['運営会社', 'システム概要'],
    })
    current = compile_task(1, '運営会社を明示する', proposal, [])
    assert 'https://' not in proposal['scope']
    assert 'result/' not in proposal['scope']
    assert 'pandas' not in proposal['scope']
    assert contract_of(current)['criterion'] == '運営会社を明示する'


def test_replace_plan_preserves_completed_matching_criteria(tmp_path):
    memory = ShortTermMemory(tmp_path / 'memory.db')
    project = memory.create_project('再計画テスト')
    first = compile_plan(['既存条件'], [task(1)])
    mission = memory.replace_plan(project['id'], first['summary'], first['tasks'])
    preparation = next(item for item in mission['tasks'] if item['task_key'] == 'SC00')
    completed = next(item for item in mission['tasks'] if item['task_key'] == 'SC01')
    memory.update_task(preparation['id'], 'completed', result='原本確認済み')
    memory.update_task(completed['id'], 'completed', result='既存成果')

    second = compile_plan(['既存条件', '追加条件'], [task(1), task(2)])
    updated = memory.replace_plan(project['id'], second['summary'], second['tasks'])
    by_key = {item['task_key']: item for item in updated['tasks']}

    assert by_key['SC01']['id'] == completed['id']
    assert by_key['SC01']['status'] == 'completed'
    assert by_key['SC01']['result'] == '既存成果'
    assert by_key['SC02']['status'] == 'pending'
    assert by_key['final_verification']['status'] == 'pending'


def test_replace_plan_reopens_completed_task_when_execution_contract_changes(tmp_path):
    memory = ShortTermMemory(tmp_path / 'memory.db')
    project = memory.create_project('再計画差分テスト')
    original = compile_plan(['既存条件'], [task(1)])
    mission = memory.replace_plan(project['id'], original['summary'], original['tasks'])
    completed = next(item for item in mission['tasks'] if item['task_key'] == 'SC01')
    memory.update_task(completed['id'], 'completed', result='古い成果')

    changed_task = compile_task(1, '既存条件', {
        'title': '変更後の資料', 'scope': '変更後の条件に対応する本文を作る',
        'headings': ['変更目的', '変更内容'], 'depends_on': [],
    }, [])
    changed = compile_plan(['既存条件'], [changed_task])
    updated = memory.replace_plan(project['id'], changed['summary'], changed['tasks'])
    current = next(item for item in updated['tasks'] if item['task_key'] == 'SC01')

    assert current['status'] == 'pending'
    assert current['result'] == ''


def test_google_site_publication_is_terminal_and_requires_registered_evidence():
    preparation = task(1)
    publication = compile_task(2, 'Google Sitesでランディングページを公開し公開URLを登録する', {
        'title': '公開', 'scope': '承認済み内容を公開してURLを検証する',
        'headings': ['公開内容', '検証結果'], 'depends_on': [],
    }, [])
    plan = compile_plan(
        ['公開内容を準備する', 'Google Sitesでランディングページを公開し公開URLを登録する'],
        [preparation, publication],
    )
    current = next(item for item in plan['tasks'] if item['task_key'] == 'SC02')
    contract = contract_of(current)
    assert contract['action_requirements'][0]['kind'] == 'google_site_publication'
    assert current['depends_on'] == ['SC00', 'SC01']
    assert contract['inputs'] == ['result/sc00.md', 'result/sc01.md']


def test_invalid_or_forward_dependencies_are_removed_by_backend():
    current = compile_task(3, '資料作成', {
        'title': '資料', 'scope': '本文を作る', 'headings': ['目的', '内容'],
        'depends_on': ['SC01', 'SC04', 'unknown', 2, 'SC01'],
    }, [])
    assert current['depends_on'] == ['SC01']
    assert contract_of(current)['inputs'] == ['result/sc01.md']


def test_planning_criteria_places_google_site_publication_last():
    mission = {
        'goal': '## 達成条件\n1. 基本資料を作る',
        'success_criteria': '',
        'instruction_messages': [
            {'kind': 'mission_instruction_user', 'message': 'Google SitesでLPを公開する'},
            {'kind': 'mission_instruction_user', 'message': '運営会社情報をLPへ記載する'},
        ],
    }
    criteria = ProjectOrchestrator._planning_criteria(mission)
    assert criteria[-1] == 'Google SitesでLPを公開する'
    assert criteria[-2] == '運営会社情報をLPへ記載する'


def test_planning_criteria_places_public_web_research_first():
    mission = {
        'goal': '## 達成条件\n1. 補助金提案書を作る',
        'success_criteria': '',
        'instruction_messages': [
            {'kind': 'mission_instruction_user',
             'message': '情報検索スキルで最新の公募要領を取得する'},
        ],
    }

    criteria = ProjectOrchestrator._planning_criteria(mission)

    assert criteria[0] == '情報検索スキルで最新の公募要領を取得する'


def test_planning_criteria_consolidates_duplicate_grant_web_instructions():
    mission = {
        'goal': '## 達成条件\n1. 補助金提案書を作る',
        'success_criteria': '',
        'instruction_messages': [
            {'kind': 'mission_instruction_user',
             'message': 'Webを検索して現行のものづくり補助金公募要領を収集する'},
            {'kind': 'mission_instruction_user',
             'message': '情報検索スキルでものづくり補助金の最新情報を取得する'},
            {'kind': 'mission_instruction_user',
             'message': '外部の情報検索タスクを最優先にする'},
        ],
    }

    criteria = ProjectOrchestrator._planning_criteria(mission)
    web_criteria = [value for value in criteria if '情報検索' in value or 'Webを検索' in value]

    assert len(web_criteria) == 1
    assert '追加Web調査要件' in web_criteria[0]


def test_public_web_task_is_scheduled_before_and_required_by_other_work():
    proposal = {
        'title': '提案書', 'scope': '補助金提案の内容を整理する',
        'headings': ['目的', '提案内容'], 'depends_on': [],
    }
    web_proposal = {
        'title': '公式情報収集', 'scope': '公式一次資料を取得して検証する',
        'headings': ['取得資料', '検証結果'], 'depends_on': ['SC01'],
    }
    regular = compile_task(1, '補助金提案書を作る', proposal, [])
    web = compile_task(2, '情報検索スキルで最新の公募要領を取得する', web_proposal, [])

    plan = compile_plan(
        ['補助金提案書を作る', '情報検索スキルで最新の公募要領を取得する'],
        [regular, web],
    )
    keys = [item['task_key'] for item in plan['tasks']]
    regular = next(item for item in plan['tasks'] if item['task_key'] == 'SC01')
    web = next(item for item in plan['tasks'] if item['task_key'] == 'SC02')

    assert keys[:3] == ['SC00', 'SC02', 'SC01']
    assert web['depends_on'] == ['SC00']
    assert 'SC02' in regular['depends_on']
    assert 'result/sc02.md' in contract_of(regular)['inputs']
    assert validate_plan(plan, {'SC01', 'SC02'})['passed']


def test_dependency_requires_ancestry_not_just_earlier_producer():
    plan = compile_plan(['a', 'b'], [task(1), task(2, ['SC01'])])
    assert validate_plan(plan, {'SC01', 'SC02'})['passed']
    plan['tasks'][2]['depends_on'] = []
    assert not validate_plan(plan, {'SC01', 'SC02'})['passed']


def test_final_verification_and_coverage_are_mandatory():
    plan = compile_plan(['a', 'b'], [task(1), task(2)])
    assert len(plan['tasks']) == 4
    assert not validate_plan(plan, {'SC01', 'SC02', 'SC03'})['passed']
    plan['tasks'][-1]['depends_on'] = ['SC02']
    assert not validate_plan(plan, {'SC01', 'SC02'})['passed']


def test_output_checks_real_headings_and_minimum_characters(tmp_path):
    current = task(1)
    path = tmp_path / 'sc01.md'
    path.write_text('目的 実施内容 ' * 100, encoding='utf-8')
    assert any('見出し' in item for item in verify_outputs(current, lambda _: path))
    headings = contract_of(current)['outputs'][0]['required_headings']
    path.write_text('\n'.join('## ' + name + '\n' + '具体的な検討内容。' * 30 for name in headings), encoding='utf-8')
    assert verify_outputs(current, lambda _: path) == []


def test_large_plan_reserves_final_capacity_without_dropping_conditions():
    plan = compile_plan(['条件'] * 18, [task(i) for i in range(1, 19)])
    assert len(plan['tasks']) == 20
    with pytest.raises(ValueError):
        compile_plan(['条件'] * 19, [task(i) for i in range(1, 20)])


def test_tracker_contract_includes_csv_and_final_inventory():
    current = compile_task(1, '案件管理表を作成する', {'title': '管理表', 'scope': '商談記録を記入する表を用意', 'headings': ['目的', '記入方法']}, [])
    plan = compile_plan(['案件管理表を作成する'], [current])
    assert validate_plan(plan, {'SC01'})['passed']
    assert contract_of(current)['outputs'][1]['path'] == 'result/sc01_tracker.csv'
    assert 'result/sc01_tracker.csv' in contract_of(plan['tasks'][-1])['inputs']


def test_execution_plan_requires_approved_external_action_evidence():
    current = compile_task(1, '仮説検証のため顧客への提案を実行する', {
        'title': '提案実行', 'scope': '提案内容と実行条件を整理',
        'headings': ['対象', '実行条件'],
    }, [])
    final = compile_plan(['仮説検証のため顧客への提案を実行する'], [current])['tasks'][-1]
    requirement = contract_of(final)['action_requirements'][0]
    assert requirement['minimum_executed'] == 1
    assert requirement['evidence_required'] is True


def test_input_artifacts_are_not_treated_as_new_outputs():
    current = task(2, ['SC01'])
    text = current['description'] + '\n' + current['acceptance_criteria']
    assert ProjectOrchestrator._expected_artifact_paths(text) == {'result/sc02.md'}
    tracker = compile_task(1, '案件管理表', {'title': '管理表', 'scope': '記入用の表を作成', 'headings': ['目的', '記入方法']}, [])
    final = compile_plan(['案件管理表'], [tracker])['tasks'][-1]
    assert not ProjectOrchestrator._task_evidence_requirements(final, [])['table_operation']


@pytest.mark.asyncio
async def test_structured_final_verification_is_backend_generated(tmp_path):
    class NoLlm:
        async def stream(self, messages):
            raise AssertionError('final verification must not call the LLM')

    memory = ShortTermMemory(tmp_path / 'memory.db')
    project = memory.create_project('最終検証テスト')
    memory.save_mission(project['id'], '## 達成条件\n1. 販売資料を作成', '', '', False, [])
    plan = compile_plan(['販売資料を作成'], [task(1)])
    mission = memory.replace_plan(project['id'], plan['summary'], plan['tasks'])
    workspace = WorkspaceSandbox(tmp_path / 'workspace')
    for current in mission['tasks'][:-1]:
        contract = contract_of(current)
        for output in contract['outputs']:
            headings = output['required_headings']
            content = '\n'.join(
                f'## {heading}\n' + ('検証可能な具体的内容。' * 50)
                for heading in headings
            )
            workspace.apply_operations(
                project.get('workspace_path', ''), project['id'],
                [{'action': 'write_text', 'path': output['path'], 'content': content}],
            )
        memory.update_task(current['id'], 'completed', result='成果物作成済み')
    latest = memory.get_mission(project['id'])
    final = latest['tasks'][-1]
    manager = ProjectOrchestrator(
        memory, NoLlm(), lambda _: ('', []), None, lambda: [], workspace=workspace,
    )

    result = await manager._execute_task(project['id'], final['id'])

    _, _, output = workspace.resolve_file(
        project.get('workspace_path', ''), project['id'],
        'result/final_verification.md', must_exist=True,
    )
    content = output.read_text(encoding='utf-8')
    assert '## 達成条件別判定' in content
    assert '## 成果物検証' in content
    assert '## 未達条件と承認待ち' in content
    assert '顧客接触' in content and '実施済みとは扱いません' in content
    assert 'バックエンド検証' in result
    assert any(
        event['kind'] == 'structured_final_verification_generated'
        for event in memory.get_mission(project['id'])['events']
    )
    memory.update_task(final['id'], 'completed', result=result)
    final_report, goal_status, unmet = await manager._final_report(project['id'])
    assert goal_status == 'passed'
    assert unmet == []
    assert '最終検証・達成条件別の判定' in final_report
    assert any(
        event['kind'] == 'structured_goal_assessment'
        for event in memory.get_mission(project['id'])['events']
    )
    memory.save_mission(
        project['id'], 'コンサル契約を販売し受注につなげる',
        '仮説検証計画を実行する', '', False, [],
    )
    _, goal_status, unmet = await manager._final_report(project['id'])
    assert goal_status == 'partial'
    assert any('外部アクション' in item for item in unmet)
    action = memory.create_action(project['id'], 'manual', '見込み客A', '提案を実施')
    memory.update_action(project['id'], action['id'], 'approved')
    memory.update_action(
        project['id'], action['id'], 'executed', evidence='2026-09-07 面談記録ID: TEST-1',
    )
    _, goal_status, unmet = await manager._final_report(project['id'])
    assert goal_status == 'passed'
    assert unmet == []


def test_structured_task_rejects_uncontracted_outputs_before_execution(tmp_path):
    memory = ShortTermMemory(tmp_path / 'memory.db')
    project = memory.create_project('契約外操作テスト')
    memory.save_mission(project['id'], '## 達成条件\n1. 資料作成', '', '', False, [])
    plan = compile_plan(['資料作成'], [task(1)])
    mission = memory.replace_plan(project['id'], plan['summary'], plan['tasks'])
    workspace = WorkspaceSandbox(tmp_path / 'workspace')
    manager = ProjectOrchestrator(
        memory, object(), lambda _: ('', []), None, lambda: [], workspace=workspace,
    )
    current = mission['tasks'][1]
    response = json.dumps({
        'result': '作成',
        'operations': [
            {'action': 'write_text', 'path': 'result/sc01.md', 'content': '正しい成果物'},
            {'action': 'write_text', 'path': 'result/sales_plan.md', 'content': '契約外'},
        ],
        'table_operations': [{
            'action': 'create_table', 'columns': ['KPI'],
            'rows': [{'KPI': '架空'}], 'output_path': 'result/kpi_dashboard.csv',
        }],
        'source_references': [], 'capability_gaps': [],
    }, ensure_ascii=False)

    _, _, evidence = manager._apply_execution_response(
        project, project['id'], current['id'], response, set(),
    )

    assert [item['path'] for item in evidence['workspace_operations']] == ['result/sc01.md']
    assert evidence['table_operations'] == []
    assert not workspace.resolve_file(
        project.get('workspace_path', ''), project['id'], 'result/sales_plan.md'
    )[2].exists()
    assert any(
        event['kind'] == 'contract_operation_rejected'
        for event in memory.get_mission(project['id'])['events']
    )


@pytest.mark.asyncio
async def test_generation_retries_only_bad_criterion_and_persists_contracts(tmp_path):
    class Llm:
        def __init__(self):
            self.calls = 0
        async def stream(self, messages):
            self.calls += 1
            if self.calls == 2:
                yield 'invalid json'
            else:
                yield json.dumps({'title': '計画', 'scope': '具体的な内容', 'headings': ['目的', '成果'], 'depends_on': []}, ensure_ascii=False)
    memory = ShortTermMemory(tmp_path / 'memory.db')
    project = memory.create_project('計画テスト')
    memory.save_mission(project['id'], '## 達成条件\n1. 市場整理\n2. 商品設計', '', '', False, [])
    llm = Llm()
    manager = ProjectOrchestrator(memory, llm, lambda _: ('', []), None, lambda: [], workspace=WorkspaceSandbox(tmp_path / 'workspace'))
    mission = await manager.generate_plan(project['id'])
    assert llm.calls == 3
    assert len(mission['tasks']) == 4
    assert all(contract_of(item) for item in mission['tasks'])
    assert manager.approve(project['id'])['status'] == 'ready'


@pytest.mark.asyncio
async def test_failed_generation_keeps_existing_plan(tmp_path):
    class Llm:
        async def stream(self, messages):
            yield 'invalid json'
    memory = ShortTermMemory(tmp_path / 'memory.db')
    project = memory.create_project('計画保持')
    memory.save_mission(project['id'], '## 達成条件\n1. 市場整理', '', '', False, [])
    memory.replace_plan(project['id'], 'existing', [{'task_key': 'old', 'title': '保持'}])
    before = deepcopy(memory.get_mission(project['id']))
    manager = ProjectOrchestrator(memory, Llm(), lambda _: ('', []), None, lambda: [], workspace=WorkspaceSandbox(tmp_path / 'workspace'))
    with pytest.raises(ValueError):
        await manager.generate_plan(project['id'])
    after = memory.get_mission(project['id'])
    assert after['plan_version'] == before['plan_version']
    assert after['tasks'] == before['tasks']


@pytest.mark.asyncio
async def test_generation_excludes_approval_start_and_duplicate_generation(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    class Llm:
        async def stream(self, messages):
            entered.set()
            await release.wait()
            yield json.dumps({'title': '計画', 'scope': '内容を整理', 'headings': ['目的', '成果'], 'depends_on': []})
    memory = ShortTermMemory(tmp_path / 'memory.db')
    project = memory.create_project('同時操作')
    memory.save_mission(project['id'], '## 達成条件\n1. 市場整理', '', '', False, [])
    manager = ProjectOrchestrator(memory, Llm(), lambda _: ('', []), None, lambda: [], workspace=WorkspaceSandbox(tmp_path / 'workspace'))
    worker = asyncio.create_task(manager.generate_plan(project['id']))
    await entered.wait()
    try:
        with pytest.raises(ValueError, match='生成中'):
            await manager.generate_plan(project['id'])
        with pytest.raises(ValueError, match='生成中'):
            await manager.start(project['id'])
        with pytest.raises(ValueError, match='生成中'):
            manager.approve(project['id'])
    finally:
        release.set()
        await worker
    assert not manager.planning_projects
