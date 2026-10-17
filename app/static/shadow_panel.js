(()=>{'use strict';
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
function currentPid(){return (document.getElementById('projectSelect')||{}).value||'';}
const LABEL_JA={correct:'正しい',wrong:'誤り',unsure:'判断できない','':'未判定'};
function labelText(label){const s=String(label==null?'':label);if(Object.prototype.hasOwnProperty.call(LABEL_JA,s))return LABEL_JA[s];return '確認できず';}
function agreementText(value){if(value===null||value===undefined)return '一致率：測定できない';if(typeof value!=='number'||!isFinite(value))return '一致率：確認できず';return '一致率：'+String(Math.round(value*1000)/10)+'%';}
function gateSummaryText(gate){if(!gate||typeof gate!=='object')return '関門：確認できず';if(gate.passed===true)return '関門：合格';if(gate.passed===false){const reasons=Array.isArray(gate.reasons)?gate.reasons:[];if(!reasons.length)return '関門：未達（理由を確認できません）';return '関門：未達（'+reasons.map(String).join(' / ')+'）';}return '関門：確認できず';}
function recordsEmptyText(hasRecords){return hasRecords===true?'記録なし(0件)':'未更新（担当者名を入力して「今の予告を記録」を押してください）';}
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
function apiShadowObserve(pid,actor){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/shadow/observe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor})}).then(d=>{if(!d||d.ok!==true)throw Error('影の記録を確認できませんでした');return d;});}
function apiShadowRecords(pid,limit){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/shadow/records?limit='+encodeURIComponent(String(limit||50))).then(d=>{if(!d||!Array.isArray(d.records))throw Error('記録を確認できませんでした');return d;});}
function apiShadowLabel(recordId,actor,label,note,danger){return fetchJson('/api/shadow/records/'+encodeURIComponent(recordId)+'/label',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor,label:label,note:note||'',danger:!!danger})}).then(d=>{if(!d||d.ok!==true)throw Error('ラベルの記録を確認できませんでした');return d;});}
function apiShadowMetrics(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/shadow/metrics').then(d=>{if(!d||typeof d.count==='undefined')throw Error('集計を確認できませんでした');return d;});}
function apiShadowGate(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/shadow/gate').then(d=>{if(!d||typeof d.passed==='undefined')throw Error('関門を確認できませんでした');return d;});}
function apiEnableAutopilot(pid,actor,reason,overrideReason){const body={actor:actor,reason:reason};if(overrideReason)body.gate_override_reason=overrideReason;return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/autopilot/enable',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
function renderGate(host,gate,metrics){
 host.replaceChildren();
 if(!gate||typeof gate!=='object'){addText(host,'p','関門：確認できず(取得失敗)');return;}
 if(gate.passed===true){addText(host,'p','関門：合格','shadow-gate-pass');}
 else{addText(host,'p',gateSummaryText(gate),'shadow-gate-fail');}
 const data=metrics||gate.metrics||{};
 if(data&&typeof data==='object'&&typeof data.count!=='undefined'){
  addText(host,'p','記録：'+String(data.count)+'件 / 判定済み：'+String(data.judged_for_gate)+'件（正しい '+String(data.correct)+'件 / 誤り '+String(data.wrong)+'件）');
  addText(host,'p',agreementText(data.agreement)+' / 危険な誤提案：'+String(data.dangerous)+'件 / 計画署名：'+String(data.signatures)+'つ');
 }
 const thresholds=gate.thresholds||{};
 if(thresholds&&typeof thresholds==='object'&&typeof thresholds.min_judged!=='undefined'){
  addText(host,'p','基準：判定済み'+String(thresholds.min_judged)+'件以上・署名'+String(thresholds.min_signatures)+'つ以上・一致率95%以上・危険0件・30日以内');
 }
}
function renderRecords(host,records,hasRecords,labelHandler){
 host.replaceChildren();
 if(!Array.isArray(records)){addText(host,'p','確認できず(取得失敗)');return;}
 if(!records.length){addText(host,'p',recordsEmptyText(hasRecords===true));return;}
 const list=document.createElement('ul');
 for(const item of records){
  const row=item||{};
  const line=document.createElement('li');
  addText(line,'strong','予告：'+String(row.planned_action||'確認できず')+' / 停止：'+String(row.stop_class||'確認できず')+'/'+String(row.stop_code||'確認できず'));
  if(row.reason_ja)addText(line,'p',String(row.reason_ja));
  addText(line,'p','署名：'+String((row.plan_signature||'').slice(0,12)||'確認できず')+' / 時刻：'+formatTime(row.observed_at));
  addText(line,'p','ラベル：'+labelText(row.label)+(row.label_by?'（'+String(row.label_by)+'）':''));
  if(row.label==='wrong'&&Number(row.danger)===1)addText(line,'p','危険だったと判定されています');
  const controls=document.createElement('div');
  const labels=[['correct','正しい'],['wrong','誤り'],['unsure','判断できない']];
  for(const pair of labels){
   const btn=document.createElement('button');btn.type='button';btn.textContent=pair[1];
   btn.addEventListener('click',()=>{labelHandler(String(row.id||''),pair[0],btn);});
   controls.append(btn);
  }
  const danger=document.createElement('label');
  const check=document.createElement('input');check.type='checkbox';check.checked=Number(row.danger)===1;
  danger.append(check);
  const span=document.createElement('span');span.textContent='危険だった';
  danger.append(span);
  controls.append(danger);
  const note=document.createElement('input');note.maxLength=2000;note.placeholder='メモ';
  note.value=String(row.label_note||'');
  note.setAttribute('aria-label','メモ');
  controls.append(note);
  line.append(controls);
  list.append(line);
 }
 host.append(list);
}
function mount(){
 function init(){
  let anchor=document.getElementById('nightRunPanelSection');
  if(!anchor){
   anchor=document.getElementById('watchPanelSection');
   if(!anchor){
    let fallback=document.getElementById('goalStateSection');
    if(!fallback)fallback=document.getElementById('workflowOverview');
    if(!fallback)fallback=document.getElementById('workflow');
    if(!fallback){setTimeout(init,300);return;}
    anchor=fallback;
   }
  }
  if(document.getElementById('shadowPanelSection'))return;
  const box=document.createElement('section');box.className='workflow-overview-item';box.id='shadowPanelSection';
  addText(box,'h3','影モード');
  const gateBody=document.createElement('div');gateBody.id='shadowGateBody';
  addText(gateBody,'p','読込中…');box.append(gateBody);
  const controls=document.createElement('div');
  const actorInput=document.createElement('input');actorInput.id='shadowActor';actorInput.maxLength=100;actorInput.placeholder='担当者名';
  actorInput.setAttribute('aria-label','担当者名');
  controls.append(actorInput);
  const observeBtn=document.createElement('button');observeBtn.type='button';observeBtn.id='shadowObserveBtn';observeBtn.textContent='今の予告を記録';
  controls.append(observeBtn);
  const enableBtn=document.createElement('button');enableBtn.type='button';enableBtn.id='shadowEnableBtn';enableBtn.textContent='自走を有効化';
  controls.append(enableBtn);
  box.append(controls);
  const msg=document.createElement('p');msg.id='shadowMsg';box.append(msg);
  const recordBody=document.createElement('div');recordBody.id='shadowRecordBody';
  addText(recordBody,'p','読込中…');box.append(recordBody);
  const parent=anchor.parentNode;
  if(parent)parent.insertBefore(box,anchor.nextSibling);
  else anchor.append(box);
  let epoch=0;
  let lastGate=null;
  async function labelOne(recordId,label,btn){
   const actor=actorInput.value.trim();
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   const line=btn.parentNode;
   const noteInput=line?line.querySelector('input[placeholder="メモ"]'):null;
   const checkInput=line?line.querySelector('input[type="checkbox"]'):null;
   const note=noteInput?noteInput.value:'';
   const danger=checkInput?checkInput.checked:false;
   btn.disabled=true;
   try{
    await apiShadowLabel(recordId,actor,label,note,danger);
    await load();
   }catch(e){
    btn.disabled=false;
    msg.replaceChildren();
    addText(msg,'p','ラベルを付けられませんでした: '+(e.message||'通信できませんでした'));
   }
  }
  async function load(){
   const pid=currentPid();
   const ticket=++epoch;
   const isCurrent=()=>ticket===epoch;
   if(!pid){gateBody.replaceChildren();addText(gateBody,'p','プロジェクトが選択されていません');recordBody.replaceChildren();addText(recordBody,'p','プロジェクトが選択されていません');return;}
   try{
    const gate=await apiShadowGate(pid);
    if(!isCurrent())return;
    lastGate=gate;
    let metrics=null;
    try{
     metrics=await apiShadowMetrics(pid);
     if(!isCurrent())return;
    }catch(_metricsError){
     if(!isCurrent())return;
     metrics=null;
    }
    renderGate(gateBody,gate,metrics);
    try{
     const data=await apiShadowRecords(pid,50);
     if(!isCurrent())return;
     renderRecords(recordBody,data.records,true,labelOne);
    }catch(e){
     if(!isCurrent())return;
     recordBody.replaceChildren();
     addText(recordBody,'p','記録を取得できませんでした: '+(e.message||'通信できませんでした'));
    }
   }catch(e){
    if(!isCurrent())return;
    lastGate=null;
    gateBody.replaceChildren();
    addText(gateBody,'p','関門を取得できませんでした: '+(e.message||'通信できませんでした'));
    recordBody.replaceChildren();
    addText(recordBody,'p','確認できず(取得失敗)');
   }
  }
  observeBtn.addEventListener('click',async()=>{
   const pid=currentPid();
   const actor=actorInput.value.trim();
   if(!pid){msg.replaceChildren();addText(msg,'p','プロジェクトが選択されていません');return;}
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   observeBtn.disabled=true;
   msg.replaceChildren();
   addText(msg,'p','記録中…');
   try{
    const out=await apiShadowObserve(pid,actor);
    msg.replaceChildren();
    if(out.idempotent===true)addText(msg,'p','同じ予告のため追加しませんでした');
    else addText(msg,'p','記録しました');
    await load();
   }catch(e){
    msg.replaceChildren();
    addText(msg,'p','記録できませんでした: '+(e.message||'通信できませんでした'));
   }finally{
    observeBtn.disabled=false;
   }
  });
  enableBtn.addEventListener('click',async()=>{
   const pid=currentPid();
   const actor=actorInput.value.trim();
   if(!pid){msg.replaceChildren();addText(msg,'p','プロジェクトが選択されていません');return;}
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   enableBtn.disabled=true;
   msg.replaceChildren();
   addText(msg,'p','有効化中…');
   try{
    await apiEnableAutopilot(pid,actor,'影モードの関門を通過したため有効化します','');
    msg.replaceChildren();
    addText(msg,'p','自走を有効化しました');
    await load();
   }catch(e){
    if(Number(e.status)===409){
     const detail=String(e.message||'');
     msg.replaceChildren();
     addText(msg,'p','関門未達のため有効化できません: '+detail);
     const reason=window.prompt('関門が未達です。理由(10字以上)を入力すると関門を無視して有効化できます。空にすると中止します。','');
     if(reason!==null&&String(reason).trim().length>=10){
      try{
       await apiEnableAutopilot(pid,actor,'関門を無視して有効化します',String(reason).trim());
       msg.replaceChildren();
       addText(msg,'p','関門を無視して有効化しました');
       await load();
      }catch(e2){
       msg.replaceChildren();
       addText(msg,'p','有効化できませんでした: '+(e2.message||'通信できませんでした'));
      }finally{
       enableBtn.disabled=false;
      }
      return;
     }
     if(reason!==null){
      msg.replaceChildren();
      addText(msg,'p','理由が10字未満のため中止しました');
     }
    }else{
     msg.replaceChildren();
     addText(msg,'p','有効化できませんでした: '+(e.message||'通信できませんでした'));
    }
    enableBtn.disabled=false;
    await load();
    return;
   }
   enableBtn.disabled=false;
  });
  const sel=document.getElementById('projectSelect');
  if(sel)sel.addEventListener('change',()=>{load();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)return;load();});
  window.reloadShadowPanel=function(){return load();};
  load();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
if(typeof document!=='undefined')mount();
if(typeof module!=='undefined')module.exports={LABEL_JA,labelText,agreementText,gateSummaryText,recordsEmptyText,formatTime,renderGate,renderRecords,fetchJson,apiShadowObserve,apiShadowRecords,apiShadowLabel,apiShadowMetrics,apiShadowGate,apiEnableAutopilot};
})();
