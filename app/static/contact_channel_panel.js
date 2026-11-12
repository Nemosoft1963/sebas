(()=>{'use strict';
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
const PLATFORMS=['X','LinkedIn','Facebook','LINE','Instagram','Threads'];
const MODE_JA={demo_test:'架空試験',production:'実運用',unset:'未設定(厳しい側=公開用途として扱う)'};
const MODE_NOTE={demo_test:'架空・公開不可',production:'',unset:'未設定は厳しい側=公開用途として扱う'};
const CONTACT_KIND_JA={form:'Webフォーム',email:'メール',phone:'電話'};
const SOURCE_JA={fictional:'架空・公開不可',registered:'登録済み(未確認)',verified:'確認済み',retired:'失効'};
function modeText(mode){const s=String(mode||'');if(Object.prototype.hasOwnProperty.call(MODE_JA,s))return MODE_JA[s];return MODE_JA.unset;}
function sourceText(source){const s=String(source||'');if(Object.prototype.hasOwnProperty.call(SOURCE_JA,s))return SOURCE_JA[s];return '確認できず';}
function contactKindText(kind){const s=String(kind||'');if(Object.prototype.hasOwnProperty.call(CONTACT_KIND_JA,s))return CONTACT_KIND_JA[s];return '確認できず';}
function formatExpiry(value){if(value===null||value===undefined||value==='')return '期限なし';let epoch=NaN;if(typeof value==='number')epoch=value;else if(typeof value==='string'&&value.trim()!=='')epoch=Number(value);if(!isFinite(epoch)||epoch<=0)return '確認できず';const d=new Date(epoch*1000);if(isNaN(d.getTime()))return '確認できず';return d.toLocaleString('ja-JP');}
const BINDING_JA={ok:'公開できる状態',stale:'登録が変わったため再プレビュー・再承認が必要',no_binding:'承認の記録が無いため再プレビュー・再承認が必要',blocked:'公開できない登録があるため公開を止めている',unreadable:'照合を読めないため公開を止めている'};
function bindingText(status){const s=String(status||'');if(Object.prototype.hasOwnProperty.call(BINDING_JA,s))return BINDING_JA[s];return '確認できず';}
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
function currentPid(){return (document.getElementById('projectSelect')||{}).value||'';}
function apiOperatingMode(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/contact-registry/operating-mode').then(d=>{if(!d||typeof d.status==='undefined')throw Error('運用区分を確認できませんでした');return d;});}
function apiSaveOperatingMode(pid,body){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/contact-registry/operating-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{if(!d||typeof d.mode==='undefined')throw Error('運用区分を保存できませんでした');return d;});}
function apiContactRoutes(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/contact-routes').then(d=>{if(!d||!Array.isArray(d.current))throw Error('問い合わせ方法を確認できませんでした');return d;});}
function apiSaveContactRoute(pid,body){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/contact-routes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{if(!d||!d.route_id)throw Error('問い合わせ方法を保存できませんでした');return d;});}
function apiVerifyContactRoute(pid,routeId,body){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/contact-routes/'+encodeURIComponent(routeId)+'/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{if(!d||!d.route_id)throw Error('確認を記録できませんでした');return d;});}
function apiRetireContactRoute(pid,routeId,body){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/contact-routes/'+encodeURIComponent(routeId)+'/retire',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{if(!d||!d.route_id)throw Error('失効を記録できませんでした');return d;});}
function apiChannelSelections(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/channel-selections').then(d=>{if(!d||!Array.isArray(d.current))throw Error('使用SNSを確認できませんでした');return d;});}
function apiSaveChannelSelection(pid,body){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/channel-selections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{if(!d||typeof d.platform==='undefined')throw Error('使用SNSを保存できませんでした');return d;});}
function apiPublicIdentities(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/public-identities').then(d=>{if(!d||!Array.isArray(d.current))throw Error('公開IDを確認できませんでした');return d;});}
function apiSavePublicIdentity(pid,body){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/public-identities',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{if(!d||!d.identity_id)throw Error('公開IDを保存できませんでした');return d;});}
function apiVerifyPublicIdentity(pid,identityId,body){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/public-identities/'+encodeURIComponent(identityId)+'/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{if(!d||!d.identity_id)throw Error('確認を記録できませんでした');return d;});}
function apiRetirePublicIdentity(pid,identityId,body){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/public-identities/'+encodeURIComponent(identityId)+'/retire',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{if(!d||!d.identity_id)throw Error('失効を記録できませんでした');return d;});}
function apiPreflight(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/contact-registry/preflight').then(d=>{if(!d||!Array.isArray(d.missing_input))throw Error('事前確認を読み取れませんでした');return d;});}
function apiPublicationBinding(pid,campaignId){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/premarketing/campaigns/'+encodeURIComponent(campaignId)+'/publication-binding').then(d=>{if(!d||typeof d.binding_status==='undefined')throw Error('公開前の版差分を確認できませんでした');return d;});}
function apiCampaigns(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/premarketing').then(d=>{if(!d||!Array.isArray(d.campaigns))throw Error('キャンペーンを確認できませんでした');return d;});}
function renderOperatingMode(host,data){
 host.replaceChildren();
 if(!data||typeof data!=='object'){addText(host,'p','読取不能');return;}
 const status=String(data.status||'');
 const current=data.current||null;
 const mode=modeText(current&&current.mode);
 addText(host,'p','運用区分：'+mode);
 if(current&&current.mode==='demo_test')addText(host,'p','架空・公開不可');
 else if(status==='unset')addText(host,'p',MODE_NOTE.unset);
 if(current&&current.mode==='demo_test'){
  addText(host,'p','担当：'+String(current.actor||'確認できず'));
  addText(host,'p','理由：'+String(current.reason||'確認できず'));
 }
 else if(status==='unreadable')addText(host,'p','読取不能');
 return {mode:(current&&current.mode)||'',status:status};
}
function renderContactRoutes(host,data){
 host.replaceChildren();
 if(!data||typeof data!=='object'){addText(host,'p','読取不能');return [];}
 const rows=Array.isArray(data.current)?data.current:[];
 if(!rows.length){addText(host,'p','未設定');addText(host,'p','次の行動：問い合わせ先を登録してください');return [];}
 const list=document.createElement('ul');
 for(const row of rows){
  const item=row||{};
  const line=document.createElement('li');
  addText(line,'strong','問い合わせ方法：'+contactKindText(item.type)+' / '+String(item.public_value||'確認できず'));
  addText(line,'p','出所：'+sourceText(item.source_kind));
  addText(line,'p','期限：'+formatExpiry(item.expires_at));
  list.append(line);
 }
 host.append(list);
 return rows;
}
function renderChannelSelections(host,data,identities){
 host.replaceChildren();
 if(!data||typeof data!=='object'){addText(host,'p','読取不能');return [];}
 const rows=Array.isArray(data.current)?data.current:[];
 const byPlatform={};
 for(const row of rows){if(row&&row.platform&&!(row.platform in byPlatform))byPlatform[row.platform]=row;}
 const enabled=PLATFORMS.filter(name=>!!(byPlatform[name]&&byPlatform[name].enabled));
 if(!rows.length){
  addText(host,'p','使用SNS：未設定');
  addText(host,'p','次の行動：使用するSNSにチェックを入れてください');
 }
 else{
  const list=document.createElement('ul');
  for(const name of PLATFORMS){
   const line=document.createElement('li');
   const row=byPlatform[name]||null;
   const label=row&&row.enabled?'有効':'無効';
   addText(line,'strong',name+'：'+label);
   const matched=(identities||[]).filter(item=>item&&item.platform===name);
   if(!matched.length)addText(line,'p','IDが未設定：公開ID(ハンドルまたはプロフィールURL)を登録してください');
   else{
    for(const one of matched){
     addText(line,'p','公開ID：'+String(one.handle||one.profile_url||'確認できず')+' / '+sourceText(one.source_kind));
    }
   }
   list.append(line);
  }
  host.append(list);
 }
 addText(host,'p','ここにはパスワードやAPIキーを入れないでください');
 return rows;
}
function renderPreflight(host,data){
 host.replaceChildren();
 if(!data||typeof data!=='object'){addText(host,'p','読取不能');return;}
 const missing=Array.isArray(data.missing_input)?data.missing_input:[];
 if(!missing.length)addText(host,'p','不足なし');
 else{
  const list=document.createElement('ul');
  for(const row of missing){
   const item=row||{};
   const line=document.createElement('li');
   addText(line,'strong','不足：'+String(item.input||item.field||'確認できず'));
   addText(line,'p','次の行動：'+String(item.next_action||'確認できず'));
   list.append(line);
  }
  host.append(list);
 }
 const blocking=Array.isArray(data.blocking_tasks)?data.blocking_tasks:[];
 if(blocking.length)addText(host,'p','止まる操作：'+blocking.join(' / ')+'（独立作業は続行可能）');
 else addText(host,'p','止まる操作なし（独立作業は続行可能）');
}
function renderPublicationBinding(host,data){
 host.replaceChildren();
 if(!data||typeof data!=='object'){addText(host,'p','読取不能');return;}
 const status=String(data.binding_status||'');
 addText(host,'p','公開前の版差分：'+bindingText(status));
 const reasons=Array.isArray(data.reasons)?data.reasons:[];
 if(reasons.length){
  const list=document.createElement('ul');
  for(const reason of reasons){
   const line=document.createElement('li');
   addText(line,'span',String(reason||'確認できず'));
   list.append(line);
  }
  host.append(list);
 }
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
  if(document.getElementById('contactChannelPanelSection'))return;
  const box=document.createElement('section');box.className='workflow-overview-item';box.id='contactChannelPanelSection';
  addText(box,'h3','発信・問い合わせ設定');
  const modeBody=document.createElement('div');modeBody.id='contactChannelModeBody';
  addText(modeBody,'p','読込中…');box.append(modeBody);
  const modeControls=document.createElement('div');
  const modeSelect=document.createElement('select');modeSelect.id='contactChannelModeSelect';
  const OPTIONS=[['demo_test','架空試験'],['production','実運用']];
  for(const pair of OPTIONS){
   const opt=document.createElement('option');opt.value=pair[0];opt.textContent=pair[1];
   modeSelect.append(opt);
  }
  modeSelect.setAttribute('aria-label','運用区分');
  modeControls.append(modeSelect);
  const modeActor=document.createElement('input');modeActor.id='contactChannelModeActor';modeActor.maxLength=100;modeActor.placeholder='担当者名（架空試験は必須）';
  modeActor.setAttribute('aria-label','担当者名');
  modeControls.append(modeActor);
  const modeReason=document.createElement('input');modeReason.id='contactChannelModeReason';modeReason.maxLength=2000;modeReason.placeholder='理由メモ（架空試験は必須）';
  modeReason.setAttribute('aria-label','理由メモ');
  modeControls.append(modeReason);
  const modeSave=document.createElement('button');modeSave.type='button';modeSave.id='contactChannelModeSave';modeSave.textContent='運用区分を保存';
  modeControls.append(modeSave);
  box.append(modeControls);
  const msg=document.createElement('p');msg.id='contactChannelMsg';box.append(msg);
  const routeBody=document.createElement('div');routeBody.id='contactChannelRouteBody';
  addText(routeBody,'p','読込中…');box.append(routeBody);
  const routeForm=document.createElement('div');
  const routeKind=document.createElement('select');routeKind.id='contactChannelRouteKind';
  for(const kind of ['form','email','phone']){
   const opt=document.createElement('option');opt.value=kind;opt.textContent=contactKindText(kind);
   routeKind.append(opt);
  }
  routeKind.setAttribute('aria-label','種類');
  routeForm.append(routeKind);
  const routeValue=document.createElement('input');routeValue.id='contactChannelRouteValue';routeValue.maxLength=2000;routeValue.placeholder='実値';
  routeValue.setAttribute('aria-label','実値');
  routeForm.append(routeValue);
  const routeSource=document.createElement('select');routeSource.id='contactChannelRouteSource';
  for(const kind of ['fictional','registered']){
   const opt=document.createElement('option');opt.value=kind;opt.textContent=sourceText(kind);
   routeSource.append(opt);
  }
  routeSource.setAttribute('aria-label','出所');
  routeForm.append(routeSource);
  const routeActor=document.createElement('input');routeActor.id='contactChannelRouteActor';routeActor.maxLength=100;routeActor.placeholder='担当者名';
  routeActor.setAttribute('aria-label','担当者名');
  routeForm.append(routeActor);
  const routeSave=document.createElement('button');routeSave.type='button';routeSave.id='contactChannelRouteSave';routeSave.textContent='問い合わせ先を登録';
  routeForm.append(routeSave);
  box.append(routeForm);
  const routeVerify=document.createElement('div');
  const routeId=document.createElement('input');routeId.id='contactChannelRouteId';routeId.maxLength=128;routeId.placeholder='対象の登録ID';
  routeId.setAttribute('aria-label','対象の登録ID');
  routeVerify.append(routeId);
  const routeMemo=document.createElement('input');routeMemo.id='contactChannelRouteMemo';routeMemo.maxLength=2000;routeMemo.placeholder='確認の根拠メモ（必須）';
  routeMemo.setAttribute('aria-label','確認の根拠メモ');
  routeVerify.append(routeMemo);
  const routeVerifyBtn=document.createElement('button');routeVerifyBtn.type='button';routeVerifyBtn.id='contactChannelRouteVerify';routeVerifyBtn.textContent='確認する';
  routeVerify.append(routeVerifyBtn);
  const routeRetireBtn=document.createElement('button');routeRetireBtn.type='button';routeRetireBtn.id='contactChannelRouteRetire';routeRetireBtn.textContent='失効する';
  routeVerify.append(routeRetireBtn);
  box.append(routeVerify);
  const channelBody=document.createElement('div');channelBody.id='contactChannelSelectionBody';
  addText(channelBody,'p','読込中…');box.append(channelBody);
  const channelForm=document.createElement('div');
  for(const name of PLATFORMS){
   const label=document.createElement('label');
   const check=document.createElement('input');check.type='checkbox';check.value=name;
   check.setAttribute('aria-label',name+'を使う');
   label.append(check);
   label.append(document.createTextNode(name));
   channelForm.append(label);
  }
  const channelActor=document.createElement('input');channelActor.id='contactChannelSelectionActor';channelActor.maxLength=100;channelActor.placeholder='担当者名';
  channelActor.setAttribute('aria-label','担当者名');
  channelForm.append(channelActor);
  const channelSave=document.createElement('button');channelSave.type='button';channelSave.id='contactChannelSelectionSave';channelSave.textContent='使用SNSを保存';
  channelForm.append(channelSave);
  box.append(channelForm);
  const identityForm=document.createElement('div');
  const identityPlatform=document.createElement('select');identityPlatform.id='contactChannelIdentityPlatform';
  for(const name of PLATFORMS){
   const opt=document.createElement('option');opt.value=name;opt.textContent=name;
   identityPlatform.append(opt);
  }
  identityPlatform.setAttribute('aria-label','媒体');
  identityForm.append(identityPlatform);
  const identityHandle=document.createElement('input');identityHandle.id='contactChannelIdentityHandle';identityHandle.maxLength=200;identityHandle.placeholder='ハンドル';
  identityHandle.setAttribute('aria-label','ハンドル');
  identityForm.append(identityHandle);
  const identityUrl=document.createElement('input');identityUrl.id='contactChannelIdentityUrl';identityUrl.maxLength=2000;identityUrl.placeholder='プロフィールURL';
  identityUrl.setAttribute('aria-label','プロフィールURL');
  identityForm.append(identityUrl);
  const identitySource=document.createElement('select');identitySource.id='contactChannelIdentitySource';
  for(const kind of ['fictional','registered']){
   const opt=document.createElement('option');opt.value=kind;opt.textContent=sourceText(kind);
   identitySource.append(opt);
  }
  identitySource.setAttribute('aria-label','出所');
  identityForm.append(identitySource);
  const identityActor=document.createElement('input');identityActor.id='contactChannelIdentityActor';identityActor.maxLength=100;identityActor.placeholder='担当者名';
  identityActor.setAttribute('aria-label','担当者名');
  identityForm.append(identityActor);
  const identitySave=document.createElement('button');identitySave.type='button';identitySave.id='contactChannelIdentitySave';identitySave.textContent='公開IDを登録';
  identityForm.append(identitySave);
  box.append(identityForm);
  const preflightBody=document.createElement('div');preflightBody.id='contactChannelPreflightBody';
  addText(preflightBody,'p','読込中…');box.append(preflightBody);
  const bindingBody=document.createElement('div');bindingBody.id='contactChannelBindingBody';
  addText(bindingBody,'p','読込中…');box.append(bindingBody);
  const parent=anchor.parentNode;
  if(parent)parent.insertBefore(box,anchor.nextSibling);
  else anchor.append(box);
  let epoch=0;
  async function load(){
   const ticket=++epoch;
   const isCurrent=()=>ticket===epoch;
   const pid=currentPid();
   if(!pid){
    if(!isCurrent())return;
    modeBody.replaceChildren();addText(modeBody,'p','プロジェクトが選択されていません');
    routeBody.replaceChildren();addText(routeBody,'p','プロジェクトが選択されていません');
    channelBody.replaceChildren();addText(channelBody,'p','プロジェクトが選択されていません');
    preflightBody.replaceChildren();addText(preflightBody,'p','プロジェクトが選択されていません');
    bindingBody.replaceChildren();addText(bindingBody,'p','プロジェクトが選択されていません');
    return;
   }
   // 入力途中の値はPJ切替で残さない。読込のたびに空に戻す。
   for(const id of ['contactChannelModeActor','contactChannelModeReason','contactChannelRouteValue','contactChannelRouteActor','contactChannelRouteId','contactChannelRouteMemo','contactChannelSelectionActor','contactChannelIdentityHandle','contactChannelIdentityUrl','contactChannelIdentityActor']){
    const node=document.getElementById(id);
    if(node)node.value='';
   }
   msg.replaceChildren();
   let routes=[];
   let identities=[];
   try{
    const mode=await apiOperatingMode(pid);
    if(!isCurrent())return;
    renderOperatingMode(modeBody,mode);
   }catch(e){
    if(!isCurrent())return;
    modeBody.replaceChildren();
    addText(modeBody,'p','読取不能：'+(e.message||'通信できませんでした'));
   }
   try{
    const data=await apiContactRoutes(pid);
    if(!isCurrent())return;
    routes=renderContactRoutes(routeBody,data);
   }catch(e){
    if(!isCurrent())return;
    routeBody.replaceChildren();
    addText(routeBody,'p','読取不能：'+(e.message||'通信できませんでした'));
   }
   try{
    const data=await apiPublicIdentities(pid);
    if(!isCurrent())return;
    identities=Array.isArray(data.current)?data.current:[];
   }catch(e){
    if(!isCurrent())return;
    identities=[];
    channelBody.replaceChildren();
    addText(channelBody,'p','読取不能：'+(e.message||'通信できませんでした'));
   }
   if(identities.length||true){
    try{
     const data=await apiChannelSelections(pid);
     if(!isCurrent())return;
     renderChannelSelections(channelBody,data,identities);
    }catch(e){
     if(!isCurrent())return;
     channelBody.replaceChildren();
     addText(channelBody,'p','読取不能：'+(e.message||'通信できませんでした'));
    }
   }
   try{
    const data=await apiPreflight(pid);
    if(!isCurrent())return;
    renderPreflight(preflightBody,data);
   }catch(e){
    if(!isCurrent())return;
    preflightBody.replaceChildren();
    addText(preflightBody,'p','読取不能：'+(e.message||'通信できませんでした'));
   }
   try{
    const pre=await apiCampaigns(pid);
    if(!isCurrent())return;
    const campaigns=Array.isArray(pre.campaigns)?pre.campaigns:[];
    if(!campaigns.length){
     bindingBody.replaceChildren();
     addText(bindingBody,'p','公開前の版差分：対象のキャンペーンなし');
    }
    else{
     const target=String(campaigns[0].id||'');
     const data=await apiPublicationBinding(pid,target);
     if(!isCurrent())return;
     renderPublicationBinding(bindingBody,data);
    }
   }catch(e){
    if(!isCurrent())return;
    bindingBody.replaceChildren();
    addText(bindingBody,'p','読取不能：'+(e.message||'通信できませんでした'));
   }
   void routes;
  }
  modeSave.addEventListener('click',async()=>{
   const pid=currentPid();
   if(!pid){msg.replaceChildren();addText(msg,'p','プロジェクトが選択されていません');return;}
   const actor=modeActor.value.trim();
   const reason=modeReason.value.trim();
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   if(modeSelect.value==='demo_test'&&!reason){msg.replaceChildren();addText(msg,'p','理由メモを入力してください');return;}
   modeSave.disabled=true;
   msg.replaceChildren();addText(msg,'p','保存中…');
   try{
    await apiSaveOperatingMode(pid,{mode:modeSelect.value,actor:actor,reason:reason});
    msg.replaceChildren();addText(msg,'p','保存しました');
    await load();
   }catch(e){
    msg.replaceChildren();
    addText(msg,'p','保存できませんでした: '+(e.message||'通信できませんでした'));
   }finally{
    modeSave.disabled=false;
   }
  });
  routeSave.addEventListener('click',async()=>{
   const pid=currentPid();
   if(!pid){msg.replaceChildren();addText(msg,'p','プロジェクトが選択されていません');return;}
   const actor=routeActor.value.trim();
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   routeSave.disabled=true;
   msg.replaceChildren();addText(msg,'p','保存中…');
   try{
    await apiSaveContactRoute(pid,{type:routeKind.value,public_value:routeValue.value.trim(),source_kind:routeSource.value,actor:actor});
    msg.replaceChildren();addText(msg,'p','保存しました');
    await load();
   }catch(e){
    msg.replaceChildren();
    addText(msg,'p','保存できませんでした: '+(e.message||'通信できませんでした'));
   }finally{
    routeSave.disabled=false;
   }
  });
  routeVerifyBtn.addEventListener('click',async()=>{
   const pid=currentPid();
   if(!pid){msg.replaceChildren();addText(msg,'p','プロジェクトが選択されていません');return;}
   const actor=routeActor.value.trim();
   const memo=routeMemo.value.trim();
   const target=routeId.value.trim();
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   if(!memo){msg.replaceChildren();addText(msg,'p','確認の根拠メモを入力してください');return;}
   if(!target){msg.replaceChildren();addText(msg,'p','対象の登録IDを入力してください');return;}
   routeVerifyBtn.disabled=true;
   msg.replaceChildren();addText(msg,'p','確認中…');
   try{
    await apiVerifyContactRoute(pid,target,{actor:actor,memo:memo});
    msg.replaceChildren();addText(msg,'p','確認しました');
    await load();
   }catch(e){
    msg.replaceChildren();
    addText(msg,'p','確認できませんでした: '+(e.message||'通信できませんでした'));
   }finally{
    routeVerifyBtn.disabled=false;
   }
  });
  routeRetireBtn.addEventListener('click',async()=>{
   const pid=currentPid();
   if(!pid){msg.replaceChildren();addText(msg,'p','プロジェクトが選択されていません');return;}
   const actor=routeActor.value.trim();
   const memo=routeMemo.value.trim();
   const target=routeId.value.trim();
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   if(!memo){msg.replaceChildren();addText(msg,'p','確認の根拠メモを入力してください');return;}
   if(!target){msg.replaceChildren();addText(msg,'p','対象の登録IDを入力してください');return;}
   routeRetireBtn.disabled=true;
   msg.replaceChildren();addText(msg,'p','失効中…');
   try{
    await apiRetireContactRoute(pid,target,{actor:actor,reason:memo});
    msg.replaceChildren();addText(msg,'p','失効しました');
    await load();
   }catch(e){
    msg.replaceChildren();
    addText(msg,'p','失効できませんでした: '+(e.message||'通信できませんでした'));
   }finally{
    routeRetireBtn.disabled=false;
   }
  });
  channelSave.addEventListener('click',async()=>{
   const pid=currentPid();
   if(!pid){msg.replaceChildren();addText(msg,'p','プロジェクトが選択されていません');return;}
   const actor=channelActor.value.trim();
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   const checked=Array.from(channelForm.querySelectorAll('input[type="checkbox"]')).filter(node=>node.checked).map(node=>String(node.value));
   const unchecked=PLATFORMS.filter(name=>checked.indexOf(name)<0);
   if(!checked.length){
    if(!window.confirm('投稿不要として登録します。よろしいですか？'))return;
   }
   const targets=checked.length?checked:unchecked;
   channelSave.disabled=true;
   msg.replaceChildren();addText(msg,'p','保存中…');
   try{
    for(const name of targets){
     const enabled=checked.indexOf(name)>=0;
     await apiSaveChannelSelection(pid,{platform:name,enabled:enabled,actor:actor});
    }
    msg.replaceChildren();addText(msg,'p','保存しました');
    await load();
   }catch(e){
    msg.replaceChildren();
    addText(msg,'p','保存できませんでした: '+(e.message||'通信できませんでした'));
   }finally{
    channelSave.disabled=false;
   }
  });
  identitySave.addEventListener('click',async()=>{
   const pid=currentPid();
   if(!pid){msg.replaceChildren();addText(msg,'p','プロジェクトが選択されていません');return;}
   const actor=identityActor.value.trim();
   if(!actor){msg.replaceChildren();addText(msg,'p','担当者名を入力してください');return;}
   identitySave.disabled=true;
   msg.replaceChildren();addText(msg,'p','保存中…');
   try{
    await apiSavePublicIdentity(pid,{platform:identityPlatform.value,handle:identityHandle.value.trim(),profile_url:identityUrl.value.trim(),source_kind:identitySource.value,actor:actor});
    msg.replaceChildren();addText(msg,'p','保存しました');
    await load();
   }catch(e){
    msg.replaceChildren();
    addText(msg,'p','保存できませんでした: '+(e.message||'通信できませんでした'));
   }finally{
    identitySave.disabled=false;
   }
  });
  const sel=document.getElementById('projectSelect');
  if(sel)sel.addEventListener('change',()=>{load();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)return;load();});
  window.reloadContactChannelPanel=function(){return load();};
  load();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
if(typeof document!=='undefined')mount();
if(typeof module!=='undefined')module.exports={MODE_JA,MODE_NOTE,CONTACT_KIND_JA,SOURCE_JA,PLATFORMS,modeText,sourceText,contactKindText,formatExpiry,BINDING_JA,bindingText,renderOperatingMode,renderContactRoutes,renderChannelSelections,renderPreflight,renderPublicationBinding,fetchJson,apiOperatingMode,apiSaveOperatingMode,apiContactRoutes,apiSaveContactRoute,apiVerifyContactRoute,apiRetireContactRoute,apiChannelSelections,apiSaveChannelSelection,apiPublicIdentities,apiSavePublicIdentity,apiVerifyPublicIdentity,apiRetirePublicIdentity,apiPreflight,apiPublicationBinding,apiCampaigns};
})();
