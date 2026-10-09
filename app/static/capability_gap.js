(()=>{'use strict';
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
function currentPid(){return (document.getElementById('projectSelect')||{}).value||'';}
const STATUS_JA={draft:'下書き',reviewed:'確認済み',export_ready:'Jenkins指示を作成済み',submitted:'Jenkinsに渡した（記録）',delivered:'実装が納品されました。まだ能力は有効ではありません',verified:'元の停止が再判定で解消しました',rejected:'却下',stale:'古い版',needs_evidence:'証拠不足'};
const PERMISSION_JA={local_reversible:'ローカルで元に戻せる',local_irreversible:'ローカルで元に戻せない',external_side_effect:'外部への影響あり'};
function statusJa(status){const s=String(status||'');if(Object.prototype.hasOwnProperty.call(STATUS_JA,s))return STATUS_JA[s];return '不明';}
function permissionJa(cls){const s=String(cls||'');if(Object.prototype.hasOwnProperty.call(PERMISSION_JA,s))return PERMISSION_JA[s];return '不明';}
function buttonStateFor(status){
 const s=String(status||'');
 if(s==='draft')return{canReview:true,canExport:false,canSubmit:false,canVerify:false,note:''};
 if(s==='reviewed'||s==='export_ready')return{canReview:false,canExport:true,canSubmit:s==='export_ready',canVerify:false,note:''};
 if(s==='submitted')return{canReview:false,canExport:false,canSubmit:false,canVerify:false,note:'Jenkinsに渡したことの記録です。実際には何も送信していません。納品の入力は画面では行いません。'};
 if(s==='delivered')return{canReview:false,canExport:false,canSubmit:false,canVerify:true,note:'実装が納品されました。まだ能力は有効ではありません。元の停止が残っているか再判定してください。'};
 if(s==='verified')return{canReview:false,canExport:false,canSubmit:false,canVerify:false,note:'元の停止が再判定で解消しました。'};
 if(s==='rejected')return{canReview:false,canExport:false,canSubmit:false,canVerify:false,note:'却下された提案です。却下だけでは元の不足は解消しません。'};
 if(s==='stale')return{canReview:false,canExport:false,canSubmit:false,canVerify:false,note:'計画が変わったため古い版です。再確認してください。'};
 if(s==='needs_evidence')return{canReview:false,canExport:false,canSubmit:false,canVerify:false,note:'証拠が不足しています。証拠をそろえてください。'};
 return{canReview:false,canExport:false,canSubmit:false,canVerify:false,note:'この状態では操作できません。'};
}
function gapCountText(count,confirmed){
 if(confirmed!==true)return '不足機能：未確認';
 const n=Number(count);
 if(!isFinite(n))return '不足機能：未確認';
 if(n===0)return '不足機能はありません';
 return '不足機能 '+String(n)+'件';
}
async function fetchJson(url,options){
 const r=await fetch(url,options);
 const d=await r.json().catch(()=>({}));
 if(!r.ok){
  const detail=typeof d.detail==='string'?d.detail:String((d&&d.detail&&d.detail.message)||d.reason||('HTTP '+r.status));
  const err=Error(detail||('HTTP '+r.status));err.status=r.status;throw err;
 }
 return d;
}
function apiGaps(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/capability-gaps');}
function apiPropose(pid,actor){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/capability-gaps/propose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor})});}
function apiDetail(pid,proposalId){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/capability-gaps/proposals/'+encodeURIComponent(proposalId));}
function apiReview(pid,proposalId,reviewer,reason){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/capability-gaps/proposals/'+encodeURIComponent(proposalId)+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewer:reviewer,reason:reason})});}
function apiExport(pid,proposalId,actor){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/capability-gaps/proposals/'+encodeURIComponent(proposalId)+'/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor})});}
function apiSubmit(pid,proposalId,actor,memo){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/capability-gaps/proposals/'+encodeURIComponent(proposalId)+'/submitted',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor,memo:memo||''})});}
function apiVerify(pid,proposalId,actor){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/capability-gaps/proposals/'+encodeURIComponent(proposalId)+'/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor})});}
function updateOverviewGap(missingCount,confirmed,hasMissing){
 const node=document.getElementById('workflowOverviewGap');
 if(node)node.textContent=gapCountText(missingCount,confirmed);
 const note=document.getElementById('workflowOverviewGapNote');
 if(note){
  note.replaceChildren();
  if(confirmed!==true){note.textContent='事前照合が未実施のため未確認です。';}
  else if(hasMissing){note.textContent='次の操作は「機能提案を確認」です。未実装なのに実行可能とは表示しません。';}
  else{note.textContent='';}
 }
}
function clearOverviewGapError(message){
 const node=document.getElementById('workflowOverviewGap');
 if(node)node.textContent='不足機能：未確認';
 const note=document.getElementById('workflowOverviewGapNote');
 if(note)note.textContent=message||'取得できませんでした。';
}
function mount(){
 function init(){
  let anchor=document.getElementById('workflow-goal_plan');
  if(!anchor)anchor=document.getElementById('workflow');
  if(!anchor){setTimeout(init,300);return;}
  if(document.getElementById('capabilityGapSection'))return;
  const box=document.createElement('section');box.className='mission-list';box.id='capabilityGapSection';
  addText(box,'h3','不足機能の提案');
  addText(box,'p','達成条件に必要な操作が無い場合、文章だけを増やさず不足機能として提案します。操作はローカルの作成・確認・指示書の保存だけであり、Jenkinsへの送信はしません。納品の入力は画面では行いません（状態表示と再判定のみ）。');
  const actorLabel=addText(box,'label','担当者名（必須。本人認証ではありません）');actorLabel.htmlFor='capabilityGapActor';
  const actor=document.createElement('input');actor.id='capabilityGapActor';actor.placeholder='操作する人の名前';actor.maxLength=100;box.append(actor);
  const proposeBtn=document.createElement('button');proposeBtn.type='button';proposeBtn.id='capabilityGapPropose';proposeBtn.textContent='不足機能を確認する（ローカルで作成）';
  const status=document.createElement('p');status.id='capabilityGapStatus';status.setAttribute('role','status');
  const listHost=document.createElement('div');listHost.id='capabilityGapList';
  const detailHost=document.createElement('div');detailHost.id='capabilityGapDetail';
  const delivery=document.createElement('p');delivery.id='capabilityGapDeliveryNote';delivery.textContent='納品の入力は画面では行いません。送付記録と再判定だけをこの画面で行い、実装完了・検証済みの有効化は再判定でのみ行います。';
  box.append(proposeBtn,status,listHost,detailHost,delivery);
  anchor.append(box);
  let epoch=0;
  function selected(){return currentPid();}
  async function load(){
   const force=Boolean(arguments[0]);
   void force;
   const pid=selected();
   const ticket=++epoch;
   const isCurrent=()=>ticket===epoch;
   if(!pid){listHost.replaceChildren();detailHost.replaceChildren();status.textContent='プロジェクトが選択されていません';clearOverviewGapError('プロジェクトが選択されていません');return;}
   status.textContent='読込中…';
   try{
    const data=await apiGaps(pid);
    if(ticket!==epoch)return;
    const preflight=data.preflight||{};
    const proposals=Array.isArray(data.proposals)?data.proposals:[];
    const missing=Array.isArray(preflight.missing_capability)?preflight.missing_capability:[];
    updateOverviewGap(missing.length,true,missing.length>0);
    renderList(listHost,detailHost,status,proposals,pid,isCurrent);
    if(!proposals.length){
     status.textContent=missing.length>0?'提案はまだありません。「不足機能を確認する」で作成してください。':'不足機能はありません';
    }else{
     status.textContent='';
    }
   }catch(e){
    if(ticket!==epoch)return;
    listHost.replaceChildren();detailHost.replaceChildren();
    const msg=e.message||'通信できませんでした';
    status.textContent=msg;
    clearOverviewGapError(msg);
   }
  }
  proposeBtn.onclick=async()=>{
   const pid=selected();if(!pid){status.textContent='プロジェクトが選択されていません';return;}
   const name=(actor.value||'').trim();
   if(!name){status.textContent='担当者名を入力してください';return;}
   proposeBtn.disabled=true;status.textContent='作成中…';
   try{
    await apiPropose(pid,name);
    await load(true);
   }catch(e){status.textContent=e.message||'作成できませんでした';proposeBtn.disabled=false;return;}
   proposeBtn.disabled=false;
  };
  const sel=document.getElementById('projectSelect');
  if(sel)sel.addEventListener('change',()=>{load(true);});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)return;load();});
  window.reloadCapabilityGap=function(){return load(true);};
  load(true);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
function renderList(listHost,detailHost,statusNode,proposals,pid,isCurrent){
 listHost.replaceChildren();detailHost.replaceChildren();
 if(!proposals.length){
  addText(listHost,'p','提案はまだありません。');
  return;
 }
 const list=document.createElement('ul');
 for(const item of proposals){
  const line=document.createElement('li');
  const pidText=String(item.proposal_id||'');
  const state=buttonStateFor(item.status);
  const criteria=Array.isArray(item.criterion_ids)?item.criterion_ids.join(' / '):'';
  addText(line,'strong',String(item.required_operation||pidText.slice(0,16)));
  addText(line,'p','状態：'+statusJa(item.status));
  addText(line,'p','必要な操作：'+String(item.required_operation||''));
  addText(line,'p','関係する達成条件：'+(criteria||'未取得'));
  addText(line,'p','権限区分：'+permissionJa(item.permission_class));
  if(state.note)addText(line,'p',state.note);
  const detailBtn=document.createElement('button');detailBtn.type='button';detailBtn.textContent='詳細を開く';
  detailBtn.onclick=async()=>{
   detailBtn.disabled=true;
   try{
    const full=await apiDetail(pid,pidText);
    if(!isCurrent())return;
    renderDetail(detailHost,statusNode,full,pid);
   }catch(e){statusNode.textContent=e.message||'詳細を取得できませんでした';}
   finally{detailBtn.disabled=false;}
  };
  line.append(detailBtn);
  const reviewBtn=document.createElement('button');reviewBtn.type='button';reviewBtn.textContent='確認済みにする';
  reviewBtn.disabled=!state.canReview;
  if(state.canReview){
   reviewBtn.onclick=()=>{renderDetailEditor(detailHost,statusNode,item,pid,'review');};
  }
  line.append(reviewBtn);
  const exportBtn=document.createElement('button');exportBtn.type='button';exportBtn.textContent='Jenkins指示を作成';
  exportBtn.disabled=!state.canExport;
  if(state.canExport){
   exportBtn.onclick=()=>{renderDetailEditor(detailHost,statusNode,item,pid,'export');};
  }
  line.append(exportBtn);
  const submitBtn=document.createElement('button');submitBtn.type='button';submitBtn.textContent='Jenkinsに渡した（記録）';
  submitBtn.disabled=!state.canSubmit;
  if(state.canSubmit){
   submitBtn.onclick=()=>{renderDetailEditor(detailHost,statusNode,item,pid,'submitted');};
  }
  line.append(submitBtn);
  const verifyBtn=document.createElement('button');verifyBtn.type='button';verifyBtn.textContent='再判定する';
  verifyBtn.disabled=!state.canVerify;
  if(state.canVerify){
   verifyBtn.onclick=()=>{renderDetailEditor(detailHost,statusNode,item,pid,'verify');};
  }
  line.append(verifyBtn);
  list.append(line);
 }
 listHost.append(list);
}
function renderDetail(host,statusNode,proposal,pid){
 host.replaceChildren();
 addText(host,'h4','提案の詳細');
 addText(host,'p','目標との関係：'+((proposal.criterion_ids||[]).join(' / ')||'未取得')+' / 工程：'+((proposal.task_keys||[]).join(' / ')||'未取得'));
 const alts=Array.isArray(proposal.existing_alternatives)?proposal.existing_alternatives:[];
 if(alts.length){
  addText(host,'p','現行機能で足りない理由：');
  const ul=document.createElement('ul');
  for(const alt of alts){
   addText(ul,'li',String((alt||{}).capability_id||'')+'：'+String((alt||{}).why_insufficient||''));
  }
  host.append(ul);
 }else{
  addText(host,'p','現行機能で足りない理由：未取得');
 }
 const contract=proposal.interface_contract||{};
 addText(host,'p','Jenkins指示の要点：入力 '+((contract.inputs||[]).join(' / ')||'-')+' / 出力 '+((contract.outputs||[]).join(' / ')||'-')+' / 失敗時 '+((contract.failure_states||[]).join(' / ')||'-'));
 const tests=Array.isArray(proposal.acceptance_tests)?proposal.acceptance_tests:[];
 addText(host,'p','開発後の再検証方法：同じ計画署名・同じ入力で事前照合を再生し、不足が消えたときだけ検証済みにします。受入条件：'+(tests.join(' / ')||'未取得'));
 addText(host,'p','状態：'+statusJa(proposal.status)+'。Jenkins指示を作成済みと実装完了・検証済みは別の状態です。実装が納品されても能力は有効になりません。元の停止が再判定で解消したときだけ検証済みになります。');
 renderDetailEditor(host,statusNode,proposal,pid,'');
 const details=document.createElement('details');
 addText(details,'summary','詳細を開く');
 const pre=document.createElement('pre');
 pre.textContent=JSON.stringify(proposal,null,2);
 details.append(pre);host.append(details);
}
function renderDetailEditor(host,statusNode,proposal,pid,mode){
 let editor=document.getElementById('capabilityGapEditor');
 if(!editor){
  editor=document.createElement('div');editor.id='capabilityGapEditor';host.append(editor);
 }else{
  editor.replaceChildren();
 }
 const state=buttonStateFor(proposal.status);
 const pidText=String(proposal.proposal_id||'');
 if(mode==='review'&&state.canReview){
  addText(editor,'h5','確認済みにする');
  addText(editor,'p','確認者名は本人認証ではありません（認証は未実装）。作業記録用の表示名です。');
  const reviewer=document.createElement('input');reviewer.placeholder='担当者名（必須）';reviewer.maxLength=100;editor.append(reviewer);
  const reason=document.createElement('input');reason.placeholder='確認の理由（必須）';reason.maxLength=2000;editor.append(reason);
  const btn=document.createElement('button');btn.type='button';btn.textContent='確認済みにする';
  btn.onclick=async()=>{
   const name=(reviewer.value||'').trim();const rsn=(reason.value||'').trim();
   if(!name){statusNode.textContent='担当者名を入力してください';return;}
   if(!rsn){statusNode.textContent='確認の理由を入力してください';return;}
   btn.disabled=true;statusNode.textContent='確認中…';
   try{
    await apiReview(pid,pidText,name,rsn);
    statusNode.textContent='確認済みにしました。';
    if(typeof window.reloadCapabilityGap==='function')await window.reloadCapabilityGap();
   }catch(e){statusNode.textContent=e.message||'確認できませんでした';btn.disabled=false;}
  };
  editor.append(btn);
 }
 if(mode==='export'&&state.canExport){
  addText(editor,'h5','Jenkins指示を作成');
  addText(editor,'p','承認前のローカル保存であり、Jenkins へは送信されません。ファイル本文は表示しません。');
  const actorInput=document.createElement('input');actorInput.placeholder='担当者名（必須）';actorInput.maxLength=100;editor.append(actorInput);
  const btn=document.createElement('button');btn.type='button';btn.textContent='Jenkins指示を作成';
  btn.onclick=async()=>{
   const name=(actorInput.value||'').trim();
   if(!name){statusNode.textContent='担当者名を入力してください';return;}
   btn.disabled=true;statusNode.textContent='作成中…';
   try{
    const out=await apiExport(pid,pidText,name);
    editor.replaceChildren();
    addText(editor,'p','承認前のローカル保存であり、Jenkins へは送信されません。');
    addText(editor,'p','保存先：'+String(out.package_rel||''));
    addText(editor,'p','instruction.json SHA-256：'+String(out.instruction_sha256||''));
    addText(editor,'p','REQUEST.md SHA-256：'+String(out.request_sha256||''));
    addText(editor,'p','manifest.json SHA-256：'+String(out.manifest_sha256||''));
    addText(editor,'p','基準版：'+String(out.base_ref||''));
    statusNode.textContent='Jenkins指示を作成済みにしました。実装完了・検証済みとは別の状態です。';
    if(typeof window.reloadCapabilityGap==='function')await window.reloadCapabilityGap();
   }catch(e){statusNode.textContent=e.message||'作成できませんでした';btn.disabled=false;}
  };
  editor.append(btn);
 }
 if(mode==='submitted'&&state.canSubmit){
  addText(editor,'h5','Jenkinsに渡した（記録）');
  addText(editor,'p','人がパッケージを渡したことの記録であり、実際には何も送信しません。');
  const actorInput=document.createElement('input');actorInput.placeholder='担当者名（必須）';actorInput.maxLength=100;editor.append(actorInput);
  const memoInput=document.createElement('input');memoInput.placeholder='メモ（任意）';memoInput.maxLength=2000;editor.append(memoInput);
  const btn=document.createElement('button');btn.type='button';btn.textContent='Jenkinsに渡した（記録）';
  btn.onclick=async()=>{
   const name=(actorInput.value||'').trim();
   if(!name){statusNode.textContent='担当者名を入力してください';return;}
   btn.disabled=true;statusNode.textContent='記録中…';
   try{
    await apiSubmit(pid,pidText,name,(memoInput.value||'').trim());
    statusNode.textContent='送付を記録しました。実際には何も送信していません。';
    if(typeof window.reloadCapabilityGap==='function')await window.reloadCapabilityGap();
   }catch(e){statusNode.textContent=e.message||'記録できませんでした';btn.disabled=false;}
  };
  editor.append(btn);
 }
 if(mode==='verify'&&state.canVerify){
  addText(editor,'h5','再判定する');
  addText(editor,'p','元の計画署名・同じ入力で事前照合を再生します。元の停止が残っている場合は検証済みになりません。');
  const actorInput=document.createElement('input');actorInput.placeholder='担当者名（必須）';actorInput.maxLength=100;editor.append(actorInput);
  const btn=document.createElement('button');btn.type='button';btn.textContent='再判定する';
  btn.onclick=async()=>{
   const name=(actorInput.value||'').trim();
   if(!name){statusNode.textContent='担当者名を入力してください';return;}
   btn.disabled=true;statusNode.textContent='再判定中…';
   try{
    const out=await apiVerify(pid,pidText,name);
    editor.replaceChildren();
    if(out.verified){addText(editor,'p','元の停止が再判定で解消しました。');}
    else{addText(editor,'p','元の停止が残っています。テスト成功だけでは能力は有効になりません。');}
    statusNode.textContent=out.verified?'再判定で不足が解消しました。':'再判定しましたが不足が残っています。';
    if(typeof window.reloadCapabilityGap==='function')await window.reloadCapabilityGap();
   }catch(e){statusNode.textContent=e.message||'再判定できませんでした';btn.disabled=false;}
  };
  editor.append(btn);
 }
}
if(typeof document!=='undefined')mount();
if(typeof module!=='undefined')module.exports={STATUS_JA,PERMISSION_JA,statusJa,permissionJa,buttonStateFor,gapCountText};
})();
