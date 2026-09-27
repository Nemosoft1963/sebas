"""Prefer structured observations; model assertions remain hypotheses."""

def classify(observations, model_report=''):
    records = []
    mapping = {
        'request_timeout': 'connection_error', 'source_missing': 'input_missing', 'extraction_failed': 'extraction_failed',
        'extraction_partial': 'extraction_failed', 'unit_omitted': 'not_presented',
        'policy_denied': 'policy_denied', 'approval_required': 'approval_required',
        'not_configured': 'configuration_missing', 'generation_rejected': 'generation_defect',
        'length': 'generation_defect', 'unsupported_contract': 'configuration_missing',
    }
    for observation in observations:
        code = observation.get('reason_code', '')
        http_status = observation.get('http_status')
        category = mapping.get(code)
        if http_status == 401:
            category = 'authentication_error'
        elif http_status == 403:
            category = 'policy_denied'
        elif http_status in {408, 429, 500, 502, 503, 504}:
            category = 'connection_error'
        if category:
            records.append({'category': category, 'reason_code': code or f'http_{http_status}',
                            'confidence': 'confirmed', 'stage': observation.get('stage', 'execution'),
                            'evidence_refs': observation.get('evidence_refs', []), 'observation': observation})
    if model_report and any(w in model_report for w in ('できない', '不足', 'ない', 'unsupported')):
        available = [x for x in observations if x.get('state') == 'available']
        records.append({'category': 'capability_misjudged' if available else 'unknown',
                        'reason_code': 'model_report_requires_diagnosis', 'confidence': 'hypothesis',
                        'stage': 'model_report', 'evidence_refs': [],
                        'note': '設定の利用可否だけでは当該原本の処理成功を証明しない'})
    return records or [{'category': 'unknown', 'reason_code': 'insufficient_observations',
                        'confidence': 'unknown', 'stage': 'execution', 'evidence_refs': []}]
