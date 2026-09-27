"""Project-wide opt-in, complete criteria and versioned output namespaces."""
import copy
import json
import re
from pathlib import Path
from app.structured_planning import contract_of,extract_criteria,requires_public_web_research


def enabled(memory_path,pid):
    path=Path(memory_path).parent/'capability_upgrade.json'
    if not path.exists():return False
    return json.loads(path.read_text(encoding='utf-8-sig')).get('planning_projects',{}).get(pid)=='two-stage-v1'


def complete_criteria(mission,legacy):
    success=mission.get('success_criteria','').strip()
    numbered=re.search(r'(?m)^\s*\d+[.）)]\s*.+',success) or re.search(r'(?m)^#{1,4}\s*達成条件\s*$',mission.get('goal',''))
    if numbered:return legacy
    outcomes=[re.sub(r'^[-・●\s]+','',line).strip() for line in success.splitlines() if line.strip()]
    outcomes.sort(key=lambda c:0 if '経営計画' in c else 1)
    goal=mission.get('goal','').strip()
    main=('全体目標に対するシステム・事業の実現計画を作成する。対象: '+goal+
          '\n各システムの用途・構成・実装工程・販売準備・検証方法を整理し、事例と構成図を含める。未実施の構築・販売を完了と数えない。') if goal else ''
    web=[c for c in legacy if requires_public_web_research(c)]
    # Procedural instructions remain constraints, not substitute deliverables.
    result=list(dict.fromkeys(web+([main] if main else [])+outcomes))
    if not result or len(result)>18:raise ValueError('全体計画の達成条件は1〜18件で指定してください')
    return result


def extension_for(task):
    contract=contract_of(task)
    if not contract:raise ValueError('全工程適用には構造化契約が必要です')
    outputs=contract.get('outputs',[])
    if len(outputs)!=1 or not str(outputs[0].get('path','')).endswith('.md'):
        raise ValueError('未対応の複数成果物・非Markdown契約です。旧経路へ戻さず計画作成を停止します')
    final=bool(contract.get('final_verification'))
    web=bool(contract.get('public_web_research',{}).get('required'))
    if contract.get('action_requirements') and not final:raise ValueError('外部実行工程は2段階計画の対象外です')
    return {'schema':'local-cowork-document/v1','version':1,'claim_format':'atomic-v1','execution_strategy':'two-stage-v1',
            'document_type':'web_evidence_report' if web else 'system_structure_proposal',
            'required_capabilities':(['web.collect_public'] if web else [] if final else ['source.read_excerpt'])+['workspace.write_text']}


def namespace_tasks(tasks,version,prioritize_web=False):
    result=copy.deepcopy(tasks);mapping={}
    for task in result:
        for output in (contract_of(task) or {}).get('outputs',[]):
            old=output['path']
            if not old.startswith('result/'):raise ValueError('成果物の保存先がresult配下ではありません')
            mapping[old]=f'result/plan_{version}/'+old.rsplit('/',1)[-1]
    for task in result:
        c=contract_of(task)
        if not c:raise ValueError('構造化契約がありません')
        for out in c['outputs']:out['path']=mapping[out['path']]
        c['inputs']=[mapping.get(value,value) for value in c.get('inputs',[])]
        text=task.get('description','')
        # Replace longest names first, once per original occurrence.
        if mapping:text=re.sub('|'.join(re.escape(k) for k in sorted(mapping,key=len,reverse=True)),lambda m:mapping[m.group()],text)
        task['description']=text;task['acceptance_criteria']=json.dumps(c,ensure_ascii=False)
        extension_for(task)
    if prioritize_web:
        web=[t for t in result if (contract_of(t) or {}).get('public_web_research',{}).get('required')]
        prep=next((t for t in result if t['task_key']=='SC00'),None)
        if web and prep:
            paths={t['task_key']:[o['path'] for o in contract_of(t)['outputs']] for t in result}
            def deps(task,keys):
                task['depends_on']=list(dict.fromkeys(keys));c=contract_of(task)
                c['inputs']=[p for key in task['depends_on'] for p in paths[key]]
                task['acceptance_criteria']=json.dumps(c,ensure_ascii=False)
            for i,t in enumerate(web):deps(t,[w['task_key'] for w in web[:i]])
            deps(prep,[t['task_key'] for t in web])
            others=[t for t in result if t not in web and t is not prep]
            for t in others:
                if not contract_of(t).get('final_verification'):deps(t,['SC00',*t['depends_on']])
            result=[*web,prep,*others]
    return result


def outcome_proposal(criterion,mission):
    """Anchor high-level deliverables; models elaborate them only at execution time."""
    if requires_public_web_research(criterion):
        return {'title':'公式公開資料の収集・版と根拠の確認','scope':criterion,
                'headings':['取得対象と公式資料一覧','公募回・版・公開更新日','対象者・対象経費・補助率・上限','期限・添付・申請経路','取得証拠と改訂情報'],'depends_on':[]}
    if criterion.startswith('全体目標に対する'):
        systems=re.findall(r'(?m)^[\t 　]+([^\n]+システム)\s*$',mission.get('goal',''))[:3]
        return {'title':'対象システムの構成・開発・販売準備計画','scope':criterion,
                'headings':['対象業務と利用場面',*systems,'全体構成図と具体事例','開発・検証・販売準備の工程','実施体制と未実施事項'],'depends_on':[]}
    if '経営計画' in criterion:
        return {'title':criterion,'scope':criterion+'。登録済み試算表を現時点の最新入力として、対象期間・数値根拠・不足情報を明示する。売上、費用、利益、投資、資金繰りの計画前提を区別し、未知の数値を作らない。',
                'headings':['採用する試算表と対象期間','現状業績・財務の整理','経営課題と施策','投資・資金繰り計画','数値計画の前提と検証','実行体制と進捗管理'],'depends_on':[]}
    if '提案書' in criterion and ('補助金' in criterion or 'ものづくり' in criterion or 'モノづくり' in criterion):
        return {'title':criterion,'scope':criterion+'。対象業務の課題、提案構成、投資根拠、導入工程、確認済み公募条件との対応を整理し、金額や採択効果を推測で確定しない。',
                'headings':['提案の目的と対象業務','課題・導入効果と根拠','対象システムと構成図','導入工程・体制','投資内容・見積根拠','公募条件との対応と未確認事項'],'depends_on':[]}
    return None
