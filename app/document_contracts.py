"""Explicit versioned document contracts. Unknown new contracts fail closed."""
TYPES = {'web_evidence_report', 'investigation_plan', 'case_analysis', 'system_structure_proposal'}


def validate_extension(extension):
    if not isinstance(extension, dict) or extension.get('schema') != 'local-cowork-document/v1':
        raise ValueError('unsupported_contract: schema')
    if extension.get('document_type') not in TYPES or extension.get('version') != 1:
        raise ValueError('unsupported_contract: type or version')
    capabilities = extension.get('required_capabilities', [])
    if not isinstance(capabilities, list) or any(not isinstance(x, str) for x in capabilities):
        raise ValueError('unsupported_contract: required capabilities')
    # No model-authored permissions, arbitrary schemas or relaxed validation policies.
    if set(extension) - {'schema', 'document_type', 'version', 'required_capabilities', 'as_of', 'allow_historical', 'claim_format', 'execution_strategy'}:
        raise ValueError('unsupported_contract: unknown properties')
    if 'execution_strategy' in extension and (extension['execution_strategy'] != 'two-stage-v1' or extension.get('claim_format') != 'atomic-v1'):
        raise ValueError('unsupported_contract: execution strategy')
    if 'claim_format' in extension and extension['claim_format'] != 'atomic-v1':
        raise ValueError('unsupported_contract: claim format')
    if 'allow_historical' in extension and type(extension['allow_historical']) is not bool:
        raise ValueError('unsupported_contract: historical policy')
    if 'as_of' in extension:
        from datetime import date
        try:
            date.fromisoformat(extension['as_of'])
        except (ValueError, TypeError):
            raise ValueError('unsupported_contract: as_of date')
    return extension


def generation_schema(headings):
    section = {'type': 'object', 'additionalProperties': False,
               'properties': {'kind': {'type': 'string', 'enum': ['fact','interpretation','hypothesis','plan','actual_result']},
                              'text': {'type': 'string', 'minLength': 50, 'maxLength': 600},
                              'unit_id': {'type': 'string'}, 'quote': {'type': 'string', 'maxLength': 600},
                              'assumptions': {'type': 'string'}, 'verification_method': {'type': 'string'}},
               'required': ['kind','text','unit_id','quote','assumptions','verification_method']}
    import copy
    properties = {f'section_{i}': copy.deepcopy(section) for i in range(1, len(headings)+1)}
    for i, heading in enumerate(headings, 1):
        if '構成図' in heading:
            target = properties[f'section_{i}']
            target['properties'].update({
                'diagram_nodes': {'type':'array','minItems':2,'maxItems':8,'items':{'type':'string','minLength':1,'maxLength':80}},
                'diagram_edges': {'type':'array','minItems':1,'maxItems':12,'items':{'type':'object','additionalProperties':False,'properties':{'from':{'type':'integer','minimum':0,'maximum':7},'to':{'type':'integer','minimum':0,'maximum':7}},'required':['from','to']}}})
            target['required'] += ['diagram_nodes','diagram_edges']
    return {'type': 'object', 'additionalProperties': False,
            'properties': properties,
            'required': [f'section_{i}' for i in range(1, len(headings)+1)]}

def render_diagram(claim):
    """Generate code-owned IDs and bounded labels; models cannot emit directives."""
    import re
    nodes, edges = claim.get('diagram_nodes'), claim.get('diagram_edges')
    if not isinstance(nodes,list) or not 2 <= len(nodes) <= 8 or not isinstance(edges,list) or not 1 <= len(edges) <= 12:
        raise ValueError('構成図の部品・接続が不足または上限超過です')
    lines = ['```mermaid','flowchart LR']
    for index,label in enumerate(nodes):
        if not isinstance(label,str) or not 1 <= len(label.strip()) <= 80:
            raise ValueError('構成図のラベルが不正です')
        label = re.sub(r'[^\wぁ-んァ-ヶ一-龥ー ・、。/()：:.-]', ' ', label).strip()
        if not label:
            raise ValueError('構成図のラベルが空欄です')
        lines.append(f'  N{index}["{label}"]')
    for edge in edges:
        if not isinstance(edge,dict) or set(edge) != {'from','to'} or any(type(edge[k]) is not int or not 0 <= edge[k] < len(nodes) for k in ('from','to')):
            raise ValueError('構成図の接続先が不正です')
        lines.append(f"  N{edge['from']} --> N{edge['to']}")
    return '\n'.join(lines + ['```'])
