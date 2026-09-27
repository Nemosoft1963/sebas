import json
import pytest
from app.structured_planning import compile_task,compile_plan,contract_of,validate_plan
from app.planning_document import planning_output,render_sections

def build(goal):
 t=compile_task(1,'成果物を作る',{'title':'営業活動というモデル案','scope':'原本を確認する','headings':['目的','内容']},['original'])
 return compile_plan(['成果物を作る'],[t],goal=goal)

@pytest.mark.parametrize('goal',['車両別月別損益の作成','補助金の調査','', '請求管理CSVを作成'])
def test_non_sales_preparation_uses_structured_renderer(goal):
 p=build(goal);t=p['tasks'][0];o=planning_output(t)
 assert o is not None
 assert 'ニーズ探索の質問票' not in o['required_headings']
 assert contract_of(t)['source_refs']==['context:original']
 assert validate_plan(p,{'SC01'})['passed']
 payload={'sections':{f'section_{i}':'原本の記載と入力条件を照合する手順を設計する。不足している情報は資料名と確認先を明記し、実行前に確認する。検証結果の記録方法と完了条件を定義し、未実施の作業は今後の手順として整理する。'*2 for i in range(1,len(o['required_headings'])+1)},'missing_inputs':[]}
 document=render_sections(t,o,json.dumps(payload,ensure_ascii=False))
 assert all('## '+h in document for h in o['required_headings'])

def test_sales_preparation_still_has_discovery():
 t=build('顧客開拓の営業活動を計画する')['tasks'][0]
 assert 'ニーズ探索の質問票' in planning_output(t)['required_headings']

def test_financial_preparation_keeps_unit_and_tax_constraints():
 t=build('営業部の車両別損益計算')['tasks'][0]
 assert '税込の明記がある金額を再度税込変換しない' in t['description']
 assert '売上と原価の一致を一般的な検証条件にせず' in t['description']

@pytest.mark.parametrize('criterion,expected',[('車両別原価管理表CSV',1),('営業案件管理CSV',2),('商談の進捗管理表',2)])
def test_csv_does_not_imply_sales_columns(criterion,expected):
 t=compile_task(1,criterion,{'title':'資料','scope':'集計の内容','headings':['目的','内容']},[])
 assert len(contract_of(t)['outputs'])==expected

def test_marker_does_not_bypass_action_gate():
 t=build('入力確認')['tasks'][0];c=contract_of(t);c['action_requirements']=[{'kind':'approval'}];t['acceptance_criteria']=json.dumps(c)
 assert planning_output(t) is None

def test_preparation_never_invents_input_facts():
 from app.planning_document import preparation_sections,validate_document_references
 t=build('車両別損益計算')['tasks'][0];o=planning_output(t)
 data=preparation_sections(t,o,[])
 document=render_sections(t,o,json.dumps(data,ensure_ascii=False))
 validate_document_references(document,[])
 assert '原本は0件' in document
 assert '2023年' not in document and '売上比率' not in document
 assert '確認完了を意味しない' in document

def test_preparation_inventory_is_scoped_and_memo_excluded():
 from app.planning_document import preparation_sections,validate_document_references
 t=build('入力確認')['tasks'][0];o=planning_output(t)
 files=[{'id':'original','source':'upload'},{'id':'foreign','source':'upload'},{'id':'memo','source':'memo'}]
 document=render_sections(t,o,json.dumps(preparation_sections(t,o,files),ensure_ascii=False))
 validate_document_references(document,files)
 assert 'context:original' in document and 'context:foreign' not in document
 assert '原本は1件' in document
 assert '税込' not in document

@pytest.mark.parametrize('text',['原本(context:ID1234)を確認した','context:memo を参照'])
def test_unknown_and_memo_citations_are_rejected(text):
 from app.planning_document import validate_document_references
 with pytest.raises(ValueError,match='存在しない'):
  validate_document_references(text,[{'id':'original','source':'upload'},{'id':'memo','source':'memo'}])

@pytest.mark.asyncio
async def test_fabricated_citation_never_reaches_saved_artifact(tmp_path):
 from app.memory.short_term import ShortTermMemory
 from app.project_manager import ProjectOrchestrator,TaskVerificationError
 from app.workspace_files import WorkspaceSandbox
 class Llm:
  async def complete_json(self,messages,schema):
   return json.dumps({'sections':{key:'担当者は原本の項目と依頼条件を照合し、確認結果と未確認事項を区別して記録する。入力変更がある場合には対応する確認工程を再実施し、検証結果に応じて計画を更新する。参照はcontext:invented。'*2 for key in schema['properties']['sections']['properties']},'missing_inputs':[]},ensure_ascii=False)
 memory=ShortTermMemory(tmp_path/'memory.db');p=memory.create_project('test',workspace_path='projects/test');memory.save_mission(p['id'],'計画作成','','',False,[])
 t=compile_task(1,'詳細計画を作る',{'title':'計画','scope':'手順を整理する','headings':['目的','手順']},[])
 plan=compile_plan(['詳細計画を作る'],[t]);mission=memory.replace_plan(p['id'],plan['summary'],plan['tasks']);prep=mission['tasks'][0];memory.update_task(prep['id'],'completed',result='準備手順')
 current=mission['tasks'][1];manager=ProjectOrchestrator(memory,Llm(),lambda p:('',[]),None,lambda:[],workspace=WorkspaceSandbox(tmp_path/'workspace'))
 with pytest.raises(TaskVerificationError,match='存在しない'):
  await manager._execute_task(p['id'],current['id'])
 assert not list((tmp_path/'workspace').rglob('sc01.md'))
