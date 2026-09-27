(()=>{'use strict';
const OCR_STATUS_LABEL={
 none:'未実行',
 queued:'キュー',
 running:'処理中',
 needs_review:'要確認',
 passed:'OCR成功',
 failed:'失敗',
 skipped:'スキップ'
};
const ADOPTION_LABEL={
 unapproved:'未承認（要レビュー）',
 approved:'承認済み（後続計算へ未公開）',
 rejected:'却下',
 published:'業務計算への採用済み'
};
const QUALITY_LABEL={
 readable:'読取可能',
 garbled:'文字化け',
 unreadable:'読取不能'
};
const VALUE_STATE_LABEL={
 value:'値あり',
 zero:'0（印字どおりのゼロ）',
 blank:'空欄',
 unreadable:'読取不能'
};
const TRIGGER_LABEL={
 garbled:'文字化けのため（TRG-02）',
 unreadable:'読取不能のため（TRG-01）',
 low_text_density:'文字密度が低いため（TRG-04）',
 table_required:'表抽出が必要なため（TRG-06）',
 manual:'ユーザーが原本照合を指定（TRG-09）'
};
const CHECK_STATUS_LABEL={
 passed:'合格',
 failed:'不合格',
 needs_review:'要確認'
};
const BUSY_STATUSES={queued:true,running:true};

function init(){
 const host=document.getElementById('ocrReviewRoot')||document.querySelector('.context-files');
 if(!host){setTimeout(init,300);return;}
 const root=document.getElementById('ocrReviewRoot')||host;
 const panel=document.createElement('section');
 panel.className='ocr-panel';
 panel.id='ocrPanel';
 const heading=document.createElement('h2');
 heading.textContent='OCRフォールバック読取';
 const note=document.createElement('p');
 note.className='ocr-panel-note';
 note.textContent='OCR結果は原本由来の未確定データです。OCR成功は業務計算への採用済みではありません。読取不能や空欄は0として表示しません。';
 const featureLine=document.createElement('p');
 featureLine.id='ocrFeatureStatus';
 featureLine.className='ocr-status-line';
 const statusLine=document.createElement('p');
 statusLine.id='ocrPanelStatus';
 statusLine.className='ocr-status-line';
 statusLine.setAttribute('role','status');
 const fileList=document.createElement('div');
 fileList.id='ocrFileList';
 fileList.className='ocr-file-list';
 panel.append(heading,note,featureLine,statusLine,fileList);
 const screen=document.createElement('section');
 screen.id='ocrReviewScreen';
 screen.className='ocr-review-screen';
 screen.hidden=true;
 const screenTitle=document.createElement('h3');
 screenTitle.textContent='OCRレビュー（原本と抽出値の左右比較）';
 const badgeRow=document.createElement('div');
 badgeRow.className='ocr-badges';
 badgeRow.id='ocrReviewBadges';
 const split=document.createElement('div');
 split.className='ocr-split';
 const left=document.createElement('div');
 left.className='ocr-pane';
 const leftTitle=document.createElement('h4');
 leftTitle.textContent='原本ページ';
 const pageBar=document.createElement('div');
 pageBar.className='ocr-page-toolbar';
 const prevBtn=document.createElement('button');
 prevBtn.type='button';
 prevBtn.id='ocrPagePrev';
 prevBtn.textContent='前のページ';
 const pageLabel=document.createElement('span');
 pageLabel.id='ocrPageLabel';
 const nextBtn=document.createElement('button');
 nextBtn.type='button';
 nextBtn.id='ocrPageNext';
 nextBtn.textContent='次のページ';
 pageBar.append(prevBtn,pageLabel,nextBtn);
 const stage=document.createElement('div');
 stage.className='ocr-page-stage';
 stage.id='ocrPageStage';
 const pageImg=document.createElement('img');
 pageImg.id='ocrPageImage';
 pageImg.alt='原本ページ画像';
 const bboxEl=document.createElement('div');
 bboxEl.id='ocrBBox';
 bboxEl.className='ocr-bbox';
 bboxEl.hidden=true;
 stage.append(pageImg,bboxEl);
 left.append(leftTitle,pageBar,stage);
 const right=document.createElement('div');
 right.className='ocr-pane';
 const mdTitle=document.createElement('h4');
 mdTitle.textContent='抽出Markdown';
 const mdBox=document.createElement('pre');
 mdBox.id='ocrMarkdown';
 mdBox.className='ocr-markdown';
 const fieldTitle=document.createElement('h4');
 fieldTitle.textContent='構造化項目';
 const fieldBox=document.createElement('div');
 fieldBox.id='ocrFieldList';
 fieldBox.className='ocr-field-list';
 const checkTitle=document.createElement('h4');
 checkTitle.textContent='検算結果';
 const checkBox=document.createElement('div');
 checkBox.id='ocrCheckList';
 checkBox.className='ocr-check-list';
 right.append(mdTitle,mdBox,fieldTitle,fieldBox,checkTitle,checkBox);
 split.append(left,right);
 const correct=document.createElement('div');
 correct.className='ocr-correct';
 const origLabel=document.createElement('label');
 origLabel.textContent='OCR値（原本の認識結果・変更しません）';
 const origValue=document.createElement('input');
 origValue.id='ocrOriginalValue';
 origValue.readOnly=true;
 origLabel.append(origValue);
 const corrLabel=document.createElement('label');
 corrLabel.textContent='訂正値';
 const corrValue=document.createElement('input');
 corrValue.id='ocrCorrectedValue';
 corrLabel.append(corrValue);
 const reasonLabel=document.createElement('label');
 reasonLabel.textContent='訂正理由（訂正して承認するとき必須）';
 const reasonBox=document.createElement('textarea');
 reasonBox.id='ocrCorrectionReason';
 reasonBox.rows=3;
 reasonLabel.append(reasonBox);
 correct.append(origLabel,corrLabel,reasonLabel);
 const actions=document.createElement('div');
 actions.className='ocr-review-actions';
 const reviewerLabel=document.createElement('label');
 reviewerLabel.textContent='レビュー者名（必須）';
 const reviewerInput=document.createElement('input');
 reviewerInput.id='ocrReviewer';
 reviewerInput.maxLength=100;
 reviewerLabel.append(reviewerInput);
 const reviewReasonLabel=document.createElement('label');
 reviewReasonLabel.textContent='承認・却下の理由（必須）';
 const reviewReason=document.createElement('textarea');
 reviewReason.id='ocrReviewReason';
 reviewReason.rows=2;
 reviewReasonLabel.append(reviewReason);
 const approveBtn=document.createElement('button');
 approveBtn.type='button';
 approveBtn.id='ocrApprove';
 approveBtn.textContent='承認';
 const correctBtn=document.createElement('button');
 correctBtn.type='button';
 correctBtn.id='ocrCorrectApprove';
 correctBtn.textContent='訂正して承認';
 const rejectBtn=document.createElement('button');
 rejectBtn.type='button';
 rejectBtn.id='ocrReject';
 rejectBtn.textContent='却下';
 const retryBtn=document.createElement('button');
 retryBtn.type='button';
 retryBtn.id='ocrRetry';
 retryBtn.textContent='再実行';
 const publishBtn=document.createElement('button');
 publishBtn.type='button';
 publishBtn.id='ocrPublish';
 publishBtn.textContent='後続計算へ公開';
 const ragLabel=document.createElement('label');
 ragLabel.className='ocr-rag-label';
 const ragBox=document.createElement('input');
 ragBox.type='checkbox';
 ragBox.id='ocrRagConfirm';
 ragLabel.append(ragBox,document.createTextNode('この承認結果を再利用知識へ登録'));
 const ragBtn=document.createElement('button');
 ragBtn.type='button';
 ragBtn.id='ocrRagRegister';
 ragBtn.textContent='RAGへ登録';
 const actionError=document.createElement('p');
 actionError.id='ocrActionError';
 actionError.className='ocr-action-error';
 const actionResult=document.createElement('p');
 actionResult.id='ocrActionResult';
 actionResult.className='ocr-action-result';
 actionResult.setAttribute('role','status');
 actions.append(reviewerLabel,reviewReasonLabel,approveBtn,correctBtn,rejectBtn,retryBtn,publishBtn,ragLabel,ragBtn);
 screen.append(screenTitle,badgeRow,split,correct,actions,actionError,actionResult);
 root.append(panel,screen);

 const el=id=>document.getElementById(id);
 let project='';
 let epoch=0;
 let timer=null;
 let featureDisabled=false;
 let files=[];
 let selectedFileId='';
 let selectedRunId='';
 let selectedFieldId='';
 let currentPage=1;
 let pageCount=1;
 let payload=null;
 let runView=null;
 let pageSizes={};

 function selectedProject(){
  return (document.getElementById('projectSelect')||{}).value||'';
 }
 function apiError(body,fallback){
  const detail=body&&body.detail;
  if(detail&&typeof detail==='object'){
   const code=detail.error_code||'';
   const message=detail.message||fallback||'';
   if(code==='feature_disabled')return 'OCR機能は無効です';
   return (code?'['+code+'] ':'')+message;
  }
  if(typeof detail==='string')return detail;
  return fallback||'要求に失敗しました';
 }
 function qualityOf(file){
  const note=String(file.extraction_note||'');
  const match=note.match(/読取品質:\s*(readable|garbled|unreadable)/);
  if(match)return match[1];
  return '';
 }
 function isPdf(file){
  const name=String(file.filename||'').toLowerCase();
  return name.endsWith('.pdf')||file.file_kind==='pdf';
 }
 function idempotencyKey(){
  return 'ocr-ui-'+Date.now().toString(16)+'-'+Math.random().toString(16).slice(2);
 }
 function stopTimer(){
  if(timer){clearTimeout(timer);timer=null;}
 }
 function busyFiles(rows){
  return rows.some(file=>{
   const run=file.latestRun;
   return run&&BUSY_STATUSES[run.ocr_status];
  });
 }
 function schedule(rows){
  stopTimer();
  if(document.hidden)return;
  const ms=busyFiles(rows)?4000:15000;
  timer=setTimeout(loadFiles,ms);
 }
 function badge(text,kind){
  const span=document.createElement('span');
  span.className='ocr-badge'+(kind?' '+kind:'');
  span.textContent=text;
  return span;
 }
 function renderBadges(view){
  const box=el('ocrReviewBadges');
  box.replaceChildren();
  if(!view)return;
  const ocr=view.ocr_status||'';
  const adoption=view.adoption_status||'unapproved';
  box.append(
   badge(OCR_STATUS_LABEL[ocr]||ocr||'未実行',ocr==='passed'?'ocr-success':ocr==='failed'?'ocr-fail':ocr==='needs_review'?'ocr-warn':'ocr-idle'),
   badge(ADOPTION_LABEL[adoption]||adoption,adoption==='published'?'ocr-adopted':adoption==='approved'?'ocr-success':'ocr-idle')
  );
  if(view.error_code){
   box.append(badge('エラー '+view.error_code,'ocr-fail'));
  }
 }
 function applyActionState(view){
  const ocr=view&&view.ocr_status||'';
  const adoption=view&&view.adoption_status||'unapproved';
  const failed=ocr==='failed';
  const busy=!!BUSY_STATUSES[ocr];
  const approved=adoption==='approved'||adoption==='published';
  const rejectable=ocr==='needs_review'||ocr==='passed';
  const approveReason=featureDisabled?'OCR機能は無効です':failed?'failed の結果は承認できません':busy?'OCRが未完了です':approved?'承認済みです':(ocr!=='needs_review'&&ocr!=='passed')?'承認できる状態ではありません':'';
  const publishReason=featureDisabled?'OCR機能は無効です':adoption!=='approved'?'承認済みの結果だけ公開できます':'';
  const ragReason=featureDisabled?'OCR機能は無効です':!approved?'承認済みでない結果はRAG登録できません':'';
  el('ocrApprove').disabled=!!approveReason;
  el('ocrApprove').title=approveReason;
  el('ocrCorrectApprove').disabled=!!approveReason;
  el('ocrCorrectApprove').title=approveReason;
  el('ocrReject').disabled=featureDisabled||!rejectable||adoption==='rejected';
  el('ocrReject').title=el('ocrReject').disabled?(featureDisabled?'OCR機能は無効です':'却下できる状態ではありません'):'';
  el('ocrPublish').disabled=!!publishReason;
  el('ocrPublish').title=publishReason;
  el('ocrRagConfirm').disabled=!!ragReason;
  el('ocrRagRegister').disabled=!!ragReason||!el('ocrRagConfirm').checked;
  el('ocrRagRegister').title=ragReason;
  el('ocrRetry').disabled=featureDisabled||!selectedFileId;
  el('ocrRetry').title=featureDisabled?'OCR機能は無効です':'';
 }
 function renderFields(){
  const box=el('ocrFieldList');
  box.replaceChildren();
  const fields=(payload&&payload.fields)||[];
  if(!fields.length){
   box.textContent='構造化項目はありません';
   return;
  }
  fields.forEach((item,index)=>{
   const fieldId=String(item.field_id||item.name||('f'+(index+1)));
   const row=document.createElement('button');
   row.type='button';
   row.className='ocr-field-row'+(selectedFieldId===fieldId?' selected':'');
   const name=document.createElement('strong');
   name.textContent=String(item.name||fieldId);
   const state=String(item.value_state||'');
   const stateEl=document.createElement('span');
   stateEl.className='state-'+state;
   stateEl.textContent=VALUE_STATE_LABEL[state]||('状態不明:'+state);
   const valueEl=document.createElement('span');
   if(state==='blank')valueEl.textContent='（空欄）';
   else if(state==='unreadable')valueEl.textContent='（読取不能）';
   else if(state==='zero')valueEl.textContent='0';
   else valueEl.textContent=String(item.value==null?'':item.value);
   const meta=document.createElement('small');
   meta.textContent='page '+(item.page||'-')+' / block '+(item.block_id||'-');
   row.append(name,document.createTextNode(' '),stateEl,document.createElement('br'),valueEl,document.createElement('br'),meta);
   row.onclick=()=>selectField(fieldId,item);
   box.append(row);
  });
 }
 function renderChecks(){
  const box=el('ocrCheckList');
  box.replaceChildren();
  const checks=(payload&&payload.checks)||[];
  if(!checks.length){
   box.textContent='検算結果はありません';
   return;
  }
  for(const item of checks){
   const row=document.createElement('div');
   const st=String(item.status||'');
   row.className='ocr-check-row '+st;
   const title=document.createElement('strong');
   title.textContent=String(item.check_id||'')+' / '+(CHECK_STATUS_LABEL[st]||st||'不明');
   const detail=document.createElement('div');
   detail.textContent=String(item.detail||item.message||'');
   row.append(title,detail);
   box.append(row);
  }
 }
 function hideBBox(){
  el('ocrBBox').hidden=true;
 }
 function showBBox(bbox,width,height){
  const box=el('ocrBBox');
  if(!bbox||bbox.length!==4||!width||!height){hideBBox();return;}
  const nums=bbox.map(Number);
  if(nums.some(n=>Number.isNaN(n))){hideBBox();return;}
  let x1,y1,x2,y2;
  if(nums.every(n=>n>=0&&n<=1)){
   x1=nums[0];y1=nums[1];x2=nums[2];y2=nums[3];
  }else{
   x1=nums[0]/width;y1=nums[1]/height;x2=nums[2]/width;y2=nums[3]/height;
  }
  const left=Math.min(x1,x2);
  const top=Math.min(y1,y2);
  const w=Math.abs(x2-x1);
  const h=Math.abs(y2-y1);
  box.hidden=false;
  box.style.left=(left*100)+'%';
  box.style.top=(top*100)+'%';
  box.style.width=(w*100)+'%';
  box.style.height=(h*100)+'%';
 }
 function pageImageUrl(page){
  return '/api/projects/'+encodeURIComponent(project)+'/ocr/'+encodeURIComponent(selectedRunId)+'/artifacts/'+encodeURIComponent('page-'+page+'.png');
 }
 function showPage(page){
  currentPage=page;
  el('ocrPageLabel').textContent=page+' / '+pageCount;
  el('ocrPagePrev').disabled=page<=1;
  el('ocrPageNext').disabled=page>=pageCount;
  if(!selectedRunId){el('ocrPageImage').removeAttribute('src');hideBBox();return;}
  el('ocrPageImage').setAttribute('src',pageImageUrl(page));
  hideBBox();
 }
 async function selectField(fieldId,item){
  selectedFieldId=fieldId;
  el('ocrOriginalValue').value=item&&item.value!=null?String(item.value):'';
  renderFields();
  if(!selectedRunId)return;
  try{
   const response=await fetch('/api/projects/'+encodeURIComponent(project)+'/ocr/'+encodeURIComponent(selectedRunId)+'/evidence/'+encodeURIComponent(fieldId));
   const body=await response.json().catch(()=>({}));
   if(!response.ok)throw Error(apiError(body,'根拠を取得できません'));
   const page=Number(body.page||item&&item.page||currentPage||1);
   const size=pageSizes[page]||{};
   if(page!==currentPage)showPage(page);
   showBBox(body.bbox,size.width,size.height);
  }catch(err){
   el('ocrActionError').textContent=err.message||String(err);
  }
 }
 async function loadArtifactText(name){
  const response=await fetch('/api/projects/'+encodeURIComponent(project)+'/ocr/'+encodeURIComponent(selectedRunId)+'/artifacts/'+encodeURIComponent(name));
  if(!response.ok){
   const body=await response.json().catch(()=>({}));
   throw Error(apiError(body,name+' を取得できません'));
  }
  if(name.endsWith('.json'))return response.json();
  return response.text();
 }
 async function openRun(fileId,runId){
  selectedFileId=fileId;
  selectedRunId=runId;
  selectedFieldId='';
  payload=null;
  pageSizes={};
  const screenEl=el('ocrReviewScreen');
  if(screenEl){
   screenEl.hidden=false;
   if(typeof screenEl.scrollIntoView==='function'){
    screenEl.scrollIntoView({block:'start',behavior:'smooth'});
   }
  }
  el('ocrActionError').textContent='';
  if(el('ocrActionResult'))el('ocrActionResult').textContent='';
  el('ocrMarkdown').textContent='読み込み中';
  el('ocrFieldList').replaceChildren();
  el('ocrCheckList').replaceChildren();
  el('ocrRagConfirm').checked=false;
  try{
   const statusRes=await fetch('/api/projects/'+encodeURIComponent(project)+'/ocr/'+encodeURIComponent(runId));
   const view=await statusRes.json().catch(()=>({}));
   if(!statusRes.ok)throw Error(apiError(view,'OCR状態を取得できません'));
   runView=view;
   renderBadges(view);
   applyActionState(view);
   const jsonData=await loadArtifactText('ocr_result.json');
   payload=jsonData;
   const source=jsonData&&jsonData.source||{};
   pageCount=Number(source.page_count||(jsonData.pages||[]).length||1);
   if(pageCount<1)pageCount=1;
   for(const page of jsonData.pages||[]){
    pageSizes[Number(page.page)]={width:Number(page.width||0),height:Number(page.height||0)};
   }
   try{
    el('ocrMarkdown').textContent=await loadArtifactText('ocr_result.md');
   }catch(mdErr){
    el('ocrMarkdown').textContent=mdErr.message||'Markdownを取得できません';
   }
   if(Array.isArray(jsonData.checks)&&jsonData.checks.length){
    payload.checks=jsonData.checks;
   }else{
    try{
     const validation=await loadArtifactText('validation.json');
     payload.checks=(validation&&validation.checks)||[];
    }catch(_err){
     payload.checks=[];
    }
   }
   renderFields();
   renderChecks();
   showPage(1);
  }catch(err){
   if(el('ocrActionResult'))el('ocrActionResult').textContent='';
   el('ocrActionError').textContent=err.message||String(err);
   el('ocrMarkdown').textContent='';
  }
 }
 function renderFiles(rows){
  const box=el('ocrFileList');
  box.replaceChildren();
  if(!rows.length){
   const empty=document.createElement('span');
   empty.className='context-empty';
   empty.textContent='登録ファイルはありません';
   box.append(empty);
   return;
  }
  const needsApproval=file=>{const run=file.latestRun;return !!run&&run.adoption_status==='unapproved'&&run.ocr_status!=='failed';};
  const rank=file=>{const run=file.latestRun;if(!run)return 3;if(needsApproval(file))return 0;return run.ocr_status==='failed'?1:2;};
  const ordered=rows.map((file,index)=>({file,index})).sort((a,b)=>rank(a.file)-rank(b.file)||a.index-b.index).map(x=>x.file);
  for(const file of ordered){
   const row=document.createElement('article');
   row.className='ocr-file-row'+(needsApproval(file)?' ocr-needs-approval':'');
   const head=document.createElement('div');
   head.className='ocr-file-head';
   const name=document.createElement('div');
   name.className='ocr-file-name';
   name.textContent=file.filename||file.id;
   name.title=file.filename||'';
   const badges=document.createElement('div');
   badges.className='ocr-badges';
   const quality=qualityOf(file);
   badges.append(badge('通常抽出: '+(QUALITY_LABEL[quality]||'記録なし'),quality==='readable'?'ocr-success':quality?'ocr-warn':'ocr-idle'));
   const run=file.latestRun;
   const ocrStatus=run?run.ocr_status:'none';
   badges.append(badge('OCR: '+(OCR_STATUS_LABEL[ocrStatus]||ocrStatus),ocrStatus==='passed'?'ocr-success':ocrStatus==='failed'?'ocr-fail':ocrStatus==='needs_review'?'ocr-warn':'ocr-idle'));
   if(run){
    badges.append(run.ocr_status==='failed'?badge('OCR失敗（承認対象外）','ocr-fail'):badge(ADOPTION_LABEL[run.adoption_status]||run.adoption_status,run.adoption_status==='published'?'ocr-adopted':run.adoption_status==='approved'?'ocr-success':run.adoption_status==='unapproved'?'ocr-warn':'ocr-idle'));
   }
   head.append(name,badges);
   const meta=document.createElement('div');
   meta.className='ocr-file-meta';
   const parts=[];
   parts.push('起動理由: '+(run&&run.trigger?(TRIGGER_LABEL[run.trigger]||run.trigger):'未実行'));
   parts.push('ページ: '+(run&&run.page_count!=null?run.page_count:'-')+' / 処理済み '+(run&&run.processed_pages!=null?run.processed_pages:'-'));
   if(run&&run.elapsed_ms!=null)parts.push('処理時間: '+run.elapsed_ms+' ms');
   const engine=run&&(run.engine_version||(run.engine&&run.engine.pipeline)||'');
   if(engine)parts.push('モデル版: '+engine);
   if(run&&run.error_code)parts.push('エラー: '+run.error_code);
   meta.textContent=parts.join(' ／ ');
   const actions=document.createElement('div');
   actions.className='ocr-file-actions';
   if(isPdf(file)){
    const start=document.createElement('button');
    start.type='button';
    start.textContent='OCRで再読取';
    start.disabled=featureDisabled||BUSY_STATUSES[ocrStatus];
    start.title=featureDisabled?'OCR機能は無効です':BUSY_STATUSES[ocrStatus]?'処理中です':'';
    start.onclick=()=>requestOcr(file.id);
    actions.append(start);
   }
   if(run){
    const open=document.createElement('button');
    open.type='button';
    open.textContent='レビューを開く';
    open.onclick=()=>openRun(file.id,run.run_id);
    actions.append(open);
   }
   row.append(head,meta,actions);
   box.append(row);
  }
 }
 async function requestOcr(fileId){
  el('ocrPanelStatus').textContent='OCRを要求しています…';
  el('ocrPanelStatus').className='ocr-status-line';
  try{
   const key=idempotencyKey();
   const response=await fetch('/api/projects/'+encodeURIComponent(project)+'/context-files/'+encodeURIComponent(fileId)+'/ocr',{
    method:'POST',
    headers:{'Content-Type':'application/json','Idempotency-Key':key},
    body:JSON.stringify({idempotency_key:key,user_requested_original_match:true})
   });
   const body=await response.json().catch(()=>({}));
   if(response.status===409&&body&&body.detail&&body.detail.error_code==='feature_disabled'){
    featureDisabled=true;
    el('ocrFeatureStatus').textContent='OCR機能は無効です';
    el('ocrFeatureStatus').className='ocr-status-line error';
    throw Error('OCR機能は無効です');
   }
   if(!response.ok)throw Error(apiError(body,'OCR要求に失敗しました'));
   el('ocrPanelStatus').textContent='OCRを受け付けました';
   await loadFiles();
  }catch(err){
   el('ocrPanelStatus').textContent=err.message||String(err);
   el('ocrPanelStatus').className='ocr-status-line error';
   renderFiles(files);
  }
 }
 async function postRun(path,body){
  const response=await fetch('/api/projects/'+encodeURIComponent(project)+'/ocr/'+encodeURIComponent(selectedRunId)+path,{
   method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify(body||{})
  });
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw Error(apiError(data,'操作に失敗しました'));
  return data;
 }
 async function loadFiles(){
  const pid=selectedProject();
  const changed=pid!==project;
  project=pid;
  const ticket=++epoch;
  if(!pid){
   el('ocrPanelStatus').textContent='プロジェクトが選択されていません';
   return;
  }
  try{
   const response=await fetch('/api/projects/'+encodeURIComponent(pid)+'/context-files');
   const body=await response.json().catch(()=>([]));
   if(ticket!==epoch)return;
   if(!response.ok)throw Error(apiError(body,'ファイル一覧を取得できません'));
   const rows=Array.isArray(body)?body:[];
   const enriched=[];
   for(const file of rows){
    const item={...file,latestRun:null};
    try{
     const ocrRes=await fetch('/api/projects/'+encodeURIComponent(pid)+'/context-files/'+encodeURIComponent(file.id)+'/ocr');
     const ocrBody=await ocrRes.json().catch(()=>({}));
     if(ticket!==epoch)return;
     if(ocrRes.ok){
      const runs=ocrBody.runs||[];
      item.latestRun=runs[0]||null;
     }
    }catch(_err){}
    enriched.push(item);
   }
   if(ticket!==epoch)return;
   files=enriched;
   renderFiles(enriched);
   const withRun=enriched.filter(file=>file.latestRun);
   const failedRuns=withRun.filter(file=>file.latestRun.ocr_status==='failed').length;
   const pending=withRun.filter(file=>file.latestRun.adoption_status==='unapproved'&&file.latestRun.ocr_status!=='failed').length;
   el('ocrPanelStatus').textContent=enriched.length+'件のファイル'+(withRun.length?' ／ OCR '+withRun.length+'件: 承認済み・却下など '+(withRun.length-pending-failedRuns)+' ／ 未承認 '+pending+(failedRuns?' ／ OCR失敗 '+failedRuns:''):'');
   el('ocrPanelStatus').className='ocr-status-line';
   if(featureDisabled){
    el('ocrFeatureStatus').textContent='OCR機能は無効です';
    el('ocrFeatureStatus').className='ocr-status-line error';
   }else{
    el('ocrFeatureStatus').textContent='';
   }
   schedule(enriched);
   if(selectedRunId&&!changed){
    const still=enriched.find(file=>file.latestRun&&file.latestRun.run_id===selectedRunId);
    if(still){
     runView=still.latestRun;
     renderBadges(runView);
     applyActionState(runView);
    }
   }
  }catch(err){
   if(ticket!==epoch)return;
   el('ocrPanelStatus').textContent=err.message||String(err);
   el('ocrPanelStatus').className='ocr-status-line error';
   schedule([{latestRun:{ocr_status:'running'}}]);
  }
 }

 el('ocrPagePrev').onclick=()=>{if(currentPage>1)showPage(currentPage-1);};
 el('ocrPageNext').onclick=()=>{if(currentPage<pageCount)showPage(currentPage+1);};
 el('ocrApprove').onclick=async()=>{
  el('ocrActionError').textContent='';
  if(el('ocrActionResult'))el('ocrActionResult').textContent='';
  const reviewer=(el('ocrReviewer').value||'').trim();
  const reason=(el('ocrReviewReason').value||'').trim();
  if(!reviewer||!reason){
   el('ocrActionError').textContent='レビュー者名と理由を入力してください';
   return;
  }
  try{
   await postRun('/review',{decision:'approve',reviewer:reviewer,reason:reason});
   if(el('ocrActionResult'))el('ocrActionResult').textContent='承認しました(レビュー者: '+reviewer+')';
   await loadFiles();
  }catch(err){
   if(el('ocrActionResult'))el('ocrActionResult').textContent='';
   el('ocrActionError').textContent=err.message||String(err);
  }
 };
 el('ocrCorrectApprove').onclick=async()=>{
  el('ocrActionError').textContent='';
  if(el('ocrActionResult'))el('ocrActionResult').textContent='';
  const reviewer=(el('ocrReviewer').value||'').trim();
  const reason=(el('ocrReviewReason').value||'').trim();
  if(!reviewer||!reason){
   el('ocrActionError').textContent='レビュー者名と理由を入力してください';
   return;
  }
  try{
   const corrected={};
   if(selectedFieldId)corrected[selectedFieldId]=el('ocrCorrectedValue').value;
   await postRun('/review',{
    decision:'correct_and_approve',
    reviewer:reviewer,
    reason:reason,
    corrected_values:corrected,
    correction_reason:el('ocrCorrectionReason').value
   });
   if(el('ocrActionResult'))el('ocrActionResult').textContent='承認しました(レビュー者: '+reviewer+')';
   await loadFiles();
  }catch(err){
   if(el('ocrActionResult'))el('ocrActionResult').textContent='';
   el('ocrActionError').textContent=err.message||String(err);
  }
 };
 el('ocrReject').onclick=async()=>{
  el('ocrActionError').textContent='';
  if(el('ocrActionResult'))el('ocrActionResult').textContent='';
  const reviewer=(el('ocrReviewer').value||'').trim();
  const reason=(el('ocrReviewReason').value||'').trim();
  if(!reviewer||!reason){
   el('ocrActionError').textContent='レビュー者名と理由を入力してください';
   return;
  }
  try{
   await postRun('/review',{decision:'reject',reviewer:reviewer,reason:reason});
   if(el('ocrActionResult'))el('ocrActionResult').textContent='却下しました';
   await loadFiles();
  }catch(err){
   if(el('ocrActionResult'))el('ocrActionResult').textContent='';
   el('ocrActionError').textContent=err.message||String(err);
  }
 };
 el('ocrPublish').onclick=async()=>{
  el('ocrActionError').textContent='';
  if(el('ocrActionResult'))el('ocrActionResult').textContent='';
  try{
   await postRun('/publish',{});
   if(el('ocrActionResult'))el('ocrActionResult').textContent='後続計算へ公開しました';
   await loadFiles();
  }catch(err){
   if(el('ocrActionResult'))el('ocrActionResult').textContent='';
   el('ocrActionError').textContent=err.message||String(err);
  }
 };
 el('ocrRagConfirm').onchange=()=>{if(runView)applyActionState(runView);};
 el('ocrRagRegister').onclick=async()=>{
  el('ocrActionError').textContent='';
  if(el('ocrActionResult'))el('ocrActionResult').textContent='';
  try{
   await postRun('/rag',{
    confirm_rag:!!el('ocrRagConfirm').checked,
    reviewer:el('ocrReviewer').value,
    reason:el('ocrReviewReason').value
   });
   if(el('ocrActionResult'))el('ocrActionResult').textContent='RAGへ登録しました';
   await loadFiles();
  }catch(err){
   if(el('ocrActionResult'))el('ocrActionResult').textContent='';
   el('ocrActionError').textContent=err.message||String(err);
  }
 };
 el('ocrRetry').onclick=()=>{if(selectedFileId)requestOcr(selectedFileId);};
 const projectSelect=document.getElementById('projectSelect');
 if(projectSelect)projectSelect.addEventListener('change',()=>{selectedRunId='';el('ocrReviewScreen').hidden=true;loadFiles();});
 document.addEventListener('visibilitychange',()=>{
  if(document.hidden){stopTimer();return;}
  loadFiles();
 });
 // プロジェクト選択は初期化後に確定するため、値の変化を検出して再読込する(goal_review.js と同じ方式)
 setInterval(()=>{if(!document.hidden&&selectedProject()!==project)loadFiles();},1000);
 loadFiles();
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
