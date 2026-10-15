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
if(typeof document!=='undefined')mount();
if(typeof module!=='undefined')module.exports={STATE_JA,NEXT_JA,stateLabel,stateLabelJa,nextStepText,nextStepTitle,summaryText,shortSignature,preflightText,readOnlyText,externalSendsText,renderGoalState,fetchJson,apiGoalState};
})();
