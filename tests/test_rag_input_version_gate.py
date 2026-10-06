from app.plan_case_reference import _evaluate_row


def test_old_v1_case_does_not_cross_to_new_input_fingerprint():
    row={'id':'case-1','status':'verified','expires':4102444800,'kind':'success','content':'verified lesson'}
    verdict, decision, reason=_evaluate_row(
        row, {}, {'input_version':'v1'}, 'new-input-fingerprint', {}, {'case-1'},
        {'status':'indexed','index_identity':'current'}, 'current')
    assert verdict=='input_version_mismatch'
    assert decision=='rejected'
    assert '旧入力版' in reason
