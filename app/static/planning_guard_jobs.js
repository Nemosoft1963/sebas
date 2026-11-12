(()=>{'use strict';
function addText(parent,tag,text){const node=document.createElement(tag);node.textContent=(text==null?'':String(text));parent.append(node);return node;}
function currentPid(){return (document.getElementById('projectSelect')||{}).value||'';}
const SENDABLE=['plan_generation','plan_review'];
const PURPOSE_JA={plan_generation:'計画の作成',plan_review:'計画の評価',capability_review:'機能の評価',failover_review:'別AI再調整',research:'調査'};
function purposeText(v){const s=String(v||'');if(Object.prototype.hasOwnProperty.call(PURPOSE_JA,s))return PURPOSE_JA[s];return '確認できず';}
const PLAN_JOB_JA={sending:'送信中',external_generated:'外部下書きあり',rejected_response:'応答を拒否',failed:'失敗',cancelled:'取消',outcome_unknown:'結果不明'};
function planJobText(s){const k=String(s||'');if(Object.prototype.hasOwnProperty.call(PLAN_JOB_JA,k))return PLAN_JOB_JA[k];return '確認できず';}
const REVIEW_JA={review_passed:'合格',review_failed:'不合格',unknown_verdict:'判定不能',not_independent:'独立と認められず',rejected_response:'応答を拒否',connection_failed:'接続失敗',failed:'失敗',outcome_unknown:'結果不明',sending:'送信中',cancelled:'取消'};
function reviewJobText(s){const k=String(s||'');if(Object.prototype.hasOwnProperty.call(REVIEW_JA,k))return REVIEW_JA[k];return '確認できず';}
function jobStatusText(purpose,status){const p=String(purpose||'');if(p==='plan_generation')return planJobText(status);if(p==='plan_review')return reviewJobText(status);return '確認できず';}
const PASS_NOTE='合格は実行の許可ではありません。人の承認と既存のゲートが別に必要です';
function isNonEmptyStr(v){return typeof v==='string'&&v!=='';}
function isValidApproval(item){if(!item||typeof item!=='object')return false;const keys=Object.keys(item);if(keys.length!==9)return false;const need=['packet_id','purpose','provider','sha256','chars','approver','approved_at','active','inactive_reason'];for(const k of need){if(keys.indexOf(k)<0)return false;}if(!isNonEmptyStr(item.packet_id))return false;const allP=['plan_generation','plan_review','capability_review','failover_review','research'];if(allP.indexOf(item.purpose)<0)return false;if(!isNonEmptyStr(item.provider))return false;if(!isNonEmptyStr(item.sha256))return false;if(typeof item.chars!=='number'||!isFinite(item.chars))return false;if(typeof item.approver!=='string')return false;if(typeof item.approved_at!=='number'||!isFinite(item.approved_at))return false;if(typeof item.active!=='boolean')return false;if(typeof item.inactive_reason!=='string')return false;if(item.active===true){if(item.inactive_reason!=='')return false;}else{if(item.inactive_reason==='')return false;}return true;}
function isValidApprovals(d){if(!d||typeof d!=='object'||!Array.isArray(d.approvals))return false;for(const item of d.approvals){if(!isValidApproval(item))return false;}return true;}
function isValidJob(d){if(!d||typeof d!=='object')return false;if(!isNonEmptyStr(d.id))return false;if(typeof d.status!=='string'||d.status==='')return false;if(typeof d.created_at!=='number'||!isFinite(d.created_at)||d.created_at<=0)return false;if(typeof d.purpose!=='string'||d.purpose==='')return false;if(typeof d.provider!=='string'||d.provider==='')return false;return true;}
function isValidJobs(d){if(!d||typeof d!=='object'||!Array.isArray(d.jobs))return false;for(const j of d.jobs){if(!isValidJob(j))return false;}return true;}
function isValidPacketText(d){if(!d||typeof d!=='object')return false;if(!isNonEmptyStr(d.id))return false;if(SENDABLE.indexOf(d.purpose)<0)return false;if(!isNonEmptyStr(d.provider))return false;if(!isNonEmptyStr(d.sha256))return false;if(typeof d.chars!=='number'||!isFinite(d.chars)||d.chars<=0)return false;if(d.status!=='approved')return false;if(typeof d.text!=='string'||d.text==='')return false;return true;}
function isValidJobKey(d){if(!d||typeof d!=='object')return false;const keys=Object.keys(d);if(keys.length!==4)return false;for(const k of ['packet_id','purpose','provider','idempotency_key']){if(keys.indexOf(k)<0)return false;}if(!isNonEmptyStr(d.packet_id))return false;if(SENDABLE.indexOf(d.purpose)<0)return false;if(!isNonEmptyStr(d.provider))return false;if(!isNonEmptyStr(d.idempotency_key))return false;return true;}
function shortSha(v){const s=String(v||'');if(!s)return '確認できず';return s.slice(0,12);}
function shortJobId(v){const s=String(v||'');if(!s)return '確認できず';return s.slice(0,8);}
function fmtTime(v){if(typeof v!=='number'||!isFinite(v)||v<=0)return '確認できず';const t=new Date(v*1000);if(isNaN(t.getTime()))return '確認できず';return t.toLocaleString('ja-JP');}
async function fetchJson(url,options){let r=null;try{r=await fetch(url,options);}catch(e){const err=Error('net');err.status=0;throw err;}let d=null;try{d=await r.json();}catch(e){const err=Error('parse');err.status=r.status;throw err;}if(!r.ok){let raw='';if(d&&typeof d.detail==='string')raw=d.detail;else if(d&&d.detail&&typeof d.detail.message==='string')raw=d.detail.message;const err=Error('http');err.status=r.status;err.code=raw;throw err;}return d;}
function apiApprovals(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/plan-packets/approvals',{method:'GET'}).then(d=>{if(!isValidApprovals(d))throw Error('bad');return d;});}
function apiPacket(pid,packetId){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/plan-packets/'+encodeURIComponent(packetId),{method:'GET'}).then(d=>{if(!isValidPacketText(d))throw Error('bad');return d;});}
function apiJobKey(pid,packetId){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/plan-packets/'+encodeURIComponent(packetId)+'/job-key',{method:'GET'}).then(d=>{if(!isValidJobKey(d))throw Error('bad');return d;});}
function apiPlanJobs(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/external-plan-jobs',{method:'GET'}).then(d=>{if(!isValidJobs(d))throw Error('bad');return d;});}
function apiReviewJobs(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/independent-review-jobs',{method:'GET'}).then(d=>{if(!isValidJobs(d))throw Error('bad');return d;});}
function apiCapabilities(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/planning-job-capabilities',{method:'GET'}).then(d=>{if(!d||typeof d!=='object'||typeof d.plan_generation!=='boolean'||typeof d.plan_review!=='boolean')throw Error('bad');return d;});}
function jobsPath(purpose){if(purpose==='plan_generation')return '/external-plan-jobs';return '/independent-review-jobs';}
async function postCreate(pid,purpose,body){let r=null;try{r=await fetch('/api/projects/'+encodeURIComponent(pid)+jobsPath(purpose),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}catch(e){const err=Error('net');err.status=0;throw err;}let d=null;try{d=await r.json();}catch(e){const err=Error('parse');err.status=r.status;throw err;}if(r.status===202||r.status===200){if(!isValidJob(d))throw Error('bad');return {status:r.status,job:d};}let raw='';if(d&&typeof d.detail==='string')raw=d.detail;else if(d&&d.detail&&typeof d.detail.message==='string')raw=d.detail.message;const err=Error('http');err.status=r.status;err.code=raw;throw err;}
async function postCancel(pid,purpose,jobId){let r=null;try{r=await fetch('/api/projects/'+encodeURIComponent(pid)+jobsPath(purpose)+'/'+encodeURIComponent(jobId)+'/cancel',{method:'POST'});}catch(e){const err=Error('net');err.status=0;throw err;}let d=null;try{d=await r.json();}catch(e){const err=Error('parse');err.status=r.status;throw err;}if(!r.ok){let raw='';if(d&&typeof d.detail==='string')raw=d.detail;else if(d&&d.detail&&typeof d.detail.message==='string')raw=d.detail.message;const err=Error('http');err.status=r.status;err.code=raw;throw err;}if(!isValidJob(d))throw Error('bad');return d;}
function isDisabledFeature(e){return !!e&&e.status===403&&e.code==='feature_disabled';}
const SEND_DETAIL_JA={method_not_external:'計画方式が「外部AIあり」ではありません',provider_mismatch:'設定された提供元と一致しません',packet_not_found:'この送付内容が見つかりません',purpose_mismatch:'用途が一致しません',idempotency_key_mismatch:'計画が変わったため送れません。もう一度確認してください',quota_exceeded:'今日の送信回数の上限(3回)に達しました',privacy_recheck_failed:'送信直前の再検査で、送れない内容が見つかりました',plan_changed:'計画が変わったため送れません',packet_tampered:'内容が変わっているため送れません',check_error:'確認に失敗したため送れません'};
function sendErrorText(status,code){if(status===403){if(code==='feature_disabled')return 'この機能は管理者の設定で無効です';return '送信できませんでした';}if(status===409)return '実行中は送信できません。先に一時停止してください';if(status===422){const s=String(code||'');if(Object.prototype.hasOwnProperty.call(SEND_DETAIL_JA,s))return SEND_DETAIL_JA[s];if(s.indexOf('permission_denied')===0)return '送信の許可がありません(承認が取り消された、または使用済みです)';return '送信できませんでした';}return '';}
function sendUnknownText(){return '送信できたか確認できませんでした。下の一覧を更新して確認してください。確認する前に、もう一度送らないでください';}
function cancelErrorText(status,code){if(status===409)return 'すでに送信が終わっているため、取り消せません';if(status===404)return 'ジョブが見つかりません';return '';}
function cancelUnknownText(){return '取り消せたか確認できませんでした';}
let epoch=0;
function nextTicket(){epoch+=1;return epoch;}
function isCurrentTicket(t){return t===epoch;}
function mount(){
 function init(){
  const host=document.getElementById('planningGuardPanelSection');
  if(!host){setTimeout(init,300);return;}
  if(!document.getElementById('planningGuardPacketsSection')){setTimeout(init,300);return;}
  if(document.getElementById('planningGuardJobsSection'))return;
  const box=document.createElement('section');box.id='planningGuardJobsSection';
  addText(box,'h3','外部AIへの送信');
  addText(box,'p','承認済みの送付内容を外部のサービスへ送ります。人の確認なしに自動で送ることはありません。');
  addText(box,'p','外部AIの下書きの採用は、この画面ではできません');
  const sendBox=document.createElement('div');sendBox.id='planningGuardJobsSendBox';box.append(sendBox);
  const sendMsg=document.createElement('p');sendMsg.id='planningGuardJobsSendMsg';box.append(sendMsg);
  const confirmBox=document.createElement('div');confirmBox.id='planningGuardJobsConfirmBox';box.append(confirmBox);
  const confirmMsg=document.createElement('p');confirmMsg.id='planningGuardJobsConfirmMsg';box.append(confirmMsg);
  const jobsBox=document.createElement('div');jobsBox.id='planningGuardJobsListBox';box.append(jobsBox);
  const jobsMsg=document.createElement('p');jobsMsg.id='planningGuardJobsListMsg';box.append(jobsMsg);
  host.append(box);
  let shownPid=null;let loaded=false;
  let approvals=[];
  let planJobs=[];let reviewJobs=[];
  let planState='loading';let reviewState='loading';
  let capabilities={plan_generation:false,plan_review:false};let capabilitiesOk=false;
  let confirmBusy=false;let sendBusy=false;
  let cancelBusy={};
  let confirmRow=null;
  let sendList=null;let jobsBody=null;
  let approverInput=null;let checkInput=null;let sendBtn=null;let quitBtn=null;
  let sendLocked={};
  let autoTimer=0;let autoCount=0;
  function clearAuto(){if(autoTimer){clearTimeout(autoTimer);autoTimer=0;}}
  function hasSending(){const all=planJobs.concat(reviewJobs);for(const j of all){if(j&&j.status==='sending')return true;}return false;}
  function scheduleAuto(){clearAuto();if(autoCount>=120){addText(jobsMsg,'p','自動更新を止めました。更新ボタンで確認してください');return;}if(!hasSending())return;let hidden=false;try{hidden=document.hidden===true;}catch(e){hidden=false;}if(hidden)return;autoTimer=setTimeout(autoTick,5000);}
  async function autoTick(){autoTimer=0;const pid=shownPid;const ticket=epoch;if(!pid)return;if(hasSending()===false)return;let hidden=false;try{hidden=document.hidden===true;}catch(e){hidden=false;}if(hidden)return;if(autoCount>=120){if(isCurrentTicket(ticket)&&currentPid()===pid)addText(jobsMsg,'p','自動更新を止めました。更新ボタンで確認してください');return;}autoCount+=1;if(autoCount>120){if(isCurrentTicket(ticket)&&currentPid()===pid)addText(jobsMsg,'p','自動更新を止めました。更新ボタンで確認してください');return;}await loadJobs(true);if(!isCurrentTicket(ticket)||currentPid()!==pid)return;if(autoCount>=120){addText(jobsMsg,'p','自動更新を止めました。更新ボタンで確認してください');clearAuto();return;}scheduleAuto();}
  function jobsOkFor(purpose){if(!capabilitiesOk||capabilities[purpose]!==true)return false;if(purpose==='plan_generation')return planState==='ok';if(purpose==='plan_review')return reviewState==='ok';return false;}
  function buildSend(){
   sendBox.replaceChildren();
   addText(sendBox,'h4','送れる内容');
   const refBtn=document.createElement('button');refBtn.type='button';refBtn.id='planningGuardJobsRefresh';refBtn.textContent='更新';
   sendBox.append(refBtn);
   refBtn.addEventListener('click',()=>{sendMsg.replaceChildren();loadAll();});
   sendList=document.createElement('div');sendList.id='planningGuardJobsSendList';sendBox.append(sendList);
   renderSend();
  }
  function renderSend(){
   if(!sendList)return;
   sendList.replaceChildren();
   if(!capabilitiesOk)addText(sendList,'p','送信の可否を確認できません。更新してください');
   else if(!capabilities.plan_generation&&!capabilities.plan_review)addText(sendList,'p','この機能は管理者の設定で無効です');
   const actives=[];for(const a of approvals){if(a&&a.active===true)actives.push(a);}
   const targets=[];const others=[];
   for(const a of actives){if(SENDABLE.indexOf(a.purpose)>=0)targets.push(a);else others.push(a);}
   if(!targets.length&&!others.length){addText(sendList,'p','送れる承認はありません');}
   const ul=document.createElement('ul');
   for(const row of targets){
    const line=document.createElement('li');
    addText(line,'p','用途：'+purposeText(row.purpose)+' / 提供元：'+row.provider);
    addText(line,'p','文字数：'+String(row.chars)+' / 識別：'+shortSha(row.sha256));
    addText(line,'p','承認者：'+row.approver+' / 承認時刻：'+fmtTime(row.approved_at));
    const btn=document.createElement('button');btn.type='button';btn.textContent='送る内容を確認する';
    const locked=!!sendLocked[row.packet_id];
    const ok=jobsOkFor(row.purpose);
    btn.disabled=locked||!ok||confirmBusy||sendBusy;
    if(capabilitiesOk&&capabilities[row.purpose]===false)addText(line,'p','この機能は管理者の設定で無効です');
    btn.addEventListener('click',()=>{openConfirm(row);});
    line.append(btn);ul.append(line);
   }
   for(const row of others){
    const line=document.createElement('li');
    addText(line,'p','用途：'+purposeText(row.purpose)+' / 提供元：'+row.provider);
    addText(line,'p','この用途は、この画面からは送れません');
    ul.append(line);
   }
   sendList.append(ul);
  }
  function closeConfirm(){confirmBox.replaceChildren();confirmMsg.replaceChildren();confirmRow=null;approverInput=null;checkInput=null;sendBtn=null;quitBtn=null;}
  function refreshSendState(){if(!sendBtn)return;const name=(approverInput&&approverInput.value||'').trim();const locked=confirmRow&&!!sendLocked[confirmRow.packet_id];sendBtn.disabled=sendBusy||locked||!name||!(checkInput&&checkInput.checked);}
  function renderConfirm(packet){
   confirmBox.replaceChildren();confirmMsg.replaceChildren();
   approverInput=null;checkInput=null;sendBtn=null;quitBtn=null;
   addText(confirmBox,'h4','送信の確認');
   addText(confirmBox,'p','用途：'+purposeText(packet.purpose)+' / 提供元：'+packet.provider);
   addText(confirmBox,'p','この内容は、外部のサービス('+packet.provider+')に送信されます。');
   addText(confirmBox,'p','1回の承認で送れるのは1回だけです。1日に3回までです。');
   const frame=document.createElement('div');frame.id='planningGuardJobsTextFrame';frame.setAttribute('style','max-height:240px;overflow:auto;white-space:pre-wrap;border:1px solid;padding:8px;');
   const body=document.createElement('pre');body.id='planningGuardJobsText';body.textContent=packet.text;
   frame.append(body);confirmBox.append(frame);
   const aLab=document.createElement('label');
   addText(aLab,'span','承認者名：');
   approverInput=document.createElement('input');approverInput.type='text';approverInput.id='planningGuardJobsApprover';
   aLab.append(approverInput);confirmBox.append(aLab);
   const cLab=document.createElement('label');
   checkInput=document.createElement('input');checkInput.type='checkbox';checkInput.id='planningGuardJobsCheck';
   cLab.append(checkInput);
   cLab.append(document.createTextNode('この内容を、この提供元へ、今、1回だけ送信することを承認します'));
   confirmBox.append(cLab);
   sendBtn=document.createElement('button');sendBtn.type='button';sendBtn.id='planningGuardJobsSend';sendBtn.textContent='この内容を送信する';
   confirmBox.append(sendBtn);
   quitBtn=document.createElement('button');quitBtn.type='button';quitBtn.id='planningGuardJobsQuit';quitBtn.textContent='やめる';
   confirmBox.append(quitBtn);
   sendBtn.addEventListener('click',doSend);
   quitBtn.addEventListener('click',()=>{closeConfirm();});
   approverInput.addEventListener('input',refreshSendState);
   checkInput.addEventListener('change',refreshSendState);
   refreshSendState();
  }
  async function openConfirm(row){
   if(confirmBusy||sendBusy)return;
   const pid=shownPid;const ticket=epoch;
   if(!pid||currentPid()!==pid){confirmMsg.replaceChildren();addText(confirmMsg,'p','現在のプロジェクトが変わったため送信しませんでした');return;}
   if(!row||!row.packet_id)return;
   if(!jobsOkFor(row.purpose))return;
   confirmBusy=true;renderSend();
   confirmMsg.replaceChildren();addText(confirmMsg,'p','確認中…');
   try{
    const packet=await apiPacket(pid,row.packet_id);
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    if(packet.id!==row.packet_id||packet.sha256!==row.sha256||packet.provider!==row.provider||packet.purpose!==row.purpose||packet.chars!==row.chars){confirmBox.replaceChildren();confirmRow=null;approverInput=null;checkInput=null;sendBtn=null;quitBtn=null;confirmMsg.replaceChildren();addText(confirmMsg,'p','確認できず');return;}
    if(!isValidPacketText(packet)){confirmBox.replaceChildren();confirmRow=null;approverInput=null;checkInput=null;sendBtn=null;quitBtn=null;confirmMsg.replaceChildren();addText(confirmMsg,'p','確認できず');return;}
    confirmRow=row;
    renderConfirm(packet);
    confirmMsg.replaceChildren();
   }catch(e){
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    closeConfirm();
    confirmMsg.replaceChildren();addText(confirmMsg,'p','確認できず');
   }finally{confirmBusy=false;if(isCurrentTicket(ticket)&&currentPid()===pid)renderSend();}
  }
  async function doSend(){
   if(sendBusy)return;
   const row=confirmRow;const pid=shownPid;const ticket=epoch;
   if(!row)return;
   const name=(approverInput&&approverInput.value||'').trim();
   if(!name||!(checkInput&&checkInput.checked))return;
   if(!pid||currentPid()!==pid){confirmMsg.replaceChildren();addText(confirmMsg,'p','現在のプロジェクトが変わったため送信しませんでした');return;}
   if(!!sendLocked[row.packet_id])return;
   if(!jobsOkFor(row.purpose))return;
   sendBusy=true;refreshSendState();renderSend();
   confirmMsg.replaceChildren();addText(confirmMsg,'p','送信中…');
   let attemptedKey='';
   try{
    let key=null;
    try{key=await apiJobKey(pid,row.packet_id);}catch(e){key=null;}
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    if(!key||!isValidJobKey(key)||key.packet_id!==row.packet_id||key.purpose!==row.purpose||key.provider!==row.provider){confirmMsg.replaceChildren();addText(confirmMsg,'p','送信の準備ができませんでした。何も送信していません。更新して確認してください');return;}
    attemptedKey=key.idempotency_key;
    const res=await postCreate(pid,row.purpose,{packet_id:row.packet_id,provider:row.provider,idempotency_key:key.idempotency_key,approver:name});
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    const sentNew=res.status===202;
    closeConfirm();
    await loadJobs(false);await loadApprovals();
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    confirmMsg.replaceChildren();
    if(sentNew)addText(confirmMsg,'p','送信を開始しました。結果は下の一覧で確認してください');
    else addText(confirmMsg,'p','この内容は、すでに送信されています(再送はしていません)');
   }catch(e){
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    const status=(e&&typeof e.status==='number')?e.status:0;
    if(e&&e.message==='bad'){confirmBox.replaceChildren();confirmRow=null;approverInput=null;checkInput=null;sendBtn=null;quitBtn=null;confirmMsg.replaceChildren();addText(confirmMsg,'p',sendUnknownText());if(row)sendLocked[row.packet_id]=attemptedKey||true;loadJobs(false);}
    else if(status===403||status===409||status===422){confirmMsg.replaceChildren();addText(confirmMsg,'p',sendErrorText(status,(e&&e.code)||''));}
    else{confirmBox.replaceChildren();confirmRow=null;approverInput=null;checkInput=null;sendBtn=null;quitBtn=null;confirmMsg.replaceChildren();addText(confirmMsg,'p',sendUnknownText());if(row)sendLocked[row.packet_id]=attemptedKey||true;loadJobs(false);}
   }finally{sendBusy=false;if(isCurrentTicket(ticket)&&currentPid()===pid){refreshSendState();renderSend();}}
  }
  function buildJobs(){
   jobsBox.replaceChildren();
   addText(jobsBox,'h4','送信の状態');
   const refBtn=document.createElement('button');refBtn.type='button';refBtn.id='planningGuardJobsListRefresh';refBtn.textContent='更新';
   jobsBox.append(refBtn);
   refBtn.addEventListener('click',()=>{jobsMsg.replaceChildren();loadJobs(false);});
   jobsBody=document.createElement('div');jobsBody.id='planningGuardJobsListBody';jobsBox.append(jobsBody);
   renderJobs();
  }
  function renderJobs(){
   if(!jobsBody)return;
   jobsBody.replaceChildren();
   let shown=false;
   const groups=[{state:planState,label:'計画の作成'},{state:reviewState,label:'計画の評価'}];
   for(const g of groups){
    if(g.state==='disabled'){addText(jobsBody,'p','この機能は管理者の設定で無効です');shown=true;continue;}
    if(g.state==='failed'){addText(jobsBody,'p',g.label+'：取得できませんでした');shown=true;continue;}
    if(g.state==='ok')shown=true;
   }
   const okRows=[];
   if(planState==='ok')for(const j of planJobs)okRows.push(j);
   if(reviewState==='ok')for(const j of reviewJobs)okRows.push(j);
   if(planState==='ok'||reviewState==='ok'){
    if(!okRows.length)addText(jobsBody,'p','ジョブはありません');
    else{
     okRows.sort((a,b)=>b.created_at-a.created_at);
     const ul=document.createElement('ul');
     for(const job of okRows){
      const line=document.createElement('li');
      addText(line,'p','ID：'+shortJobId(job.id)+' / 用途：'+purposeText(job.purpose)+' / 提供元：'+job.provider);
      addText(line,'p','状態：'+jobStatusText(job.purpose,job.status));
      if(job.purpose==='plan_review'&&job.status==='review_passed')addText(line,'p',PASS_NOTE);
      addText(line,'p','作成：'+fmtTime(job.created_at));
      if(job.status==='sending'&&SENDABLE.indexOf(job.purpose)>=0){
       const btn=document.createElement('button');btn.type='button';btn.textContent='取り消す';
       if(!!cancelBusy[job.id])btn.disabled=true;
       btn.addEventListener('click',()=>{doCancel(job);});
       line.append(btn);
      }
      ul.append(line);
     }
     jobsBody.append(ul);
    }
   }else if(!shown){addText(jobsBody,'p','読込中…');}
  }
  async function loadApprovals(){
   const pid=shownPid;const ticket=epoch;
   if(!pid)return;
   try{
    const d=await apiApprovals(pid);
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    approvals=d.approvals.slice();
    sendMsg.replaceChildren();
   }catch(e){
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    approvals=[];
    sendMsg.replaceChildren();addText(sendMsg,'p','確認できず');
   }finally{if(isCurrentTicket(ticket)&&currentPid()===pid)renderSend();}
  }
  async function loadJobs(fromAuto){
   const pid=shownPid;const ticket=epoch;
   if(!pid)return;
   let planOk=false;let reviewOk=false;
   try{
    const d=await apiPlanJobs(pid);
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    planJobs=d.jobs.slice();planState='ok';planOk=true;
   }catch(e){
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    planJobs=[];if(isDisabledFeature(e))planState='disabled';else planState='failed';
   }
   try{
    const d2=await apiReviewJobs(pid);
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    reviewJobs=d2.jobs.slice();reviewState='ok';reviewOk=true;
   }catch(e){
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    reviewJobs=[];if(isDisabledFeature(e))reviewState='disabled';else reviewState='failed';
   }
   if(planOk||reviewOk){jobsMsg.replaceChildren();}
   for(const job of planJobs){if(job&&typeof sendLocked[job.packet_id]==='string'&&sendLocked[job.packet_id]===job.idempotency_key)delete sendLocked[job.packet_id];}
   for(const job of reviewJobs){if(job&&typeof sendLocked[job.packet_id]==='string'&&sendLocked[job.packet_id]===job.idempotency_key)delete sendLocked[job.packet_id];}
   if(isCurrentTicket(ticket)&&currentPid()===pid){renderJobs();renderSend();}
   if(!fromAuto){autoCount=0;scheduleAuto();}
   else{scheduleAuto();}
  }
  async function doCancel(job){
   if(!job||!job.id||SENDABLE.indexOf(job.purpose)<0)return;
   if(cancelBusy[job.id])return;
   const pid=shownPid;const ticket=epoch;
   if(!pid||currentPid()!==pid){jobsMsg.replaceChildren();addText(jobsMsg,'p','現在のプロジェクトが変わったため取り消しませんでした');return;}
   cancelBusy[job.id]=true;renderJobs();
   jobsMsg.replaceChildren();addText(jobsMsg,'p','取消中…');
   try{
    const updated=await postCancel(pid,job.purpose,job.id);
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    if(updated.status!=='cancelled')throw Error('bad');
    await loadJobs(false);
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    jobsMsg.replaceChildren();addText(jobsMsg,'p','取り消しました(すでに外部へ送られた内容は戻りません)');
   }catch(e){
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    jobsMsg.replaceChildren();
    const status=(e&&typeof e.status==='number')?e.status:0;
    const txt=cancelErrorText(status,(e&&e.code)||'');
    if(txt)addText(jobsMsg,'p',txt);
    else addText(jobsMsg,'p',cancelUnknownText());
   }finally{cancelBusy[job.id]=false;if(isCurrentTicket(ticket)&&currentPid()===pid)renderJobs();}
  }
  async function loadCapabilities(){
   const pid=shownPid;const ticket=epoch;
   capabilitiesOk=false;capabilities={plan_generation:false,plan_review:false};renderSend();
   if(!pid)return;
   try{const d=await apiCapabilities(pid);if(!isCurrentTicket(ticket)||currentPid()!==pid)return;capabilities=d;capabilitiesOk=true;}
   catch(e){if(!isCurrentTicket(ticket)||currentPid()!==pid)return;capabilitiesOk=false;}
   renderSend();
  }
  async function loadAll(){await loadCapabilities();await loadApprovals();await loadJobs(false);}
  async function load(){
   const pid=currentPid();
   if(pid===shownPid&&loaded)return;
   clearAuto();autoCount=0;
   closeConfirm();
   shownPid=pid;
   const ticket=nextTicket();
   planState='loading';reviewState='loading';planJobs=[];reviewJobs=[];approvals=[];
   capabilities={plan_generation:false,plan_review:false};capabilitiesOk=false;
   sendLocked={};cancelBusy={};confirmBusy=false;sendBusy=false;
   buildSend();buildJobs();
   sendMsg.replaceChildren();confirmMsg.replaceChildren();jobsMsg.replaceChildren();
   if(!pid){
    sendMsg.replaceChildren();addText(sendMsg,'p','プロジェクトが選択されていません');
    jobsMsg.replaceChildren();addText(jobsMsg,'p','プロジェクトが選択されていません');
    loaded=true;return;
   }
   sendMsg.replaceChildren();addText(sendMsg,'p','読込中…');
   jobsMsg.replaceChildren();addText(jobsMsg,'p','読込中…');
   await loadAll();
   if(isCurrentTicket(ticket))loaded=true;
  }
  buildSend();buildJobs();
  const sel=document.getElementById('projectSelect');
  if(sel)sel.addEventListener('change',()=>{loaded=false;load();});
  load();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
if(typeof document!=='undefined')mount();
if(typeof module!=='undefined')module.exports={SENDABLE,PURPOSE_JA,PLAN_JOB_JA,REVIEW_JA,PASS_NOTE,purposeText,planJobText,reviewJobText,jobStatusText,isValidApproval,isValidApprovals,isValidJob,isValidJobs,isValidPacketText,isValidJobKey,shortSha,shortJobId,fmtTime,fetchJson,apiApprovals,apiPacket,apiJobKey,apiPlanJobs,apiReviewJobs,apiCapabilities,postCreate,postCancel,sendErrorText,sendUnknownText,cancelErrorText,cancelUnknownText,nextTicket,isCurrentTicket};
})();
