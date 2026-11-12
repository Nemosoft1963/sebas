(()=>{'use strict';
function addText(parent,tag,text){const node=document.createElement(tag);node.textContent=(text==null?'':String(text));parent.append(node);return node;}
function currentPid(){return (document.getElementById('projectSelect')||{}).value||'';}
const PURPOSES=['plan_generation','plan_review','capability_review','failover_review','research'];
const PURPOSE_JA={plan_generation:'計画の作成',plan_review:'計画の評価',capability_review:'機能の評価',failover_review:'別AI再調整',research:'調査'};
function purposeText(v){const s=String(v||'');if(Object.prototype.hasOwnProperty.call(PURPOSE_JA,s))return PURPOSE_JA[s];return '確認できず';}
const PUBLIC_FIELDS=['goal','success_criteria','constraints'];
const PREVIEW_DETAIL_JA={unknown_purpose:'用途が正しくありません',provider_required:'提供元を選んでください',invalid_binding:'計画の確認に失敗しました',unknown_field:'送る項目が正しくありません',invalid_item:'送る内容を確認してください',no_public_items:'送る内容を入力してください',check_error:'確認に失敗しました'};
function previewErrorText(status,detail){if(status===422){const s=String(detail||'');if(Object.prototype.hasOwnProperty.call(PREVIEW_DETAIL_JA,s))return PREVIEW_DETAIL_JA[s];return '内容を確認できませんでした';}return '内容を確認できませんでした。入力は保存・送信されていません';}
function previewUnknownErrorText(){return '内容を確認できませんでした。入力は保存・送信されていません';}
const BLOCKED_DETECT=['secret','email','abs_path','url_query','hash_like','blocked_term','unusable_input','phone','postal_code','address','person_name','bank_account','amount_detail','long_number','encoded_blob','source_quote'];
function blockedReasonText(code){const s=String(code||'');if(!s)return '確認できず';if(s==='check_error')return '検査に失敗(安全のため止めています)';if(BLOCKED_DETECT.indexOf(s)>=0)return '送れない内容を検出しました';if(s.indexOf('planning_')===0||s.indexOf('outbound_')===0)return '送れない内容を検出しました';return '確認できず';}
const APPROVE_DETAIL_JA={plan_changed:'計画が変わったため承認できません(もう一度内容を確認してください)',packet_tampered:'内容が変わっているため承認できません',privacy_recheck_failed:'再検査で送れない内容が見つかりました',approver_required:'承認者名を入力してください',packet_not_found:'この送付内容は承認できません',packet_not_previewed:'この送付内容は承認できません',method_not_external:'計画方式が「外部AIあり」ではないため承認できません',provider_mismatch:'設定された提供元と一致しないため承認できません',invalid_binding:'計画の確認に失敗したため承認できません',check_error:'確認に失敗しました'};
function approveErrorText(status,detail){if(status===409)return '実行中は承認できません';if(status===422){const s=String(detail||'');if(Object.prototype.hasOwnProperty.call(APPROVE_DETAIL_JA,s))return APPROVE_DETAIL_JA[s];return '承認できませんでした';}return '承認できたか確認できませんでした。一覧を更新して確認してください';}
function approveUnknownErrorText(){return '承認できたか確認できませんでした。一覧を更新して確認してください';}
const REVOKE_DETAIL_JA={packet_not_approved:'承認済みではありません'};
function revokeErrorText(status,detail){if(status===422){const s=String(detail||'');if(Object.prototype.hasOwnProperty.call(REVOKE_DETAIL_JA,s))return REVOKE_DETAIL_JA[s];return '取り消せませんでした';}return '取り消せたか確認できませんでした';}
function revokeUnknownErrorText(){return '取り消せたか確認できませんでした';}
function isValidSettings(d){if(!d||typeof d!=='object')return false;const s=d.settings;if(!s||typeof s!=='object')return false;if(s.method!=='local_only'&&s.method!=='external_guarded')return false;if(typeof s.generator_provider!=='string')return false;if(typeof s.reviewer_provider!=='string')return false;if(!Array.isArray(d.known_providers))return false;for(const v of d.known_providers){if(typeof v!=='string')return false;}return true;}
function isNonEmptyStr(v){return typeof v==='string'&&v!=='';}
function isFieldList(v){if(!Array.isArray(v))return false;for(const f of v){if(PUBLIC_FIELDS.indexOf(f)<0)return false;}return true;}
function isValidPreview(d){if(!d||typeof d!=='object')return false;if(!isNonEmptyStr(d.id))return false;if(PURPOSES.indexOf(d.purpose)<0)return false;if(!isNonEmptyStr(d.provider))return false;if(!isNonEmptyStr(d.plan_signature))return false;if(d.status!=='previewed'&&d.status!=='blocked')return false;if(typeof d.created_at!=='number'||!(d.created_at>0))return false;if(!Array.isArray(d.included_fields)||!d.included_fields.length)return false;if(!isFieldList(d.included_fields))return false;if(!isFieldList(d.withheld_fields))return false;if(typeof d.withheld_count!=='number'||d.withheld_count!==d.withheld_fields.length)return false;const p=d.privacy;if(!p||typeof p!=='object')return false;if(typeof p.ok!=='boolean')return false;if(!Array.isArray(p.codes))return false;for(const c of p.codes){if(typeof c!=='string')return false;}if(d.status==='previewed'){if(!isNonEmptyStr(d.sha256))return false;if(typeof d.chars!=='number'||!(d.chars>0))return false;if(p.ok!==true||p.codes.length!==0)return false;if(typeof d.text!=='string'||!d.text)return false;let units=d.text.length;let points=units;try{points=[...d.text].length;}catch(e){points=units;}if(d.chars!==units&&d.chars!==points)return false;}else{if(typeof d.sha256!=='string')return false;if(typeof d.chars!=='number'||!(d.chars>=0))return false;if(p.ok!==false||!p.codes.length)return false;if('text' in d&&d.text)return false;}return true;}
function isValidPacketView(d,expected){if(!d||typeof d!=='object')return false;if(!isNonEmptyStr(d.id))return false;if(PURPOSES.indexOf(d.purpose)<0)return false;if(!isNonEmptyStr(d.provider))return false;if(!isNonEmptyStr(d.plan_signature))return false;if(!isNonEmptyStr(d.sha256))return false;if(typeof d.chars!=='number'||!(d.chars>0))return false;if(!Array.isArray(d.included_fields)||!d.included_fields.length||!isFieldList(d.included_fields))return false;if(!isFieldList(d.withheld_fields))return false;if(typeof d.withheld_count!=='number'||d.withheld_count!==d.withheld_fields.length)return false;const p=d.privacy;if(!p||typeof p!=='object'||p.ok!==true||!Array.isArray(p.codes)||p.codes.length!==0)return false;if(d.status!==expected)return false;if(typeof d.created_at!=='number'||!(d.created_at>0))return false;if('text' in d&&d.text)return false;return true;}
const APPROVAL_INACTIVE_JA={method_not_external:'計画方式が「外部AIあり」ではありません',settings_changed:'設定が変更されました',no_permission:'許可がありません',binding_mismatch:'承認時の内容と一致しません',plan_changed:'計画が変わりました',packet_tampered:'内容が変わっています',check_error:'確認に失敗しました'};
function inactiveReasonText(code){const s=String(code||'');if(Object.prototype.hasOwnProperty.call(APPROVAL_INACTIVE_JA,s))return APPROVAL_INACTIVE_JA[s];return '確認できず';}
function isValidApproval(item){if(!item||typeof item!=='object')return false;const keys=Object.keys(item);if(keys.length!==9)return false;for(const k of ['packet_id','purpose','provider','sha256','chars','approver','approved_at','active','inactive_reason']){if(keys.indexOf(k)<0)return false;}if(!isNonEmptyStr(item.packet_id))return false;if(PURPOSES.indexOf(item.purpose)<0)return false;if(!isNonEmptyStr(item.provider))return false;if(!isNonEmptyStr(item.sha256))return false;if(typeof item.chars!=='number'||!isFinite(item.chars))return false;if(typeof item.approver!=='string')return false;if(typeof item.approved_at!=='number'||!isFinite(item.approved_at))return false;if(typeof item.active!=='boolean')return false;if(typeof item.inactive_reason!=='string')return false;if(item.active===true){if(item.inactive_reason!=='')return false;}else{if(item.inactive_reason==='')return false;}return true;}
function isValidApprovals(d){if(!d||typeof d!=='object'||!Array.isArray(d.approvals))return false;for(const item of d.approvals){if(!isValidApproval(item))return false;}return true;}
function fmtTime(v){if(typeof v!=='number'||!isFinite(v)||v<=0)return '確認できず';const t=new Date(v*1000);if(isNaN(t.getTime()))return '確認できず';return t.toLocaleString('ja-JP');}
function shortSha(v){const s=String(v||'');if(!s)return '確認できず';return s.slice(0,12);}
function hasPublicItems(goal,criteria,constraints){return !!((goal||'')||(criteria||'')||(constraints||''));}
async function fetchJson(url,options){let r=null;try{r=await fetch(url,options);}catch(e){const err=Error('net');err.status=0;throw err;}let d=null;try{d=await r.json();}catch(e){const err=Error('parse');err.status=r.status;throw err;}if(!r.ok){let raw='';if(d&&typeof d.detail==='string')raw=d.detail;else if(d&&d.detail&&typeof d.detail.message==='string')raw=d.detail.message;const err=Error('http');err.status=r.status;err.code=raw;throw err;}return d;}
function apiSettings(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/planning-settings',{method:'GET'}).then(d=>{if(!isValidSettings(d))throw Error('bad');return d;});}
function apiPreview(pid,body){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/plan-packets/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{if(!isValidPreview(d))throw Error('bad');return d;});}
function apiApprove(pid,packetId,approver){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/plan-packets/'+encodeURIComponent(packetId)+'/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approver:approver})}).then(d=>{if(!isValidPacketView(d,'approved'))throw Error('bad');return d;});}
function apiRevoke(pid,packetId,approver){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/plan-packets/'+encodeURIComponent(packetId)+'/revoke',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approver:approver})}).then(d=>{if(!isValidPacketView(d,'revoked'))throw Error('bad');return d;});}
function apiApprovals(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/plan-packets/approvals',{method:'GET'}).then(d=>{if(!isValidApprovals(d))throw Error('bad');return d;});}
let epoch=0;
function nextTicket(){epoch+=1;return epoch;}
function isCurrentTicket(t){return t===epoch;}
function mount(){
 function init(){
  const host=document.getElementById('planningGuardPanelSection');
  if(!host){setTimeout(init,300);return;}
  if(document.getElementById('planningGuardPacketsSection'))return;
  const box=document.createElement('section');box.id='planningGuardPacketsSection';
  addText(box,'h3','外部AIへ送る内容の承認');
  addText(box,'p','この承認は、この内容を、この提供元へ、1回だけ送ってよいという許可です。送信そのものではありません。この画面からは送信できません(送信は別の操作です)。');
  const createBox=document.createElement('div');createBox.id='planningGuardPacketsCreateBox';box.append(createBox);
  const approveBox=document.createElement('div');approveBox.id='planningGuardPacketsApproveBox';box.append(approveBox);
  const approveMsg=document.createElement('p');approveMsg.id='planningGuardPacketsApproveMsg';box.append(approveMsg);
  const listBox=document.createElement('div');listBox.id='planningGuardPacketsListBox';box.append(listBox);
  const listMsg=document.createElement('p');listMsg.id='planningGuardPacketsListMsg';box.append(listMsg);
  host.append(box);
  let shownPid=null;let loaded=false;
  let known=[];let settingsOk=false;
  let currentPreview=null;
  let previewBusy=false;let approveBusy=false;let revokeBusy=false;
  let approvalsSeq=0;
  let purposeSel=null;let providerSel=null;let goalInput=null;let criteriaInput=null;let constraintsInput=null;let previewBtn=null;let previewMsg=null;let previewBody=null;
  let checkBox=null;let approverInput=null;let approveBtn=null;
  let revokeApproverInput=null;let listBody=null;
  function makeArea(id,label){const lab=document.createElement('label');addText(lab,'span',label);const t=document.createElement('textarea');t.id=id;lab.append(t);createBox.append(lab);return t;}
  function refreshProviders(){if(!providerSel)return;const keep=providerSel.value;providerSel.replaceChildren();const empty=document.createElement('option');empty.value='';empty.textContent='未設定';providerSel.append(empty);for(const name of known){const opt=document.createElement('option');opt.value=name;opt.textContent=name;providerSel.append(opt);}providerSel.value=(keep&&known.indexOf(keep)>=0)?keep:'';}
  function readItems(){return {goal:(goalInput.value||'').trim(),success_criteria:(criteriaInput.value||'').trim(),constraints:(constraintsInput.value||'').trim()};}
  function refreshPreview(){if(!previewBtn||!purposeSel||!providerSel||!goalInput||!criteriaInput||!constraintsInput)return;const items=readItems();const okItems=hasPublicItems(items.goal,items.success_criteria,items.constraints);const okProvider=providerSel.value&&known.indexOf(providerSel.value)>=0;previewBtn.disabled=previewBusy||!settingsOk||!okItems||!okProvider;}
  function buildCreate(){
   createBox.replaceChildren();
   addText(createBox,'h4','送付内容の作成');
   addText(createBox,'p','各欄は、この内容を外部へ送る前提で、公開可能な文だけを書いてください。空欄の項目は送らず、伏せた項目として数えます。');
   const pLab=document.createElement('label');
   addText(pLab,'span','用途：');
   purposeSel=document.createElement('select');purposeSel.id='planningGuardPacketsPurpose';
   for(const p of PURPOSES){const opt=document.createElement('option');opt.value=p;opt.textContent=PURPOSE_JA[p];purposeSel.append(opt);}
   purposeSel.value=PURPOSES[0];
   pLab.append(purposeSel);createBox.append(pLab);
   const vLab=document.createElement('label');
   addText(vLab,'span','提供元：');
   providerSel=document.createElement('select');providerSel.id='planningGuardPacketsProvider';
   vLab.append(providerSel);createBox.append(vLab);
   goalInput=makeArea('planningGuardPacketsGoal','目標：');
   criteriaInput=makeArea('planningGuardPacketsCriteria','達成条件：');
   constraintsInput=makeArea('planningGuardPacketsConstraints','制約：');
   previewBtn=document.createElement('button');previewBtn.type='button';previewBtn.id='planningGuardPacketsPreview';previewBtn.textContent='内容を確認する';
   createBox.append(previewBtn);
   addText(createBox,'p','内容の確認は保存・承認・送信ではありません。');
   previewMsg=document.createElement('p');previewMsg.id='planningGuardPacketsPreviewMsg';createBox.append(previewMsg);
   previewBody=document.createElement('div');previewBody.id='planningGuardPacketsPreviewBody';createBox.append(previewBody);
   previewBtn.addEventListener('click',preview);
   for(const node of [purposeSel,providerSel])node.addEventListener('change',()=>{refreshProvidersKeep();refreshPreview();});
   for(const node of [goalInput,criteriaInput,constraintsInput]){node.addEventListener('input',refreshPreview);node.addEventListener('change',refreshPreview);}
   refreshProviders();refreshPreview();
  }
  function refreshProvidersKeep(){if(!providerSel)return;const keep=providerSel.value;refreshProviders();if(keep&&known.indexOf(keep)>=0)providerSel.value=keep;}
  function clearApprove(){approveBox.replaceChildren();addText(approveBox,'h4','承認');addText(approveBox,'p','送付内容がありません');approveMsg.replaceChildren();checkBox=null;approverInput=null;approveBtn=null;}
  function refreshApprove(){if(!approveBtn)return;const name=(approverInput&&approverInput.value||'').trim();approveBtn.disabled=approveBusy||!currentPreview||!name||!(checkBox&&checkBox.checked);}
  function renderBlocked(pkt){
   currentPreview=null;clearApprove();
   previewMsg.replaceChildren();addText(previewMsg,'p','この内容は送れません');
   previewBody.replaceChildren();
   addText(previewBody,'p','用途：'+purposeText(pkt.purpose));
   addText(previewBody,'p','提供元：'+pkt.provider);
   const codes=(pkt.privacy&&Array.isArray(pkt.privacy.codes))?pkt.privacy.codes:[];
   for(const c of codes){addText(previewBody,'p','理由：'+blockedReasonText(c));}
   if(!codes.length)addText(previewBody,'p','理由：確認できず');
  }
  function renderPreviewed(pkt){
   previewMsg.replaceChildren();
   previewBody.replaceChildren();
   addText(previewBody,'p','用途：'+purposeText(pkt.purpose));
   addText(previewBody,'p','提供元：'+pkt.provider);
   addText(previewBody,'p','文字数：'+String(pkt.chars));
   addText(previewBody,'p','内容の識別：'+shortSha(pkt.sha256));
   addText(previewBody,'p','伏せた項目：'+String(pkt.withheld_count)+'件');
   const frame=document.createElement('div');frame.id='planningGuardPacketsTextFrame';frame.setAttribute('style','max-height:240px;overflow:auto;white-space:pre-wrap;border:1px solid;padding:8px;');
   const body=document.createElement('pre');body.id='planningGuardPacketsText';body.textContent=pkt.text;
   frame.append(body);previewBody.append(frame);
  }
  function renderApproveFor(pkt){
   approveBox.replaceChildren();approveMsg.replaceChildren();
   checkBox=null;approverInput=null;approveBtn=null;
   addText(approveBox,'h4','承認');
   addText(approveBox,'p','用途：'+purposeText(pkt.purpose)+' / 提供元：'+pkt.provider+' / 文字数：'+String(pkt.chars)+' / 識別：'+shortSha(pkt.sha256));
   addText(approveBox,'p','承認の前に本文を読み直してください。本文の表示は承認後に消えます。');
   const aLab=document.createElement('label');
   addText(aLab,'span','承認者名：');
   approverInput=document.createElement('input');approverInput.type='text';approverInput.id='planningGuardPacketsApprover';
   aLab.append(approverInput);approveBox.append(aLab);
   const cLab=document.createElement('label');
   checkBox=document.createElement('input');checkBox.type='checkbox';checkBox.id='planningGuardPacketsCheck';
   cLab.append(checkBox);
   cLab.append(document.createTextNode('この内容を、この提供元へ、1回だけ送ることを承認します'));
   approveBox.append(cLab);
   approveBtn=document.createElement('button');approveBtn.type='button';approveBtn.id='planningGuardPacketsApprove';approveBtn.textContent='承認する';
   approveBox.append(approveBtn);
   approveBtn.addEventListener('click',approve);
   approverInput.addEventListener('input',refreshApprove);
   checkBox.addEventListener('change',refreshApprove);
   refreshApprove();
  }
  async function preview(){
   if(previewBusy)return;
   const pid=shownPid;const ticket=epoch;
   if(!pid||currentPid()!==pid)return;
   if(!purposeSel||!providerSel||!goalInput||!criteriaInput||!constraintsInput)return;
   const purpose=purposeSel.value;const provider=providerSel.value;
   if(PURPOSES.indexOf(purpose)<0||!provider||known.indexOf(provider)<0)return;
   const items=readItems();
   const publicItems={};const withheld=[];
   for(const f of PUBLIC_FIELDS){if(items[f])publicItems[f]=items[f];else withheld.push(f);}
   if(!Object.keys(publicItems).length)return;
   previewBusy=true;refreshPreview();
   previewMsg.replaceChildren();addText(previewMsg,'p','確認中…');
   previewBody.replaceChildren();currentPreview=null;clearApprove();
   try{
    const pkt=await apiPreview(pid,{purpose:purpose,provider:provider,public_items:publicItems,withheld_fields:withheld});
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    if(pkt.status==='blocked')renderBlocked(pkt);
    else{currentPreview=pkt;renderPreviewed(pkt);renderApproveFor(pkt);}
   }catch(e){
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    currentPreview=null;clearApprove();
    previewMsg.replaceChildren();
    const status=(e&&typeof e.status==='number')?e.status:0;
    if(status===422)addText(previewMsg,'p',previewErrorText(status,(e&&e.code)||''));
    else addText(previewMsg,'p',previewUnknownErrorText());
   }finally{previewBusy=false;if(isCurrentTicket(ticket)&&currentPid()===pid)refreshPreview();}
  }
  async function approve(){
   if(approveBusy)return;
   const pkt=currentPreview;const pid=shownPid;const ticket=epoch;
   if(!pkt||!pkt.id)return;
   const name=(approverInput&&approverInput.value||'').trim();
   if(!name||!(checkBox&&checkBox.checked))return;
   if(!pid||currentPid()!==pid){approveMsg.replaceChildren();addText(approveMsg,'p','現在のプロジェクトが変わったため承認しませんでした');return;}
   approveBusy=true;refreshApprove();
   approveMsg.replaceChildren();addText(approveMsg,'p','承認中…');
   try{
    await apiApprove(pid,pkt.id,name);
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    currentPreview=null;previewBody.replaceChildren();clearApprove();
    approveMsg.replaceChildren();
    addText(approveMsg,'p','承認しました(送信はまだ行われていません。送信は別の操作です)');
    loadApprovals();
   }catch(e){
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    approveMsg.replaceChildren();
    const status=(e&&typeof e.status==='number')?e.status:0;
    if(status===409||status===422)addText(approveMsg,'p',approveErrorText(status,(e&&e.code)||''));
    else addText(approveMsg,'p',approveUnknownErrorText());
   }finally{approveBusy=false;if(isCurrentTicket(ticket)&&currentPid()===pid)refreshApprove();}
  }
  function buildList(){
   listBox.replaceChildren();
   addText(listBox,'h4','承認済みの送付内容');
   const rLab=document.createElement('label');
   addText(rLab,'span','取消用の承認者名：');
   revokeApproverInput=document.createElement('input');revokeApproverInput.type='text';revokeApproverInput.id='planningGuardPacketsRevokeApprover';
   rLab.append(revokeApproverInput);listBox.append(rLab);
   const refBtn=document.createElement('button');refBtn.type='button';refBtn.id='planningGuardPacketsRefresh';refBtn.textContent='更新';
   listBox.append(refBtn);
   refBtn.addEventListener('click',()=>{listMsg.replaceChildren();loadApprovals();});
   listBody=document.createElement('div');listBody.id='planningGuardPacketsListBody';
   addText(listBody,'p','読込中…');listBox.append(listBody);
  }
  function renderApprovedList(items){
   listBody.replaceChildren();
   if(!items.length){addText(listBody,'p','承認済みの送付内容はありません');return;}
   const list=document.createElement('ul');
   for(const item of items){
    const line=document.createElement('li');
    addText(line,'p','用途：'+purposeText(item.purpose)+' / 提供元：'+item.provider);
    addText(line,'p','文字数：'+String(item.chars)+' / 識別：'+shortSha(item.sha256));
    addText(line,'p','承認者：'+item.approver+' / 承認時刻：'+fmtTime(item.approved_at));
    if(item.active===true)addText(line,'p','有効');
    else addText(line,'p','承認は無効です:'+inactiveReasonText(item.inactive_reason));
    const btn=document.createElement('button');btn.type='button';btn.textContent='取り消す';
    btn.addEventListener('click',()=>{revoke(item.packet_id);});
    line.append(btn);list.append(line);
   }
   listBody.append(list);
  }
  async function loadApprovals(){
   const pid=shownPid;const ticket=epoch;const seq=approvalsSeq+1;approvalsSeq=seq;
   if(!pid){if(listBody){listBody.replaceChildren();addText(listBody,'p','プロジェクトが選択されていません');}return;}
   if(listBody){listBody.replaceChildren();addText(listBody,'p','読込中…');}
   try{
    const d=await apiApprovals(pid);
    if(!isCurrentTicket(ticket)||seq!==approvalsSeq||currentPid()!==pid)return;
    if(!isValidApprovals(d))throw Error('bad');
    renderApprovedList(d.approvals);
   }catch(e){
    if(!isCurrentTicket(ticket)||seq!==approvalsSeq||currentPid()!==pid)return;
    if(listBody)listBody.replaceChildren();
    listMsg.replaceChildren();addText(listMsg,'p','確認できず');
   }
  }
  async function revoke(packetId){
   if(revokeBusy)return;
   const pid=shownPid;const ticket=epoch;
   if(!packetId)return;
   const name=(revokeApproverInput&&revokeApproverInput.value||'').trim();
   if(!name){listMsg.replaceChildren();addText(listMsg,'p','承認者名を入力してください');return;}
   if(!pid||currentPid()!==pid){listMsg.replaceChildren();addText(listMsg,'p','現在のプロジェクトが変わったため取り消しませんでした');return;}
   revokeBusy=true;
   listMsg.replaceChildren();addText(listMsg,'p','取消中…');
   try{
    await apiRevoke(pid,packetId,name);
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    listMsg.replaceChildren();addText(listMsg,'p','取り消しました');
    loadApprovals();
   }catch(e){
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    listMsg.replaceChildren();
    const status=(e&&typeof e.status==='number')?e.status:0;
    if(status===422)addText(listMsg,'p',revokeErrorText(status,(e&&e.code)||''));
    else addText(listMsg,'p',revokeUnknownErrorText());
   }finally{revokeBusy=false;}
  }
  async function load(){
   const pid=currentPid();
   if(pid===shownPid&&loaded)return;
   shownPid=pid;
   const ticket=nextTicket();
   currentPreview=null;clearApprove();
   if(previewBody)previewBody.replaceChildren();
   if(!pid){
    known=[];settingsOk=false;refreshProviders();refreshPreview();
    if(previewMsg){previewMsg.replaceChildren();addText(previewMsg,'p','プロジェクトが選択されていません');}
    if(listBody){listBody.replaceChildren();addText(listBody,'p','プロジェクトが選択されていません');}
    listMsg.replaceChildren();
    loaded=true;return;
   }
   if(previewMsg){previewMsg.replaceChildren();addText(previewMsg,'p','読込中…');}
   loadApprovals();
   try{
    const d=await apiSettings(pid);
    if(!isCurrentTicket(ticket))return;
    known=d.known_providers.slice();settingsOk=true;
    refreshProviders();
    if(previewMsg)previewMsg.replaceChildren();
   }catch(e){
    if(!isCurrentTicket(ticket))return;
    known=[];settingsOk=false;refreshProviders();
    if(previewMsg){previewMsg.replaceChildren();addText(previewMsg,'p','提供元を確認できないため、内容を確認できません');}
   }finally{if(isCurrentTicket(ticket)){refreshPreview();loaded=true;}}
  }
  buildCreate();buildList();clearApprove();
  const sel=document.getElementById('projectSelect');
  if(sel)sel.addEventListener('change',()=>{loaded=false;load();});
  load();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
if(typeof document!=='undefined')mount();
if(typeof module!=='undefined')module.exports={PURPOSES,PURPOSE_JA,PUBLIC_FIELDS,purposeText,previewErrorText,previewUnknownErrorText,blockedReasonText,approveErrorText,approveUnknownErrorText,revokeErrorText,revokeUnknownErrorText,isValidSettings,isValidPreview,isValidPacketView,isValidApproval,isValidApprovals,inactiveReasonText,fmtTime,shortSha,hasPublicItems,fetchJson,apiSettings,apiPreview,apiApprove,apiRevoke,apiApprovals,nextTicket,isCurrentTicket};
})();
