from types import SimpleNamespace
from unittest.mock import patch
import pytest
from app import recovery_record as rr

class Memory:
    def __init__(self,path,status='needs_review'):
        self.path=path
        self.status=status
        self.mission_status="ready"
    def get_project(self,p): return {"id":p,"workspace_path":""}
    def get_mission(self,p):
        return {'status':self.mission_status,'plan_version':3,'tasks':[{'id':'t','task_key':'SC01','status':self.status}],
                'goal':'goal','success_criteria':'criterion','constraints_text':'','instruction_messages':[]}

@pytest.fixture
def manager(tmp_path):
    workspace=SimpleNamespace(resolve_file=lambda _wp,_pid,relative,must_exist=False: (None,None,tmp_path/relative))
    return SimpleNamespace(memory=Memory(tmp_path/'memory.sqlite3'),workspace=workspace)

@pytest.fixture
def snapshot():
    with patch.object(rr,'plan_snapshot',return_value=({},'sig')):
        with patch('app.goal_contract.get_active',return_value=None):
            yield

def test_start_requires_current_task_plan_and_failure(manager,snapshot):
    with pytest.raises(ValueError,match='task is not present'):
        rr.start(manager,'p','bogus',{'kind':'content'},'a','3')
    with pytest.raises(ValueError,match='input_version'):
        rr.start(manager,'p','SC01',{'kind':'content'},'a','2')
    manager.memory.status='pending'
    with pytest.raises(ValueError,match='recorded failure'):
        rr.start(manager,'p','SC01',{'kind':'content'},'a','3')
    manager.memory.status='needs_review'
    row=rr.start(manager,'p','SC01',{'kind':'content'},'a','3')
    assert row['state']=='classified' and row['human_approved'] is False

def test_self_report_cannot_advance_or_mark_recovered(manager,snapshot):
    row=rr.start(manager,'p','SC01',{'kind':'content'},'a','3')
    rid=row['id']
    row=rr.advance(manager,'p',rid,'experience_checked','checked',{},'a')
    assert row['state']=='experience_checked'
    with pytest.raises(ValueError,match='persisted TRIZ'):
        rr.advance(manager,'p',rid,'triz_candidates','claimed',{'candidates':[{'id':'fake'}]},'a')
    with pytest.raises(ValueError,match='invalid recovery transition'):
        rr.advance(manager,'p',rid,'human_approval','claimed',{'approved':True,'required':False},'a')
    with pytest.raises(ValueError,match='invalid recovery transition'):
        rr.advance(manager,'p',rid,'recovered','claimed',{'passed':True},'a')
    assert rr.get(manager,'p',rid)['state']=='experience_checked'

def test_plan_drift_blocks_transition(manager,snapshot):
    row=rr.start(manager,'p','SC01',{'kind':'content'},'a','3')
    with patch.object(rr,'plan_snapshot',return_value=({},'new')):
        with pytest.raises(ValueError,match='plan or source changed'):
            rr.advance(manager,'p',row['id'],'experience_checked','checked',{},'a')

def test_source_unverified_experience_is_rejected(manager,tmp_path):
    from app.experience_store import ExperienceStore
    from time import time
    path=tmp_path/'experience_memory'/'experience.sqlite3'
    store=ExperienceStore(path)
    eid=store.add('p','success','lesson',{'input_version':'3'}, {})
    store.review('p',eid,'verified','r','note',time()+3600)
    found=rr.check_experiences(manager,'p','3','')
    assert found['accepted']==[]
    assert 'source_hash_unverified' in found['rejected'][0]['reasons']


def test_business_and_human_self_report_are_ignored(manager,snapshot):
    row=rr.start(manager,'p','SC01',{'kind':'content'},'a','3')
    rid=row['id']
    rr.advance(manager,'p',rid,'experience_checked','checked',{},'a')
    trial={'signature':'trial','status':'artifact_trial_passed',
           'candidates':[{'id':'c1','title':'candidate','steps':[{'capability':'workflow.design'}],'missing':[]}],
           'experiments':[{'candidate':'c1','cases':[{'checks':{'passed':True}}, {'checks':{'passed':True}}]}]}
    with patch.object(rr,'_stored_triz',return_value=trial):
        rr.advance(manager,'p',rid,'triz_candidates','stored',{'candidates':[{'id':'forged'}]},'a')
        rr.advance(manager,'p',rid,'isolated_trial','tested',{'candidate_id':'c1','capability':'workflow.design'},'a')
    with patch('app.completion_gate.evaluate',return_value={'criteria':[{'status':'FAIL'}]}):
        with pytest.raises(ValueError,match='independent goal'):
            rr.advance(manager,'p',rid,'business_check','claimed',{'passed':True,'independent_evidence':{'reviewer':'b','summary':'fake'}},'a')
    passed={'criteria':[{'criterion_id':'C1','status':'PASS'}], 'coverage_passed':True,
            'retention_passed':True,'artifact_hash':'hash','source_hash':'source',
            'human_accepted':False,'human_acceptance':{'valid':False}}
    with patch('app.completion_gate.evaluate',return_value=passed):
        rr.advance(manager,'p',rid,'business_check','checked',{'passed':False},'a')
        with pytest.raises(ValueError,match='persisted human acceptance'):
            rr.advance(manager,'p',rid,'human_approval','claimed',{'approved':True,'required':False},'a')
    assert rr.get(manager,'p',rid)['state']=='business_check'


def test_vehicle_business_check_waits_for_human_without_false_recovery(manager,snapshot):
    row=rr.start(manager,'p','SC01',{'kind':'content'},'a','3')
    rid=row['id']
    rr.advance(manager,'p',rid,'experience_checked','checked',{},'a')
    trial={'signature':'trial','status':'artifact_trial_passed',
           'candidates':[{'id':'c1','steps':[{'capability':'workflow.design'}],'missing':[]}],
           'experiments':[{'candidate':'c1','cases':[{'checks':{'passed':True}}, {'checks':{'passed':True}}]}]}
    with patch.object(rr,'_stored_triz',return_value=trial):
        rr.advance(manager,'p',rid,'triz_candidates','stored',{},'a')
        rr.advance(manager,'p',rid,'isolated_trial','tested',{'candidate_id':'c1','capability':'workflow.design'},'a')
    vehicle_gate={'criteria':[{'criterion_id':'SC01','status':'FAIL','reason_code':'HUMAN_ACCEPTANCE_MISSING'}],
                  'coverage_passed':True,'retention_passed':True,'human_accepted':False,
                  'human_acceptance':{'valid':False}}
    with patch('app.completion_gate.evaluate',return_value=vehicle_gate):
        rr.advance(manager,'p',rid,'business_check','independent',{},'a')
        with pytest.raises(ValueError,match='persisted human acceptance'):
            rr.advance(manager,'p',rid,'human_approval','self reported',{'approved':True},'a')
    assert rr.get(manager,'p',rid)['state']=='business_check'


def test_confirmed_plan_conflict_hands_off_even_if_caller_says_content(manager,snapshot):
    manager.memory.mission_status='plan_conflict'
    manager.memory.status='pending'
    with patch('app.local_patch_preview.preview',return_value={'candidates':[]}):
        row=rr.start(manager,'p','SC01',{'kind':'content'},'a','3')
    assert row['state']=='handed_off' and row['classification']=='plan_repair'
    assert row['triz_candidates']==[]
