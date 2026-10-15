(()=>{'use strict';
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
const SEVERITY_JA={info:'お知らせ',attention:'注意',alert:'要確認'};
const KIND_JA={stop_appeared:'停止の出現',stop_changed:'停止の変化',stop_cleared:'停止の解消',criteria_progress:'達成条件の変化',autopilot_failed:'自走の失敗',read_failed:'読み取り不能',read_recovered:'読み取り回復',new_project:'新規PJ',project_removed:'PJの消失'};
const DECISION_KIND_JA={goal_plan_create:'目標と計画の作成',plan_approval:'計画承認',issue_binding:'指摘の採否',fact_confirm:'業務事実の確認',proposal_confirm:'不足機能の提案の確認',instruction_output:'指示の出力',evidence_confirm:'証拠の確認',unknown_confirm:'確認できず'};
function severityText(severity){const s=String(severity||'');if(Object.prototype.hasOwnProperty.call(SEVERITY_JA,s))return SEVERITY_JA[s];return '確認できず';}
function kindText(kind){const s=String(kind||'');if(Object.prototype.hasOwnProperty.call(KIND_JA,s))return KIND_JA[s];return '確認できず';}
function decisionKindText(kind){const s=String(kind||'');if(Object.prototype.hasOwnProperty.call(DECISION_KIND_JA,s))return DECISION_KIND_JA[s];return '確認できず';}
function unreadText(count){if(typeof count==='number'&&isFinite(count))return '未読 '+String(count)+'件';return '未読：確認できず';}
function eventsEmptyText(hasSnapshot){return hasSnapshot===true?'通知なし(0件)':'未更新（担当者名を入力して「今すぐ更新」を押してください）';}
function formatTime(epoch){if(typeof epoch!=='number'||!isFinite(epoch)||epoch<=0)return '確認できず';const d=new Date(epoch*1000);if(isNaN(d.getTime()))return '確認できず';return d.toLocaleString('ja-JP');}
function lastRefreshText(epoch){if(typeof epoch!=='number'||!isFinite(epoch)||epoch<=0)return '最終更新：確認できず';return '最終更新：'+formatTime(epoch);}
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
function apiWatchStatus(){return fetchJson('/api/watch/status').then(d=>{if(!d||typeof d.unread_count==='undefined'||typeof d.last_refresh_at==='undefined')throw Error('見張りの状態を確認できませんでした');return d;});}
function apiWatchEvents(unread,limit){const q='/api/watch/events?unread='+(unread?'1':'0')+'&limit='+encodeURIComponent(String(limit||50));return fetchJson(q).then(d=>{if(!d||!Array.isArray(d.events))throw Error('通知を確認できませんでした');return d;});}
function apiWatchDecisions(){return fetchJson('/api/watch/decisions').then(d=>{if(!d||!Array.isArray(d.decisions))throw Error('判断待ちを確認できませんでした');return d;});}
function apiWatchRefresh(actor){return fetchJson('/api/watch/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor})}).then(d=>{if(!d||d.ok!==true)throw Error('見張りの更新を確認できませんでした');return d;});}
function apiWatchAck(eventId,actor,note){return fetchJson('/api/watch/events/'+encodeURIComponent(eventId)+'/ack',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor,note:note||''})}).then(d=>{if(!d||d.ok!==true)throw Error('既読の記録を確認できませんでした');return d;});}
function renderEvents(host,events,hasSnapshot,ackHandler){
 host.replaceChildren();
 if(!Array.isArray(events)){addText(host,'p','確認できず(取得失敗)');return;}
 if(!events.length){addText(host,'p',eventsEmptyText(hasSnapshot));return;}
 const list=document.createElement('ul');
 for(const item of events){
  const row=item||{};
  const line=document.createElement('li');
  const head=String(severityText(row.severity))+' / '+String(kindText(row.kind));
  addText(line,'strong',head);
  addText(line,'p','PJ：'+String(row.project_id||'確認できず'));
  if(row.summary_ja)addText(line,'p',String(row.summary_ja));
  addText(line,'p','時刻：'+formatTime(row.created_at));
  if(row.ack_by)addText(line,'p','既読：'+String(row.ack_by));
  else{
   const btn=document.createElement('button');btn.type='button';btn.textContent='既読にする';
   btn.addEventListener('click',()=>{ackHandler(String(row.id||''),btn);});
   line.append(btn);
  }
  list.append(line);
 }
 host.append(list);
}
function renderDecisions(host,decisions){
 host.replaceChildren();
 if(!Array.isArray(decisions)){addText(host,'p','確認できず(取得失敗)');return;}
 if(!decisions.length){addText(host,'p','通知なし(0件)');return;}
 const groups={};
 for(const item of decisions){
  const row=item||{};
  const kind=String(row.kind||'unknown_confirm');
  if(!groups[kind])groups[kind]=[];
  groups[kind].push(row);
 }
 const kinds=Object.keys(groups).sort();
 for(const kind of kinds){
  const details=document.createElement('details');
  if(kind==='unknown_confirm')details.open=true;
  addText(details,'summary',decisionKindText(kind)+'（'+String(groups[kind].length)+'件）');
  const list=document.createElement('ul');
  for(const row of groups[kind]){
   const line=document.createElement('li');
   addText(line,'strong','PJ：'+String(row.project_name||row.project_id||'確認できず'));
   addText(line,'p','判断：'+String(row.what_ja||'確認できず'));
   addText(line,'p','画面：'+String(row.screen||'確認できず'));
   if(row.since)addText(line,'p','いつから：'+String(row.since));
   list.append(line);
  }
  details.append(list);
  host.append(details);
 }
}
function mount(){
 function init(){
  let anchor=document.getElementById('goalStateSection');
  if(!anchor){
   let fallback=document.getElementById('workflowOverview');
   if(!fallback)fallback=document.getElementById('workflow');
   if(!fallback){setTimeout(init,300);return;}
   anchor=fallback;
  }
  if(document.getElementById('watchPanelSection'))return;
  const box=document.createElement('section');box.className='workflow-overview-item';box.id='watchPanelSection';
  addText(box,'h3','見張りの通知');
  const statusLine=document.createElement('p');statusLine.id='watchStatusLine';
  addText(statusLine,'span','読込中…');box.append(statusLine);
  const controls=document.createElement('div');
  const actorInput=document.createElement('input');actorInput.id='watchActor';actorInput.maxLength=100;actorInput.placeholder='担当者名';
  actorInput.setAttribute('aria-label','担当者名');
  controls.append(actorInput);
  const refreshBtn=document.createElement('button');refreshBtn.type='button';refreshBtn.id='watchRefreshBtn';refreshBtn.textContent='今すぐ更新';
  controls.append(refreshBtn);
  box.append(controls);
  const refreshMsg=document.createElement('p');refreshMsg.id='watchRefreshMsg';box.append(refreshMsg);
  const unreadLine=document.createElement('p');unreadLine.id='watchUnreadLine';box.append(unreadLine);
  const eventBody=document.createElement('div');eventBody.id='watchEventBody';
  addText(eventBody,'p','読込中…');box.append(eventBody);
  addText(box,'h3','判断待ち一覧');
  const decisionBody=document.createElement('div');decisionBody.id='watchDecisionBody';
  addText(decisionBody,'p','読込中…');box.append(decisionBody);
  anchor.append(box);
  let epoch=0;
  async function ackOne(eventId,btn){
   const actor=actorInput.value.trim();
   if(!actor){refreshMsg.replaceChildren();addText(refreshMsg,'p','担当者名を入力してください');return;}
   btn.disabled=true;
   try{
    await apiWatchAck(eventId,actor,'');
    await load();
   }catch(e){
    btn.disabled=false;
    refreshMsg.replaceChildren();
    addText(refreshMsg,'p','既読にできませんでした: '+(e.message||'通信できませんでした'));
   }
  }
  async function load(){
   const ticket=++epoch;
   const isCurrent=()=>ticket===epoch;
   try{
    const status=await apiWatchStatus();
    if(!isCurrent())return;
    statusLine.replaceChildren();
    addText(statusLine,'span',lastRefreshText(status.last_refresh_at));
    unreadLine.replaceChildren();
    addText(unreadLine,'span',unreadText(status.unread_count));
    if(typeof status.unreadable_projects==='number'&&status.unreadable_projects>0){
     addText(unreadLine,'p','読み取れないPJが'+String(status.unreadable_projects)+'件あります');
    }
    try{
     const data=await apiWatchEvents(false,50);
     if(!isCurrent())return;
     renderEvents(eventBody,data.events,status.has_snapshot===true,ackOne);
    }catch(e){
     if(!isCurrent())return;
     eventBody.replaceChildren();
     addText(eventBody,'p','通知を取得できませんでした: '+(e.message||'通信できませんでした'));
    }
    try{
     const dd=await apiWatchDecisions();
     if(!isCurrent())return;
     renderDecisions(decisionBody,dd.decisions);
    }catch(e){
     if(!isCurrent())return;
     decisionBody.replaceChildren();
     addText(decisionBody,'p','判断待ちを取得できませんでした: '+(e.message||'通信できませんでした'));
    }
   }catch(e){
    if(!isCurrent())return;
    statusLine.replaceChildren();
    addText(statusLine,'span','見張りの状態を取得できませんでした: '+(e.message||'通信できませんでした'));
    eventBody.replaceChildren();
    addText(eventBody,'p','確認できず(取得失敗)');
    decisionBody.replaceChildren();
    addText(decisionBody,'p','確認できず(取得失敗)');
   }
  }
  refreshBtn.addEventListener('click',async()=>{
   const actor=actorInput.value.trim();
   if(!actor){refreshMsg.replaceChildren();addText(refreshMsg,'p','担当者名を入力してください');return;}
   refreshBtn.disabled=true;
   refreshMsg.replaceChildren();
   addText(refreshMsg,'p','更新中…');
   try{
    const out=await apiWatchRefresh(actor);
    refreshMsg.replaceChildren();
    addText(refreshMsg,'p','更新しました（新規'+String(out.new_events)+'件、全'+String(out.total_projects)+'件）');
    await load();
   }catch(e){
    refreshMsg.replaceChildren();
    addText(refreshMsg,'p','更新できませんでした: '+(e.message||'通信できませんでした'));
   }finally{
    refreshBtn.disabled=false;
   }
  });
  document.addEventListener('visibilitychange',()=>{if(document.hidden)return;load();});
  window.reloadWatchPanel=function(){return load();};
  load();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
if(typeof document!=='undefined')mount();
if(typeof module!=='undefined')module.exports={SEVERITY_JA,KIND_JA,DECISION_KIND_JA,severityText,severityTextJa:severityText,kindText,decisionKindText,unreadText,eventsEmptyText,formatTime,lastRefreshText,renderEvents,renderDecisions,fetchJson,apiWatchStatus,apiWatchEvents,apiWatchDecisions,apiWatchRefresh,apiWatchAck};
})();
