(()=>{'use strict';
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
const MODES=[['replan','計画を作り直す'],['rerun','実行をやり直す'],['fresh','内容から再出発']];
function currentPid(){return (document.getElementById('projectSelect')||{}).value||'';}
async function api(path,body,method){
 const m=method||(body===undefined?'GET':'POST');
 const opt=m==='GET'?{}:{method:m,headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})};
 const r=await fetch('/api/projects/'+encodeURIComponent(currentPid())+'/lifecycle'+path,opt);
 const d=await r.json().catch(()=>({}));
 if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:String((d&&d.detail&&d.detail.message)||d.reason||('HTTP '+r.status)));
 return d;
}
function renderPreview(host,view){
 host.replaceChildren();
 addText(host,'h4','モード: '+String(view.mode||''));
 const dl=document.createElement('dl');host.append(dl);
 const pairs=[
  ['残るもの',(view.keep||[]).join(' / ')],
  ['初期化されるもの',(view.reset||[]).join(' / ')],
  ['復元方法',view.restore||''],
  ['世代',String(view.generation||0)],
  ['計画版',String(view.plan_version||0)],
  ['対象行数(概算)',String(view.total_rows||0)],
  ['停止条件',((view.stops||{}).reasons||[]).join(' / ')||'なし'],
  ['初期化できる',view.can_initialize?'はい':'いいえ'],
 ];
 for(const row of pairs){addText(dl,'dt',row[0]);addText(dl,'dd',row[1]||'未取得');}
 const blocked=document.createElement('ul');
 for(const r of (view.blocked_reasons||[])){addText(blocked,'li',r);}
 if((view.blocked_reasons||[]).length)host.append(blocked);
 const pending=((view.stops||{}).pending_external)||[];
 if(pending.length){
  addText(host,'h5','未完了の外部操作');
  const list=document.createElement('ul');
  for(const item of pending){
   addText(list,'li',String(item.store||'')+' / '+String(item.ref||'')+' / '+String(item.status||item.kind||''));
  }
  host.append(list);
  addText(host,'p','外部で投稿・送信済みかを確認し、承認状態を整理してから再プレビューしてください。初期化は公開物を取り消しません。');
 }
}
async function loadGenerations(host){
 const pid=currentPid();if(!host||!pid)return;
 host.replaceChildren();addText(host,'p','世代を読込中…');
 try{
  const d=await api('/generations');
  const rows=(d&&d.generations)||[];
  host.replaceChildren();
  if(!rows.length){addText(host,'p','世代履歴はまだありません（初期化すると新世代が始まります）。');return;}
  const list=document.createElement('ol');
  for(const g of rows.slice(-8)){
   const line=document.createElement('li');
   line.textContent='世代'+String(g.generation)+' / '+String(g.mode||'')+' / '+String(g.state||'')+' / 退避='+String(g.backup_id||'').slice(0,40)+' ';
   if(g.backup_id){
    const btn=document.createElement('button');
    btn.type='button';btn.textContent='この退避から復元';
    btn.onclick=async()=>{
     const statusEl=document.getElementById('resetStatus');
     const actor=((document.getElementById('resetActor')||{}).value||'').trim();
     if(!actor){if(statusEl)statusEl.textContent='担当者名を入力してください';return;}
     if(statusEl)statusEl.textContent='復元中…';
     try{
      const out=await api('/restore-generation',{backup_id:g.backup_id,actor:actor});
      if(statusEl)statusEl.textContent='世代 '+String(out.generation)+' へ復元しました。現行は退避 '+String(out.current_backup_id||'').slice(0,40)+' に残しています。';
      await loadGenerations(host);
     }catch(e){if(statusEl)statusEl.textContent=e.message||'復元できませんでした';}
    };
    line.append(btn);
   }
   list.append(line);
  }
  host.append(list);
 }catch(e){host.replaceChildren();addText(host,'p','世代を取得できません: '+(e.message||'通信できませんでした'));}
}
function mount(){
 function init(){
  const anchor=document.getElementById('workflow-manage');
  if(!anchor){setTimeout(init,300);return;}
  if(document.getElementById('resetPreviewHost'))return;
  const box=document.createElement('section');box.className='mission-list';
  addText(box,'h3','PJ初期化（世代切替）');
  addText(box,'p','IDと名前を保ったまま作業状態を新世代へ切り替えます。操作前は退避し、送信済みメール・公開・契約は取り消しません。同じ外部アクションは自動再実行しません。');
  const modeRow=document.createElement('div');
  addText(modeRow,'p','モード選択');
  const sel=document.createElement('select');sel.id='resetMode';
  for(const m of MODES){const o=document.createElement('option');o.value=m[0];o.textContent=m[1];sel.append(o);}
  modeRow.append(sel);box.append(modeRow);
  const actor=document.createElement('input');actor.id='resetActor';actor.placeholder='担当者名（必須）';actor.maxLength=100;box.append(actor);
  const reason=document.createElement('input');reason.id='resetReason';reason.placeholder='理由（任意）';reason.maxLength=2000;box.append(reason);
  const name=document.createElement('input');name.id='resetName';name.placeholder='PJ名を再入力（確定に必須）';name.maxLength=120;box.append(name);
  const previewHost=document.createElement('div');previewHost.id='resetPreviewHost';box.append(previewHost);
  const gensHost=document.createElement('div');gensHost.id='resetGenerationsHost';box.append(gensHost);
  const status=document.createElement('p');status.id='resetStatus';box.append(status);
  const previewBtn=document.createElement('button');previewBtn.type='button';previewBtn.textContent='プレビューを表示（変更なし）';
  const execBtn=document.createElement('button');execBtn.type='button';execBtn.textContent='初期化を実行';execBtn.disabled=true;
  let token='';let canInitialize=false;
  previewBtn.onclick=async()=>{
   status.textContent='確認中…';execBtn.disabled=true;token='';canInitialize=false;
   try{
    const view=await api('/reset-preview?mode='+encodeURIComponent(sel.value));
    renderPreview(previewHost,view);
    token=view.preview_token||'';canInitialize=!!view.can_initialize;
    status.textContent=view.can_initialize?'プレビューを表示しました。PJ名を入力すると確定できます。':'初期化できません: '+((view.blocked_reasons||[]).join(' / '));
   }catch(e){status.textContent=e.message||'確認できませんでした';}
  };
  name.addEventListener('input',()=>{execBtn.disabled=!(canInitialize&&token&&name.value.trim());});
  sel.addEventListener('change',()=>{execBtn.disabled=true;token='';});
  execBtn.onclick=async()=>{
   status.textContent='実行中…';execBtn.disabled=true;
   try{
    const out=await api('/initialize',{mode:sel.value,preview_token:token,project_name:name.value.trim(),actor:(document.getElementById('resetActor')||{}).value||'',reason:(document.getElementById('resetReason')||{}).value||'',idempotency_key:token.slice(0,64)});
    status.textContent='新世代 '+String(out.generation)+' を開始しました。旧世代は退避 '+String(out.backup_id||'').slice(0,40)+' から復元できます。';
    token='';
    await loadGenerations(gensHost);
   }catch(e){status.textContent=e.message||'実行できませんでした';execBtn.disabled=false;}
  };
  box.append(previewBtn,execBtn,status,previewHost,gensHost);
  anchor.append(box);
  loadGenerations(gensHost);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
mount();
async function refreshAll(){
 const pid=currentPid();
 const host=document.getElementById('resetGenerationsHost');
 if(pid&&host)await loadGenerations(host);
}
if(typeof document!=='undefined'){
 document.addEventListener('change',e=>{if(e&&e.target&&e.target.id==='projectSelect')refreshAll();});
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>setTimeout(refreshAll,800));else setTimeout(refreshAll,800);
}
if(typeof module!=='undefined')module.exports={MODES};
})();
