(()=>{'use strict';
function addText(parent,tag,text){const node=document.createElement(tag);node.textContent=text==null?'':String(text);parent.append(node);return node;}
function currentPid(){return (document.getElementById('projectSelect')||{}).value||'';}
const HOW_JA={local_only:'ローカル計画方式（外部送信の可否は別設定）',external_guarded:'外部AIあり(承認・検査つき)'};
function howText(v){const s=String(v||'');if(Object.prototype.hasOwnProperty.call(HOW_JA,s))return HOW_JA[s];return '確認できず';}
const METHODS=['local_only','external_guarded'];
const DETAIL_JA={unknown_provider:'選べない提供元です',provider_required:'生成AIの提供元を選んでください',invalid_method:'計画方式が正しくありません',unknown_field:'変更できない項目です'};
function putErrorText(status,detail){if(status===409)return '実行中は変更できません。先に一時停止してください';if(status===422){const s=String(detail||'');if(Object.prototype.hasOwnProperty.call(DETAIL_JA,s))return DETAIL_JA[s];return '変更できませんでした';}return '変更できたか確認できませんでした。画面を更新して現在の設定を確認してください';}
function unknownErrorText(){return '変更できたか確認できませんでした。画面を更新して現在の設定を確認してください';}
function isValidPayload(d){if(!d||typeof d!=='object')return false;const s=d.settings;if(!s||typeof s!=='object')return false;if(METHODS.indexOf(s.method)<0)return false;if(typeof s.generator_provider!=='string')return false;if(typeof s.reviewer_provider!=='string')return false;if(!Array.isArray(d.known_providers))return false;for(const v of d.known_providers){if(typeof v!=='string')return false;}return true;}
function sameProvider(gen,rev){if(!gen)return false;return gen===rev;}
function confirmKind(oldMethod,newMethod){if(oldMethod==='local_only'&&newMethod==='external_guarded')return 'to_external';if(oldMethod==='external_guarded'&&newMethod==='local_only')return 'to_local';return 'providers';}
function needsCheck(kind){return kind==='to_external'||kind==='to_local';}
async function fetchJson(url,options){let r=null;try{r=await fetch(url,options);}catch(e){const err=Error('net');err.status=0;throw err;}let d=null;try{d=await r.json();}catch(e){const err=Error('parse');err.status=r.status;throw err;}if(!r.ok){let raw='';if(d&&typeof d.detail==='string')raw=d.detail;else if(d&&d.detail&&typeof d.detail.message==='string')raw=d.detail.message;const err=Error('http');err.status=r.status;err.code=raw;throw err;}return d;}
function apiGet(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/planning-settings',{method:'GET'}).then(d=>{if(!isValidPayload(d))throw Error('bad');return d;});}
function apiPut(pid,body){const payload={method:body.method,generator_provider:body.generator_provider,reviewer_provider:body.reviewer_provider};return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/planning-settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(d=>{const s=d&&d.settings;if(!s||METHODS.indexOf(s.method)<0||typeof s.generator_provider!=='string'||typeof s.reviewer_provider!=='string'||!Array.isArray(d.revoked)||d.revoked.some(v=>typeof v!=='string'))throw Error('bad');return d;});}
let epoch=0;
function nextTicket(){epoch+=1;return epoch;}
function isCurrentTicket(t){return t===epoch;}
function mount(){
 function init(){
  const host=document.getElementById('planningGuardPanelSection');
  if(!host){setTimeout(init,300);return;}
  if(document.getElementById('planningGuardSettingsSection'))return;
  const box=document.createElement('section');box.id='planningGuardSettingsSection';
  addText(box,'h3','計画方式の変更');
  addText(box,'p','設定を変更すると、既存の送信許可(承認済みの送付内容)はすべて取り消されます');
  const body=document.createElement('div');body.id='planningGuardSettingsBody';
  addText(body,'p','読込中…');box.append(body);
  const confirmBox=document.createElement('div');confirmBox.id='planningGuardSettingsConfirm';box.append(confirmBox);
  const msg=document.createElement('p');msg.id='planningGuardSettingsMsg';box.append(msg);
  host.append(box);
  let shownPid=null;let loaded=false;let saving=false;
  let current={method:'local_only',generator_provider:'',reviewer_provider:''};
  let known=[];
  let methodSel=null;let genSel=null;let revSel=null;let changeBtn=null;
  let sameWarn=null;let needGen=null;
  let commitBtn=null;let cancelBtn=null;let checkBox=null;
  function setDisabled(on){const list=[methodSel,genSel,revSel,changeBtn,commitBtn,cancelBtn,checkBox];for(const node of list){if(node)node.disabled=on;}}
  function refreshState(){
   if(!methodSel||!genSel||!revSel||!changeBtn||!sameWarn||!needGen)return;
   const m=methodSel.value;const g=genSel.value;const r=revSel.value;
   sameWarn.replaceChildren();
   if(sameProvider(g,r))addText(sameWarn,'p','生成AIと評価AIが同じ提供元のため、独立評価として数えられません');
   needGen.replaceChildren();
   if((g&&known.indexOf(g)<0)||(r&&known.indexOf(r)<0)){addText(needGen,'p','現在の提供元は接続されていません。接続済みの提供元を選んでください');changeBtn.disabled=true;}
   else if(m==='external_guarded'&&!g){addText(needGen,'p','生成AIの提供元を選んでください(このままでは確定できません)');changeBtn.disabled=true;}
   else if(!saving){changeBtn.disabled=false;}
  }
  function providerOptions(sel,value){
   sel.replaceChildren();
   const empty=document.createElement('option');empty.value='';empty.textContent='未設定';sel.append(empty);
   for(const name of known){const opt=document.createElement('option');opt.value=name;opt.textContent=name;sel.append(opt);}
   if(value&&known.indexOf(value)<0){const opt=document.createElement('option');opt.value=value;opt.textContent=value+'（接続なし）';opt.disabled=true;sel.append(opt);}
   sel.value=value;
  }
  function renderForm(){
   body.replaceChildren();confirmBox.replaceChildren();msg.replaceChildren();
   commitBtn=null;cancelBtn=null;checkBox=null;
   const mLabel=document.createElement('label');
   addText(mLabel,'span','計画方式：');
   methodSel=document.createElement('select');methodSel.id='planningGuardSettingsMethod';
   for(const m of METHODS){const opt=document.createElement('option');opt.value=m;opt.textContent=HOW_JA[m];methodSel.append(opt);}
   methodSel.value=current.method;
   mLabel.append(methodSel);body.append(mLabel);
   const gLabel=document.createElement('label');
   addText(gLabel,'span','生成AIの提供元：');
   genSel=document.createElement('select');genSel.id='planningGuardSettingsGenerator';
   providerOptions(genSel,current.generator_provider);
   gLabel.append(genSel);body.append(gLabel);
   const rLabel=document.createElement('label');
   addText(rLabel,'span','評価AIの提供元：');
   revSel=document.createElement('select');revSel.id='planningGuardSettingsReviewer';
   providerOptions(revSel,current.reviewer_provider);
   rLabel.append(revSel);body.append(rLabel);
   sameWarn=document.createElement('div');sameWarn.id='planningGuardSettingsSameWarn';body.append(sameWarn);
   needGen=document.createElement('div');needGen.id='planningGuardSettingsNeedGen';body.append(needGen);
   changeBtn=document.createElement('button');changeBtn.type='button';changeBtn.id='planningGuardSettingsChange';changeBtn.textContent='変更する';
   body.append(changeBtn);
   methodSel.addEventListener('change',refreshState);
   genSel.addEventListener('change',refreshState);
   revSel.addEventListener('change',refreshState);
   changeBtn.addEventListener('click',openConfirm);
   refreshState();
  }
  function openConfirm(){
   if(saving)return;
   if(!methodSel||!genSel||!revSel)return;
   const next={method:methodSel.value,generator_provider:genSel.value,reviewer_provider:revSel.value};
   if(next.method==='external_guarded'&&!next.generator_provider)return;
   if((next.generator_provider&&known.indexOf(next.generator_provider)<0)||(next.reviewer_provider&&known.indexOf(next.reviewer_provider)<0))return;
   const kind=confirmKind(current.method,next.method);
   confirmBox.replaceChildren();msg.replaceChildren();
   commitBtn=null;cancelBtn=null;checkBox=null;
   addText(confirmBox,'p','変更後の計画方式：'+howText(next.method));
   addText(confirmBox,'p','変更後の生成AIの提供元：'+(next.generator_provider||'未設定'));
   addText(confirmBox,'p','変更後の評価AIの提供元：'+(next.reviewer_provider||'未設定'));
   addText(confirmBox,'p','設定を変更すると、既存の送信許可(承認済みの送付内容)はすべて取り消されます');
   if(kind==='to_external'){
    addText(confirmBox,'p','この計画方式では、旧来の4経路(計画評価・別AI再調整・実現手段の検討・調査)の外部送信が止まります');
    addText(confirmBox,'p','目標達成ゲートの計画評価も止まります');
    addText(confirmBox,'p','実行の開始には、独立評価の合格が必要になります');
    addText(confirmBox,'p','外部へ送る内容は毎回あなたの承認が必要です');
   }else if(kind==='to_local'){
    addText(confirmBox,'p','旧来の外部送信の経路が再開される場合があります');
   }
   if(needsCheck(kind)){
    const lab=document.createElement('label');
    checkBox=document.createElement('input');checkBox.type='checkbox';checkBox.id='planningGuardSettingsCheck';
    lab.append(checkBox);
    lab.append(document.createTextNode('上記を確認しました'));
    confirmBox.append(lab);
   }
   commitBtn=document.createElement('button');commitBtn.type='button';commitBtn.id='planningGuardSettingsCommit';commitBtn.textContent='確定';
   if(needsCheck(kind)){
    commitBtn.disabled=true;
    if(checkBox)checkBox.addEventListener('change',()=>{if(commitBtn&&checkBox)commitBtn.disabled=!checkBox.checked;});
   }
   confirmBox.append(commitBtn);
   cancelBtn=document.createElement('button');cancelBtn.type='button';cancelBtn.id='planningGuardSettingsCancel';cancelBtn.textContent='やめる';
   confirmBox.append(cancelBtn);
   commitBtn.addEventListener('click',()=>{if(needsCheck(kind)&&!(checkBox&&checkBox.checked))return;save(next);});
   cancelBtn.addEventListener('click',()=>{confirmBox.replaceChildren();commitBtn=null;cancelBtn=null;checkBox=null;});
  }
  async function save(next){
   if(saving)return;
   saving=true;
   const ticket=epoch;
   const pid=shownPid;
   setDisabled(true);
   msg.replaceChildren();addText(msg,'p','保存中…');
   try{
    if(!pid||currentPid()!==pid)throw Error('nopid');
    const d=await apiPut(pid,next);
    if(!isCurrentTicket(ticket))return;
    const countText=String(d.revoked.length)+'件';
    current={method:d.settings.method,generator_provider:d.settings.generator_provider,reviewer_provider:d.settings.reviewer_provider};
    confirmBox.replaceChildren();commitBtn=null;cancelBtn=null;checkBox=null;
    methodSel.value=current.method;
    providerOptions(genSel,current.generator_provider);
    providerOptions(revSel,current.reviewer_provider);
    refreshState();
    msg.replaceChildren();
    addText(msg,'p','変更しました');
    addText(msg,'p','取り消した送信許可：'+countText);
    if(typeof window!=='undefined'&&typeof window.reloadPlanningGuardPanel==='function'){
     try{window.reloadPlanningGuardPanel();}catch(e){addText(msg,'p','G5-aの表示を更新できませんでした');}
    }
   }catch(e){
    if(!isCurrentTicket(ticket))return;
    confirmBox.replaceChildren();commitBtn=null;cancelBtn=null;checkBox=null;
    msg.replaceChildren();
    const status=(e&&typeof e.status==='number')?e.status:0;
    if(status===409||status===422)addText(msg,'p',putErrorText(status,(e&&e.code)||''));
    else addText(msg,'p',unknownErrorText());
   }finally{
    saving=false;
    if(isCurrentTicket(ticket))setDisabled(false);
   }
  }
  async function load(){
   const pid=currentPid();
   if(pid===shownPid&&loaded)return;
   shownPid=pid;
   const ticket=nextTicket();
   if(!pid){
    current={method:'local_only',generator_provider:'',reviewer_provider:''};
    known=[];
    body.replaceChildren();addText(body,'p','プロジェクトが選択されていません');
    confirmBox.replaceChildren();msg.replaceChildren();
    methodSel=null;genSel=null;revSel=null;changeBtn=null;sameWarn=null;needGen=null;
    commitBtn=null;cancelBtn=null;checkBox=null;
    loaded=true;
    return;
   }
   body.replaceChildren();addText(body,'p','読込中…');
   confirmBox.replaceChildren();msg.replaceChildren();
   try{
    const d=await apiGet(pid);
    if(!isCurrentTicket(ticket))return;
    current={method:d.settings.method,generator_provider:d.settings.generator_provider,reviewer_provider:d.settings.reviewer_provider};
    known=d.known_providers.slice();
    renderForm();
   }catch(e){
    if(!isCurrentTicket(ticket))return;
    body.replaceChildren();
    addText(body,'p','現在の設定を確認できないため、変更できません');
    confirmBox.replaceChildren();msg.replaceChildren();
    methodSel=null;genSel=null;revSel=null;changeBtn=null;sameWarn=null;needGen=null;
    commitBtn=null;cancelBtn=null;checkBox=null;
   }finally{
    if(isCurrentTicket(ticket))loaded=true;
   }
  }
  const sel=document.getElementById('projectSelect');
  if(sel)sel.addEventListener('change',()=>{loaded=false;load();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)return;load();});
  if(typeof window!=='undefined')window.reloadPlanningGuardSettings=function(){loaded=false;return load();};
  load();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
if(typeof document!=='undefined')mount();
if(typeof module!=='undefined')module.exports={HOW_JA,METHODS,DETAIL_JA,howText,putErrorText,unknownErrorText,isValidPayload,sameProvider,confirmKind,needsCheck,fetchJson,apiGet,apiPut,nextTicket,isCurrentTicket};
})();
