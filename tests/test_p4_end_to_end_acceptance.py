"""P4 anonymous sales replay acceptance; no real customer data or network."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from app.completion_replay import preview as replay
from app.completion_gate import evaluate
from app.goal_contract import from_mission
from app.local_patch_preview import preview as patch_preview
from app.premarketing_monitor import campaign_execution_monitor
from app.safe_auto_resume import _save as save_run


class Memory:
    def __init__(self,path,mission): self.path=path;self.mission=mission;self.actions=[]
    def get_mission(self,pid): return self.mission
    def list_actions(self,pid): return self.actions


def fixture(tmp_path):
    task={'id':'t1','task_key':'SC01','title':'匿名販売資料','description':'SC01 資料作成','acceptance_criteria':'result/sales.md','mode':'local','status':'pending','depends_on':[]}
    mission={'project_id':'p','goal':'匿名販売案件','success_criteria':'販売活動の証拠を確認する','constraints_text':'','plan_version':1,'status':'planning','tasks':[task],'instruction_messages':[]}
    return SimpleNamespace(memory=Memory(tmp_path/'memory.db',mission)),mission,task


def test_sales_flow_stops_at_real_boundaries_and_replay_is_read_only(tmp_path):
    m,mission,task=fixture(tmp_path); contract=from_mission(mission)
    contract['criteria'][0]['exec_task_keys']=['SC01']
    issue={'id':'conflict','criterion':'SC01','text':'公開前に承認する順序が矛盾している'}
    with patch('app.local_patch_preview.plan_snapshot',return_value=({'tasks':[task]},'same-sig')), \
         patch('app.local_patch_preview.contract_preview',return_value=contract), \
         patch('app.local_patch_preview.ReviewStore.get',return_value={'status':'not_passed'}):
        patch_row=patch_preview(m,'p',[issue])
    assert patch_row['candidates'][0]['classification']=='safe_plan_patch'
    assert patch_row['external_validation_signature']=='same-sig'
    assert patch_row['execution_start_allowed'] is False

    save_run(m,'p','SC01','input','idem',run_id='local-run',status='completed',artifact_hash='artifact',evidence='[{"path":"result/sales.md"}]')
    before_db=m.memory.path.with_name(m.memory.path.name+'.auto_resume.sqlite3').read_bytes()
    before_reviews=(tmp_path/'goal_reviews.sqlite3').read_bytes()
    with patch('app.completion_replay.plan_snapshot',return_value=({},'same-sig')):
        row=replay(m,'p')
    assert before_reviews==(tmp_path/'goal_reviews.sqlite3').read_bytes()
    assert not (tmp_path/'goal_completion.sqlite3').exists()
    assert row['read_only'] and not row['crossed_approval_boundary'] and not row['would_achieve']
    assert before_db==m.memory.path.with_name(m.memory.path.name+'.auto_resume.sqlite3').read_bytes()
    assert next(x for x in row['stages'] if x['id']=='external_operation')['reachable'] is False


def test_real_completion_gate_rejects_artifact_only_anonymous_case(tmp_path):
    m,mission,_task=fixture(tmp_path)
    # Actual completion gate function: no active acceptance/evidence can never achieve.
    gate=evaluate(m,'p',persist=False)
    assert gate['achieved'] is False
    assert gate['reason_code'] or gate['failed_criteria']


def test_sales_monitor_incomplete_variants():
    base={'id':'c','publication_status':'published','site_publication_status':'published','google_form_id':'f','google_site_url':'https://new.example','creative':{'status':'approved','quality_score':90}}
    variants=[
        (dict(base,google_form_id=''),[],[]),                         # artifact/site only
        (base,[],[]),                                                # form published, zero answers
        (base,[{'status':'awaiting_approval','open_count':0}],[]),    # unapproved post
        (dict(base,google_site_url='https://old.example',site_publication_status='failed'),
         [{'status':'evidence_registered','evidence_url':'https://old.example/post','open_count':1}],[]),
    ]
    for campaign,shares,leads in variants:
        monitor=campaign_execution_monitor(campaign,shares,leads)
        assert monitor['counts']['leads']==0
        assert any(x['state']!='complete' for x in monitor['stages'])
        assert monitor['progress_percent']<100
