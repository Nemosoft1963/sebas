(()=>{'use strict';
function addText(parent,tag,text){const node=document.createElement(tag);node.textContent=(text==null?'':String(text));parent.append(node);return node;}
function currentPid(){return (document.getElementById('projectSelect')||{}).value||'';}
function isNonEmptyStr(v){return typeof v==='string'&&v!=='';}
function isValidJob(d){if(!d||typeof d!=='object')return false;if(!isNonEmptyStr(d.id))return false;if(typeof d.status!=='string'||d.status==='')return false;if(typeof d.created_at!=='number'||!isFinite(d.created_at)||d.created_at<=0)return false;if(typeof d.purpose!=='string'||d.purpose==='')return false;if(typeof d.provider!=='string'||d.provider==='')return false;return true;}
function isValidJobs(d){if(!d||typeof d!=='object'||!Array.isArray(d.jobs))return false;for(const j of d.jobs){if(!isValidJob(j))return false;}return true;}
function isValidCapabilities(d){if(!d||typeof d!=='object')return false;if(typeof d.plan_generation!=='boolean')return false;if(typeof d.plan_review!=='boolean')return false;return true;}
function isValidMission(d){if(!d||typeof d!=='object')return false;if(!Number.isInteger(d.plan_version))return false;if(typeof d.status!=='string'||d.status==='')return false;return true;}
function isValidDraft(d){if(!d||typeof d!=='object'||Array.isArray(d))return false;if(!Array.isArray(d.tasks))return false;for(const t of d.tasks){if(!t||typeof t!=='object'||Array.isArray(t))return false;if(typeof t.title!=='string')return false;if(typeof t.description!=='string')return false;if(t.outputs!==undefined){if(!Array.isArray(t.outputs))return false;for(const v of t.outputs){if(typeof v!=='string')return false;}}if(t.depends_on!==undefined){if(!Array.isArray(t.depends_on))return false;for(const v of t.depends_on){if(typeof v!=='string')return false;}}}if(d.required_outputs!==undefined){if(!Array.isArray(d.required_outputs))return false;for(const v of d.required_outputs){if(typeof v!=='string')return false;}}if(d.unverified_facts!==undefined){if(!Array.isArray(d.unverified_facts))return false;for(const v of d.unverified_facts){if(typeof v!=='string')return false;}}if(d.external_operations_pending!==undefined){if(!Array.isArray(d.external_operations_pending))return false;for(const v of d.external_operations_pending){if(typeof v==='string')continue;if(v&&typeof v==='object'&&!Array.isArray(v))continue;return false;}}return true;}
function shortJobId(v){const s=String(v||'');if(!s)return '確認できず';return s.slice(0,8);}
function fmtTime(v){if(typeof v!=='number'||!isFinite(v)||v<=0)return '確認できず';const t=new Date(v*1000);if(isNaN(t.getTime()))return '確認できず';return t.toLocaleString('ja-JP');}
function truncateText(s,limit){const body=String(s==null?'':s);if(body.length<=limit)return body;return body.slice(0,limit)+'…(以降は省略)';}
async function fetchJson(url,options){let r=null;try{r=await fetch(url,options);}catch(e){const err=Error('net');err.status=0;throw err;}let d=null;try{d=await r.json();}catch(e){const err=Error('parse');err.status=r.status;throw err;}if(!r.ok){let raw='';if(d&&typeof d.detail==='string')raw=d.detail;else if(d&&d.detail&&typeof d.detail.message==='string')raw=d.detail.message;const err=Error('http');err.status=r.status;err.code=raw;throw err;}return d;}
function apiPlanJobs(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/external-plan-jobs',{method:'GET'}).then(d=>{if(!isValidJobs(d))throw Error('bad');return d;});}
function apiCapabilities(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/planning-job-capabilities',{method:'GET'}).then(d=>{if(!isValidCapabilities(d))throw Error('bad');return d;});}
function apiMission(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/mission',{method:'GET'}).then(d=>{if(!isValidMission(d))throw Error('bad');return d;});}
async function apiGenerate(pid,jobId){let r=null;try{r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/mission/plan/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({external_job_id:jobId})});}catch(e){const err=Error('net');err.status=0;throw err;}let d=null;try{d=await r.json();}catch(e){const err=Error('parse');err.status=r.status;throw err;}if(!r.ok){let raw='';if(d&&typeof d.detail==='string')raw=d.detail;else if(d&&d.detail&&typeof d.detail.message==='string')raw=d.detail.message;const err=Error('http');err.status=r.status;err.code=raw;throw err;}return d;}
function listFailedText(){return '取得できませんでした';}
function disabledText(){return 'この機能は管理者の設定で無効です';}
function capabilitiesUnknownText(){return '可否を確認できません';}
function noDraftsText(){return '反映できる下書きはありません';}
function draftInvalidText(){return '下書きの形式が確認できません';}
function missionUnknownText(){return 'いまの計画を確認できませんでした';}
function runningText(){return '実行中は計画を作り直せません';}
function runningWithStopText(){return '実行中は計画を作り直せません。先に一時停止してください';}
function unknownResultText(){return '作り直せたか確認できませんでした。計画の画面で確認してください。確認する前に、もう一度は押さないでください';}
function successText(beforeV,afterV){return '計画を作り直しました(版 '+String(beforeV)+' → '+String(afterV)+')。次は、独立評価と計画の承認です。この画面ではどちらも行いません。';}
function generateErrorText(status,code){const c=String(code||'');if(status===403)return disabledText();if(status===409)return runningWithStopText();if(status===503)return 'ローカルのAIが利用できないため、作り直せません';if(status===404)return 'この下書きが見つかりません';if(status===400)return 'この計画では、外部の下書きを使った作り直しはできません';if(status===502)return '作り直しに失敗しました';if(status===422){if(c==='job_not_usable')return 'この下書きは使えない状態です';if(c==='plan_changed')return '送信のあとで計画が変わったため、この下書きは使えません';if(c==='draft_missing')return '下書きの内容がありません';return '作り直せませんでした';}return '';}
function pinMismatchText(){return '現在のプロジェクトが変わったため作り直しませんでした';}
let epoch=0;
function nextTicket(){epoch+=1;return epoch;}
function isCurrentTicket(t){return t===epoch;}
function mount(){
 function init(){
  const host=document.getElementById('planningGuardPanelSection');
  if(!host){setTimeout(init,300);return;}
  if(!document.getElementById('planningGuardJobsSection')){setTimeout(init,300);return;}
  if(document.getElementById('planningGuardDraftsSection'))return;
  const box=document.createElement('section');box.id='planningGuardDraftsSection';
  addText(box,'h3','外部AIの下書き');
  addText(box,'p','外部AIの下書きを人が読んで、ローカルの計画づくりの参考にするための区画です。自動で計画にはなりません。');
  const versionMsg=document.createElement('p');versionMsg.id='planningGuardDraftsVersionMsg';box.append(versionMsg);
  const refreshBtn=document.createElement('button');refreshBtn.type='button';refreshBtn.id='planningGuardDraftsRefresh';refreshBtn.textContent='更新';box.append(refreshBtn);
  refreshBtn.addEventListener('click',()=>{listMsg.replaceChildren();actionMsg.replaceChildren();loadAll();});
  const listMsg=document.createElement('p');listMsg.id='planningGuardDraftsListMsg';box.append(listMsg);
  const listBody=document.createElement('div');listBody.id='planningGuardDraftsListBody';box.append(listBody);
  const actionMsg=document.createElement('p');actionMsg.id='planningGuardDraftsActionMsg';box.append(actionMsg);
  host.append(box);
  let shownPid=null;let loaded=false;
  let jobs=[];let jobsState='loading';
  let capabilities={plan_generation:false,plan_review:false};let capabilitiesOk=false;
  let unknownLocked={};
  let confirmJobId=null;let confirmBusy=false;let generateBusy=false;
  let executorInput=null;let checkInput=null;let generateBtn=null;let quitBtn=null;let confirmBaseVersion=null;
  function canReflect(){if(!capabilitiesOk)return false;if(capabilities.plan_generation!==true)return false;return true;}
  function canReflectUnknown(){return canReflect();}
  function refreshGenerateState(){if(!generateBtn)return;const name=(executorInput&&executorInput.value||'').trim();generateBtn.disabled=generateBusy||!name||!(checkInput&&checkInput.checked);}
  function closeConfirms(){confirmJobId=null;confirmBaseVersion=null;executorInput=null;checkInput=null;generateBtn=null;quitBtn=null;confirmBusy=false;}
  function renderVersion(text){versionMsg.replaceChildren();if(text!==null&&text!==undefined)addText(versionMsg,'span',text);}
  function renderJobs(){
   listBody.replaceChildren();
   if(jobsState==='loading'){addText(listBody,'p','読込中…');return;}
   if(jobsState==='failed'){addText(listBody,'p',listFailedText());return;}
   if(jobsState==='disabled'){addText(listBody,'p',disabledText());return;}
   if(!capabilitiesOk)addText(listBody,'p',capabilitiesUnknownText());
   else if(capabilities.plan_generation!==true)addText(listBody,'p',disabledText());
   const targets=[];for(const j of jobs){if(j&&j.status==='external_generated')targets.push(j);}
   if(!targets.length){addText(listBody,'p',noDraftsText());return;}
   targets.sort((a,b)=>b.created_at-a.created_at);
   const ul=document.createElement('ul');
   for(const job of targets){
    const line=document.createElement('li');
    addText(line,'p','ID：'+shortJobId(job.id)+' / 提供元：'+String(job.provider||'')+'');
    addText(line,'p','作成：'+fmtTime(job.created_at));
    const draftOk=isValidDraft(job.external_draft);
    if(draftOk&&job.external_draft&&Array.isArray(job.external_draft.tasks))addText(line,'p','工程数：'+String(job.external_draft.tasks.length));
    else addText(line,'p','工程数：確認できず');
    const openBtn=document.createElement('button');openBtn.type='button';openBtn.textContent='下書きを読む';
    const detail=document.createElement('div');
    openBtn.addEventListener('click',()=>{renderDetail(detail,job);});
    line.append(openBtn);line.append(detail);ul.append(line);
   }
   listBody.append(ul);
  }
  function pendingLine(parent,label,value){
   if(typeof value==='string'){addText(parent,'p',label+'：'+truncateText(value,1000));return;}
   if(value&&typeof value==='object'&&!Array.isArray(value)){
    for(const k of Object.keys(value)){
     const v=value[k];
     if(typeof v!=='string')continue;
     addText(parent,'p',label+'：'+truncateText(String(k),200)+'： '+truncateText(v,1000));
    }
   }
  }
  function renderDetail(detail,job){
   detail.replaceChildren();
   if(confirmJobId!==null&&confirmJobId!==job.id){closeConfirms();}
   addText(detail,'p','外部AIの下書きです。未検証で、事実の根拠にはなりません');
   const draft=job.external_draft;
   if(!isValidDraft(draft)){addText(detail,'p',draftInvalidText());return;}
   const tasks=draft.tasks||[];
   addText(detail,'p','工程の一覧');
   const show=tasks.slice(0,50);
   const ol=document.createElement('ol');
   for(let idx=0;idx<show.length;idx+=1){
    const t=show[idx];
    const item=document.createElement('li');
    addText(item,'p','番号：'+String(idx+1));
    addText(item,'p','題名：'+truncateText(t.title,200));
    addText(item,'p','説明：'+truncateText(t.description,1000));
    if(Array.isArray(t.outputs)&&t.outputs.length){for(const v of t.outputs){addText(item,'p','出力：'+truncateText(v,1000));}}
    if(Array.isArray(t.depends_on)&&t.depends_on.length){for(const v of t.depends_on){addText(item,'p','依存：'+truncateText(v,1000));}}
    ol.append(item);
   }
   detail.append(ol);
   if(tasks.length>50)addText(detail,'p','ほか'+String(tasks.length-50)+'件');
   if(Array.isArray(draft.required_outputs)&&draft.required_outputs.length){addText(detail,'p','必要な出力');for(const v of draft.required_outputs){addText(detail,'p','出力：'+truncateText(v,1000));}}
   if(Array.isArray(draft.unverified_facts)&&draft.unverified_facts.length){addText(detail,'p','未検証の事実');for(const v of draft.unverified_facts){addText(detail,'p',truncateText(v,1000));}}
   if(Array.isArray(draft.external_operations_pending)&&draft.external_operations_pending.length){addText(detail,'p','外部の操作の予定');for(const v of draft.external_operations_pending){pendingLine(detail,'予定',v);}}
   const claims=Array.isArray(job.unverified_claims)?job.unverified_claims.filter(v=>typeof v==='string'):[]; 
   if(claims.length){
    addText(detail,'p','証拠のない主張(実施済み等)');
    addText(detail,'p','ここにある『実施済み』『達成』を事実として扱わないでください');
    for(const v of claims){addText(detail,'p',truncateText(v,1000));}
   }
   const reflectBox=document.createElement('div');detail.append(reflectBox);
   const locked=!!unknownLocked[job.id];
   if(!capabilitiesOk){addText(reflectBox,'p',capabilitiesUnknownText());return;}
   if(capabilities.plan_generation!==true){addText(reflectBox,'p',disabledText());return;}
   if(locked){
    const b=document.createElement('button');b.type='button';b.textContent='この下書きを参考に、ローカルで計画を作り直す';b.disabled=true;reflectBox.append(b);return;
   }
   const rb=document.createElement('button');rb.type='button';rb.textContent='この下書きを参考に、ローカルで計画を作り直す';
   rb.addEventListener('click',()=>{openConfirm(job,reflectBox);});
   reflectBox.append(rb);
  }
  async function openConfirm(job,reflectBox){
   if(confirmBusy||generateBusy)return;
   const pid=shownPid;const ticket=epoch;
   if(!pid||currentPid()!==pid){actionMsg.replaceChildren();addText(actionMsg,'p',pinMismatchText());return;}
   if(!job||!job.id)return;
   if(!!unknownLocked[job.id])return;
   if(!canReflect())return;
   if(!isValidDraft(job.external_draft))return;
   confirmBusy=true;confirmJobId=job.id;
   reflectBox.replaceChildren();
   addText(reflectBox,'p','確認中…');
   try{
    const mission=await apiMission(pid);
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    if(mission.status==='running'){reflectBox.replaceChildren();addText(reflectBox,'p',runningText());confirmJobId=null;confirmBusy=false;return;}
    const baseV=mission.plan_version;
    confirmBaseVersion=baseV;
    reflectBox.replaceChildren();
    addText(reflectBox,'p','いまの計画(版 '+String(baseV)+')は、置き換わります。この画面には、元に戻す操作はありません。');
    addText(reflectBox,'p','外部の下書きは参考情報です。そのままは採用されず、ローカルで作り直されます。');
    addText(reflectBox,'p','作り直すと、承認済みの送付内容は無効になります。実行を始めるには、独立評価をやり直す必要があります。');
    const lab=document.createElement('label');
    addText(lab,'span','実行者名：');
    executorInput=document.createElement('input');executorInput.type='text';executorInput.id='planningGuardDraftsExecutor';
    lab.append(executorInput);reflectBox.append(lab);
    const clab=document.createElement('label');
    checkInput=document.createElement('input');checkInput.type='checkbox';checkInput.id='planningGuardDraftsCheck';
    clab.append(checkInput);
    clab.append(document.createTextNode('いまの計画が置き換わることを理解したうえで、作り直します'));
    reflectBox.append(clab);
    generateBtn=document.createElement('button');generateBtn.type='button';generateBtn.id='planningGuardDraftsGenerate';generateBtn.textContent='計画を作り直す';generateBtn.disabled=true;
    reflectBox.append(generateBtn);
    quitBtn=document.createElement('button');quitBtn.type='button';quitBtn.id='planningGuardDraftsCancel';quitBtn.textContent='やめる';
    reflectBox.append(quitBtn);
    generateBtn.addEventListener('click',()=>{doGenerate(job,reflectBox);});
    quitBtn.addEventListener('click',()=>{reflectBox.replaceChildren();confirmJobId=null;confirmBaseVersion=null;executorInput=null;checkInput=null;generateBtn=null;quitBtn=null;confirmBusy=false;});
    executorInput.addEventListener('input',refreshGenerateState);
    checkInput.addEventListener('change',refreshGenerateState);
    refreshGenerateState();
    confirmBusy=false;
   }catch(e){
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    reflectBox.replaceChildren();addText(reflectBox,'p',missionUnknownText());confirmJobId=null;confirmBusy=false;
   }
  }
  async function doGenerate(job,reflectBox){
   if(generateBusy)return;
   const pid=shownPid;const ticket=epoch;
   if(!job||!job.id)return;
   const name=(executorInput&&executorInput.value||'').trim();
   if(!name||!(checkInput&&checkInput.checked))return;
   if(!pid||currentPid()!==pid){actionMsg.replaceChildren();addText(actionMsg,'p',pinMismatchText());return;}
   if(!!unknownLocked[job.id])return;
   if(!canReflectUnknown())return;
   if(confirmJobId!==job.id)return;
   generateBusy=true;refreshGenerateState();
   actionMsg.replaceChildren();addText(actionMsg,'p','作り直し中…');
   try{
    const res=await apiGenerate(pid,job.id);
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    if(!res||typeof res!=='object'||!Number.isInteger(res.plan_version))throw Error('bad');
    const afterV=res.plan_version;const beforeV=confirmBaseVersion;
    reflectBox.replaceChildren();confirmJobId=null;confirmBaseVersion=null;executorInput=null;checkInput=null;generateBtn=null;quitBtn=null;confirmBusy=false;
    actionMsg.replaceChildren();
    const okLine=actionMsg;addText(okLine,'p','計画を作り直しました(版 '+String(beforeV)+' → '+String(afterV)+')。次は、独立評価と計画の承認です。この画面ではどちらも行いません。');
    renderVersion('いまの計画の版：'+String(afterV));
    await loadJobs(false);
   }catch(e){
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    const status=(e&&typeof e.status==='number')?e.status:0;
    if(e&&e.message==='bad'){
     reflectBox.replaceChildren();confirmJobId=null;confirmBaseVersion=null;executorInput=null;checkInput=null;generateBtn=null;quitBtn=null;confirmBusy=false;
     unknownLocked[job.id]=true;
     actionMsg.replaceChildren();addText(actionMsg,'p',unknownResultText());
     await loadJobs(false);renderJobsKeepLocked(job.id);
    }else if(status===403||status===409||status===503||status===404||status===422||status===400||status===502){
     const txt=generateErrorText(status,(e&&e.code)||'');
     reflectBox.replaceChildren();confirmJobId=null;confirmBaseVersion=null;executorInput=null;checkInput=null;generateBtn=null;quitBtn=null;confirmBusy=false;
     if(txt){actionMsg.replaceChildren();addText(actionMsg,'p',txt);}
     else{unknownLocked[job.id]=true;actionMsg.replaceChildren();addText(actionMsg,'p',unknownResultText());}
     await loadJobs(false);if(unknownLocked[job.id])renderJobsKeepLocked(job.id);
    }else{
     reflectBox.replaceChildren();confirmJobId=null;confirmBaseVersion=null;executorInput=null;checkInput=null;generateBtn=null;quitBtn=null;confirmBusy=false;
     unknownLocked[job.id]=true;
     actionMsg.replaceChildren();addText(actionMsg,'p',unknownResultText());
     await loadJobs(false);renderJobsKeepLocked(job.id);
    }
   }finally{generateBusy=false;if(isCurrentTicket(ticket)&&currentPid()===pid)refreshGenerateState();}
  }
  function renderJobsKeepLocked(){renderJobs();}
  async function loadCapabilities(){
   const pid=shownPid;const ticket=epoch;
   capabilitiesOk=false;capabilities={plan_generation:false,plan_review:false};
   if(!pid)return;
   try{const d=await apiCapabilities(pid);if(!isCurrentTicket(ticket)||currentPid()!==pid)return;capabilities=d;capabilitiesOk=true;}
   catch(e){if(!isCurrentTicket(ticket)||currentPid()!==pid)return;capabilitiesOk=false;}
  }
  async function loadVersion(){
   const pid=shownPid;const ticket=epoch;
   if(!pid){renderVersion('プロジェクトが選択されていません');return;}
   try{const m=await apiMission(pid);if(!isCurrentTicket(ticket)||currentPid()!==pid)return;if(!isValidMission(m))throw Error('bad');renderVersion('いまの計画の版：'+String(m.plan_version));}
   catch(e){if(!isCurrentTicket(ticket)||currentPid()!==pid)return;renderVersion(missionUnknownText());}
  }
  async function loadJobs(fromAuto){
   void fromAuto;
   const pid=shownPid;const ticket=epoch;
   if(!pid)return;
   try{
    const d=await apiPlanJobs(pid);
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    jobs=d.jobs.slice();jobsState='ok';listMsg.replaceChildren();
   }catch(e){
    if(!isCurrentTicket(ticket)||currentPid()!==pid)return;
    jobs=[];if(e&&e.status===403&&e.code==='feature_disabled')jobsState='disabled';else jobsState='failed';
    listMsg.replaceChildren();
    if(jobsState==='failed')addText(listMsg,'p',listFailedText());
    else addText(listMsg,'p',disabledText());
   }
   if(isCurrentTicket(ticket)&&currentPid()===pid)renderJobs();
  }
  async function loadAll(){await loadCapabilities();renderVersion('読込中…');await loadJobs(false);await loadVersion();}
  async function load(){
   const pid=currentPid();
   if(pid===shownPid&&loaded)return;
   closeConfirms();
   shownPid=pid;
   const ticket=nextTicket();
   void ticket;
   jobs=[];jobsState='loading';
   capabilities={plan_generation:false,plan_review:false};capabilitiesOk=false;
   unknownLocked={};confirmBusy=false;generateBusy=false;
   listMsg.replaceChildren();actionMsg.replaceChildren();
   renderVersion('読込中…');
   renderJobs();
   if(!pid){
    listMsg.replaceChildren();addText(listMsg,'p','プロジェクトが選択されていません');
    renderVersion('プロジェクトが選択されていません');
    loaded=true;return;
   }
   listMsg.replaceChildren();addText(listMsg,'p','読込中…');
   await loadAll();
   loaded=true;
  }
  const sel=document.getElementById('projectSelect');
  if(sel)sel.addEventListener('change',()=>{loaded=false;load();});
  load();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
if(typeof document!=='undefined')mount();
if(typeof module!=='undefined')module.exports={isValidJob,isValidJobs,isValidCapabilities,isValidMission,isValidDraft,shortJobId,fmtTime,truncateText,fetchJson,apiPlanJobs,apiCapabilities,apiMission,apiGenerate,listFailedText,disabledText,capabilitiesUnknownText,noDraftsText,draftInvalidText,missionUnknownText,runningText,runningWithStopText,unknownResultText,successText,generateErrorText,pinMismatchText,nextTicket,isCurrentTicket};
})();
