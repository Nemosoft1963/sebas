(()=>{'use strict';
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
const METRIC_JA={unknown_passed:'判定不能の通過',duplicate_proposals:'重複提案',unauthorized_external:'無許可の外部作用',false_achieved:'目標の誤達成',stop_to_action_rate:'停止から次操作への到達率',criteria_fulfilment:'達成条件の充足率'};
const SAFETY_KEYS=['unknown_passed','duplicate_proposals','unauthorized_external','false_achieved'];
function metricText(key){const s=String(key||'');if(Object.prototype.hasOwnProperty.call(METRIC_JA,s))return METRIC_JA[s];return '確認できず';}
function formatCount(item){if(!item||typeof item!=='object')return '確認できず';if(item.status!=='ok')return '確認できず';if(typeof item.value!=='number'||!isFinite(item.value))return '確認できず';return String(item.value)+'件';}
function metricLineText(key,item){if(!item||typeof item!=='object')return metricText(key)+'：確認できず';if(item.status!=='ok')return metricText(key)+'：確認できず';if(typeof item.value!=='number'||!isFinite(item.value))return metricText(key)+'：確認できず';if(SAFETY_KEYS.indexOf(String(key))>=0){if(item.value===0)return metricText(key)+'：0件（問題なし）';return metricText(key)+'：'+String(item.value)+'件（要確認）';}return metricText(key)+'：'+String(item.value);}
function rateText(item){if(!item||typeof item!=='object')return '測定できない';if(item.status!=='ok')return '確認できず';if(item.value===null||item.value===undefined)return '測定できない';if(typeof item.value!=='number'||!isFinite(item.value))return '確認できず';return String(Math.round(item.value*1000)/10)+'%';}
function activityText(activity){if(!activity||typeof activity!=='object')return '活動量：確認できず';const parts=[];const ticks=activity.ticks;const steps=activity.night_run_steps;const shadow=activity.shadow_records;parts.push('ティック：'+((typeof ticks==='number'&&isFinite(ticks))?String(ticks)+'件':'確認できず'));parts.push('夜間実行：'+((typeof steps==='number'&&isFinite(steps))?String(steps)+'件':'確認できず'));parts.push('影モード：'+((typeof shadow==='number'&&isFinite(shadow))?String(shadow)+'件':'確認できず'));return '活動量（参考）：'+parts.join(' / ');}
function decisionText(decision){const s=String(decision||'');if(s==='adopted')return '採用';if(s==='rejected')return '却下';if(s==='deferred')return '保留';return '確認できず';}
function formatTime(epoch){if(typeof epoch!=='number'||!isFinite(epoch)||epoch<=0)return '確認できず';const d=new Date(epoch*1000);if(isNaN(d.getTime()))return '確認できず';return d.toLocaleString('ja-JP');}
async function fetchJson(url,options){
 let r=null;
 try{r=await fetch(url,options);}catch(_netError){throw Error('通信できませんでした');}
 let d=null;
 try{d=await r.json();}catch(_parseError){throw Error('応答を読み取れませんでした');}
 if(!r.ok){
  const detail=typeof d.detail==='string'?d.detail:String((d&&d.detail&&d.detail.message)||('HTTP '+r.status));
  const err=Error(detail||('HTTP '+r.status));err.status=r.status;throw err;
 }
 return d;
}
function apiLedgerMetrics(){return fetchJson('/api/ledger/metrics').then(d=>{if(!d||typeof d.unknown_passed==='undefined')throw Error('成果台帳の指標を確認できませんでした');return d;});}
function apiLedgerSnapshot(actor){return fetchJson('/api/ledger/snapshot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor})}).then(d=>{if(!d||d.ok!==true)throw Error('台帳の記録を確認できませんでした');return d;});}
function apiLedgerSnapshots(limit){return fetchJson('/api/ledger/snapshots?limit='+encodeURIComponent(String(limit||5))).then(d=>{if(!d||!Array.isArray(d.snapshots))throw Error('記録を確認できませんでした');return d;});}
function apiLedgerSuggestions(){return fetchJson('/api/ledger/suggestions').then(d=>{if(!d||!Array.isArray(d.suggestions))throw Error('提案を確認できませんでした');return d;});}
function apiLedgerDecision(suggestionId,actor,decision,reason){return fetchJson('/api/ledger/suggestions/'+encodeURIComponent(suggestionId)+'/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor,decision:decision,reason:reason})}).then(d=>{if(!d||d.ok!==true)throw Error('採否の記録を確認できませんでした');return d;});}
function renderMetrics(host,metrics){
 host.replaceChildren();
 if(!metrics||typeof metrics!=='object'){addText(host,'p','確認できず(取得失敗)');return;}
 const order=['unknown_passed','duplicate_proposals','unauthorized_external','false_achieved','stop_to_action_rate','criteria_fulfilment'];
 const list=document.createElement('ul');
 for(const key of order){
  const item=metrics[key]||{};
  const line=document.createElement('li');
  const head=metricText(key);
  if(SAFETY_KEYS.indexOf(key)>=0){
   if(item.status!=='ok')addText(line,'strong',head+'：確認できず','ledger-metric-unknown');
   else if(item.value===0)addText(line,'strong',head+'：0件（問題なし）','ledger-metric-ok');
   else addText(line,'strong',head+'：'+String(item.value)+'件（要確認）','ledger-metric-warn');
  }else if(key==='stop_to_action_rate'||key==='criteria_fulfilment'){
   addText(line,'strong',head+'：'+rateText(item));
  }else{
   addText(line,'strong',metricLineText(key,item));
  }
  if(item.status!=='ok'){
   addText(line,'p','確認できず：読み取れないPJがあります');
   const unread=Array.isArray(item.unreadable_projects)?item.unreadable_projects:[];
   if(unread.length)addText(line,'p','読めないPJ：'+unread.slice(0,5).join('、')+(unread.length>5?'（他'+String(unread.length-5)+'件）':''));
  }else if(key==='stop_to_action_rate'||key==='criteria_fulfilment'){
   const detail=item.detail||{};
   if(typeof detail.numerator!=='undefined')addText(line,'p','内訳：'+String(detail.numerator)+' / '+String(detail.denominator));
   if(typeof detail.total!=='undefined')addText(line,'p','内訳：達成'+String(detail.achieved)+'件 / 全'+String(detail.total)+'件');
  }
  list.append(line);
 }
 host.append(list);
 const activity=metrics.activity||{};
 const small=document.createElement('p');small.className='ledger-activity';
 addText(small,'span',activityText(activity));
 host.append(small);
}
function renderSnapshots(host,data){
 host.replaceChildren();
 if(!data||typeof data!=='object'){addText(host,'p','確認できず(取得失敗)');return;}
 const rows=Array.isArray(data.snapshots)?data.snapshots:[];
 if(!rows.length){addText(host,'p','記録なし（担当者名を入力して「今の指標を台帳に記録」を押してください）');return;}
 const first=rows[0]||{};
 addText(host,'p','直近：'+formatTime(first.created_at)+' / 担当：'+String(first.actor||'確認できず')+' / 安全：'+(first.safety_ok===true?'問題なし':'要確認または確認できず'));
 const diff=data.diff||{};
 const keys=Object.keys(diff).sort();
 if(keys.length){
  const list=document.createElement('ul');
  for(const key of keys){
   const row=diff[key]||{};
   const line=document.createElement('li');
   if(row.delta===null||row.delta===undefined)addText(line,'span',metricText(key)+'：差は測定できない');
   else addText(line,'span',metricText(key)+'：前回との差 '+(Number(row.delta)>0?'+':'')+String(row.delta));
   list.append(line);
  }
  host.append(list);
 }
}
function renderSuggestions(host,suggestions,decisionHandler){
 host.replaceChildren();
 if(!Array.isArray(suggestions)){addText(host,'p','確認できず(取得失敗)');return;}
 if(!suggestions.length){addText(host,'p','提案なし(0件)');return;}
 const list=document.createElement('ul');
 for(const item of suggestions){
  const row=item||{};
  const line=document.createElement('li');
  addText(line,'strong',String(row.title_ja||'確認できず'));
  const evidence=row.evidence||{};
  if(typeof evidence.count!=='undefined')addText(line,'p','件数：'+String(evidence.count)+'件');
  const pids=Array.isArray(evidence.project_ids)?evidence.project_ids:[];
  if(pids.length)addText(line,'p','PJ：'+pids.slice(0,5).join('、')+(pids.length>5?'（他'+String(pids.length-5)+'件）':''));
  if(row.suggested_human_action)addText(line,'p','人の操作：'+String(row.suggested_human_action));
  addText(line,'p','自動変更：なし');
  const controls=document.createElement('div');
  const pairs=[['adopted','採用'],['rejected','却下'],['deferred','保留']];
  for(const pair of pairs){
   const btn=document.createElement('button');btn.type='button';btn.textContent=pair[1];
   btn.addEventListener('click',()=>{decisionHandler(String(row.id||''),pair[0],btn);});
   controls.append(btn);
  }
  const reason=document.createElement('input');reason.maxLength=2000;reason.placeholder='理由';
  reason.value='';
  reason.setAttribute('aria-label','理由');
  controls.append(reason);
  line.append(controls);
  list.append(line);
 }
 host.append(list);
}
function mount(){
 function init(){
  let anchor=document.getElementById('watchPanelSection');
  if(!anchor){
   let fallback=document.getElementById('goalStateSection');
   if(!fallback)fallback=document.getElementById('workflowOverview');
   if(!fallback)fallback=document.getElementById('workflow');
   if(!fallback){setTimeout(init,300);return;}
   anchor=fallback;
  }
  if(document.getElementById('ledgerPanelSection'))return;
  const box=document.createElement('section');box.className='workflow-overview-item';box.id='ledgerPanelSection';
  addText(box,'h3','成果台帳');
  const metricBody=document.createElement('div');metricBody.id='ledgerMetricBody';
  addText(metricBody,'p','読込中…');box.append(metricBody);
  const controls=document.createElement('div');
  const actorInput=document.createElement('input');actorInput.id='ledgerActor';actorInput.maxLength=100;actorInput.placeholder='担当者名';
  actorInput.setAttribute('aria-label','担当者名');
  controls.append(actorInput);
  const recordBtn=document.createElement('button');recordBtn.type='button';recordBtn.id='ledgerRecordBtn';recordBtn.textContent='今の指標を台帳に記録';
  controls.append(recordBtn);
  box.append(controls);
  const msg=document.createElement('p');msg.id='ledgerMsg';box.append(msg);
  const snapshotBody=document.createElement('div');snapshotBody.id='ledgerSnapshotBody';
  addText(snapshotBody,'p','読込中…');box.append(snapshotBody);
  addText(box,'h3','方針の改善提案');
  const suggestionBody=document.createElement('div');suggestionBody.id='ledgerSuggestionBody';
  addText(suggestionBody,'p','読込中…');box.append(suggestionBody);
  const parent=anchor.parentNode;
  if(parent)parent.insertBefore(box,anchor.nextSibling);
  else anchor.append(box);
  let epoch=0;
  async function decideOne(suggestionId,decision,btn){
   const actor=actorInput.value.trim();
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   const line=btn.parentNode;
   const reasonInput=line?line.querySelector('input[placeholder="理由"]'):null;
   const reason=reasonInput?reasonInput.value.trim():'';
   if(!reason){msg.replaceChildren();addText(msg,'p','理由を入力してください');return;}
   btn.disabled=true;
   try{
    await apiLedgerDecision(suggestionId,actor,decision,reason);
    msg.replaceChildren();
    addText(msg,'p','採否を記録しました（'+decisionText(decision)+'）');
    await load();
   }catch(e){
    btn.disabled=false;
    msg.replaceChildren();
    addText(msg,'p','採否を記録できませんでした: '+(e.message||'通信できませんでした'));
   }
  }
  async function load(){
   const ticket=++epoch;
   const isCurrent=()=>ticket===epoch;
   try{
    const metrics=await apiLedgerMetrics();
    if(!isCurrent())return;
    renderMetrics(metricBody,metrics);
   }catch(e){
    if(!isCurrent())return;
    metricBody.replaceChildren();
    addText(metricBody,'p','指標を取得できませんでした: '+(e.message||'通信できませんでした'));
   }
   try{
    const data=await apiLedgerSnapshots(5);
    if(!isCurrent())return;
    renderSnapshots(snapshotBody,data);
   }catch(e){
    if(!isCurrent())return;
    snapshotBody.replaceChildren();
    addText(snapshotBody,'p','記録を取得できませんでした: '+(e.message||'通信できませんでした'));
   }
   try{
    const data=await apiLedgerSuggestions();
    if(!isCurrent())return;
    renderSuggestions(suggestionBody,data.suggestions,decideOne);
   }catch(e){
    if(!isCurrent())return;
    suggestionBody.replaceChildren();
    addText(suggestionBody,'p','提案を取得できませんでした: '+(e.message||'通信できませんでした'));
   }
  }
  recordBtn.addEventListener('click',async()=>{
   const actor=actorInput.value.trim();
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   recordBtn.disabled=true;
   msg.replaceChildren();
   addText(msg,'p','記録中…');
   try{
    const out=await apiLedgerSnapshot(actor);
    msg.replaceChildren();
    if(out.idempotent===true)addText(msg,'p','同じ内容のため追加しませんでした');
    else addText(msg,'p','記録しました');
    await load();
   }catch(e){
    msg.replaceChildren();
    addText(msg,'p','記録できませんでした: '+(e.message||'通信できませんでした'));
   }finally{
    recordBtn.disabled=false;
   }
  });
  document.addEventListener('visibilitychange',()=>{if(document.hidden)return;load();});
  window.reloadLedgerPanel=function(){return load();};
  load();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
if(typeof document!=='undefined')mount();
if(typeof module!=='undefined')module.exports={METRIC_JA,metricText,metricLineText,rateText,activityText,decisionText,formatCount,formatTime,renderMetrics,renderSnapshots,renderSuggestions,fetchJson,apiLedgerMetrics,apiLedgerSnapshot,apiLedgerSnapshots,apiLedgerSuggestions,apiLedgerDecision};
})();
