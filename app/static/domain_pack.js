(()=>{'use strict';
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
function currentPid(){return (document.getElementById('projectSelect')||{}).value||'';}
const CANDIDATE_STATUS_JA={decided:'候補あり',ambiguous:'候補が複数',none:'候補なし',unknown:'確認できず'};
const VERDICT_JA={conforms:'適合',violates:'違反',unknown:'確認できず'};
const PERMISSION_JA={external_send:'外部送信',external_publish:'外部公開',customer_contact:'顧客接触',rag_register:'RAG登録',plan_approve:'計画承認'};
const EXECUTOR_JA={'source.read_excerpt':'保存された原本版のunitだけを読む','workspace.write_text':'契約で許可された出力先のみ','table.profile':'登録表データの構造確認','table.transform':'許可された宣言型の集計・加工','web.collect_public':'許可された公開調査のみ。取得成功は別途検証','human:approved_action':'人の承認済み操作','human:review':'人の確認'};
function candidateStatusJa(status){const s=String(status||'');if(Object.prototype.hasOwnProperty.call(CANDIDATE_STATUS_JA,s))return CANDIDATE_STATUS_JA[s];return '確認できず';}
function verdictJa(verdict){const v=String(verdict||'');if(Object.prototype.hasOwnProperty.call(VERDICT_JA,v))return VERDICT_JA[v];return '確認できず';}
function executorJa(executorId){const s=String(executorId||'');if(Object.prototype.hasOwnProperty.call(EXECUTOR_JA,s))return EXECUTOR_JA[s];return '';}
function countConformance(tasks){const counts={conforms:0,violates:0,unknown:0};const rows=Array.isArray(tasks)?tasks:[];for(const row of rows){const v=String((row||{}).verdict||'');if(v==='conforms')counts.conforms+=1;else if(v==='violates')counts.violates+=1;else counts.unknown+=1;}return counts;}
function countText(counts){return '適合 '+counts.conforms+'件 / 違反 '+counts.violates+'件 / 確認できず '+counts.unknown+'件';}
function warningSummary(conformance){const report=conformance||{};const tasks=Array.isArray(report.tasks)?report.tasks:[];const bad=tasks.filter(row=>String((row||{}).verdict||'')!=='conforms');const parts=bad.map(row=>{const key=String((row||{}).task_key||'(工程不明)');const reasons=Array.isArray((row||{}).reasons)?(row||{}).reasons.join('; '):'';return key+':'+verdictJa((row||{}).verdict)+(reasons?':'+reasons:'');});if(!parts.length&&String(report.verdict||'')==='unknown'&&report.reason)parts.push(String(report.reason));if(!parts.length&&!tasks.length&&report.reason)parts.push(String(report.reason));return parts;}
function packButtonState(selection,conformance,inputs){
 const opts=inputs||{};
 const actor=String(opts.actor||'').trim();
 const reason=String(opts.reason||'').trim();
 const acknowledged=opts.acknowledged===true;
 const busy=opts.busy===true;
 const hasActor=actor!=='';
 const hasReason=reason!=='';
 const hasSelection=Boolean(selection&&String(selection.domain_id||'')!=='');
 const report=conformance||null;
 const isConforming=Boolean(report&&report.conforms===true&&String(report.verdict||'')==='conforms');
 const needsAck=!isConforming;
 const canSelect=hasActor&&hasReason&&!busy&&(isConforming||acknowledged);
 const canClear=hasActor&&hasReason&&!busy&&hasSelection;
 let selectNote='';
 if(busy)selectNote='処理中のため操作できません。完了後に再試行してください。';
 else if(!hasActor)selectNote='担当者名を入力してください。';
 else if(!hasReason)selectNote='理由を入力してください。';
 else if(needsAck&&!acknowledged)selectNote='適合していない（違反または確認できずを含む）ため、警告を確認するチェックが必要です。確認できずは適合に数えません。';
 const clearNote=(!hasActor||!hasReason)?'解除には担当者名と理由が必要です。':(busy?'処理中のため操作できません。':(hasSelection?'':'選択が無いため解除できません。'));
 return {canSelect:canSelect,canClear:canClear,needsAck:needsAck,isConforming:isConforming,hasSelection:hasSelection,selectNote:selectNote,clearNote:clearNote};
}
async function fetchJson(url,options){
 const r=await fetch(url,options);
 let d;try{d=await r.json();}catch(_error){throw Error('応答を読み取れませんでした。再読み込みしてください');}
 if(!r.ok){
  const detail=typeof d.detail==='string'?d.detail:String((d&&d.detail&&d.detail.message)||d.reason||('HTTP '+r.status));
  const err=Error(detail||('HTTP '+r.status));err.status=r.status;throw err;
 }
 return d;
}
function apiPacks(){return fetchJson('/api/domain-packs').then(d=>{if(!d||!Array.isArray(d.packs))throw Error('パック一覧を確認できませんでした');return d;});}
function apiProjectPacks(pid){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/domain-packs').then(d=>{if(!d||!Object.prototype.hasOwnProperty.call(d,'selection')||!d.candidates||!Array.isArray(d.candidates.candidates))throw Error('案件のパック情報を確認できませんでした');return d;});}
function apiConformance(pid,domainId,version){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/domain-packs/'+encodeURIComponent(domainId)+'/conformance?version='+encodeURIComponent(version||'v1')).then(d=>{if(!d||!['conforms','violates','unknown'].includes(d.verdict)||typeof d.conforms!=='boolean'||!Array.isArray(d.tasks))throw Error('適合検査の結果を確認できませんでした');return d;});}
function apiSelect(pid,domainId,version,actor,reason){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/domain-packs/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({domain_id:domainId,version:version,actor:actor,reason:reason})}).then(d=>{if(!d||d.ok!==true)throw Error('選択の記録を確認できませんでした');return d;});}
function apiClear(pid,actor,reason){return fetchJson('/api/projects/'+encodeURIComponent(pid)+'/domain-packs/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor,reason:reason})}).then(d=>{if(!d||d.ok!==true)throw Error('解除の記録を確認できませんでした');return d;});}
function permissionBoundaryText(boundary){
 const b=boundary||{};
 const keys=['external_send','external_publish','customer_contact','rag_register','plan_approve'];
 const denied=keys.filter(k=>b[k]===false);
 const allowed=keys.filter(k=>b[k]===true);
 if(allowed.length===0&&denied.length===keys.length)return '権限境界: すべて不許可（外部送信・外部公開・顧客接触・RAG登録・計画承認の個別許可なし）';
 const names=list=>list.map(k=>PERMISSION_JA[k]||k).join('・');
 let text='権限境界: ';
 if(allowed.length)text+='許可あり（'+names(allowed)+'）。この画面では採用の判断材料にし、許可の付与は行いません。';
 else text+='許可なし';
 if(denied.length)text+=' / 不許可（'+names(denied)+'）';
 const unknownKeys=Object.keys(b).filter(k=>keys.indexOf(k)<0);
 if(unknownKeys.length)text+=' / 不明な権限キーあり（確認できず）:'+unknownKeys.join(',');
 return text;
}
function renderPackList(host,data){
 host.replaceChildren();
 addText(host,'p','形式: '+String(data.schema||'不明'));
 const packs=Array.isArray(data.packs)?data.packs:[];
 if(!packs.length){addText(host,'p','登録済みの汎用パックがありません。');return;}
 addText(host,'p','登録済みの汎用パックは'+packs.length+'件です。実案件の表示・パックは作りません。');
 const list=document.createElement('ul');
 for(const spec of packs){
  const line=document.createElement('li');
  addText(line,'strong',String(spec.label_ja||spec.domain_id||''));
  addText(line,'p','ID: '+String(spec.domain_id||'')+' / 版: '+String(spec.version||''));
  const executors=Array.isArray(spec.executor_ids)?spec.executor_ids:[];
  if(executors.length){
   const names=executors.map(id=>{const label=executorJa(id);return label?String(id)+'（'+label+'）':String(id);});
   addText(line,'p','実行器: '+names.join(' / '));
  }else{
   addText(line,'p','実行器: 確認できず');
  }
  addText(line,'p',permissionBoundaryText(spec.permission_boundary));
  list.append(line);
 }
 host.append(list);
}
function candidateNote(status){
 const s=String(status||'');
 if(s==='decided')return '候補です。採用は人が決めます。自動では選びません。';
 if(s==='ambiguous')return '候補が同点のため、自動では選べません。人が選んでください。';
 if(s==='none')return '該当する候補がありません。';
 return '確認できず。適合・選択可とは表示しません。';
}
function renderCandidates(host,statusNode,candidatesBox,pid,body,isCurrent,conformanceCache,onConformance){
 host.replaceChildren();
 const info=(body&&body.candidates)||{};
 const status=String(info.status||'unknown');
 const reason=String(info.reason||'');
 const list=Array.isArray(info.candidates)?info.candidates:[];
 addText(host,'p','候補の状態: '+candidateStatusJa(status));
 addText(host,'p',candidateNote(status));
 if(reason)addText(host,'p','理由: '+reason);
 if(!list.length){addText(host,'p','候補の一覧がありません。');return;}
 const ul=document.createElement('ul');
 for(const item of list){
  const line=document.createElement('li');
  addText(line,'strong',String(item.label_ja||item.domain_id||''));
  addText(line,'p','ID: '+String(item.domain_id||'')+' / 版: '+String(item.version||'')+' / 適合度: '+String(item.score)+'点（文章の一致 '+String(item.statement_hits)+'件 / 工程の一致 '+String(item.task_hits)+'件）');
  const checkBtn=document.createElement('button');checkBtn.type='button';checkBtn.textContent='適合を確認';
  checkBtn.onclick=async()=>{
   checkBtn.disabled=true;statusNode.textContent='適合検査を読込中…';
   try{
    const report=await apiConformance(pid,String(item.domain_id||''),String(item.version||'v1'));
    if(!isCurrent())return;
    conformanceCache.key=String(item.domain_id||'')+' '+String(item.version||'v1');
    conformanceCache.report=report;
    statusNode.textContent='';
    onConformance(report,item);
   }catch(e){if(!isCurrent())return;statusNode.textContent='適合検査を取得できませんでした: '+(e.message||'通信できませんでした');}
   finally{checkBtn.disabled=false;}
  };
  line.append(checkBtn);
  ul.append(line);
 }
 host.append(ul);
 if(info.selected&&String(status)==='decided'){
  addText(host,'p','適合度の筆頭候補: '+String(info.selected.label_ja||info.selected.domain_id||'')+'。候補です。採用は人が決めます。');
 }
}
function renderConformance(host,report){
 host.replaceChildren();
 if(!report){addText(host,'p','適合検査はまだ実行していません。候補ごとの「適合を確認」を押したときだけ取得します。');return;}
 addText(host,'p','対象: '+String(report.domain_id||'')+' '+String(report.version||''));
 addText(host,'p','全体: '+verdictJa(report.verdict)+(report.reason?'（'+String(report.reason)+'）':''));
 const counts=countConformance(report.tasks);
 const serverCounts=(report&&report.counts)||{};
 void serverCounts;
 addText(host,'p','件数: '+countText(counts)+'。確認できずは適合に数えません。');
 const skipped=Array.isArray(report.skipped_task_keys)?report.skipped_task_keys:[];
 if(skipped.length)addText(host,'p','検査対象外の工程: '+skipped.join(' / '));
 const rows=Array.isArray(report.tasks)?report.tasks:[];
 if(!rows.length){addText(host,'p','評価できる工程がありません。適合と判定しません。');return;}
 const ul=document.createElement('ul');
 for(const row of rows){
  const line=document.createElement('li');
  addText(line,'strong',String(row.task_key||'(工程不明)')+': '+verdictJa(row.verdict));
  const reasons=Array.isArray(row.reasons)?row.reasons:[];
  if(reasons.length)addText(line,'p','理由: '+reasons.join('; '));
  ul.append(line);
 }
 host.append(ul);
}
function formBusy(selectBtn,clearBtn){return selectBtn.dataset.busy==='1'||clearBtn.dataset.busy==='1';}
function refreshSelectButton(selectBtn,clearBtn,ackBox,ackNote,selection,conformanceCache,actorInput,reasonInput){
 const inputs={actor:actorInput.value,reason:reasonInput.value,acknowledged:ackBox.checked,busy:formBusy(selectBtn,clearBtn)};
 const state=packButtonState(selection,conformanceCache.report,inputs);
 const warnings=warningSummary(conformanceCache.report);
 ackNote.replaceChildren();
 if(state.needsAck){
  if(warnings.length){
   addText(ackNote,'p','警告: '+warnings.join(' / ')+'。適合していないため記録には確認が必要です。');
  }else{
   addText(ackNote,'p','警告: 適合を確認できていません（違反または確認できずの可能性）。確認できずは適合に数えません。');
  }
  ackBox.disabled=false;
 }else{
  addText(ackNote,'p','適合している候補です。記録は人が行います。');
  ackBox.disabled=false;
 }
 selectBtn.disabled=!state.canSelect;
 clearBtn.disabled=!state.canClear;
 return state;
}
function mount(){
 function init(){
  let anchor=document.getElementById('workflow-goal_plan');
  if(!anchor)anchor=document.getElementById('workflow');
  if(!anchor){setTimeout(init,300);return;}
  if(document.getElementById('domainPackSection'))return;
  const box=document.createElement('section');box.className='mission-list';box.id='domainPackSection';
  addText(box,'h3','分野実行パック（汎用）');
  addText(box,'p','登録済みの汎用パックの一覧と、PJごとの候補・適合検査・選択の記録だけを表示します。実案件の表示・パックは作りません。選択は記録のみで、計画・工程・実行は変わりません。実行の振り分けは変更しません。');
  const status=document.createElement('p');status.id='domainPackStatus';status.setAttribute('role','status');
  const packHost=document.createElement('div');packHost.id='domainPackList';
  const candHost=document.createElement('div');candHost.id='domainPackCandidates';
  const confHost=document.createElement('div');confHost.id='domainPackConformance';
  const selHost=document.createElement('div');selHost.id='domainPackSelection';
  addText(box,'h4','登録済みの汎用パック');
  box.append(packHost);
  addText(box,'h4','PJの候補');
  box.append(candHost);
  addText(box,'h4','適合検査');
  box.append(confHost);
  addText(box,'h4','選択の記録');
  addText(box,'p','記録のみで、計画・工程・実行は変わりません。');
  box.append(selHost,status);
  anchor.append(box);
  let epoch=0;
  let pendingProjectPolls=0;
  const conformanceCache={key:'',report:null};
  function selected(){return currentPid();}
  function renderSelection(selection){
   selHost.replaceChildren();
   addText(selHost,'p','記録のみで、計画・工程・実行は変わりません。');
   if(selection&&String(selection.domain_id||'')){
    addText(selHost,'p','現在の選択: '+String(selection.domain_id)+' '+String(selection.version||'')+' / 選択者: '+String(selection.actor||'')+' / 時刻: '+String(selection.updated_at||selection.selected_at||''));
    const warnings=Array.isArray(selection.warnings)?selection.warnings:[];
    if(warnings.length)addText(selHost,'p','適合の警告: '+warnings.join(' / '));
    else addText(selHost,'p',selection.conforms?'適合の警告はありません。':'適合の警告: 適合していない記録です。');
   }else{
    addText(selHost,'p','現在の選択: 未選択');
   }
   addText(selHost,'p','確認者名は本人認証ではありません（認証は未実装）。作業記録用の表示名です。');
   const actorLabel=addText(selHost,'label','担当者名（必須）');actorLabel.htmlFor='domainPackActor';
   const actor=document.createElement('input');actor.id='domainPackActor';actor.placeholder='操作する人の名前';actor.maxLength=100;selHost.append(actor);
   const reasonLabel=addText(selHost,'label','理由（必須）');reasonLabel.htmlFor='domainPackReason';
   const reason=document.createElement('input');reason.id='domainPackReason';reason.placeholder='採用・解除の理由';reason.maxLength=2000;selHost.append(reason);
   const ackLabel=document.createElement('label');
   const ack=document.createElement('input');ack.type='checkbox';ack.id='domainPackAck';
   ackLabel.append(ack);
   ackLabel.append(document.createTextNode('警告を確認しました（適合していない場合に必須）'));
   selHost.append(ackLabel);
   const ackNote=document.createElement('div');ackNote.id='domainPackAckNote';selHost.append(ackNote);
   const selectBtn=document.createElement('button');selectBtn.type='button';selectBtn.id='domainPackSelect';selectBtn.textContent='このパックを採用として記録する';
   const clearBtn=document.createElement('button');clearBtn.type='button';clearBtn.id='domainPackClear';clearBtn.textContent='採用を解除する';
   selHost.append(selectBtn,clearBtn);
   const targetLabel=document.createElement('p');targetLabel.id='domainPackTargetNote';targetLabel.textContent='採用対象: 候補一覧で「適合を確認」したパック（未確認のまま記録する場合は警告の確認が必要です）';selHost.append(targetLabel);
   let target={domain_id:'',version:'v1',label_ja:''};
   function syncButtons(){return refreshSelectButton(selectBtn,clearBtn,ack,ackNote,selection,conformanceCache,actor,reason);}
   actor.addEventListener('input',syncButtons);
   reason.addEventListener('input',syncButtons);
   ack.addEventListener('change',syncButtons);
   selHost.dataset.targetDomain='';
   selHost.dataset.targetVersion='v1';
   function setTarget(item){
    target={domain_id:String((item||{}).domain_id||''),version:String((item||{}).version||'v1'),label_ja:String((item||{}).label_ja||'')};
    selHost.dataset.targetDomain=target.domain_id;
    selHost.dataset.targetVersion=target.version;
    targetLabel.textContent=target.domain_id?('採用対象: '+target.domain_id+' '+target.version):'採用対象: 候補一覧で「適合を確認」したパック（未確認のまま記録する場合は警告の確認が必要です）';
    syncButtons();
   }
   box.dataset.targetSetter='1';
   box.setTargetPack=setTarget;
   selectBtn.onclick=async()=>{
    const pid=selected();if(!pid){status.textContent='プロジェクトが選択されていません';return;}
    const state=syncButtons();
    if(!state.canSelect){status.textContent=state.selectNote||'入力を確認してください';return;}
    if(!target.domain_id){status.textContent='候補一覧で「適合を確認」したパックを選んでください';return;}
    selectBtn.disabled=true;selectBtn.dataset.busy='1';clearBtn.disabled=true;status.textContent='記録中…';
    try{
     const out=await apiSelect(pid,target.domain_id,target.version,actor.value.trim(),reason.value.trim());
     if(selected()!==pid)return;
     const authNote=String(out.auth_note||'');
     void authNote;
     status.textContent=String(out.note||'記録しました')+'（記録のみで、計画・工程・実行は変わりません）';
     await load(true);
    }catch(e){if(selected()!==pid)return;status.textContent='記録できませんでした: '+(e.message||'通信できませんでした');selectBtn.disabled=false;selectBtn.dataset.busy='';syncButtons();return;}
    selectBtn.dataset.busy='';
   };
   clearBtn.onclick=async()=>{
    const pid=selected();if(!pid){status.textContent='プロジェクトが選択されていません';return;}
    const state=syncButtons();
    if(!state.canClear){status.textContent=state.clearNote||'解除できません';return;}
    selectBtn.disabled=true;clearBtn.disabled=true;clearBtn.dataset.busy='1';status.textContent='解除中…';
    try{
     const out=await apiClear(pid,actor.value.trim(),reason.value.trim());
     if(selected()!==pid)return;
     status.textContent=String(out.note||'解除しました')+'（記録のみで、計画・工程・実行は変わりません）';
     await load(true);
    }catch(e){if(selected()!==pid)return;status.textContent='解除できませんでした: '+(e.message||'通信できませんでした');clearBtn.disabled=false;clearBtn.dataset.busy='';syncButtons();return;}
    clearBtn.dataset.busy='';
   };
   syncButtons();
  }
  function onConformance(report,item){
   renderConformance(confHost,report);
   if(item&&box.setTargetPack)box.setTargetPack(item);
   const inputs={actor:(document.getElementById('domainPackActor')||{}).value||'',reason:(document.getElementById('domainPackReason')||{}).value||'',acknowledged:Boolean((document.getElementById('domainPackAck')||{}).checked),busy:false};
   const state=packButtonState(renderSelection.current||null,report,inputs);
   void state;
  }
  async function load(){
   const force=Boolean(arguments[0]);
   void force;
   const pid=selected();
   const ticket=++epoch;
   const isCurrent=()=>ticket===epoch;
   if(!pid){packHost.replaceChildren();candHost.replaceChildren();confHost.replaceChildren();selHost.replaceChildren();status.textContent='プロジェクトが選択されていません';if(pendingProjectPolls<30){pendingProjectPolls++;setTimeout(()=>{if(ticket===epoch)load(true);},500);}return;}
   pendingProjectPolls=0;
   status.textContent='読込中…';
   try{
    const packs=await apiPacks();
    if(ticket!==epoch)return;
    renderPackList(packHost,packs);
   }catch(e){
    if(ticket!==epoch)return;
    packHost.replaceChildren();candHost.replaceChildren();confHost.replaceChildren();selHost.replaceChildren();
    status.textContent='パック一覧を取得できませんでした: '+(e.message||'通信できませんでした');
    return;
   }
   try{
    const body=await apiProjectPacks(pid);
    if(ticket!==epoch)return;
    const info=(body&&body.candidates)||{};
    conformanceCache.key='';conformanceCache.report=null;
    renderConformance(confHost,null);
    renderSelection(body.selection||null);
    renderSelection.current=body.selection||null;
    renderCandidates(candHost,status,confHost,pid,body,isCurrent,conformanceCache,onConformance);
    if(String(info.status||'')==='unknown'){
     status.textContent='候補を確認できませんでした: '+String(info.reason||'理由不明');
    }else{
     status.textContent='';
    }
   }catch(e){
    if(ticket!==epoch)return;
    packHost.replaceChildren();candHost.replaceChildren();confHost.replaceChildren();selHost.replaceChildren();
    const msg=e.message||'通信できませんでした';
    if(e.status===410||e.status===404)status.textContent=msg;
    else status.textContent='候補を取得できませんでした: '+msg;
   }
  }
  const sel=document.getElementById('projectSelect');
  if(sel)sel.addEventListener('change',()=>{load(true);});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)return;load();});
  window.reloadDomainPack=function(){return load(true);};
  load(true);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
if(typeof document!=='undefined')mount();
if(typeof module!=='undefined')module.exports={CANDIDATE_STATUS_JA,VERDICT_JA,PERMISSION_JA,EXECUTOR_JA,candidateStatusJa,verdictJa,executorJa,countConformance,countText,warningSummary,packButtonState,permissionBoundaryText,candidateNote,formBusy,fetchJson,apiPacks,apiProjectPacks,apiConformance,apiSelect,apiClear};
})();
