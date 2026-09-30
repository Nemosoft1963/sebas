import json
import pytest
from unittest.mock import Mock

from app.core import Ollama
from app.experience_store import fingerprint
from app.goal_review import (
    ReviewStore, plan_snapshot, review_plan, record_send_approval, all_reviews_history,
)
from app.plan_feedback import (
    import_feedback, import_legacy_review, archive_legacy_reviews, issues_for, propose, apply,
)
from test_goal_review import setup


@pytest.mark.asyncio
async def test_legacy_review_does_not_enter_current_issues_for(tmp_path):
    """1. legacy review (source_plan_version=None / mission.plan_reviews) は現行の issues_for に入らない"""
    manager, pid, task = setup(tmp_path, True)
    sig = plan_snapshot(manager, pid)[1]

    # mission.plan_reviews に旧レビューを設定
    manager.memory.set_plan_reviews(pid, [
        {'id': 'provider_alpha', 'ok': True, 'review': '旧版への指摘: 原本照合を強化してください。'},
        {'id': 'provider_beta', 'ok': False, 'error': '一時的なエラー'},
    ])

    # issues_for には legacy review は含まれない
    issues = issues_for(manager, pid, sig)
    assert issues == []

    # archive_legacy_reviews を呼んでも issues_for には入らない
    archived = archive_legacy_reviews(manager, pid)
    assert len(archived) == 1
    assert archived[0]['origin'] == 'legacy_api'
    assert archived[0]['source_plan_version'] is None
    assert issues_for(manager, pid, sig) == []


@pytest.mark.asyncio
async def test_only_exact_signature_matching_review_enters_issues_for(tmp_path):
    """2. 現行計画署名と完全一致する review だけが入る"""
    manager, pid, task = setup(tmp_path, True)
    sig1 = plan_snapshot(manager, pid)[1]
    store = ReviewStore(manager.memory.path)

    # 別の署名 sig_other のレビューを保存
    sig_other = 'other_plan_signature_12345'
    store.put(pid, 'plan', sig_other, {
        'status': 'fail',
        'reviews': [{
            'provider': 'provider_alpha',
            'status': 'fail',
            'issues': ['旧計画版に対する指摘です。'],
        }],
    })

    # sig1 に対する issues_for には入らない
    assert issues_for(manager, pid, sig1) == []
    # sig_other に対する issues_for には入る
    assert len(issues_for(manager, pid, sig_other)) == 1

    # sig1 に対するレビューを保存
    store.put(pid, 'plan', sig1, {
        'status': 'fail',
        'reviews': [{
            'provider': 'provider_alpha',
            'status': 'fail',
            'issues': ['現行計画版に対する指摘です。'],
        }],
    })
    issues_sig1 = issues_for(manager, pid, sig1)
    assert len(issues_sig1) == 1
    assert issues_sig1[0]['text'] == '現行計画版に対する指摘です。'


@pytest.mark.asyncio
async def test_deduplication_of_identical_issues(tmp_path):
    """3. 同一provider・同一正規化本文・同一対象criterionの指摘は重複排除される"""
    manager, pid, task = setup(tmp_path, True)
    sig = plan_snapshot(manager, pid)[1]
    store = ReviewStore(manager.memory.path)

    # 重複する指摘を持つレビュー
    store.put(pid, 'plan', sig, {
        'status': 'fail',
        'reviews': [
            {
                'provider': 'provider_alpha',
                'status': 'fail',
                'issues': [
                    '原本データの参照手順が不足しています。',
                    '  原本データの参照手順が不足しています。  ',  # 空白違い
                    {'text': '原本データの参照手順が不足しています。', 'criterion': 'SC01'},
                ],
            },
            {
                'provider': 'provider_alpha',  # 同一 provider
                'status': 'fail',
                'issues': [
                    '原本データの参照手順が不足しています。',  # 完全一致
                ],
            },
            {
                'provider': 'provider_beta',  # 異なる provider
                'status': 'fail',
                'issues': [
                    '原本データの参照手順が不足しています。',
                ],
            },
        ],
    })

    issues = issues_for(manager, pid, sig)
    # provider_alpha (criterion無) -> 1件
    # provider_alpha (criterion=SC01) -> 1件
    # provider_beta (criterion無) -> 1件
    # 合計3件に重複排除されていること
    assert len(issues) == 3
    alpha_issues = [i for i in issues if i['provider'] == 'provider_alpha']
    assert len(alpha_issues) == 2
    beta_issues = [i for i in issues if i['provider'] == 'provider_beta']
    assert len(beta_issues) == 1


@pytest.mark.asyncio
async def test_manual_legacy_import_becomes_user_import(tmp_path):
    """4. 人間が明示的に「現行版へ取り込む」操作をした場合だけ、legacy reviewが user_import として反映される"""
    manager, pid, task = setup(tmp_path, True)
    sig = plan_snapshot(manager, pid)[1]

    manager.memory.set_plan_reviews(pid, [
        {'id': 'provider_gamma', 'ok': True, 'review': '旧版指摘: 成果物の判定基準を明記してください。'},
    ])

    # 自動的には入らない
    assert issues_for(manager, pid, sig) == []

    # 人間が明示的に取り込む
    result = import_legacy_review(manager, pid, sig, 'provider_gamma')
    assert result['status'] == 'feedback_only'
    assert result['lifecycle'] == 'imported'

    issues = issues_for(manager, pid, sig)
    assert len(issues) == 1
    assert issues[0]['origin'] == 'user_import'
    assert issues[0]['provider'] == 'provider_gamma'
    assert '成果物の判定基準' in issues[0]['text']


@pytest.mark.asyncio
async def test_all_reviews_history_preserves_audit_trail(tmp_path):
    """5. 履歴表示API・関数では legacy review を含め全件が引き続き閲覧取得できる"""
    manager, pid, task = setup(tmp_path, True)
    sig = plan_snapshot(manager, pid)[1]
    store = ReviewStore(manager.memory.path)

    # 1. mission の legacy review
    manager.memory.set_plan_reviews(pid, [
        {'id': 'legacy_1', 'ok': True, 'review': '旧レビュー1の本文'},
    ])

    # 2. ReviewStore の plan review
    store.put(pid, 'plan', sig, {
        'status': 'passed',
        'reviews': [{'provider': 'api_1', 'status': 'pass', 'issues': []}],
    })

    # 3. user feedback
    import_feedback(manager, pid, sig, 'human_tester', '人間による手動指摘内容です。1234567890')

    # 履歴専用取得
    history = all_reviews_history(manager, pid)
    assert len(history['mission_legacy_reviews']) == 1
    assert history['mission_legacy_reviews'][0]['id'] == 'legacy_1'
    assert len(history['plan_reviews']) == 1
    assert len(history['user_feedbacks']) == 1

    # 修正入力 issues_for と履歴閲覧経路が分離されていることの検証
    issues = issues_for(manager, pid, sig)
    # issues には legacy_1 は自動混入しない（human_tester の手動指摘のみ入る）
    assert len(issues) == 1
    assert issues[0]['provider'] == 'human_tester'


@pytest.mark.asyncio
async def test_replan_ver22_to_ver23_does_not_revive_legacy_review(tmp_path):
    """6. Ver.22相当の状態からVer.23相当の計画を生成しても、legacy reviewが自動的に現行指摘として復活しない"""
    manager, pid, task = setup(tmp_path, True)
    manager.llm = Mock(spec=Ollama)

    # Ver.22 相当の状態: mission に plan_reviews (旧レビュー) が残っている
    manager.memory.set_plan_reviews(pid, [
        {'id': 'claude_legacy', 'ok': True, 'review': 'Ver.22以前の旧指摘です。'},
    ])

    mission_v22 = manager.memory.get_mission(pid)
    v22_sig = plan_snapshot(manager, pid)[1]
    assert issues_for(manager, pid, v22_sig) == []

    # Ver.23 への再計画 (generate_plan)
    async def mock_generate_impl(_):
        m = manager.memory.get_mission(pid)
        manager.memory.replace_plan(pid, 'Ver.23 計画案サマリー', m['tasks'], expected_version=m['plan_version'])
    manager._generate_plan_impl = mock_generate_impl

    result = await manager.generate_plan(pid)
    v23_sig = plan_snapshot(manager, pid)[1]

    # Ver.23 でも legacy review が自動復活していないことを検証
    assert issues_for(manager, pid, v23_sig) == []
    # mission.plan_reviews の監査履歴自体は失われていないこと
    mission_v23 = manager.memory.get_mission(pid)
    assert len(mission_v23['plan_reviews']) == 1
    assert mission_v23['plan_reviews'][0]['id'] == 'claude_legacy'
