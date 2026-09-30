"""Tests for P0-3: Generic Plan Rebuilding (rebuild_generic)."""
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from app.core import Ollama
from app.goal_contract import preview as get_goal_contract, put_draft as put_goal_contract
from app.goal_review import ReviewStore, plan_snapshot
from app.plan_feedback import (
    DISPOSITIONS, apply, import_feedback, issues_for, propose, validate_candidate,
)
from app.structured_planning import (
    SCHEMA, build_rebuild_generic_plan, compile_plan, compile_task, contract_of,
    validate_rebuild_generic_candidate,
)
from test_goal_review import setup
from test_structured_planning import task as make_structured_task


def test_dispositions_contains_rebuild_generic():
    """1. disposition 種別に rebuild_generic が含まれていること。"""
    assert 'rebuild_generic' in DISPOSITIONS


@pytest.mark.asyncio
async def test_rebuild_generic_proposal_generation(tmp_path):
    """2. 汎用案件（架空プロジェクト）で rebuild_generic 候補が生成できること。"""
    manager, pid, task = setup(tmp_path, True)
    manager.llm = Mock(spec=Ollama)
    sig = plan_snapshot(manager, pid)[1]

    import_feedback(
        manager, pid, sig, 'advisor_ai',
        '成果物の構成と依存関係を最新の業務要件に合わせて全体再構成してください。',
        criterion='SC01',
    )
    issues = issues_for(manager, pid, sig)
    assert len(issues) == 1

    async def local_complete(system, prompt, schema=None):
        return json.dumps({
            'actions': {
                issues[0]['id']: {
                    'disposition': 'rebuild_generic',
                    'target': 'SC01',
                    'change': '達成条件SC01の成果物見出しおよび手順を最新の業務要件に再構成する。',
                    'reason': 'タスクの依存関係と成果物契約を安全に再構成するため。',
                }
            }
        })

    manager._local_complete = local_complete
    row = await propose(manager, pid, sig)
    assert row['status'] == 'draft'
    assert row['candidate_id']
    assert row['execution_plan'] is not None
    assert row['blockers'] == []
    assert len(row['changes']) >= 1
    assert row['changes'][0]['target'] == 'execution_pipeline'

    # propose の段階では自動保存されていないこと（plan_version は変更されない）
    mission = manager.memory.get_mission(pid)
    assert mission['plan_version'] == 1


@pytest.mark.asyncio
async def test_human_explicit_apply_triggers_saving_new_plan_version(tmp_path):
    """3. 保存は人間の明示操作(apply)をトリガーとすることを検証（自動保存されないこと）。"""
    manager, pid, task = setup(tmp_path, True)
    manager.llm = Mock(spec=Ollama)
    sig = plan_snapshot(manager, pid)[1]

    import_feedback(
        manager, pid, sig, 'advisor_ai',
        '成果物の構成と依存関係を最新の業務要件に合わせて全体再構成してください。',
        criterion='SC01',
    )
    issues = issues_for(manager, pid, sig)

    async def local_complete(system, prompt, schema=None):
        return json.dumps({
            'actions': {
                issues[0]['id']: {
                    'disposition': 'rebuild_generic',
                    'target': 'SC01',
                    'change': '達成条件SC01の成果物見出しおよび手順を最新の業務要件に再構成する。',
                    'reason': 'タスクの依存関係と成果物契約を安全に再構成するため。',
                }
            }
        })

    manager._local_complete = local_complete
    row = await propose(manager, pid, sig)
    mission_before = manager.memory.get_mission(pid)
    assert mission_before['plan_version'] == 1

    # 人間が apply を明示的に呼ぶ
    applied = apply(manager, pid, sig, row['candidate_id'])
    assert applied['status'] == 'awaiting_review'
    assert applied['lifecycle'] == 'revalidation_pending'

    mission_after = manager.memory.get_mission(pid)
    assert mission_after['plan_version'] == 2  # 新しい plan_version として保存された
    assert '汎用計画の再構成' in mission_after['plan_summary']


@pytest.mark.asyncio
async def test_vehicle_case_uses_rebuild_vehicle_and_rejects_rebuild_generic(tmp_path):
    """4. 車両案件では従来どおり rebuild_vehicle が使われ、rebuild_generic は拒否される。"""
    # 車両案件（vehicle_calculate を含む計画）をセットアップ
    from app.vehicle_workflow import make_plan
    manager, pid, _ = setup(tmp_path, True)
    mission = manager.memory.get_mission(pid)
    v_plan = make_plan(mission, ['SC01'], [])
    # 意図的に vehicle_extract がない旧車両計画にする
    tasks_without_extract = [t for t in v_plan['tasks'] if (contract_of(t) or {}).get('execution_kind') != 'vehicle_extract']
    manager.memory.replace_plan(pid, '旧車両計画', tasks_without_extract)

    sig = plan_snapshot(manager, pid)[1]
    import_feedback(manager, pid, sig, 'vehicle_reviewer', '自動抽出工程vehicle_extractが欠けています。')
    issues = issues_for(manager, pid, sig)
    snapshot = plan_snapshot(manager, pid)[0]

    # 車両案件に対して rebuild_generic を適用しようとすると拒否される
    generic_body = {
        'actions': [
            {
                'issue_id': issues[0]['id'],
                'disposition': 'rebuild_generic',
                'target': 'SC01',
                'change': '汎用再構成を試みる',
                'reason': 'テスト目的での汎用再構成指定',
            }
        ]
    }
    with pytest.raises(ValueError, match='車両案件はrebuild_vehicleで再構成してください'):
        validate_candidate(generic_body, issues, snapshot, detail=None)

    # 車両案件に対して rebuild_vehicle を指定すると成功する
    vehicle_body = {
        'actions': [
            {
                'issue_id': issues[0]['id'],
                'disposition': 'rebuild_vehicle',
                'target': 'SC01',
                'change': '自動抽出工程を追加する',
                'reason': '自動抽出工程の欠落に対応するため。',
            }
        ]
    }
    validated = validate_candidate(vehicle_body, issues, snapshot, detail=None)
    assert validated[0]['disposition'] == 'rebuild_vehicle'


@pytest.mark.asyncio
async def test_non_vehicle_case_rejects_rebuild_vehicle(tmp_path):
    """5. 車両以外の汎用案件で rebuild_vehicle を生成・適用しようとすると拒否される。"""
    manager, pid, task = setup(tmp_path, True)
    sig = plan_snapshot(manager, pid)[1]
    import_feedback(manager, pid, sig, 'reviewer', 'タスクの構成と成果物契約を最新の業務要件に合わせて変更してください。')
    issues = issues_for(manager, pid, sig)
    snapshot = plan_snapshot(manager, pid)[0]

    # 汎用案件に対して rebuild_vehicle を指定すると拒否される
    vehicle_body = {
        'actions': [
            {
                'issue_id': issues[0]['id'],
                'disposition': 'rebuild_vehicle',
                'target': 'SC01',
                'change': '車両パイプラインへ再構成',
                'reason': '不適切な車両再構成指定',
            }
        ]
    }
    with pytest.raises(ValueError, match='自動抽出工程のない車両全体計画だけを実装済みパイプラインへ再構成できます'):
        validate_candidate(vehicle_body, issues, snapshot, detail=None)


def test_six_validation_checks_each_rejects_when_broken(tmp_path):
    """
    6. 保存前6項目バリデーションのネガティブテスト:
       ① 目標要求保持率100%
       ② 計画被覆 (coverage) PASS
       ③ task key / 出力パス 重複なし
       ④ 依存関係に循環なし
       ⑤ 成果物契約が検証可能
       ⑥ 外部アクションに承認点あり
       のいずれか1つでも欠けると保存が拒否されること。
    """
    # 正常な汎用計画をベースとして作成
    criteria = ['成果物の作成と検証', '営業活動の設計']
    tasks = [make_structured_task(1), make_structured_task(2)]
    plan = compile_plan(criteria, tasks, goal='汎用プロジェクト')

    dummy_mission = {
        'goal': '汎用プロジェクト',
        'success_criteria': '1. 成果物の作成と検証\n2. 営業活動の設計',
        'plan_version': 1,
        'tasks': plan['tasks'],
    }
    goal_contract = {
        'schema': 'sebas-goal-contract/v1',
        'content_hash': 'dummy_hash',
        'criteria': [
            {
                'criterion_id': 'SC01',
                'statement': '成果物の作成と検証',
                'exec_task_keys': ['SC01'],
                'verify_task_keys': ['final_verification'],
            },
            {
                'criterion_id': 'SC02',
                'statement': '営業活動の設計',
                'exec_task_keys': ['SC02'],
                'verify_task_keys': ['final_verification'],
            },
        ],
    }

    # 正常な計画は合格する
    valid_res = validate_rebuild_generic_candidate(plan, goal_contract, dummy_mission)
    assert valid_res['passed'] is True

    # 検査1: 目標要求保持率欠落 (SC02 がタスク契約から漏れている)
    broken1 = deepcopy(plan)
    sc02_task = next(t for t in broken1['tasks'] if t['task_key'] == 'SC02')
    sc02_contract = contract_of(sc02_task)
    sc02_contract['criterion_ids'] = []  # SC02 を落とす
    sc02_task['acceptance_criteria'] = json.dumps(sc02_contract, ensure_ascii=False)
    with pytest.raises(ValueError, match='RETENTION_INCOMPLETE'):
        validate_rebuild_generic_candidate(broken1, goal_contract, dummy_mission)

    # 検査2: 計画被覆 (coverage) FAIL (exec と verify が同じタスクになっている)
    broken2 = deepcopy(plan)
    broken_gc = deepcopy(goal_contract)
    broken_gc['criteria'][0]['verify_task_keys'] = ['SC01']  # exec と同一
    with pytest.raises(ValueError, match='COVERAGE_INCOMPLETE'):
        validate_rebuild_generic_candidate(broken2, broken_gc, dummy_mission)

    # 検査3-a: task key 重複
    broken3a = deepcopy(plan)
    broken3a['tasks'][1]['task_key'] = broken3a['tasks'][0]['task_key']
    with pytest.raises(ValueError, match='DUPLICATE_KEY'):
        validate_rebuild_generic_candidate(broken3a, goal_contract, dummy_mission)

    # 検査3-b: 出力パス重複
    broken3b = deepcopy(plan)
    c0 = contract_of(broken3b['tasks'][0])
    c1 = contract_of(broken3b['tasks'][1])
    c1['outputs'][0]['path'] = c0['outputs'][0]['path']  # 同じ出力パス
    broken3b['tasks'][1]['acceptance_criteria'] = json.dumps(c1, ensure_ascii=False)
    with pytest.raises(ValueError, match='DUPLICATE_OUTPUT_PATH'):
        validate_rebuild_generic_candidate(broken3b, goal_contract, dummy_mission)

    # 検査4: 依存関係循環 (SC01 と SC02 が相互依存)
    broken4 = deepcopy(plan)
    t_sc01 = next(t for t in broken4['tasks'] if t['task_key'] == 'SC01')
    t_sc02 = next(t for t in broken4['tasks'] if t['task_key'] == 'SC02')
    t_sc01['depends_on'] = ['SC02']
    t_sc02['depends_on'] = ['SC01']
    with pytest.raises(ValueError, match='CIRCULAR_DEPENDENCY'):
        validate_rebuild_generic_candidate(broken4, goal_contract, dummy_mission)

    # 検査5: 成果物契約検証不能 (必須見出しが不足)
    broken5 = deepcopy(plan)
    t_target = broken5['tasks'][1]
    c_target = contract_of(t_target)
    c_target['outputs'][0]['required_headings'] = ['見出し1件のみ']  # 2件未満
    t_target['acceptance_criteria'] = json.dumps(c_target, ensure_ascii=False)
    with pytest.raises(ValueError, match='INVALID_ARTIFACT_CONTRACT'):
        validate_rebuild_generic_candidate(broken5, goal_contract, dummy_mission)

    # 検査6: 外部アクションに承認点なし
    broken6 = deepcopy(plan)
    t_ext = broken6['tasks'][1]
    c_ext = contract_of(t_ext)
    c_ext['criterion'] = '実際の営業活動として顧客へ送信する'
    c_ext['external_actions'] = 'none'  # approval_required ではない
    c_ext.pop('action_requirements', None)
    t_ext['acceptance_criteria'] = json.dumps(c_ext, ensure_ascii=False)
    with pytest.raises(ValueError, match='MISSING_APPROVAL_GATE'):
        validate_rebuild_generic_candidate(broken6, goal_contract, dummy_mission)


@pytest.mark.asyncio
async def test_apply_cannot_bypass_candidate_validation(tmp_path):
    """7. LLMが生成した計画をそのまま無検証で保存するバイパスコードパスが存在しないこと。"""
    manager, pid, task = setup(tmp_path, True)
    sig = plan_snapshot(manager, pid)[1]
    store = ReviewStore(manager.memory.path)

    # 壊れた execution_plan を持つ revision ドラフトを偽装注入
    broken_plan = {
        'tasks': [
            {
                'task_key': 'fake_task',
                'title': '壊れたタスク',
                'depends_on': [],
                'acceptance_criteria': json.dumps({'schema': 'wrong-schema'}),
            }
        ]
    }
    candidate_id = 'fake_candidate_1234'
    store.put(pid, 'revision', sig, {
        'status': 'draft',
        'candidate_id': candidate_id,
        'task_id': None,
        'issues': [],
        'blockers': [],
        'changes': [{'target': 'fake', 'before': '', 'after': ''}],
        'actions': [{'disposition': 'rebuild_generic', 'issue_id': 'iss1', 'target': 'SC01', 'change': 'c', 'reason': 'r'}],
        'execution_plan': broken_plan,
    })

    # apply を呼んでもバリデーションで弾かれ、保存されないこと
    with pytest.raises(ValueError):
        apply(manager, pid, sig, candidate_id)

    mission = manager.memory.get_mission(pid)
    assert mission['plan_version'] == 1  # 保存されていない


@pytest.mark.asyncio
async def test_truly_unimplemented_capabilities_remain_development_required(tmp_path):
    """8. 未実装な機能が必要な指摘に対しては development として明示的に残り停止すること。"""
    manager, pid, task = setup(tmp_path, True)
    manager.llm = Mock(spec=Ollama)
    sig = plan_snapshot(manager, pid)[1]

    import_feedback(
        manager, pid, sig, 'system_architect',
        '未実装の専用ブロックチェーン外部ノード通信機能が必要です。',
        criterion='SC01',
    )
    issues = issues_for(manager, pid, sig)

    async def local_complete(system, prompt, schema=None):
        return json.dumps({
            'actions': {
                issues[0]['id']: {
                    'disposition': 'development',
                    'target': 'SC01',
                    'change': '専用ノード通信モジュールの実装と受入テスト',
                    'reason': '未実装の外部通信能力が必要なため、文章の書き換えではなく開発課題として扱う。',
                }
            }
        })

    manager._local_complete = local_complete
    row = await propose(manager, pid, sig)
    assert row['status'] == 'draft'
    assert len(row['blockers']) == 1
    assert row['blockers'][0]['disposition'] == 'development'
    assert row['blockers'][0]['lifecycle'] == 'development_pending'

    # blockers があるため apply は拒否される
    with pytest.raises(ValueError, match='未解決または追加開発が必要な指摘があります'):
        apply(manager, pid, sig, row['candidate_id'])
