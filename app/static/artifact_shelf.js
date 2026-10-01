(()=>{'use strict';
function init(){
 const host=document.querySelector('#workflow');if(!host){setTimeout(init,300);return;}
 const panel=document.createElement('section');panel.id='artifactShelf';panel.className='artifact-shelf';
 panel.innerHTML='<div class="artifact-heading"><h2>成果物を取り出す</h2><button type="button" id="artifactRefresh">更新</button><button type="button" id="artifactZip">成果物をまとめて保存（ZIP）</button></div><p>保存済みファイルです。途中成果を含み、検証・承認の完了を示すものではありません。</p><div class="artifact-filters"><label>ファイル名で検索 <input id="artifactSearch" type="search" placeholder="例：損益、計画書"></label><label>形式 <select id="artifactType"><option value="">すべて</option><option value="xlsx">Excel</option><option value="pdf">PDF</option><option value="md">Markdown</option><option value="csv">CSV</option></select></label><label><input id="artifactAll" type="checkbox"> 作業フォルダ全体も表示</label></div><p id="artifactStatus" role="status" aria-live="polite"></p><div id="artifactList"></div><dialog id="artifactPreview"><button type="button" id="artifactClose">閉じる</button><h3 id="artifactPreviewTitle"></h3><pre id="artifactPreviewText"></pre></dialog>';
 const artifactsPanel=document.getElementById('workflow-artifacts');
 if(artifactsPanel)artifactsPanel.appendChild(panel);
 else host.querySelector('nav').before(panel);
 const inputBox=document.createElement('details');inputBox.id='vehicleInputBox';inputBox.hidden=true;
 inputBox.innerHTML='<summary>車両別・月別損益の自動生成</summary><p>登録原本から明細を自動抽出し、承認・検証済みの計画を実行するとExcelを生成します。未解決事項は暫定Excelに表示します。</p><button type="button" id="vehicleAutoPrepare">原本から自動抽出・確認事項を更新</button><p id="vehicleAutoProgress"></p><div id="vehicleAutoQuestions"></div><details><summary>詳細設定：確認済みJSON入力</summary><input id="vehicleInputFile" type="file" accept=".json,application/json" aria-label="計算入力JSON"><label><input id="vehicleInputConfirmed" type="checkbox">対象車両・期間・明細・税区分・原本合計を照合した</label><button id="vehicleInputSave" type="button">計算入力を登録</button></details><p id="vehicleInputMessage" role="status"></p>';
 const executePanel=document.getElementById('workflow-execute');
 if(executePanel)executePanel.appendChild(inputBox);
 else panel.append(inputBox);let vehicleState=null,currentPaths=new Set(),missionState='',lastMissionStatus='';
 const get=id=>document.getElementById(id);let files=[],project='',generation=0;
 function selected(){return document.getElementById('projectSelect').value;}
 function url(path){return '/api/projects/'+encodeURIComponent(project)+'/workspace/files/'+path.split('/').map(encodeURIComponent).join('/')+'/download';}
 function resultFile(x){return /^(成果フォルダ|output|result)\//.test(x.path);}
 function completedGoalLabel(){const snap=typeof window.workflowReadinessSnapshot==='function'?window.workflowReadinessSnapshot():null;if(snap&&snap.final_completed===true)return '目標達成(確定)';return '全工程完了・目標達成は未確定';}
 function missionStatusCaption(status){return status==='completed'?completedGoalLabel():status==='paused'?'確認待ち・目標未達':status==='failed'?'失敗停止・目標未達':'目標未達';}window.refreshArtifactGoalLabels=function(){missionState=missionStatusCaption(lastMissionStatus);const box=get('vehicleInputMessage');if(box&&vehicleState&&vehicleState.applicable&&box.textContent.indexOf('計画版 ')===0)box.textContent='計画版 '+vehicleState.version+' / '+missionState;};
 function render(){
  const query=get('artifactSearch').value.toLocaleLowerCase(),type=get('artifactType').value;
  const shown=files.filter(x=>(get('artifactAll').checked||resultFile(x))&&x.path.toLocaleLowerCase().includes(query)&&(!type||x.path.toLowerCase().endsWith('.'+type)));
  get('artifactList').replaceChildren();get('artifactStatus').textContent=shown.length+'件 ／ 更新日時が新しい順';
  get('artifactZip').disabled=!files.some(resultFile);
  if(!shown.length){get('artifactList').textContent='まだ成果物がありません。計画を実行すると生成されます。絞り込み中の場合は条件を解除するか「作業フォルダ全体も表示」を選んでください。';return;}
  const snap=typeof window.workflowReadinessSnapshot==='function'?window.workflowReadinessSnapshot():null;
  function artifactClassLabel(file){
   const current=currentPaths.has(file.path)||(vehicleState&&file.path.startsWith('result/vehicle/v'+vehicleState.version+'/'));
   if(!current)return '下書き';
   if(snap&&snap.final_completed===true)return '人間承認済み';
   if(snap&&(snap.artifact_class==='final'||snap.phase==='complete'))return '検証済み';
   if(snap&&(snap.artifact_class==='provisional'||snap.provisional||snap.phase==='provisional'))return '暫定';
   if(resultFile(file))return '下書き';
   return '下書き';
  }
  for(const file of shown){const row=document.createElement('article');row.className='artifact-row';const info=document.createElement('div');const title=document.createElement('strong');title.textContent=file.path.split('/').pop();const klass=artifactClassLabel(file);const badge=document.createElement('span');badge.className='artifact-class-badge artifact-class-'+klass;badge.textContent=klass;const meta=document.createElement('small');meta.textContent=((currentPaths.has(file.path)||(vehicleState&&file.path.startsWith('result/vehicle/v'+vehicleState.version+'/')))?'現行計画 / '+missionState:'過去版・計画外（現行の達成証拠ではありません）')+' · '+file.path+' · '+(file.size_bytes/1024).toFixed(1)+' KB · '+new Date(file.modified_at).toLocaleString('ja-JP');info.append(title,badge,meta);const actions=document.createElement('div');actions.className='artifact-actions';const link=document.createElement('a');link.href=url(file.path);link.textContent='保存';link.setAttribute('download','');link.setAttribute('aria-label',file.path+' を保存');actions.append(link);
   if(/\.(md|txt|csv|json|log)$/i.test(file.path)&&file.size_bytes<=1024*1024){const preview=document.createElement('button');preview.type='button';preview.textContent='内容を見る';preview.onclick=async()=>{const ticket=generation;get('artifactPreviewTitle').textContent=file.path;get('artifactPreviewText').textContent='読み込み中…';get('artifactPreview').showModal();try{const response=await fetch(url(file.path));if(!response.ok)throw Error('取得できませんでした');const text=await response.text();if(ticket===generation)get('artifactPreviewText').textContent=text;}catch(e){if(ticket===generation)get('artifactPreviewText').textContent=e.message;}};actions.append(preview);}row.append(info,actions);get('artifactList').append(row);}
 }
 async function load(){const ticket=++generation;project=selected();files=[];get('artifactList').replaceChildren();get('artifactZip').disabled=true;get('artifactStatus').textContent='成果物を確認中…';get('artifactPreview').close();try{const response=await fetch('/api/projects/'+encodeURIComponent(project)+'/workspace');if(!response.ok)throw Error('成果物一覧を取得できませんでした。更新を押して再試行してください。');const data=await response.json();
 const responses=await Promise.all([fetch('/api/projects/'+encodeURIComponent(project)+'/mission'),fetch('/api/projects/'+encodeURIComponent(project)+'/vehicle-profit')]);
 if(!responses.every(r=>r.ok))throw Error('目標・計画状態を取得できません。更新してください。');
 const [mission,vehicle]=await Promise.all(responses.map(r=>r.json()));if(ticket!==generation)return;
 currentPaths=new Set();for(const task of mission.tasks||[]){try{for(const output of JSON.parse(task.acceptance_criteria).outputs||[])currentPaths.add(output.path);}catch(e){}}
 lastMissionStatus=mission.status||'';missionState=missionStatusCaption(lastMissionStatus);
 vehicleState=vehicle;renderAutomatic();inputBox.hidden=!vehicle.applicable;get('vehicleInputFile').value='';get('vehicleInputConfirmed').checked=false;get('vehicleInputMessage').textContent=vehicle.applicable?'計画版 '+vehicle.version+' / '+missionState:'';
 files=(data.entries||[]).filter(x=>x.kind==='file').sort((a,b)=>String(b.modified_at).localeCompare(String(a.modified_at)));render();if(data.truncated)get('artifactStatus').textContent+='（一覧の上限に達しています）';}catch(e){if(ticket===generation)get('artifactStatus').textContent=e.message;}}

 function renderAutomatic(){
  const a=vehicleState?.automatic;get('vehicleAutoQuestions').replaceChildren();
  get('vehicleAutoProgress').textContent=a?.prepared?`${a.vehicles}台 × ${a.months}か月 / 実額明細 ${a.actual_records}件 / 仮定ゼロ ${a.assumed_zeros}件 / 未配賦 ${a.unallocated.toLocaleString()}円${a.stale?' / 原本・条件が変更されています':''}`:'実行時に自動抽出します。計画検証前にも原本の確認を準備できます。';
  for(const q of a?.questions||[]){
   const box=document.createElement('div'),label=document.createElement('p');
   label.textContent=`${q.message} / ${q.locator} / ${q.count}件 / ${q.amount.toLocaleString()}円${q.month?' / '+q.month:''}${q.allocation_subject?' / '+q.allocation_subject:''}`;
   box.append(label);const sourceLink=document.createElement('a');sourceLink.textContent='該当原本を開く';sourceLink.href='/api/projects/'+encodeURIComponent(project)+'/context-files/'+encodeURIComponent(q.source_ref.replace(/^context:/,''))+'/download';sourceLink.target='_blank';sourceLink.rel='noopener';box.append(sourceLink);
   if(q.kind==='allocation'){
    const monthNote=document.createElement('p');monthNote.textContent='対象月: '+(q.month||'（明細の月）')+'。既定は当月のみ。全月一括は期間指定が必要です。';box.append(monthNote);
    const apply=document.createElement('select');apply.setAttribute('aria-label','適用範囲');apply.append(new Option('この月のみ (this_month)','this_month'),new Option('期間指定 (date_range)','date_range'));apply.value='this_month';
    const from=document.createElement('input');from.placeholder='開始月 YYYY-MM';from.setAttribute('aria-label','開始月');from.hidden=true;
    const to=document.createElement('input');to.placeholder='終了月 YYYY-MM';to.setAttribute('aria-label','終了月');to.hidden=true;
    apply.onchange=()=>{from.hidden=apply.value!=='date_range';to.hidden=apply.value!=='date_range';};
    const rows=document.createElement('div');
    const addRow=()=>{const row=document.createElement('div');const select=document.createElement('select');select.setAttribute('aria-label','配賦先');select.append(new Option('選択してください',''));for(const v of a.vehicle_choices)select.append(new Option(v.company+' / '+v.id,v.id));select.append(new Option('車両損益の対象外','__exclude__'));const ratio=document.createElement('input');ratio.type='text';ratio.value='1';ratio.setAttribute('aria-label','配賦比率');ratio.placeholder='比率';row.append(select,ratio);rows.append(row);};
    addRow();
    const more=document.createElement('button');more.type='button';more.textContent='配賦先を追加';more.onclick=addRow;
    const reason=document.createElement('input');reason.placeholder='判断の根拠（原本・担当関係など）';reason.setAttribute('aria-label','判断の根拠');
    const button=document.createElement('button');button.type='button';button.textContent='この対象の回答を保存';button.onclick=async()=>{
     const pid=project,ticket=generation;const chosen=[...rows.children].map(row=>({vehicle_id:row.querySelector('select').value,ratio:row.querySelector('input').value})).filter(x=>x.vehicle_id);
     if(!chosen.length)return;button.disabled=true;
     const exclude=chosen.length===1&&chosen[0].vehicle_id==='__exclude__';
     const allocations=exclude?[]:chosen.filter(x=>x.vehicle_id!=='__exclude__').map(x=>({vehicle_id:x.vehicle_id,ratio:x.ratio}));
     try{const response=await fetch('/api/projects/'+encodeURIComponent(pid)+'/vehicle-profit/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:vehicleState.version,input_hash:a.input_hash,issue_id:q.id,vehicle_id:exclude?'':(allocations[0]&&allocations[0].vehicle_id)||'',exclude,reason:reason.value,apply_to:apply.value,allocations,date_from:from.value||null,date_to:to.value||null})});const result=await response.json();if(!response.ok)throw Error(result.detail);if(ticket===generation)await load();}catch(e){if(ticket===generation)get('vehicleInputMessage').textContent=e.message;}finally{button.disabled=false;}
    };box.append(apply,from,to,rows,more,reason,button);
   }else if(q.kind==='adapter'){
    const select=document.createElement('select');select.setAttribute('aria-label','対象外判断');select.append(new Option('選択してください',''),new Option('当月取引なし・対象外と原本確認済み','__exclude__'));
    const reason=document.createElement('input');reason.placeholder='判断の根拠（原本・担当関係など）';reason.setAttribute('aria-label','判断の根拠');
    const button=document.createElement('button');button.type='button';button.textContent='この対象の回答を保存';button.onclick=async()=>{
     if(select.value!=='__exclude__')return;const pid=project,ticket=generation;button.disabled=true;
     try{const response=await fetch('/api/projects/'+encodeURIComponent(pid)+'/vehicle-profit/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:vehicleState.version,input_hash:a.input_hash,issue_id:q.id,vehicle_id:'',exclude:true,reason:reason.value,apply_to:'this_month'})});const result=await response.json();if(!response.ok)throw Error(result.detail);if(ticket===generation)await load();}catch(e){if(ticket===generation)get('vehicleInputMessage').textContent=e.message;}finally{button.disabled=false;}
    };box.append(select,reason,button);
   }get('vehicleAutoQuestions').append(box);
  }
 }
 get('vehicleAutoPrepare').onclick=async()=>{const pid=project,ticket=generation;get('vehicleAutoPrepare').disabled=true;get('vehicleAutoProgress').textContent='原本を自動解析しています…';try{const r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/vehicle-profit/prepare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:vehicleState.version})});const result=await r.json();if(!r.ok)throw Error(result.detail);if(ticket===generation)await load();}catch(e){if(ticket===generation)get('vehicleInputMessage').textContent=e.message;}finally{if(typeof window.reloadWorkflowReadiness==='function')window.reloadWorkflowReadiness();else if(typeof window.applyWorkflowReadiness==='function')window.applyWorkflowReadiness();else get('vehicleAutoPrepare').disabled=false;}};
 get('vehicleInputSave').onclick=async()=>{const selectedFile=get('vehicleInputFile').files[0];if(!selectedFile||!vehicleState){get('vehicleInputMessage').textContent='入力JSONを選択してください';return;}if(selectedFile.size>10*1024*1024){get('vehicleInputMessage').textContent='入力は10MB以下にしてください';return;}const pid=project,ticket=generation,state=vehicleState;const confirmed=get('vehicleInputConfirmed').checked;get('vehicleInputSave').disabled=true;try{const data=JSON.parse(await selectedFile.text());if(ticket!==generation)return;const response=await fetch('/api/projects/'+encodeURIComponent(pid)+'/vehicle-profit/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:state.version,previous_hash:state.input_hash,data,confirmed})});const result=await response.json();if(!response.ok)throw Error(typeof result.detail==='string'?result.detail:JSON.stringify(result.detail));if(ticket!==generation)return;await load();get('vehicleInputMessage').textContent=result.changed?'入力を保存しました。「実行再開」で計算してください。':result.message;}catch(e){if(ticket===generation)get('vehicleInputMessage').textContent=e.message;}finally{get('vehicleInputSave').disabled=false;}};
 get('artifactRefresh').onclick=load;get('artifactSearch').oninput=render;get('artifactType').onchange=render;get('artifactAll').onchange=render;get('artifactClose').onclick=()=>get('artifactPreview').close();get('artifactZip').onclick=()=>downloadSavedResult('/api/projects/'+encodeURIComponent(project)+'/mission/artifacts/download');document.getElementById('projectSelect').addEventListener('change',load);setInterval(()=>{if(project!==selected())load();},1000);load();
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
