(()=>{'use strict';
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
function currentPid(){return (document.getElementById('projectSelect')||{}).value||'';}
const STATE_JA={achieved:'達成',capability_gap:'機能不足',evidence_gap:'証拠不足',human_decision:'人の判断待ち',unknown:'確認できず'};
const NEXT_JA={autonomous:'自走が進めます',human_confirm:'人の確認が必要',capability_dev:'機能開発が必要',evidence_confirm:'証拠の確認が必要',blocked_unknown:'確認できず'};
const NEXT_TITLE_JA={autonomous:'自走が進めます',human_confirm:'人の確認が必要',capability_dev:'機能開発が必要',evidence_confirm:'証拠の確認が必要',blocked_unknown:'確認できず'};
function stateLabel(state){const s=String(state||'');if(Object.prototype.hasOwnProperty.call(STATE_JA,s))return STATE_JA[s];return '確認できず';}
function nextStepText(step){const item=step||{};const kind=String(item.kind||'');const base=Object.prototype.hasOwnProperty.call(NEXT_JA,kind)?NEXT_JA[kind]:'確認できず';const detail=String(item.detail_ja||'');return detail?base+'：'+detail:base;}
function nextStepTitle(step){const item=step||{};const kind=String(item.kind||'');if(Object.prototype.hasOwnProperty.call(NEXT_TITLE_JA,kind))return NEXT_TITLE_JA[kind];return '確認できず';}
function summaryText(summary){const info=summary||{};const total=info.total;if(total==null)return '残件：確認できず';const remaining=Array.isArray(info.goal_remaining)?info.goal_remaining.length:0;if(info.consistent===false)return '残件：確認できず（集計不一致）';if(info.goal_achieved===true)return '残件：0件（全条件が達成）';return '残件：'+String(remaining)+'件 / 全'+String(total)+'件';}
function shortSignature(signature){const text=String(signature||'');if(!text)return '確認できず';return text.slice(0,12);}
function preflightText(status){return String(status||'')==='ok'?'ok':'確認できず';}
function readOnlyText(value){return value===true?'読取専用':value===false?'書込あり':'確認できず';}
function externalSendsText(value){if(typeof value==='number'&&isFinite(value))return '外部送信 '+String(value)+'件';return '確認できず';}
function stateLabelJa(state){return stateLabel(state);}
function countOrUnknown(value){if(typeof value==='number'&&isFinite(value))return String(value)+'件';return '確認できず';}
const RUN_EXEC_JA={running:'実行中',finished:'終了',stopped_error:'異常停止',stopped_time_limit:'時間切れで停止',cancelled:'停止（取消）'};
const RUN_GOAL_JA={achieved:'達成',unmet:'未達',undetermined:'判定待ち'};
const RUN_OWNER_JA={human:'人',system:'システム',external:'外部'};
const RUN_KIND_JA={artifact_missing:'成果物不足',phrase_missing:'必須語不足',unsupported_fact:'原本外の断定',human_decision:'人の判断待ち',external_evidence:'外部証拠待ち',system_error:'基盤異常',external_result:'実施結果待ち',unclassified:'分類不能'};
const RUN_RESP_JA={fix_artifact:'成果物を修正',register_source:'出典を登録',human_decision:'人が判断',await_external:'外部実施結果待ち',unclassified:'分類できていない'};
const RUN_COUNT_JA={pass:'合格',fail:'不合格',untestable:'判定不能',human_pending:'人の確認待ち',input_missing:'入力待ち'};
function runCountText(key,value){const label=Object.prototype.hasOwnProperty.call(RUN_COUNT_JA,String(key||''))?RUN_COUNT_JA[String(key||'')]:'分類できていない';if(typeof value==='number'&&isFinite(value))return label+'：'+String(value)+'件';return label+'：確認できず';}
function runResponsibilityText(resp){const s=String(resp||'');if(Object.prototype.hasOwnProperty.call(RUN_RESP_JA,s))return RUN_RESP_JA[s];return '分類できていない';}
function runBasisText(basis){const s=String((basis||{}).basis_status||'');if(s==='available')return '当時の根拠あり';if(s==='unavailable')return '当時の根拠を保存できなかった';return '確認できず';}
function runAtDateLabel(atRun){const t=(atRun||{}).recorded_at;if(typeof t!=='number'||!isFinite(t))return '';const d=new Date(t*1000);const m=String(d.getMonth()+1);const day=String(d.getDate());return m+'月'+day+'日';}
function runExecutionText(state){const s=String(state||'');if(Object.prototype.hasOwnProperty.call(RUN_EXEC_JA,s))return RUN_EXEC_JA[s];return '確認できず';}
function runGoalText(verdict){const s=String(verdict||'');if(Object.prototype.hasOwnProperty.call(RUN_GOAL_JA,s))return RUN_GOAL_JA[s];return '確認できず';}
function runKindText(kind){const s=String(kind||'');if(Object.prototype.hasOwnProperty.call(RUN_KIND_JA,s))return RUN_KIND_JA[s];if(s==='')return '確認できず';return '分類できていない';}
function runOwnerText(owner){const s=String(owner||'');if(Object.prototype.hasOwnProperty.call(RUN_OWNER_JA,s))return RUN_OWNER_JA[s];if(s==='')return '確認できず';return '分類できていない';}
function isGoalAchievedOnly(latest){return !!latest&&latest.found===true&&String(latest.goal_verdict||'')==='achieved';}
function currentRunItems(latest){if(!latest||latest.found!==true)return [];const items=Array.isArray(latest.open_items)?latest.open_items:[];return items.filter(r=>r&&typeof r==='object');}
function runStatusLine(runState,goalVerdict){
 if(!runState&&!goalVerdict)return '確認できず';
 if(runState==='finished'&&goalVerdict==='achieved')return '今回の実行: 終了 / 目標: 達成';
 if(runState==='finished')return '今回の実行: 終了 / 目標: '+runGoalText(goalVerdict);
 return '今回の実行: '+runExecutionText(runState)+' / 目標: '+runGoalText(goalVerdict);
}
function appendRunRecovery(host,latest){
 const state=String(latest.execution_state||'');
 addText(host,'p','停止理由：'+String(latest.stop_reason||'確認できず'));
 addText(host,'p','復旧操作：残件へ証拠を登録し、新しい実行で再判定する');
 addText(host,'p','安全な再試行先：次の実行を開始（既存の開始操作へ進む）');
 if(state==='stopped_error')addText(host,'p','異常停止は完走と区別する。成功・達成とは表示しない。');
}
function openItemSummary(item){
 const parts=[String(item.item_id||'(項目不明)'),runKindText(item.kind),String(item.status_text||'')].filter(s=>s!=='');
 return parts.join(' / ');
}
function groupOpenItemsByResponsibility(items){
 const groups={fix_artifact:[],register_source:[],human_decision:[],await_external:[],unclassified:[]};
 for(const row of (items||[])){
  if(!row||typeof row!=='object')continue;
  const key=String(row.responsibility||'unclassified');
  if(Object.prototype.hasOwnProperty.call(groups,key))groups[key].push(row);
  else groups['unclassified'].push(row);
 }
 return groups;
}
function responsibilityActionText(resp,item){
 const r=String(resp||'unclassified');
 if(r==='fix_artifact'){
  const target=String((item&&item.artifacts&&item.artifacts[0])||'');
  if(target)return '該当の成果物（'+target+'）を直す。理由：'+String((item&&item.status_text)||'確認できず');
  return '該当の成果物を直す。理由：'+String((item&&item.status_text)||'確認できず');
 }
 if(r==='register_source')return '証拠を登録する（出典の登録）: '+String((item&&item.evidence_needed)||'確認できず');
 if(r==='human_decision')return '人が判断する：'+String((item&&item.status_text)||'確認できず');
 if(r==='await_external')return '待っている実施結果：'+String((item&&item.status_text)||'確認できず');
 return '分類できていないため人が確認する。現状：'+String((item&&item.status_text)||'確認できず');
}
function appendGroupedOpenItems(host,items){
 const groups=groupOpenItemsByResponsibility(items);
 const order=['fix_artifact','register_source','human_decision','await_external','unclassified'];
 for(const key of order){
  const rows=groups[key];
  if(!rows.length)continue;
  addText(host,'h4','残件：'+runResponsibilityText(key)+'（'+String(rows.length)+'件）');
  const list=document.createElement('ul');
  for(const row of rows){
   const line=document.createElement('li');
   addText(line,'strong',openItemSummary(row));
   addText(line,'p','現状：'+String(row.status_text||'確認できず'));
   addText(line,'p','必要な証拠：'+String(row.evidence_needed||'確認できず'));
   addText(line,'p','具体的な操作：'+responsibilityActionText(key,row));
   if(key==='human_decision')addText(line,'p','判断が要る内容：'+String(row.status_text||'確認できず'));
   if(key==='await_external')addText(line,'p','証拠の登録先：対象の残件へ証拠を登録（下の入力）');
   addText(line,'p','担当主体：'+runOwnerText(row.owner));
   addText(line,'p','次の操作：'+String(row.next_action||'確認できず'));
   if(Array.isArray(row.artifacts)&&row.artifacts.length)addText(line,'p','関連成果物：'+row.artifacts.map(String).join(' / '));
   list.append(line);
  }
  host.append(list);
 }
}
function appendAtRunSection(host,atRun,currentRunId){
 const basis=(atRun&&typeof atRun==='object')?atRun:null;
 const titleDate=basis?runAtDateLabel(basis):'';
 addText(host,'h4','当時の検証'+(titleDate?'（'+titleDate+'の実行）':''));
 if(!basis){
  addText(host,'p','当時の根拠を保存できなかった（保存なし・機能追加前の実行）');
  return;
 }
 if(String(basis.basis_status||'')==='unavailable'){
  addText(host,'p','当時の根拠を保存できなかった');
  if(basis.basis_reason)addText(host,'p','理由：'+String(basis.basis_reason));
  return;
 }
 const counts=(basis.counts&&typeof basis.counts==='object')?basis.counts:null;
 if(!counts){
  addText(host,'p','当時の根拠を保存できなかった');
  return;
 }
 const parts=['要求項目の当時の集計'];
 for(const key of ['pass','fail','untestable','human_pending','input_missing']){
  parts.push(runCountText(key,counts[key]));
 }
 addText(host,'p',parts.join(' / '));
 addText(host,'p','検証書：'+(String(basis.verification_ref||'')||'（なし）')+(basis.verification_hash?' / ハッシュあり':''));
 const snap=Array.isArray(basis.open_items)?basis.open_items:[];
 if(!snap.length)addText(host,'p','当時の残件：なし');
 else{
  const lines=snap.map(r=>String(r.item_id||'(項目不明)')+'（'+runKindText(r.kind)+'・'+runResponsibilityText(r.responsibility)+'）');
  addText(host,'p','当時の残件（'+String(snap.length)+'件）：'+lines.join(' / '));
 }
 if(currentRunId&&String(basis.run_id||'')!==String(currentRunId))addText(host,'p','当時の実行と現在の実行は別の記録です（書き換えなし）');
}
function renderRunCycle(host,latest,ctx){
 host.replaceChildren();
 addText(host,'h3','今回の実行');
 if(!latest){addText(host,'p','実行履歴未判定');return;}
 if(latest.found==='unreadable'){
  addText(host,'p','実行記録を読めません：'+String(latest.error||'確認できず'));
  addText(host,'p','完走・未達と区別する。成功・達成とは表示しない。');
  return;
 }
 if(latest.found!==true){addText(host,'p','実行履歴未判定');return;}
 addText(host,'p',runStatusLine(latest.execution_state,latest.goal_verdict));
 if(isGoalAchievedOnly(latest))addText(host,'p','目標：達成');
 else{addText(host,'p','目標：'+runGoalText(latest.goal_verdict));addText(host,'p','達成と表示されるのは、完了ゲートを通ったときだけです');}
 if(String(latest.execution_state||'')==='stopped_error')appendRunRecovery(host,latest);
 const items=currentRunItems(latest);
 addText(host,'h4','現在の残件');
 if(!items.length){addText(host,'p','残件なし');}
 else{appendGroupedOpenItems(host,items);}
 appendAtRunSection(host,latest.at_run,latest.run_id);
 const history=Array.isArray(latest.open_item_history)?latest.open_item_history:[];
 if(history.length>items.length){
  const details=document.createElement('details');
  addText(details,'summary','履歴（'+String(history.length)+'件）');
  const pre=document.createElement('pre');
  pre.textContent=JSON.stringify(history,null,2);
  details.append(pre);host.append(details);
 }
 renderRunActions(host,latest,ctx);
}
function renderRunActions(host,latest,ctx){
 const safeCtx=(ctx&&typeof ctx==='object')?ctx:{};
 const runId=String(latest.run_id||'');
 const items=currentRunItems(latest);
 const selected=safeCtx.selectedOpenItemId?String(safeCtx.selectedOpenItemId):'';
 let target=items.find(r=>String(r.id||'')===selected)||items[0]||null;
 if(target)safeCtx.selectedOpenItemId=String(target.id||'');
 const controls=document.createElement('div');
 const detailBtn=document.createElement('button');
 detailBtn.type='button';detailBtn.textContent='結果を確認';
 detailBtn.onclick=()=>{
  if(!target)return;
  safeCtx.expandedOpenItemId=String(target.id||'');
  if(typeof safeCtx.onSelect==='function')safeCtx.onSelect(safeCtx.selectedOpenItemId,safeCtx.expandedOpenItemId);
  renderRunCycle(host,latest,safeCtx);
 };
 controls.append(detailBtn);
 if(target){
  const idLabel=document.createElement('span');
  idLabel.textContent='対象残件: '+openItemSummary(target);
  controls.append(idLabel);
  const assignee=document.createElement('input');
  assignee.type='text';assignee.placeholder='担当者名（必須）';assignee.value='';
  const evidence=document.createElement('input');
  evidence.type='text';evidence.placeholder='証拠の内容/参照（必須）';evidence.value='';
  const reason=document.createElement('input');
  reason.type='text';reason.placeholder='理由（必須）';reason.value='';
  const submit=document.createElement('button');
  submit.type='button';submit.textContent='証拠を登録';
  submit.disabled=true;
  const refresh=()=>{submit.disabled=!(assignee.value.trim()&&evidence.value.trim()&&reason.value.trim());};
  assignee.addEventListener('input',refresh);
  evidence.addEventListener('input',refresh);
  reason.addEventListener('input',refresh);
  submit.onclick=async()=>{
   if(!(assignee.value.trim()&&evidence.value.trim()&&reason.value.trim()))return;
   if(!target)return;
   submit.disabled=true;
   const doneAssignee=assignee.value;const doneEvidence=evidence.value;const doneReason=reason.value;
   const doneId=String(target.id||'');
   try{await safeCtx.submitEvidence(runId,doneId,doneAssignee.trim(),doneEvidence.trim(),doneReason.trim());}
   catch(err){
    const msg=(err&&err.message)?String(err.message):'証拠の登録に失敗';
    const line=document.createElement('p');
    line.textContent='失敗: '+msg;
    host.append(line);
    return;
   }
   finally{await safeCtx.reload();}
  };
  controls.append(assignee,evidence,reason,submit);
 }
 const nextWrap=document.createElement('div');
 const nextActor=document.createElement('input');
 nextActor.type='text';nextActor.placeholder='担当者名（必須）';nextActor.value='';
 const nextBtn=document.createElement('button');
 nextBtn.type='button';nextBtn.textContent='次の実行を開始';
 nextBtn.disabled=true;
 nextActor.addEventListener('input',()=>{nextBtn.disabled=!nextActor.value.trim();});
 nextBtn.onclick=async()=>{
  nextBtn.disabled=true;
  const doneActor=nextActor.value;
  try{await safeCtx.startNext(runId,doneActor.trim());}
  catch(err){
   const msg=(err&&err.message)?String(err.message):'次の実行の開始に失敗';
   const line=document.createElement('p');
   line.textContent='失敗: '+msg;
   host.append(line);
   return;
  }
  finally{await safeCtx.reload();}
 };
 nextWrap.append(nextActor,nextBtn);
 controls.append(nextWrap);
 host.append(controls);
 if(target&&safeCtx.expandedOpenItemId===String(target.id||'')){
  const detail=document.createElement('div');
  addText(detail,'p','必要な証拠：'+String(target.evidence_needed||'確認できず'));
  addText(detail,'p','次の操作：'+String(target.next_action||'確認できず'));
  if(Array.isArray(target.artifacts)&&target.artifacts.length)addText(detail,'p','関連成果物：'+target.artifacts.map(String).join(' / '));
  host.append(detail);
 }
}
function apiRunLatest(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/runs/latest');}
async function postRunJson(pid,path,body){
 const r=await fetch('/api/projects/'+encodeURIComponent(pid)+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
 let d=null;
 try{d=await r.json();}catch(_parseError){throw Error('応答を読み取れませんでした');}
 if(!r.ok){
  const detail=typeof d.detail==='string'?d.detail:String((d&&d.detail&&d.detail.message)||('HTTP '+r.status));
  const err=Error(detail||('HTTP '+r.status));err.status=r.status;throw err;
 }
 return d;
}
function renderGoalState(host,data){
 host.replaceChildren();
 addText(host,'h3','自走の現在地');
 if(!data){addText(host,'p','自走の現在地を取得できません');return;}
 addText(host,'p','読取専用：'+readOnlyText(data.read_only));
 addText(host,'p',externalSendsText(data.external_sends));
 addText(host,'p','計画署名：'+shortSignature(data.plan_signature));
 addText(host,'p','事前照合：'+preflightText(data.preflight_status));
 const stop=data.stop||{};
 addText(host,'p','主たる停止：'+String(stop.stop_class||'確認できず')+' / '+String(stop.stop_code||'確認できず'));
 if(stop.reason_ja)addText(host,'p',String(stop.reason_ja));
 addText(host,'p','次の一手：'+String(stop.next_action||'確認できず'));
 const summary=data.summary||{};
 addText(host,'p',summaryText(summary));
 const nextStep=data.next_step||{};
 addText(host,'p','次に起きること：'+nextStepTitle(nextStep));
 addText(host,'p',nextStepText(nextStep));
 if(nextStep.basis)addText(host,'p','根拠：'+String(nextStep.basis));
 const criteria=Array.isArray(data.criteria)?data.criteria:[];
 if(!criteria.length){addText(host,'p','達成条件を確認できません');}
 else{
  const list=document.createElement('ul');
  for(const row of criteria){
   const line=document.createElement('li');
   const state=String((row||{}).state||'unknown');
   addText(line,'strong',String((row||{}).criterion_id||'(条件不明)')+': '+stateLabelJa(state)+'（'+String((row||{}).state_label_ja||stateLabelJa(state))+'）');
   if((row||{}).statement_ref)addText(line,'p','引用：'+String((row||{}).statement_ref||''));
   const blockers=Array.isArray((row||{}).blockers)?(row||{}).blockers:[];
   if(blockers.length)addText(line,'p','理由：'+blockers.join(' / '));
   else if((row||{}).reason)addText(line,'p','理由：'+String((row||{}).reason||''));
   list.append(line);
  }
  host.append(list);
 }
 const proposals=data.proposals||{};
 if(proposals.readable===true){
  const byStatus=proposals.by_status||{};
  const keys=Object.keys(byStatus).sort();
  const parts=keys.map(key=>key+':'+String(byStatus[key]));
  addText(host,'p','提案：'+(parts.length?parts.join(' / '):'0件'));
 }else{
  addText(host,'p','提案：確認できず');
 }
 const autopilot=data.autopilot||{};
 if(autopilot.readable===true){
  addText(host,'p','自走：'+(autopilot.enabled===true?'有効':'無効'));
  const latest=autopilot.latest||null;
  if(latest)addText(host,'p','直近のティック：'+String(latest.status||'')+' / '+String(latest.stop_class||'')+' / '+(latest.gate_status?String(latest.gate_status):'確認できず'));
  else addText(host,'p','直近のティック：なし');
 }else{
  addText(host,'p','自走：確認できず');
 }
 if(data.summary&&data.summary.goal_achieved!==true){
  addText(host,'p','全条件の達成は確認できていません。達成とは表示しません。');
 }
 const details=document.createElement('details');
 addText(details,'summary','診断情報（JSON）');
 const pre=document.createElement('pre');
 pre.textContent=JSON.stringify(data,null,2);
 details.append(pre);host.append(details);
}
async function fetchJson(url){
 const r=await fetch(url);
 let d=null;
 try{d=await r.json();}catch(_parseError){throw Error('応答を読み取れませんでした');}
 if(!r.ok){
  const detail=typeof d.detail==='string'?d.detail:String((d&&d.detail&&d.detail.message)||('HTTP '+r.status));
  const err=Error(detail||('HTTP '+r.status));err.status=r.status;throw err;
 }
 return d;
}
function apiGoalState(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/goal-progress').then(d=>{if(!d||!Object.prototype.hasOwnProperty.call(d,'criteria')||!Object.prototype.hasOwnProperty.call(d,'summary')||!Object.prototype.hasOwnProperty.call(d,'next_step'))throw Error('自走の現在地を確認できませんでした');return d;});}
function mountRunCycle(mountBody,runBody,ctxFactory){
 const safeFactory=(typeof ctxFactory==='function')?ctxFactory:defaultRunCtx;
 function init(){
  let anchor=mountBody||document.getElementById('goalStateSection');
  if(!anchor)anchor=document.getElementById('workflow');
  if(!anchor){setTimeout(init,300);return;}
  if(document.getElementById('runCycleSection'))return;
  const box=document.createElement('section');box.className='workflow-overview-item';box.id='runCycleSection';
  addText(box,'h3','今回の実行');
  const body=runBody||document.createElement('div');body.id='runCycleBody';
  addText(body,'p','読込中…');
  box.append(body);
  anchor.append(box);
  let epoch=0;
  const state={selectedOpenItemId:'',expandedOpenItemId:''};
  function ctxFor(latest,pid){
   const base=safeFactory(latest,pid)||{};
   const merged=Object.assign({},base,{
    selectedOpenItemId:state.selectedOpenItemId,
    expandedOpenItemId:state.expandedOpenItemId,
   });
   merged.onSelect=(selected,expanded)=>{
    state.selectedOpenItemId=String(selected||'');
    state.expandedOpenItemId=String(expanded||'');
   };
   const submitEvidence=merged.submitEvidence;
   merged.submitEvidence=async(runId,openItemId,assignee,evidence,reason)=>{
    const out=await submitEvidence(runId,openItemId,assignee,evidence,reason);
    state.expandedOpenItemId=String(openItemId||'');
    state.selectedOpenItemId=String(openItemId||'');
    return out;
   };
   const startNext=merged.startNext;
   merged.startNext=async(runId,actor)=>{
    const out=await startNext(runId,actor);
    state.expandedOpenItemId='';
    return out;
   };
   return merged;
  }
  async function loadLatest(pid){
   try{return await apiRunLatest(pid);}
   catch(err){
    const status=err&&err.status;
    if(status===409)return {found:'unreadable',error:String(err.message||'実行記録を読めません')};
    throw err;
   }
  }
  async function load(){
   const pid=currentPid();
   const ticket=++epoch;
   const isCurrent=()=>ticket===epoch;
   if(!pid){body.replaceChildren();addText(body,'p','プロジェクトが選択されていません');return;}
   let latest=null;
   try{latest=await loadLatest(pid);}
   catch(e){
    if(!isCurrent())return;
    body.replaceChildren();
    addText(body,'p','実行履歴未判定');
    return;
   }
   if(!isCurrent())return;
   if(latest&&latest.found==='unreadable'){
    body.replaceChildren();
    addText(body,'p','実行記録を読めません：'+String(latest.error||'確認できず'));
    addText(body,'p','完走・未達と区別する。成功・達成とは表示しない。');
    return;
   }
   renderRunCycle(body,latest,ctxFor(latest,pid));
  }
  const sel=document.getElementById('projectSelect');
  if(sel)sel.addEventListener('change',()=>{load();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)return;load();});
  window.reloadRunCycle=function(){return load();};
  load();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
function defaultRunCtx(latest,pid){
 const pidValue=String(pid||'');
 return {
  submitEvidence:(runId,openItemId,assignee,evidence,reason)=>postRunJson(pidValue,'/runs/'+encodeURIComponent(runId)+'/open-items/'+encodeURIComponent(openItemId)+'/evidence',{assignee:assignee,evidence:evidence,reason:reason}),
  startNext:async(runId,actor)=>{
   const checked=await postRunJson(pidValue,'/runs/next',{actor:String(actor||''),plan_signature:String((latest||{}).plan_signature||''),source_hash:String((latest||{}).source_hash||'')});
   if(!checked||checked.can_start!==true)throw Error(String((checked&&checked.reason&&checked.reason.message)||'次の実行を開始できません'));
   return postRunJson(pidValue,'/mission/start',{});
  },
  reload:()=>window.reloadRunCycle?window.reloadRunCycle():null,
 };
}
function mount(){
 function init(){
  let anchor=document.getElementById('workflowOverview');
  if(!anchor)anchor=document.getElementById('workflow');
  if(!anchor){setTimeout(init,300);return;}
  if(document.getElementById('goalStateSection'))return;
  const box=document.createElement('section');box.className='workflow-overview-item';box.id='goalStateSection';
  addText(box,'h3','自走の現在地');
  const body=document.createElement('div');body.id='goalStateBody';
  addText(body,'p','読込中…');
  box.append(body);
  anchor.append(box);
  let epoch=0;
  function selected(){return currentPid();}
  async function load(){
   const pid=selected();
   const ticket=++epoch;
   const isCurrent=()=>ticket===epoch;
   if(!pid){body.replaceChildren();addText(body,'p','プロジェクトが選択されていません');return;}
   try{
    const data=await apiGoalState(pid);
    if(!isCurrent())return;
    renderGoalState(body,data);
   }catch(e){
    if(!isCurrent())return;
    body.replaceChildren();
    addText(body,'p','自走の現在地を取得できませんでした: '+(e.message||'通信できませんでした'));
   }
  }
  const sel=document.getElementById('projectSelect');
  if(sel)sel.addEventListener('change',()=>{load();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)return;load();});
  window.reloadGoalState=function(){return load();};
  load();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
if(typeof document!=='undefined'){mount();mountRunCycle();}
if(typeof module!=='undefined')module.exports={STATE_JA,NEXT_JA,RUN_EXEC_JA,RUN_GOAL_JA,stateLabel,stateLabelJa,nextStepText,nextStepTitle,summaryText,shortSignature,preflightText,readOnlyText,externalSendsText,runExecutionText,runGoalText,runKindText,runOwnerText,runResponsibilityText,runBasisText,runAtDateLabel,runCountText,isGoalAchievedOnly,runStatusLine,openItemSummary,groupOpenItemsByResponsibility,responsibilityActionText,currentRunItems,renderRunCycle,renderRunActions,renderGoalState,fetchJson,apiGoalState,apiRunLatest,postRunJson,mountRunCycle,defaultRunCtx};
})();
