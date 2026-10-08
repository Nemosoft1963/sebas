(()=>{'use strict';
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
function currentPid(){return (document.getElementById('projectSelect')||{}).value||'';}
async function apiGet(path){
 const r=await fetch('/api/projects/'+encodeURIComponent(currentPid())+'/lifecycle'+path);
 const d=await r.json().catch(()=>({}));
 if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:String((d&&d.detail&&d.detail.message)||d.reason||('HTTP '+r.status)));
 return d;
}
async function apiPost(path,body){
 const r=await fetch('/api/projects/'+encodeURIComponent(currentPid())+'/lifecycle'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
 const d=await r.json().catch(()=>({}));
 if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:String((d&&d.detail&&d.detail.message)||d.reason||('HTTP '+r.status)));
 return d;
}
function renderPreview(host,view){
 host.replaceChildren();
 addText(host,'h4','削除プレビュー');
 const dl=document.createElement('dl');host.append(dl);
 const pairs=[
  ['PJ名',String((view.project||{}).name||view.project_name||'')],
  ['対象行数(概算)',String(view.total_rows||0)],
  ['復元可能期間',String(view.restorable_note||'')],
  ['外部公開物の警告',String(view.external_warning_full||view.external_warning||'なし')],
  ['削除できる',view.can_delete?'はい':'いいえ'],
 ];
 for(const row of pairs){addText(dl,'dt',row[0]);addText(dl,'dd',row[1]||'未取得');}
 const ext=document.createElement('ul');
 for(const item of (view.external_items||[]).slice(0,20)){addText(ext,'li',String(item.store||'')+' / '+String(item.kind||'')+' / '+String(item.note||''));}
 if((view.external_items||[]).length)host.append(ext);
 const blocked=document.createElement('ul');
 for(const r of (view.blocked_reasons||[])){addText(blocked,'li',r);}
 if((view.blocked_reasons||[]).length)host.append(blocked);
 const stops=document.createElement('p');
 stops.textContent='停止条件: '+((((view.stop_conditions||{}).reasons)||[]).join(' / ')||'なし');
 host.append(stops);
}
async function loadDeleted(host){
 if(!host)return;
 host.replaceChildren();addText(host,'p','削除済み一覧を読込中…');
 try{
  const r=await fetch('/api/projects/deleted/list');
  const d=await r.json().catch(()=>({}));
  if(!r.ok)throw Error('HTTP '+r.status);
  const rows=(d&&d.deleted)||[];
  host.replaceChildren();
  addText(host,'h4','削除済み一覧');
  if(!rows.length){addText(host,'p','削除済みPJはありません。');return;}
  const list=document.createElement('ol');
  for(const g of rows.slice(-20)){
   const line=document.createElement('li');
   line.textContent='PJ='+String(g.project_id||'')+' / 状態='+String(g.state||'')+' / 退避='+String(g.backup_id||'').slice(0,40)+' / 削除日時='+String(g.deleted_at||'')+' / purge可能='+String(g.purgeable_at||'')+' ';
   if(String(g.state||'')==='deleted'){
    const rb=document.createElement('button');
    rb.type='button';rb.textContent='復元';
    rb.onclick=async()=>{
     const statusEl=document.getElementById('deleteStatus');
     const actor=((document.getElementById('deleteActor')||{}).value||'').trim();
     if(!actor){if(statusEl)statusEl.textContent='担当者名を入力してください';return;}
     if(statusEl)statusEl.textContent='復元中…';
     try{
      const pid=currentPid();
      const rr=await fetch('/api/projects/'+encodeURIComponent(g.project_id)+'/lifecycle/delete-restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:actor,idempotency_key:'ui-restore-'+String(Date.now())})});
      const out=await rr.json().catch(()=>({}));
      if(!rr.ok)throw Error(typeof out.detail==='string'?out.detail:String(out.reason||('HTTP '+rr.status)));
      if(statusEl)statusEl.textContent='復元しました。件数とハッシュが退避と一致しました。';
      if(String(g.project_id)===pid){try{location.reload();}catch(e){}}
      await loadDeleted(host);
     }catch(e){if(statusEl)statusEl.textContent=e.message||'復元できませんでした';}
    };
    line.append(rb);
   }
   list.append(line);
  }
  host.append(list);
 }catch(e){host.replaceChildren();addText(host,'p','削除済み一覧を取得できません: '+(e.message||'通信できませんでした'));}
}
function mount(){
 function init(){
  const legacyDelete=document.getElementById('projectDelete');
  if(legacyDelete)legacyDelete.hidden=true;
  const anchor=document.getElementById('workflow-manage');
  if(!anchor){setTimeout(init,300);return;}
  if(document.getElementById('deletePreviewHost'))return;
  const box=document.createElement('section');box.className='mission-list';
  addText(box,'h3','PJ削除（安全な手順）');
  addText(box,'p','削除は「プレビュー → 退避して削除（復元可能） → 完全削除」の段階です。従来の即時削除ボタンは無効化しました。外部に公開済みの成果物（Google公開・メール等）は消せません・取り消されません。');
  const actor=document.createElement('input');actor.id='deleteActor';actor.placeholder='担当者名（必須）';actor.maxLength=100;box.append(actor);
  const name=document.createElement('input');name.id='deleteName';name.placeholder='PJ名を再入力（確定に必須）';name.maxLength=120;box.append(name);
  const previewHost=document.createElement('div');previewHost.id='deletePreviewHost';box.append(previewHost);
  const deletedHost=document.createElement('div');deletedHost.id='deleteDeletedHost';box.append(deletedHost);
  const purgeHost=document.createElement('div');purgeHost.id='deletePurgeHost';box.append(purgeHost);
  const status=document.createElement('p');status.id='deleteStatus';box.append(status);
  const previewBtn=document.createElement('button');previewBtn.type='button';previewBtn.textContent='削除プレビューを表示（変更なし）';
  const execBtn=document.createElement('button');execBtn.type='button';execBtn.textContent='退避して削除（復元可能）';execBtn.disabled=true;
  const purgePrevBtn=document.createElement('button');purgePrevBtn.type='button';purgePrevBtn.textContent='完全削除プレビュー';
  const purgeBtn=document.createElement('button');purgeBtn.type='button';purgeBtn.textContent='完全削除する';purgeBtn.disabled=true;
  let token='';let canDelete=false;
  let purgeToken='';
  previewBtn.onclick=async()=>{
   status.textContent='確認中…';execBtn.disabled=true;token='';canDelete=false;
   try{
    const view=await apiGet('/delete-request-preview');
    renderPreview(previewHost,view);
    token=view.preview_token||'';canDelete=!!view.can_delete;
    status.textContent=view.can_delete?'プレビューを表示しました。PJ名を入力すると確定できます。':'削除できません: '+((view.blocked_reasons||[]).join(' / '));
    execBtn.disabled=!(view.can_delete&&token);
   }catch(e){status.textContent=e.message||'確認できませんでした';}
  };
  name.addEventListener('input',()=>{execBtn.disabled=!(canDelete&&token&&name.value.trim());purgeBtn.disabled=!(purgeToken&&name.value.trim());});
  execBtn.onclick=async()=>{
   status.textContent='退避して削除中…';execBtn.disabled=true;
   try{
    const out=await apiPost('/delete-request',{preview_token:token,project_name:name.value.trim(),actor:(document.getElementById('deleteActor')||{}).value||'',idempotency_key:String(token).slice(0,64)});
    status.textContent='論理削除しました。退避 '+String(out.backup_id||'').slice(0,40)+' / purge可能='+String((out.delete_state||{}).purgeable_at||out.purgeable_at||'')+'。'+String(out.external_note||'');
    token='';
    await loadDeleted(deletedHost);
   }catch(e){status.textContent=e.message||'実行できませんでした';execBtn.disabled=false;}
  };
  purgePrevBtn.onclick=async()=>{
   status.textContent='完全削除プレビューを確認中…';purgeBtn.disabled=true;purgeToken='';
   try{
    const view=await apiGet('/purge-preview');
    purgeHost.replaceChildren();
    addText(purgeHost,'h4','完全削除プレビュー');
    const dl=document.createElement('dl');purgeHost.append(dl);
    addText(dl,'dt','対象行数(概算)');addText(dl,'dd',String(view.total_rows||0));
    addText(dl,'dt','外部公開物の警告');addText(dl,'dd',String(view.external_warning||'なし'));
    addText(dl,'dt','退避');addText(dl,'dd',String(view.restore_hint||''));
    addText(dl,'dt','完全削除できる');addText(dl,'dd',view.can_purge?'はい':'いいえ');
    const blocked=document.createElement('ul');
    for(const r of (view.blocked_reasons||[])){addText(blocked,'li',r);}
    if((view.blocked_reasons||[]).length)purgeHost.append(blocked);
    purgeToken=view.purge_token||'';
    status.textContent=view.can_purge?'完全削除プレビューを表示しました。PJ名を入力すると確定できます。':'完全削除できません: '+((view.blocked_reasons||[]).join(' / '));
    purgeBtn.disabled=!(view.can_purge&&purgeToken&&name.value.trim());
   }catch(e){status.textContent=e.message||'確認できませんでした';}
  };
  purgeBtn.onclick=async()=>{
   status.textContent='完全削除中…';purgeBtn.disabled=true;
   try{
    const out=await apiPost('/purge',{purge_token:purgeToken,project_name:name.value.trim(),actor:(document.getElementById('deleteActor')||{}).value||'',idempotency_key:String(purgeToken).slice(0,64)});
    status.textContent='完全削除しました。'+String(out.restore_procedure||'');
    purgeToken='';
    await loadDeleted(deletedHost);
   }catch(e){status.textContent=e.message||'実行できませんでした';purgeBtn.disabled=false;}
  };
  box.append(previewBtn,execBtn,purgePrevBtn,purgeBtn,status,previewHost,purgeHost,deletedHost);
  anchor.append(box);
  loadDeleted(deletedHost);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
mount();
if(typeof module!=='undefined')module.exports={};
})();
