(()=>{'use strict';

function init(){
 const host=document.getElementById('workflow-artifacts')||document.getElementById('experienceImportPanel')||document.getElementById('experienceImportRoot')||document.querySelector('.mission-shell')||document.body;
 if(!host){setTimeout(init,300);return;}

 let panel=document.getElementById('experienceImportPanel');
 if(!panel){
  panel=document.createElement('section');
  panel.className='card experience-import-panel';
  panel.id='experienceImportPanel';
  host.append(panel);
 }else if(host!==panel&&host.id==='workflow-artifacts'&&panel.parentElement!==host){
  host.append(panel);
 }

 const heading=document.createElement('h2');
 heading.textContent='経験RAG取り込み(成功事例)';

 const desc=document.createElement('p');
 desc.className='experience-import-desc';
 desc.textContent='収集エージェントが集めた成功事例は、まず候補(candidate)として受領されます。RAG検索・計画・TRIZには反映されません。原本参照・適用条件・確認記録を満たしたものだけ、人が承認できます。確認者名は本人確認を保証しません。';

 const candidateSection=document.createElement('div');
 candidateSection.id='experienceCandidateSection';
 candidateSection.className='experience-candidate-section';
 const candidateHeading=document.createElement('h3');
 candidateHeading.textContent='承認待ち候補(candidate)';
 const candidateNote=document.createElement('p');
 candidateNote.className='experience-candidate-note';
 candidateNote.textContent='候補は検索・RAG参照・計画・TRIZへ渡りません。確認者名は本人確認を保証しません(認証は未実装)。';
 const candidateStatus=document.createElement('p');
 candidateStatus.id='experienceCandidateStatus';
 candidateStatus.className='experience-status-line';
 const candidateList=document.createElement('ul');
 candidateList.id='experienceCandidateList';
 candidateList.className='experience-candidate-list';
 const reviewerRow=document.createElement('div');
 reviewerRow.className='experience-field-row'; const reviewerLabel=document.createElement('label');
 reviewerLabel.textContent='確認者名(承認・却下・差戻しの必須入力): ';
 const reviewerInput=document.createElement('input');
 reviewerInput.type='text';
 reviewerInput.id='experienceReviewer';
 reviewerInput.maxLength=100;
 reviewerLabel.append(reviewerInput);
 const reasonLabel=document.createElement('label');
 reasonLabel.textContent='理由(必須): ';
 const reasonInput=document.createElement('input');
 reasonInput.type='text';
 reasonInput.id='experienceReviewReason';
 reasonInput.maxLength=4000;
 reasonLabel.append(reasonInput);
 const versionLabel=document.createElement('label');
 versionLabel.textContent='適用する案件入力版(承認時のみ必須): ';
 const versionInput=document.createElement('input');
 versionInput.type='text';
 versionInput.id='experienceInputVersion';
 versionInput.maxLength=64;
 versionInput.placeholder='例: v3';
 versionLabel.append(versionInput);
 const versionNote=document.createElement('p');
 versionNote.className='experience-input-version-note';
 versionNote.textContent='この事例を適用する案件入力版を指定してください。収集エージェントは入力版を保証しません。';
 const reloadBtn=document.createElement('button');
 reloadBtn.type='button';
 reloadBtn.id='experienceCandidateReloadBtn';
 reloadBtn.textContent='候補一覧を更新';
 reviewerRow.append(reviewerLabel,reasonLabel,versionLabel,reloadBtn);
 candidateSection.append(candidateHeading,candidateNote,reviewerRow,versionNote,candidateStatus,candidateList);

 const indexSection=document.createElement('div');
 indexSection.id='experienceIndexSection';
 indexSection.className='experience-index-section';
 const indexHeading=document.createElement('h3');
 indexHeading.textContent='索引状態(verified事例の索引: pending/indexed/failed)';
 const indexNote=document.createElement('p');
 indexNote.className='experience-index-note';
 indexNote.textContent='verifiedのままでもfailedは「検索可能」と表示しません。索引は再生成可能なキャッシュであり正本の承認状態より強い権限を持ちません。';
 const indexStatus=document.createElement('p');
 indexStatus.id='experienceIndexStatus';
 indexStatus.className='experience-status-line';
 const indexList=document.createElement('ul');
 indexList.id='experienceIndexList';
 indexList.className='experience-index-list';
 const reindexRow=document.createElement('div');
 reindexRow.className='experience-field-row';
 const reindexBtn=document.createElement('button');
 reindexBtn.type='button';
 reindexBtn.id='experienceReindexBtn';
 reindexBtn.textContent='失敗分のみ再索引';
 reindexBtn.addEventListener('click',()=>{reindexFailures();});
 reindexRow.append(reindexBtn);
 indexSection.append(indexHeading,indexNote,indexStatus,indexList,reindexRow);

 const statusLine=document.createElement('p');
 statusLine.id='experienceImportStatus';
 statusLine.className='experience-status-line';

 const fileRow=document.createElement('div');
 fileRow.className='experience-field-row';
 const fileLabel=document.createElement('label');
 fileLabel.textContent='エクスポートJSONファイル選択: ';
 const fileInput=document.createElement('input');
 fileInput.type='file';
 fileInput.id='experienceJsonFile';
 fileInput.accept='.json,application/json';
 fileLabel.append(fileInput);
 fileRow.append(fileLabel);

 const previewBox=document.createElement('div');
 previewBox.id='experiencePreviewBox';
 previewBox.className='experience-preview-box';
 previewBox.hidden=true;

 const previewSummary=document.createElement('p');
 previewSummary.id='experiencePreviewSummary';
 previewSummary.className='experience-preview-summary';

 const previewList=document.createElement('ul');
 previewList.id='experiencePreviewList';
 previewList.className='experience-preview-list';
 previewList.style.maxHeight='220px';
 previewList.style.overflowY='auto';

 previewBox.append(previewSummary,previewList);

 const formRow=document.createElement('div');
 formRow.className='experience-form-row';

 const actorLabel=document.createElement('label');
 actorLabel.textContent='回答者名(必須): ';
 const actorInput=document.createElement('input');
 actorInput.type='text';
 actorInput.id='experienceActor';
 actorInput.maxLength=100;
 actorLabel.append(actorInput);

 const confirmLabel=document.createElement('label');
 confirmLabel.className='experience-confirm-label';
 const confirmBox=document.createElement('input');
 confirmBox.type='checkbox';
 confirmBox.id='experienceRagConfirm';
 confirmLabel.append(confirmBox,document.createTextNode('上記の内容を人が確認し、経験RAGへ登録することを確認しました'));

 const submitBtn=document.createElement('button');
 submitBtn.type='button';
 submitBtn.id='experienceImportBtn';
 submitBtn.textContent='経験RAGへ登録';
 submitBtn.disabled=true;

 formRow.append(actorLabel,confirmLabel,submitBtn);

 const actionError=document.createElement('p');
 actionError.id='experienceActionError';
 actionError.className='experience-action-error error';

 const actionResult=document.createElement('p');
 actionResult.id='experienceActionResult';
 actionResult.className='experience-action-result';
 actionResult.setAttribute('role','status');

 panel.append(heading,desc,statusLine,fileRow,previewBox,formRow,actionError,actionResult,candidateSection,indexSection);

 const el=id=>document.getElementById(id);
 let project='';
 let parsedItems=[];

 function selectedProject(){
  return (document.getElementById('projectSelect')||{}).value||'default';
 }

 function updateButtonState(){
  const actor=(el('experienceActor').value||'').trim();
  const confirmed=!!el('experienceRagConfirm').checked;
  const hasItems=parsedItems.length>0;
  el('experienceImportBtn').disabled=!actor||!confirmed||!hasItems;
 }

 function extractItemPreview(item){
  if(!item||typeof item!=='object')return '';
  const text=item.content||item.situation||item.lesson||item.title||item.action||item.outcome||'';
  const str=String(text).trim();
  return str.length>80?str.slice(0,80)+'…':str;
 }

 function handleFileSelect(e){
  actionError.textContent='';
  actionResult.textContent='';
  parsedItems=[];
  previewList.replaceChildren();
  previewBox.hidden=true;

  const file=e.target.files&&e.target.files[0];
  if(!file){
   updateButtonState();
   return;
  }

  const reader=new FileReader();
  reader.onload=()=>{
   try{
    const json=JSON.parse(reader.result);
    let items=[];
    if(json&&Array.isArray(json.items)){
     items=json.items;
    }else if(json&&json.export&&Array.isArray(json.export.items)){
     items=json.export.items;
    }else{
     throw Error('JSONに items 配列が見つかりません');
    }

    if(!items.length){
     throw Error('取り込み対象の事例が0件です');
    }

    parsedItems=items;
    previewSummary.textContent='読み込み件数: '+items.length+'件';
    items.forEach((it,idx)=>{
     const li=document.createElement('li');
     const previewText=extractItemPreview(it)||'(本文なし)';
     li.textContent='#'+(idx+1)+': '+previewText;
     previewList.append(li);
    });
    previewBox.hidden=false;
   }catch(err){
    actionError.textContent='JSONパースエラー: '+(err.message||String(err));
    parsedItems=[];
   }finally{
    updateButtonState();
   }
  };
  reader.onerror=()=>{
   actionError.textContent='ファイル読み込みに失敗しました';
   parsedItems=[];
   updateButtonState();
  };
  reader.readAsText(file,'utf-8');
 }

 async function submitImport(){
  actionError.textContent='';
  actionResult.textContent='';

  const actor=(el('experienceActor').value||'').trim();
  const confirmed=!!el('experienceRagConfirm').checked;

  if(!actor||!confirmed){
   actionError.textContent='回答者名の入力と確認チェックが必要です';
   return;
  }
  if(!parsedItems.length){
   actionError.textContent='取り込むJSONファイルを選択してください';
   return;
  }

  project=selectedProject();
  el('experienceImportBtn').disabled=true;

  try{
   const response=await fetch('/api/projects/'+encodeURIComponent(project)+'/experience/import-success-cases',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
     items:parsedItems,
     proof:'成功事例収集エージェントでの人手レビュー済み',
     actor:actor,
     confirm_rag:confirmed
    })
   });

   const data=await response.json().catch(()=>({}));

   if(response.status===409){
    const detail=data&&data.detail;
    const code=detail&&(detail.code||detail.error_code);
    if(code==='EXPERIENCE_OFF'){
     throw Error('この設定ではまだ経験RAGが有効になっていません');
    }
    if(code==='RAG_NOT_CONFIRMED'){
     throw Error('確認チェックが送信されていません(通常起きません)');
    }
    const msg=(detail&&(detail.message||detail))||data.message||'登録が拒絶されました';
    throw Error(typeof msg==='string'?msg:JSON.stringify(msg));
   }

   if(!response.ok){
    const msg=(data&&data.detail)||(data&&data.message)||'登録に失敗しました';
    throw Error(typeof msg==='string'?msg:JSON.stringify(msg));
   }

   const reg=data.registered!=null?data.registered:0;
   const skip=data.skipped_duplicate!=null?data.skipped_duplicate:0;
   const failCount=Array.isArray(data.failed)?data.failed.length:0;
   const accepted=data.accepted_status||'candidate';

   actionResult.textContent='受領完了('+accepted+'): '+reg+'件受領 / '+skip+'件重複スキップ / '+failCount+'件失敗。候補はRAG検索・計画・TRIZへ渡りません。確認者名は本人確認を保証しません。';

   // 送信後、チェックボックスは自動でオフに戻す
   el('experienceRagConfirm').checked=false;
   await refreshCandidates();
   await refreshIndexStatus();
  }catch(err){
   actionError.textContent=err.message||String(err);
  }finally{
   updateButtonState();
  }
 }

 fileInput.addEventListener('change',handleFileSelect);
 actorInput.addEventListener('input',updateButtonState);
 confirmBox.addEventListener('change',updateButtonState);
 submitBtn.addEventListener('click',submitImport);
 reloadBtn.addEventListener('click',()=>{refreshCandidates();});

 async function apiFetch(path,options){
  project=selectedProject();
  const response=await fetch('/api/projects/'+encodeURIComponent(project)+path,options);
  const data=await response.json().catch(()=>({}));
  return {response,data};
 }

 async function refreshCandidates(){
  candidateStatus.textContent='候補一覧を読み込み中…';
  candidateList.replaceChildren();
  try{
   const {response,data}=await apiFetch('/experience/candidates');
   if(!response.ok){
    const msg=(data&&data.detail)||'候補一覧の取得に失敗しました';
    throw Error(typeof msg==='string'?msg:JSON.stringify(msg));
   }
   const items=Array.isArray(data.candidates)?data.candidates:[];
   if(!items.length){
    candidateStatus.textContent='承認待ち候補はありません';
    return;
   }
   candidateStatus.textContent='承認待ち候補: '+items.length+'件(候補はRAG検索・計画・TRIZへ渡りません)';
   items.forEach(item=>{
    const li=document.createElement('li');
    li.className='experience-candidate-item';
    const missing=Array.isArray(item.missing)&&item.missing.length?('欠落: '+item.missing.join(', ')):'欠落なし';
    const blocked=Array.isArray(item.blocked)&&item.blocked.length?(' / 拒否条件: '+item.blocked.join(', ')):'';
    const approvable=item.approvable?'承認可':'承認不可';
    const claimed='出所申告: '+(item.claimed&&(item.claimed.actor||item.claimed.proof)?((item.claimed.actor||'')+' / '+(item.claimed.proof||'')):'-');
    const head=document.createElement('div');
    head.textContent='#'+String(item.id).slice(0,8)+'… ['+item.status+'] ['+approvable+'] '+missing+blocked+' | '+claimed;
    const preview=document.createElement('div');
    preview.textContent=item.lesson_preview||'';
    const btnRow=document.createElement('div');
    btnRow.className='experience-candidate-actions';
    const detailBtn=document.createElement('button');
    detailBtn.type='button';
    detailBtn.textContent='詳細';
    detailBtn.addEventListener('click',()=>{showCandidateDetail(item.id);});
    const approveBtn=document.createElement('button');
    approveBtn.type='button';
    approveBtn.textContent='承認';
    approveBtn.disabled=!item.approvable;
    const applicability=item.applicability||{};
    const presetInputVersion=typeof applicability.input_version==='string'?applicability.input_version:'';
    approveBtn.addEventListener('click',()=>{reviewCandidate(item.id,'approve',presetInputVersion);});
    const rejectBtn=document.createElement('button');
    rejectBtn.type='button';
    rejectBtn.textContent='却下';
    rejectBtn.addEventListener('click',()=>{reviewCandidate(item.id,'reject');});
    const returnBtn=document.createElement('button');
    returnBtn.type='button';
    returnBtn.textContent='差戻し';
    returnBtn.addEventListener('click',()=>{reviewCandidate(item.id,'return');});
    btnRow.append(detailBtn,approveBtn,rejectBtn,returnBtn);
    li.append(head,preview,btnRow);
    candidateList.append(li);
   });
  }catch(err){
   candidateStatus.textContent='';
   actionError.textContent=err.message||String(err);
  }
 }

 async function showCandidateDetail(cid){
  actionError.textContent='';
  actionResult.textContent='';
  try{
   const {response,data}=await apiFetch('/experience/candidates/'+encodeURIComponent(cid));
   if(!response.ok){
    const msg=(data&&data.detail)||'候補詳細の取得に失敗しました';
    throw Error(typeof msg==='string'?msg:JSON.stringify(msg));
   }
   const lines=[
    '候補ID: '+data.id,
    '状態: '+data.status,
    '承認可否: '+(data.approvable?'承認可':'承認不可'),
    '承認時指定: '+(((data.needs_at_approval||[]).join(', '))||'なし'),
    '欠落: '+((data.missing||[]).join(', ')||'なし'),
    '拒否条件: '+((data.blocked||[]).join(', ')||'なし'),
    '原本参照: '+(data.source_ref||'(なし)'),
    '原本SHA-256: '+(data.source_hash||'(なし)'),
    '取得日時: '+(data.fetched_at||'(なし)'),
    '抽出/要約方法: '+(data.extraction_method||'(なし)'),
    '適用条件: '+JSON.stringify(data.applicability||{}),
    '禁止条件: '+JSON.stringify(data.prohibitions||[]),
    '内容ハッシュ: '+(data.content_sha256||'(なし)'),
    '要約: '+(data.content||'')
   ];
   actionResult.textContent=lines.join('\n');
  }catch(err){
   actionError.textContent=err.message||String(err);
  }
 }

 async function reviewCandidate(cid,action,presetInputVersion){
  actionError.textContent='';
  actionResult.textContent='';
  const reviewer=(el('experienceReviewer').value||'').trim();
  const reason=(el('experienceReviewReason').value||'').trim();
  const versionField=el('experienceInputVersion');
  let inputVersion=(versionField?versionField.value:'')||'';
  if(action==='approve'&&!inputVersion.trim()&&typeof presetInputVersion==='string'&&presetInputVersion.trim()){
   inputVersion=presetInputVersion;
  }
  if(!reviewer||!reason){
   actionError.textContent='確認者名と理由の入力が必要です(確認者名は本人確認を保証しません)';
   return;
  }
  if(action==='approve'&&!inputVersion.trim()){
   actionError.textContent='承認時は適用する案件入力版の指定が必要です';
   return;
  }
  try{
   const body={reviewer:reviewer,reason:reason};
   if(action==='approve'){body.input_version=inputVersion.trim();}
   const {response,data}=await apiFetch('/experience/candidates/'+encodeURIComponent(cid)+'/'+action,{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)
   });
   if(response.status===409){
    const detail=data&&data.detail;
    const msg=(detail&&(detail.message||detail))||'承認できませんでした';
    throw Error(typeof msg==='string'?msg:JSON.stringify(msg));
   }
   if(!response.ok){
    const msg=(data&&data.detail)||'操作に失敗しました';
    throw Error(typeof msg==='string'?msg:JSON.stringify(msg));
   }
   actionResult.textContent='候補操作が完了しました: '+action+' (索引更新: '+(data.reindexed!=null?data.reindexed:'-')+')';
   await refreshCandidates();
   await refreshIndexStatus();
  }catch(err){
   actionError.textContent=err.message||String(err);
  }
 }

 async function refreshIndexStatus(){
  indexStatus.textContent='索引状態を読み込み中…';
  indexList.replaceChildren();
  try{
   const {response,data}=await apiFetch('/experience/index-status');
   if(!response.ok){
    const msg=(data&&data.detail)||'索引状態の取得に失敗しました';
    throw Error(typeof msg==='string'?msg:JSON.stringify(msg));
   }
   const counts=data.counts||{};
   const items=Array.isArray(data.items)?data.items:[];
   const searchable=items.filter(it=>it.searchable).length;
   indexStatus.textContent='索引: indexed '+counts.indexed+'件 / pending '+counts.pending+'件 / failed '+counts.failed+'件(検索可能: '+searchable+'件。failedは検索可能と表示しません)';
   items.forEach(item=>{
    const li=document.createElement('li');
    const label=item.searchable?'検索可能':'検索不可';
    li.textContent='#'+String(item.id).slice(0,8)+'… ['+item.status+'] ['+label+']'+(item.fail_reason?(' 理由: '+item.fail_reason):'');
    indexList.append(li);
   });
  }catch(err){
   indexStatus.textContent='';
   actionError.textContent=err.message||String(err);
  }
 }

 async function reindexFailures(){
  actionError.textContent='';
  actionResult.textContent='';
  const reviewer=(el('experienceReviewer').value||'').trim();
  if(!reviewer){
   actionError.textContent='確認者名の入力が必要です(確認者名は本人確認を保証しません)';
   return;
  }
  try{
   const {response,data}=await apiFetch('/experience/reindex',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({reviewer:reviewer,actor:reviewer,reason:'画面からの失敗分再索引'})
   });
   if(response.status===422){
    throw Error('確認者名の入力が必要です');
   }
   if(!response.ok){
    const msg=(data&&data.detail)||'再索引に失敗しました';
    throw Error(typeof msg==='string'?msg:JSON.stringify(msg));
   }
   actionResult.textContent='再索引が完了しました: 再試行 '+(data.retried!=null?data.retried:0)+'件';
   await refreshIndexStatus();
  }catch(err){
   actionError.textContent=err.message||String(err);
  }
 }

 refreshCandidates();
 refreshIndexStatus();

 const projectSelect=document.getElementById('projectSelect');
 if(projectSelect){
  projectSelect.addEventListener('change',()=>{
   project=selectedProject();
   actionError.textContent='';
   actionResult.textContent='';
   refreshCandidates();
   refreshIndexStatus();
  });
 }
}

if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
