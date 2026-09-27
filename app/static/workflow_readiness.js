(()=>{'use strict';
const ACTION_BUTTONS={prepare:'vehicleAutoPrepare',import_feedback:'goalFeedbackImport',propose_feedback:'goalFeedbackPropose',apply:'goalFeedbackApply',external_review:'goalPlanSend',approve_plan:'missionApprove',start:'missionStart',cancel_queue:'goalQueueCancel'};
const HEADINGS=['不明点','止まる条件','確認済み事実','候補と影響','再実行範囲'];
function add(parent,tag,text){const node=document.createElement(tag);node.textContent=text==null?'':String(text);parent.appendChild(node);return node;}
function describe(value){if(value==null)return '不明';if(typeof value==='string')return value;try{return JSON.stringify(value);}catch(_){return String(value);}}
function unknownMetric(value,label){return value==null?label:String(value);}
function presenceMetric(value){if(value==null)return '不明';return value?'あり':'なし';}
function formatUnknown(unknown){if(unknown==null)return{message:'不明',note:''};if(typeof unknown==='string')return{message:unknown,note:''};const message=unknown.message||'';const parts=[];if(unknown.source_ref)parts.push('原本: '+unknown.source_ref);if(unknown.locator)parts.push(unknown.locator);return{message:message||'不明',note:parts.join(' ')};}
function formatStopCondition(stop){if(stop==null||stop==='')return 'なし';return typeof stop==='string'?stop:String(stop);}
function formatCandidatesSummary(candidates){if(!candidates||!candidates.length)return '候補なし';const count=candidates.length;const firstImpact=(candidates[0]&&candidates[0].impact)||{};let summary='候補 '+count+'件';const amountStr=firstImpact.amount!=null?'¥'+Number(firstImpact.amount).toLocaleString():'';const impactDetails=[firstImpact.month,firstImpact.count!=null?firstImpact.count+'件':''].filter(Boolean).join('、');if(amountStr){summary+='、この確認事項の影響額 '+amountStr;if(impactDetails)summary+='（'+impactDetails+'）';}return summary;}
function formatRerunScope(scope){if(!scope)return '再実行範囲: 未定';const months=(scope.months||[]).join(',');const count=scope.count!=null?scope.count:0;return '対象月 '+(months||'未定')+'、'+count+'件を再計算';}
function questionCard(question,context,onAnswered){
 const card=document.createElement('article');card.className='workflow-question';
 if(question&&question.id)card.dataset.questionId=question.id;
 add(card,'h4','不明点');
 const unknownInfo=formatUnknown(question.unknown);
 const pUnknown=add(card,'p',unknownInfo.message);
 if(unknownInfo.note){const note=document.createElement('small');note.textContent=' ('+unknownInfo.note+')';pUnknown.appendChild(note);}
 add(card,'h4','止まる条件');
 add(card,'p',formatStopCondition(question.stop_condition));
 add(card,'h4','確認済み事実');
 const factsDiv=document.createElement('div');card.appendChild(factsDiv);
 add(card,'h4','候補と影響');
 add(card,'p',formatCandidatesSummary(question.candidates));
 if(question.candidates&&question.candidates.length){
  const details=document.createElement('details');
  const summary=document.createElement('summary');summary.textContent='候補一覧を表示';details.appendChild(summary);
  const ul=document.createElement('ul');
  question.candidates.forEach(row=>{const c=row.candidate||row;const label=(c.company&&c.id)?(c.company+' / '+c.id):(c.company||c.id||c.value||describe(c));add(ul,'li',label);});
  details.appendChild(ul);card.appendChild(details);
 }
 add(card,'h4','再実行範囲');
 add(card,'p',formatRerunScope(question.rerun_scope));
 if(!question.answerable){add(card,'p','再抽出が必要');return card;}
 const form=document.createElement('form');
 const select=add(form,'select','');
 select.name='vehicle';
 const defaultOpt=add(select,'option','車両を選択');defaultOpt.value='';
 const excludeOpt=add(select,'option','除外（計算対象外）');excludeOpt.value='__exclude__';
 const candidateIds=new Set();
 const candidateOptions=[];
 (question.candidates||[]).forEach(row=>{
  const c=row.candidate||row;const id=c.id||(typeof c==='string'?c:null);
  if(id&&!candidateIds.has(id)){candidateIds.add(id);candidateOptions.push({id,label:(c.company&&c.id)?(c.company+' / '+c.id):(c.company||id)});}
 });
 const otherOptions=[];
 (context.vehicleChoices||[]).forEach(choice=>{
  if(choice&&choice.id&&!candidateIds.has(choice.id)){otherOptions.push({id:choice.id,label:choice.company?(choice.company+' / '+choice.id):choice.id});}
 });
 if(candidateOptions.length>0){
  const grp=add(select,'option','候補');grp.disabled=true;
  candidateOptions.forEach(opt=>{const o=add(select,'option',opt.label);o.value=opt.id;});
  if(otherOptions.length>0){const sep=add(select,'option','――');sep.disabled=true;}
 }
 otherOptions.forEach(opt=>{const o=add(select,'option',opt.label);o.value=opt.id;});
 const facts=question.confirmed_facts||[];
 if(facts.length===0){
  add(factsDiv,'p','過去の回答はありません');
 }else{
  facts.forEach(fact=>{
   const val=fact.value||{};
   const pFact=document.createElement('p');
   const months=(val.months||[]).join(',');
   const label=val.exclude?'除外':(val.vehicle_id||'');
   pFact.textContent='過去の回答: '+label+(months?' ('+months+')':'');
   const useBtn=document.createElement('button');
   useBtn.type='button';useBtn.textContent='この回答を使う';
   useBtn.onclick=()=>{if(val.exclude){select.value='__exclude__';}else if(val.vehicle_id){select.value=val.vehicle_id;}};
   pFact.appendChild(document.createTextNode(' '));pFact.appendChild(useBtn);factsDiv.appendChild(pFact);
  });
 }
 const reason=add(form,'input','');reason.placeholder='理由';reason.name='reason';
 const apply=add(form,'select','');apply.name='apply_to';[['this_month','この月'],['date_range','期間指定']].forEach(row=>{const option=add(apply,'option',row[1]);option.value=row[0];});
 const dateFrom=add(form,'input','');dateFrom.placeholder='開始月 YYYY-MM';dateFrom.name='date_from';
 const dateTo=add(form,'input','');dateTo.placeholder='終了月 YYYY-MM';dateTo.name='date_to';
 const updateDateInputs=()=>{const show=apply.value==='date_range';dateFrom.hidden=!show;dateTo.hidden=!show;};
 apply.addEventListener('change',updateDateInputs);updateDateInputs();
 const answeredBy=add(form,'input','');answeredBy.placeholder='回答者名（必須）';answeredBy.name='answered_by';
 const submit=add(form,'button','回答を保存');submit.type='submit';
 const status=add(form,'p','');status.setAttribute('role','status');
 form.addEventListener('submit',async event=>{
  event.preventDefault();if(!answeredBy.value.trim()){status.textContent='回答者名を入力してください';return;}
  submit.disabled=true;
  const isExclude=select.value==='__exclude__';
  const vehicleId=isExclude?'':select.value;
  const date_from=(apply.value==='date_range'&&dateFrom.value.trim())?dateFrom.value.trim():null;
  const date_to=(apply.value==='date_range'&&dateTo.value.trim())?dateTo.value.trim():null;
  try{
   const response=await fetch('/api/projects/'+encodeURIComponent(context.projectId)+'/questions/answer',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
     version:context.planVersion,
     input_hash:context.inputHash,
     issue_id:question.id,
     vehicle_id:vehicleId,
     exclude:isExclude,
     reason:reason.value,
     apply_to:apply.value,
     allocations:[],
     date_from:date_from,
     date_to:date_to,
     answered_by:answeredBy.value.trim()
    })
   });
   const body=await response.json().catch(()=>({}));
   if(!response.ok){if(response.status===409&&body.detail&&body.detail.code==='FLAG_OFF')throw Error('この機能は無効です。既存の回答画面を利用してください');throw Error(typeof body.detail==='string'?body.detail:(body.detail&&body.detail.message)||'HTTP '+response.status);}
   status.textContent=body.fact_error?'回答は適用されましたが履歴の記録に失敗しました: '+body.fact_error:'保存しました';if(onAnswered)onAnswered();
  }catch(error){status.textContent=error.message||'保存できませんでした';}finally{submit.disabled=false;}
 });
 card.appendChild(form);return card;
}
function init(){
 const host=document.getElementById('workflow');if(!host){setTimeout(init,300);return;}
 const card=document.createElement('section');card.id='workflowReadiness';card.className='workflow-readiness';
 add(card,'h2','進行状態');const status=add(card,'p','');status.id='workflowReadinessStatus';status.setAttribute('role','status');
 const dl=add(card,'dl','');[['目標','workflowReadinessGoal'],['計画版','workflowReadinessVersion'],['停止理由','workflowReadinessStop'],['次の操作','workflowReadinessNext'],['正本状態','workflowReadinessCanonical'],['未達条件','workflowReadinessGate'],['成果','workflowReadinessArtifact']].forEach(row=>{add(dl,'dt',row[0]);const dd=add(dl,'dd','');dd.id=row[1];});
 const retry=add(card,'button','状態を再取得');retry.type='button';retry.id='workflowReadinessRetry';
 const qsection=add(card,'section','');add(qsection,'h3','確認事項（最大5件）');const qstatus=add(qsection,'p','');const qlist=add(qsection,'div','');
 const msection=add(card,'section','');add(msection,'h3','指標');
 const mstatus=add(msection,'p','');mstatus.id='workflowGoalMetricsStatus';mstatus.setAttribute('role','status');
 const mretry=add(msection,'button','指標を更新');mretry.type='button';mretry.id='workflowGoalMetricsRefresh';
 const mdl=add(msection,'dl','');[['計画の版数','workflowGoalMetricsPlanVersions'],['未解決の配賦','workflowGoalMetricsAllocations'],['未解決の読取','workflowGoalMetricsReads'],['Gate','workflowGoalMetricsGate'],['再停止回数','workflowGoalMetricsStopCount'],['暫定成果物の有無','workflowGoalMetricsProvisional'],['人間の確定の有無','workflowGoalMetricsAcceptance']].forEach(row=>{add(mdl,'dt',row[0]);const dd=add(mdl,'dd','');dd.id=row[1];});
 const nav=host.querySelector('nav');if(nav)nav.before(card);else host.prepend(card);
 const el=id=>document.getElementById(id);let project='',epoch=0,timer=null,state=null,metricsEpoch=0;
 function selected(){return (document.getElementById('projectSelect')||{}).value||'';}
 function applyActions(data){const allowed=(data&&data.allowed_actions)||{};for(const [action,id] of Object.entries(ACTION_BUTTONS)){const button=el(id);if(!button)continue;const rule=allowed[action];if(!rule){button.disabled=true;button.title='';continue;}button.disabled=!rule.allowed;button.title=rule.allowed?'':(rule.reason||'');}}
 window.applyWorkflowReadiness=function(){if(state)applyActions(state);};window.reloadWorkflowReadiness=function(){return load(true);};
 window.workflowReadinessSnapshot=function(){return state;};
 function notifyGoalLabels(){if(typeof window.refreshGoalCompletionLabels==='function')window.refreshGoalCompletionLabels();if(typeof window.refreshArtifactGoalLabels==='function')window.refreshArtifactGoalLabels();}
 function render(data,error){if(error){status.textContent='状態取得失敗: '+error;status.className='fetch-error';retry.disabled=false;return;}status.textContent='';status.className='';el('workflowReadinessGoal').textContent=data.goal||'（未設定）';el('workflowReadinessVersion').textContent='v'+(data.plan_version||0)+(data.mission_status?' / '+data.mission_status:'');el('workflowReadinessStop').textContent=data.stop_reason||'';el('workflowReadinessCanonical').textContent=data.canonical_state||data.phase||'';const failed=((data.gate||{}).failed_criteria)||[];el('workflowReadinessGate').textContent=failed.length?failed.join(', '):'なし';const next=data.next_action||{};el('workflowReadinessNext').textContent=(next.label||next.id||'')+(next.endpoint?' ('+next.endpoint+')':'');const klass=data.artifact_class||'none';const artifact=el('workflowReadinessArtifact');artifact.className=klass==='final'?'artifact-final':klass==='provisional'?'artifact-provisional':'';artifact.textContent=data.final_completed?'確定':(data.provisional||klass==='provisional'?'暫定':'なし');applyActions(data);}
 function renderMetrics(data){
  el('workflowGoalMetricsPlanVersions').textContent=unknownMetric(data.plan_versions,'不明');
  el('workflowGoalMetricsAllocations').textContent=unknownMetric(data.unresolved_allocations,'不明');
  el('workflowGoalMetricsReads').textContent=unknownMetric(data.unresolved_reads,'不明');
  const gate=data.gate;let gateText='不明';
  if(gate!=null){
   const head=gate.achieved==null?'不明':(gate.achieved?'達成':'未達');
   const counts=gate.criteria_counts;
   if(counts==null)gateText=head+'、条件別件数: 不明';
   else gateText=head+'、PASS '+unknownMetric(counts.PASS,'不明')+' / FAIL '+unknownMetric(counts.FAIL,'不明')+' / UNTESTABLE '+unknownMetric(counts.UNTESTABLE,'不明');
  }
  el('workflowGoalMetricsGate').textContent=gateText;
  el('workflowGoalMetricsStopCount').textContent=data.stop_count==null?'未計測':String(data.stop_count);
  el('workflowGoalMetricsProvisional').textContent=presenceMetric(data.provisional_artifact);
  el('workflowGoalMetricsAcceptance').textContent=presenceMetric(data.human_acceptance);
 }
 function isEditingQuestions(){
  const active=document.activeElement;
  return Boolean(active&&qlist&&qlist.contains(active)&&(active.tagName==='INPUT'||active.tagName==='SELECT'||active.tagName==='TEXTAREA'||active.closest('form')));
 }
 function snapshotQuestionsState(){
  const state={};
  qlist.querySelectorAll('.workflow-question').forEach(card=>{
   const qid=card.dataset.questionId;
   if(!qid)return;
   const item={};
   const details=card.querySelector('details');
   if(details)item.detailsOpen=details.open;
   const form=card.querySelector('form');
   if(form){
    const v=form.querySelector('select[name="vehicle"]');
    const r=form.querySelector('input[name="reason"]');
    const a=form.querySelector('select[name="apply_to"]');
    const df=form.querySelector('input[name="date_from"]');
    const dt=form.querySelector('input[name="date_to"]');
    const ab=form.querySelector('input[name="answered_by"]');
    if(v&&v.value)item.vehicle=v.value;
    if(r&&r.value)item.reason=r.value;
    if(a&&a.value)item.apply_to=a.value;
    if(df&&df.value)item.date_from=df.value;
    if(dt&&dt.value)item.date_to=dt.value;
    if(ab&&ab.value)item.answered_by=ab.value;
   }
   state[qid]=item;
  });
  return state;
 }
 function restoreQuestionState(card,saved){
  if(!card||!saved)return;
  if(saved.detailsOpen!=null){
   const details=card.querySelector('details');
   if(details)details.open=saved.detailsOpen;
  }
  const form=card.querySelector('form');
  if(!form)return;
  if(saved.vehicle!=null&&saved.vehicle!==''){
   const el=form.querySelector('select[name="vehicle"]');
   if(el)el.value=saved.vehicle;
  }
  if(saved.reason!=null&&saved.reason!==''){
   const el=form.querySelector('input[name="reason"]');
   if(el)el.value=saved.reason;
  }
  if(saved.apply_to!=null&&saved.apply_to!==''){
   const el=form.querySelector('select[name="apply_to"]');
   if(el){el.value=saved.apply_to;el.dispatchEvent(new Event('change'));}
  }
  if(saved.date_from!=null&&saved.date_from!==''){
   const el=form.querySelector('input[name="date_from"]');
   if(el)el.value=saved.date_from;
  }
  if(saved.date_to!=null&&saved.date_to!==''){
   const el=form.querySelector('input[name="date_to"]');
   if(el)el.value=saved.date_to;
  }
  if(saved.answered_by!=null&&saved.answered_by!==''){
   const el=form.querySelector('input[name="answered_by"]');
   if(el)el.value=saved.answered_by;
  }
 }
 async function loadQuestions(pid,ticket,forced){
  if(!forced&&isEditingQuestions())return;
  const savedState=!forced?snapshotQuestionsState():{};
  try{
   const response=await fetch('/api/projects/'+encodeURIComponent(pid)+'/questions?limit=5');
   const body=await response.json().catch(()=>({}));
   if(ticket!==epoch)return;
   if(!response.ok)throw Error(typeof body.detail==='string'?body.detail:'HTTP '+response.status);
   qstatus.textContent=body.prepared?'':'質問を準備できません';
   qlist.replaceChildren();
   (body.questions||[]).forEach(question=>{
    const card=questionCard(question,{projectId:pid,planVersion:body.plan_version,inputHash:body.input_hash,vehicleChoices:body.vehicle_choices||[]},()=>load(true));
    if(!forced&&savedState[question.id]){
     restoreQuestionState(card,savedState[question.id]);
    }
    qlist.appendChild(card);
   });
   if(body.omitted>0)add(qlist,'p','ほか '+body.omitted+'件');
  }catch(error){
   qstatus.textContent='確認事項の取得失敗: '+(error.message||'通信できませんでした');
  }
 }
 function busyPoll(data){return !!(data&&(data.active_job||data.phase==='waiting_budget'||data.phase==='running'||data.mission_status==='running'));}
 function schedule(data){if(timer){clearTimeout(timer);timer=null;}if(document.hidden)return;timer=setTimeout(load,busyPoll(data)?4000:15000);}
 async function load(){
  const force=Boolean(arguments[0]);
  const pid=selected();
  const projectChanged=project!==pid;
  project=pid;
  const ticket=++epoch;
  if(!pid){render(null,'プロジェクトが選択されていません');notifyGoalLabels();return;}
  loadQuestions(pid,ticket,force||projectChanged);
  try{
   const response=await fetch('/api/projects/'+encodeURIComponent(pid)+'/workflow-readiness');
   const body=await response.json().catch(()=>({}));
   if(ticket!==epoch)return;
   if(!response.ok)throw Error(typeof body.detail==='string'?body.detail:'HTTP '+response.status);
   state=body;
   render(body);
   notifyGoalLabels();
   schedule(body);
  }catch(error){
   if(ticket!==epoch)return;
   state=null;
   render(null,error.message||'通信できませんでした');
   notifyGoalLabels();
   schedule({phase:'waiting_budget'});
  }
 }
 async function loadMetrics(){const pid=selected();const ticket=++metricsEpoch;const button=el('workflowGoalMetricsRefresh');if(button)button.disabled=true;if(!pid){mstatus.textContent='プロジェクトが選択されていません';if(ticket===metricsEpoch&&button)button.disabled=false;return;}try{const response=await fetch('/api/projects/'+encodeURIComponent(pid)+'/goal-metrics');const body=await response.json().catch(()=>({}));if(ticket!==metricsEpoch)return;if(!response.ok)throw Error(typeof body.detail==='string'?body.detail:'HTTP '+response.status);mstatus.textContent='';renderMetrics(body);}catch(error){if(ticket!==metricsEpoch)return;mstatus.textContent='指標を取得できませんでした: '+(error.message||'通信できませんでした');}finally{if(ticket===metricsEpoch&&button)button.disabled=false;}}
 retry.onclick=()=>load(true);mretry.onclick=loadMetrics;document.getElementById('projectSelect').addEventListener('change',()=>{load(true);});document.getElementById('projectSelect').addEventListener('change',loadMetrics);document.addEventListener('visibilitychange',()=>{if(document.hidden){if(timer){clearTimeout(timer);timer=null;}return;}load();});load(true);loadMetrics();
}
if(typeof module!=='undefined')module.exports={HEADINGS,questionCard};
if(typeof document!=='undefined'){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();}
})();
