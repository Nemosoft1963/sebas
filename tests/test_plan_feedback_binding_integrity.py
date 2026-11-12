from app.plan_feedback import _same_issues_except_binding


def test_bound_issue_remains_same_when_only_binding_added():
    saved = [{'id': 'i1', 'text': '根拠不足', 'provider': 'claude'},
             {'id': 'i2', 'text': '検査不足', 'provider': 'claude'}]
    bound = [dict(saved[0], binding={'state': 'resolved', 'candidate_task_keys': ['SC01']}),
             dict(saved[1], binding={'state': 'multiple_targets', 'candidate_task_keys': ['SC01', 'SC02']})]
    assert _same_issues_except_binding(bound, saved)


def test_bound_issue_rejects_review_changes():
    saved = [{'id': 'i1', 'text': '根拠不足', 'provider': 'claude'},
             {'id': 'i2', 'text': '検査不足', 'provider': 'claude'}]
    changed = [dict(saved[0], binding={'state': 'resolved'}),
               dict(saved[1], text='別の指摘', binding={'state': 'resolved'})]
    assert not _same_issues_except_binding(changed, saved)
    assert not _same_issues_except_binding(list(reversed(saved)), saved)
