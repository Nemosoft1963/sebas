(()=>{'use strict';
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
const RUN_STATE_JA={running:'実行中',finished:'終了',aborted:'中止',killed:'全体停止で終了',expired:'heartbeat途絶で終了'};
const KILL_STATE_JA={active:'有効（停止中）',inactive:'無効',unknown:'確認できず'};
function runStateText(state){const s=String(state||'');if(Object.prototype.hasOwnProperty.call(RUN_STATE_JA,s))return RUN_STATE_JA[s];return '確認できず';}
function killStateText(state){const s=String(state||'');if(Object.prototype.hasOwnProperty.call(KILL_STATE_JA,s))return KILL_STATE_JA[s];return '確認できず';}
function latestEmptyText(hasRun){return hasRun===true?'報告なし':'実行なし';}
function formatTime(epoch){if(typeof epoch!=='number'||!isFinite(epoch)||epoch<=0)return '確認できず';const d=new Date(epoch*1000);if(isNaN(d.getTime()))return '確認できず';return d.toLocaleString('ja-JP');}
function outcomeText(row){const outcome=String((row||{}).outcome||'');if(outcome==='進んだ'||outcome==='止まった'||outcome==='失敗')return outcome;return '確認できず';}
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
function apiNightLatest(){return fetchJson('/api/night-run/latest').then(d=>{if(!d||typeof d.has_run==='undefined')throw Error('夜間実行の状態を確認できませんでした');return d;});}
function apiKillState(){return fetchJson('/api/autopilot/kill-switch').then(d=>{if(!d||typeof d.state==='undefined')throw Error('キルスイッチの状態を確認できませんでした');return d;});}
function apiDisableAll(actor){return fetchJson('/api/autopilot/disable-all',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor})}).then(d=>{if(!d||typeof d.ok==='undefined')throw Error('全PJの自走停止を確認できませんでした');return d;});}
function apiKillRelease(actor){return fetchJson('/api/autopilot/kill-switch/release',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor})}).then(d=>{if(!d||typeof d.state==='undefined')throw Error('キルスイッチの解除を確認できませんでした');return d;});}
function renderLatest(host,data){
 host.replaceChildren();
 if(!data||typeof data!=='object'){addText(host,'p','確認できず(取得失敗)');return;}
 if(data.has_run!==true){addText(host,'p',latestEmptyText(false));return;}
 addText(host,'p','状態：'+runStateText(data.state));
 addText(host,'p','開始：'+formatTime(data.started_at));
 addText(host,'p','終了：'+formatTime(data.finished_at));
 const report=data.report;
 if(!report||typeof report!=='object'){addText(host,'p','報告なし');return;}
 addText(host,'p','PJ：'+String(report.total_projects)+'件 / ティック：'+String(report.total_ticks)+'件');
 if(report.end_reason)addText(host,'p','終了理由：'+String(report.end_reason));
 if(report.watch_note)addText(host,'p','通知：'+String(report.watch_note));
 const rows=Array.isArray(report.per_project)?report.per_project:[];
 if(!rows.length){addText(host,'p','PJごとの結果：確認できず');}
 else{
  const list=document.createElement('ul');
  for(const row of rows){
   const item=row||{};
   const line=document.createElement('li');
   addText(line,'strong','PJ：'+String(item.project_id||'確認できず')+'（'+outcomeText(item)+'）');
   addText(line,'p','ティック：'+String(item.ticks)+'件 / 最終：'+String(item.last_status||'確認できず'));
   addText(line,'p','次の操作：'+String(item.next_action||'確認できず'));
   list.append(line);
  }
  host.append(list);
 }
 const next=Array.isArray(report.next_actions)?report.next_actions:[];
 if(next.length){
  addText(host,'p','人の次の操作：');
  const actions=document.createElement('ul');
  for(const text of next){
   const line=document.createElement('li');
   addText(line,'span',String(text||'確認できず'));
   actions.append(line);
  }
  host.append(actions);
 }
 const external='外部呼出/送信/承認：'+String(report.external_calls_total)+'件/'+String(report.external_rounds_total)+'件/0件';
 addText(host,'p',external);
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
  if(document.getElementById('nightRunPanelSection'))return;
  const box=document.createElement('section');box.className='workflow-overview-item';box.id='nightRunPanelSection';
  addText(box,'h3','夜間実行');
  const killLine=document.createElement('p');killLine.id='nightRunKillLine';
  addText(killLine,'span','キルスイッチ：読込中…');box.append(killLine);
  const controls=document.createElement('div');
  const actorInput=document.createElement('input');actorInput.id='nightRunActor';actorInput.maxLength=100;actorInput.placeholder='担当者名';
  actorInput.setAttribute('aria-label','担当者名');
  controls.append(actorInput);
  const stopBtn=document.createElement('button');stopBtn.type='button';stopBtn.id='nightRunStopBtn';stopBtn.textContent='全PJの自走を止める';
  controls.append(stopBtn);
  const releaseBtn=document.createElement('button');releaseBtn.type='button';releaseBtn.id='nightRunReleaseBtn';releaseBtn.textContent='キルスイッチを解除';
  controls.append(releaseBtn);
  box.append(controls);
  const msg=document.createElement('p');msg.id='nightRunMsg';box.append(msg);
  const body=document.createElement('div');body.id='nightRunBody';
  addText(body,'p','読込中…');box.append(body);
  const parent=anchor.parentNode;
  if(parent)parent.insertBefore(box,anchor.nextSibling);
  else anchor.append(box);
  let epoch=0;
  async function load(){
   const ticket=++epoch;
   const isCurrent=()=>ticket===epoch;
   try{
    const kill=await apiKillState();
    if(!isCurrent())return;
    killLine.replaceChildren();
    addText(killLine,'span','キルスイッチ：'+killStateText(kill.state));
   }catch(e){
    if(!isCurrent())return;
    killLine.replaceChildren();
    addText(killLine,'span','キルスイッチ：確認できず（'+(e.message||'通信できませんでした')+'）');
   }
   try{
    const data=await apiNightLatest();
    if(!isCurrent())return;
    renderLatest(body,data);
   }catch(e){
    if(!isCurrent())return;
    body.replaceChildren();
    addText(body,'p','夜間実行を取得できませんでした: '+(e.message||'通信できませんでした'));
   }
  }
  stopBtn.addEventListener('click',async()=>{
   const actor=actorInput.value.trim();
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   if(!window.confirm('全PJの自走を止めます。よろしいですか？'))return;
   stopBtn.disabled=true;
   releaseBtn.disabled=true;
   msg.replaceChildren();
   addText(msg,'p','停止中…');
   try{
    const out=await apiDisableAll(actor);
    msg.replaceChildren();
    if(out.ok===true)addText(msg,'p','全PJの自走を止めました（'+String((out.disabled||[]).length)+'件）');
    else addText(msg,'p','一部に失敗があります。確認してください');
    await load();
   }catch(e){
    msg.replaceChildren();
    addText(msg,'p','停止できませんでした: '+(e.message||'通信できませんでした'));
   }finally{
    stopBtn.disabled=false;
    releaseBtn.disabled=false;
   }
  });
  releaseBtn.addEventListener('click',async()=>{
   const actor=actorInput.value.trim();
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   releaseBtn.disabled=true;
   stopBtn.disabled=true;
   msg.replaceChildren();
   addText(msg,'p','解除中…');
   try{
    const out=await apiKillRelease(actor);
    msg.replaceChildren();
    addText(msg,'p','キルスイッチ：'+killStateText(out.state));
    await load();
   }catch(e){
    msg.replaceChildren();
    addText(msg,'p','解除できませんでした: '+(e.message||'通信できませんでした'));
   }finally{
    releaseBtn.disabled=false;
    stopBtn.disabled=false;
   }
  });
  document.addEventListener('visibilitychange',()=>{if(document.hidden)return;load();});
  window.reloadNightRunPanel=function(){return load();};
  load();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
if(typeof document!=='undefined')mount();
if(typeof module!=='undefined')module.exports={runStateText,killStateText,latestEmptyText,formatTime,outcomeText,renderLatest,fetchJson,apiNightLatest,apiKillState,apiDisableAll,apiKillRelease};
})();
