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
 desc.textContent='収集エージェントが集めた成功事例を、内容を確認したうえで経験RAGへ登録します。登録は取り消せません。人が中身を見て確認した場合だけ、次に進めます。';

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

 panel.append(heading,desc,statusLine,fileRow,previewBox,formRow,actionError,actionResult);

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

   actionResult.textContent='登録完了: '+reg+'件登録 / '+skip+'件重複スキップ / '+failCount+'件失敗';

   // 送信後、チェックボックスは自動でオフに戻す
   el('experienceRagConfirm').checked=false;
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

 const projectSelect=document.getElementById('projectSelect');
 if(projectSelect){
  projectSelect.addEventListener('change',()=>{
   project=selectedProject();
   actionError.textContent='';
   actionResult.textContent='';
  });
 }
}

if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
