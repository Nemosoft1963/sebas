"""Quote existence is not semantic support. Unresolved assertions require review."""
import re


def normalize(text):
    return re.sub(r'\s+', '', text)


def verify_claim(claim, units, *, action_events=(), allow_historical=False):
    kind = claim.get('kind')
    text = str(claim.get('text', '')).strip()
    result = {'citation_match': 'absent', 'support_state': 'needs_review',
              'version_state': 'unknown', 'action_evidence_state': 'not_applicable', 'reason': ''}
    if kind not in {'fact','interpretation','hypothesis','plan','actual_result'} or len(text) < 30:
        result['support_state'], result['reason'] = 'contradicted', 'invalid_claim'
        return result
    if kind in {'hypothesis','plan'}:
        if not str(claim.get('assumptions','')).strip() or not str(claim.get('verification_method','')).strip():
            result['reason'] = 'missing_assumptions_or_verification'
        elif re.search(r'実施しました|実行しました|取得しました|保存しました|申請済|承認済|採択されました|採択された。|受給した。|実績がある', text):
            result['support_state'], result['reason'] = 'contradicted', 'unobserved_result'
        else:
            # Hypothetical examples and future plans can have no original source.
            result.update(support_state='supported', version_state='not_applicable')
        return result
    if kind == 'actual_result':
        event_id = claim.get('event_id')
        matches = [e for e in action_events if e.get('id') == event_id and e.get('verified') is True]
        result['action_evidence_state'] = 'verified' if matches else 'missing'
        result['reason'] = 'action_content_needs_review' if matches else 'missing_action_evidence'
        return result
    unit = next((u for u in units if u['unit_id'] == claim.get('unit_id')), None)
    quote = str(claim.get('quote', '')).strip()
    if unit is None or not quote:
        result['reason'] = 'missing_source_or_quote'
        return result
    if quote in unit['content']:
        result['citation_match'] = 'exact'
    elif normalize(quote) in normalize(unit['content']):
        result['citation_match'] = 'normalized'
    else:
        result.update(citation_match='mismatch', support_state='contradicted', reason='quote_mismatch')
        return result
    # Only a direct quotation is automatically supported. Paraphrases/interpretations
    # stay pending rather than asking another model to pronounce them verified.
    if normalize(text) == normalize(quote) and kind == 'fact':
        result['support_state'] = 'supported'
    else:
        result['reason'] = 'semantic_support_needs_review'
    result['version_state'] = 'applicable' if allow_historical else 'unknown'
    if result['version_state'] == 'unknown':
        result['reason'] = 'version_applicability_needs_review'
    return result


def gates(verdicts, format_passed, execution_passed):
    content = ('failed' if any(v['support_state'] == 'contradicted' or v['citation_match'] == 'mismatch' for v in verdicts)
               else 'needs_review' if not verdicts or any(v['support_state'] != 'supported' or v['version_state'] == 'unknown' for v in verdicts)
               else 'passed')
    return {'format_gate': 'passed' if format_passed else 'failed', 'content_gate': content,
            'execution_gate': 'passed' if execution_passed else 'pending'}

def document_gates(verdicts, document, output, document_type):
    """Structural defects and contradicted claims must not become review-only."""
    headings = output.get('required_headings', [])
    errors = []
    for heading in headings:
        if f'## {heading}' not in document.splitlines():
            errors.append('必須見出し不足: ' + heading)
    if len(document.strip()) < output.get('minimum_characters', 600):
        errors.append('本文の最低文字数不足')
    if any('構成図' in h for h in headings):
        diagrams = re.findall(r'```mermaid\s*\n(.*?)```', document, re.S)
        if not any(re.search(r'(?m)^\s*(?:flowchart|graph)\s+(?:TD|TB|LR|RL|BT)\b', d) and '-->' in d for d in diagrams):
            errors.append('システム構成図が不足: mermaidのflowchartと接続を含めてください')
    state = gates(verdicts, not errors, False)
    state['format_errors'] = errors
    state['requirement_gate'] = 'needs_review'
    if state['format_gate'] == 'passed' and state['content_gate'] == 'passed':
        if document_type in {'case_analysis','web_evidence_report','system_structure_proposal'}:
            state['content_gate'] = 'needs_review'
        else:
            state['requirement_gate'] = 'passed'
    return state
