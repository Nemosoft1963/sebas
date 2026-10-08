(()=>{'use strict';
const byId=id=>document.getElementById(id);
const add=(parent,tag,value,className)=>{
  const node=document.createElement(tag);
  if(value!=null)node.textContent=String(value);
  if(className)node.className=className;
  parent.appendChild(node);
  return node;
};
const currentPid=()=>String((byId('projectSelect')||{}).value||'');
async function request(url,options){
  const response=await fetch(url,options);
  const body=await response.json().catch(()=>({}));
  if(!response.ok){
    const detail=body.detail;
    throw Error(typeof detail==='string'?detail:String((detail&&detail.message)||body.reason||('HTTP '+response.status)));
  }
  return body;
}
function mount(){
  const panel=byId('workflow-manage');
  if(!panel){setTimeout(mount,300);return;}
  if(byId('managementCreateName'))return;
  const intro=add(panel,'p','このタブでPJの新規作成、退避、初期化、削除を管理します。上部の「作業対象」で選んだPJだけが操作対象です。','project-management-intro');
  panel.prepend(intro);
  const createCard=document.createElement('section');
  createCard.className='project-management-card';
  add(createCard,'h3','新しいPJを作成');
  const createRow=add(createCard,'div',null,'project-management-row');
  const createName=add(createRow,'input');
  createName.id='managementCreateName';
  createName.placeholder='新しいPJ名';
  createName.maxLength=120;
  createName.setAttribute('aria-label','新しいPJ名');
  const createButton=byId('projectNew');
  if(createButton){
    createButton.textContent='PJを作成';
    createRow.appendChild(createButton);
  }
  const createStatus=add(createCard,'p','', 'project-management-status');
  createStatus.id='managementCreateStatus';
  panel.insertBefore(createCard,intro.nextSibling);
  if(createButton)createButton.onclick=async()=>{
    const name=createName.value.trim();
    if(!name){createStatus.textContent='PJ名を入力してください。';createName.focus();return;}
    createButton.disabled=true;
    createStatus.textContent='作成中…';
    try{
      const created=await request('/api/projects',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name,context_text:''})
      });
      createStatus.textContent='作成しました。新しいPJへ切り替えます。';
      localStorage.setItem('localVoiceProject',created.id);
      location.reload();
    }catch(error){
      createStatus.textContent='作成できません: '+error.message;
      createButton.disabled=false;
    }
  };
  const backupCard=document.createElement('section');
  backupCard.className='project-management-card';
  add(backupCard,'h3','PJをバックアップ');
  add(backupCard,'p','選択中PJの登録データと作業ファイルを退避し、SHA-256で検証します。公開済みの外部サービスは複製しません。');
  const row=add(backupCard,'div',null,'project-management-row');
  const actor=add(row,'input');
  actor.id='managementBackupActor';
  actor.placeholder='担当者名（必須）';
  actor.maxLength=100;
  actor.setAttribute('aria-label','バックアップ担当者名');
  const createBackup=add(row,'button','バックアップを作成・検証');
  createBackup.type='button';
  createBackup.id='managementBackupCreate';
  const refresh=add(row,'button','一覧を更新');
  refresh.type='button';
  refresh.className='secondary';
  const status=add(backupCard,'p','', 'project-management-status');
  status.id='managementBackupStatus';
  const list=add(backupCard,'ol',null,'project-management-list');
  list.id='managementBackupList';
  panel.insertBefore(backupCard,createCard.nextSibling);
  let requestVersion=0;
  async function loadBackups(){
    const pid=currentPid(),version=++requestVersion;
    list.replaceChildren();
    if(!pid){add(list,'li','PJを選択してください。');return;}
    add(list,'li','退避一覧を取得中…');
    try{
      const data=await request('/api/projects/'+encodeURIComponent(pid)+'/lifecycle/backups');
      if(version!==requestVersion||pid!==currentPid())return;
      list.replaceChildren();
      const backups=data.backups||[];
      if(!backups.length){add(list,'li','バックアップはまだありません。');return;}
      for(const item of backups.slice(-20).reverse()){
        const li=add(list,'li');
        add(li,'span',String(item.created_at||'日時不明')+' / '+String(item.total_rows||0)+'行 / 作業ファイル'+String(item.workspace_files||0)+'件 / '+String(item.backup_id||'')+' ');
        const verify=add(li,'button','整合性を再検証');
        verify.type='button';
        verify.onclick=async()=>{
          verify.disabled=true;status.textContent='検証中…';
          try{
            const result=await request('/api/projects/'+encodeURIComponent(pid)+'/lifecycle/backups/'+encodeURIComponent(item.backup_id)+'/verify',{method:'POST'});
            status.textContent=result.ok?'バックアップの整合性を確認しました。':'検証失敗: '+(result.failures||[]).join(' / ');
          }catch(error){status.textContent='検証できません: '+error.message;}
          finally{verify.disabled=false;}
        };
      }
    }catch(error){list.replaceChildren();add(list,'li','一覧を取得できません: '+error.message);}
  }
  createBackup.onclick=async()=>{
    const pid=currentPid(),name=actor.value.trim();
    if(!pid){status.textContent='先にPJを選択してください。';return;}
    if(!name){status.textContent='担当者名を入力してください。';actor.focus();return;}
    createBackup.disabled=true;status.textContent='バックアップを作成中…';
    try{
      const result=await request('/api/projects/'+encodeURIComponent(pid)+'/lifecycle/backup',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actor:name})
      });
      const verified=await request('/api/projects/'+encodeURIComponent(pid)+'/lifecycle/backups/'+encodeURIComponent(result.backup_id)+'/verify',{method:'POST'});
      status.textContent=verified.ok?'作成・検証完了: '+result.backup_id:'作成後の検証に失敗しました: '+(verified.failures||[]).join(' / ');
      await loadBackups();
    }catch(error){status.textContent='バックアップできません: '+error.message;}
    finally{createBackup.disabled=false;}
  };
  refresh.onclick=loadBackups;
  byId('projectSelect').addEventListener('change',()=>{status.textContent='';loadBackups();});
  loadBackups();
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',mount);else mount();
})();
