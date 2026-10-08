(()=>{'use strict';
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
const FLOW=['既存計画から草案','ローカル構造検査','評価1/2','指摘取込','草案反映','評価2/2','人の承認待ち'];
const PROVIDERS=[['chatgpt','ChatGPT'],['claude','Claude'],['grok','Grok'],['meta','Meta']];
function flowLine(host,run){
 const line=document.createElement('p');line.className='mission-note';host.append(line);
 const state=String(run.state||'');
 const order={permitted:0,drafting:1,organized:2,structure_checked:2,verifying:3,intake:4,proposing:5,applied:6,awaiting_human:7,stopped:7,cancelled:7};
 const pos=order[state]==null?0:order[state];
 line.textContent=FLOW.map((name,i)=>((i<pos?'■ ':'□ ')+name)).join(' → ')+' / 現在: '+String(run.state_ja||state);
}
function cardFor(run){
 const card=document.createElement('article');card.className='mission-event resolution-card';
 addText(card,'h4','自動評価 '+String(run.id||'').slice(0,8)+' / '+String(run.state_ja||run.state||''));
 flowLine(card,run);
 const list=document.createElement('dl');card.append(list);
 const rows=[
  ['検証ラウンド',String(run.verification_rounds||0)+' / '+String(run.verification_limit||2)],
  ['外部呼出数',String(run.external_calls||0)+' / '+String(run.max_external_calls||0)],
  ['実呼出数',String(run.actual_external_calls==null?run.external_calls:run.actual_external_calls)],
  ['レビュー元',run.review_source||'未記録'],
  ['検証AI',(run.providers||[]).join('、')||'未選択'],
  ['自己評価の警告',run.self_review_warning||'なし'],
  ['指摘の取り込み主体',run.intake_actor||'未実施'],
  ['停止理由',run.stop_reason||'なし'],
  ['接続診断',(run.provider_failures||[]).map(f=>{const phase=f.phase==='organize'?'指摘整理':'草案作成';const status=Number.isInteger(f.status_code)?'HTTP '+f.status_code:'HTTP状態不明';return 'Gemini / '+phase+' / '+status+' / '+String(f.category||'unknown')+' / '+(f.retryable?'再試行可能':'再試行不可');}).join('、')||'記録なし'],
  ['担当',run.actor||''],
 ];
 for(const row of rows){addText(list,'dt',row[0]);addText(list,'dd',row[1]||'未取得');}
 const rounds=document.createElement('ol');card.append(rounds);
 for(const item of (run.rounds||[])){
  const line=document.createElement('li');
  const revs=(item.reviews||[]).map(r=>String(r.provider||'')+':'+String(r.status||'')).join(' / ');
  line.textContent='第'+String(item.round)+'回 '+String(item.status||'')+' ['+revs+']'+(item.gemini_organized?'（Gemini整理あり）':'')+' / 元:'+String(item.review_source||'');
  rounds.append(line);
 }
 const bindings=document.createElement('ul');card.append(bindings);
 for(const b of (run.issue_bindings||[])){
  const line=document.createElement('li');
  line.textContent='指摘 '+String(b.issue_id||'').slice(0,8)+' 状態:'+String(b.state||'')+' 候補工程:'+((b.candidate_task_keys||[]).join('、')||'なし')+' 達成条件:'+((b.candidate_criterion_ids||[]).join('、')||'なし')+' 理由:'+String(b.basis||'');
  bindings.append(line);
 }
 if(run.state==='stopped'&&run.stop_kind==='human_required'){
  const note=document.createElement('p');note.className='mission-note';
  note.textContent='同じ指摘が未処理のため、新規開始より対応付け再開を先に検討してください。';
  card.append(note);
  const btn=document.createElement('button');btn.type='button';
  btn.textContent='保存済み指摘を対応付け直す（外部送信なし）';
  btn.onclick=async()=>{
   btn.disabled=true;
   try{
    const pid=currentPid();
    const prev=await fetch('/api/projects/'+encodeURIComponent(pid)+'/goal-review/auto-loop/'+encodeURIComponent(run.id)+'/remap-preview').then(r=>r.json());
    const msg=document.createElement('p');
    msg.textContent='対応付けプレビュー: '+String((prev.bindings||[]).length)+'件 / 外部呼出0';
    card.append(msg);
    for(const b of (prev.bindings||[])){
     const line=document.createElement('p');
     line.textContent='指摘 '+String(b.issue_id||'').slice(0,8)+' 状態:'+String(b.state||'')+' 候補:'+((b.candidate_task_keys||[]).join('、')||'なし')+' 理由:'+String(b.basis||'');
     card.append(line);
    }
    const done=await api('/'+encodeURIComponent(run.id)+'/remap',{actor:((document.getElementById('autoLoopActor')||{}).value||'').trim(),idempotency_key:'remap-'+String(run.id).slice(0,8)});
    const fin=document.createElement('p');
    fin.textContent='対応付け直しを記録しました。状態:'+String(done.state_ja||done.state||'');
    card.append(fin);
    await load(pid,'autoLoopCards');
   }catch(e){
    const err=document.createElement('p');
    err.textContent='対応付け直しできません: '+(e.message||'通信できませんでした');
    card.append(err);
   }finally{btn.disabled=false;}
  };
  card.append(btn);
 }
 return card;
}
function selectedProviders(){
 return PROVIDERS.map(([id])=>document.getElementById('autoLoopProv-'+id)).filter(x=>x&&x.checked).map(x=>x.value);
}
function currentPid(){return (document.getElementById('projectSelect')||{}).value||'';}
async function api(path,body){
 const r=await fetch('/api/projects/'+encodeURIComponent(currentPid())+'/goal-review/auto-loop'+path,
  body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const d=await r.json().catch(()=>({}));
 if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'HTTP '+r.status);
 return d;
}
function renderPreview(host,view){
 host.replaceChildren();
 const rows=[
  ['草案の作成',view.draft_role||''],
  ['検証AI',(view.verification_providers||[]).join('、')||'未選択'],
  ['自己評価の警告',view.self_review_warning||'なし'],
  ['最大検証回数',String(view.max_verification_rounds||2)],
  ['最大外部呼出',String(view.max_external_calls||0)],
  ['今回必要な呼出枠',String(view.required_calls||0)],
  ['本日の残り枠',String(view.budget_remaining==null?'不明':view.budget_remaining)],
  ['開始条件',view.can_start?'満たしています':(view.start_blockers||[]).join(' / ')],
  ['公開用説明の文字数',String((view.public_summary||'').length)],
  ['原本本文を送る',view.sends_original_body?'はい':'いいえ'],
  ['ファイル名を送る',view.sends_filenames?'はい':'いいえ'],
  ['この時点で外部送信',view.external_send?'する':'しない'],
  ['範囲',view.scope||''],
 ];
 const list=document.createElement('dl');
 for(const row of rows){addText(list,'dt',row[0]);addText(list,'dd',row[1]||'未取得');}
 host.append(list);
 const packet=document.createElement('details');
 addText(packet,'summary','外部AIへ送る内容を確認');
 addText(packet,'pre',JSON.stringify(view.packet_preview||{},null,2));
 host.append(packet);
 addText(host,'p',view.note||'');
}
async function load(pid,hostId){
 const host=document.getElementById(hostId);if(!host||!pid)return;
 host.replaceChildren();addText(host,'p','読込中…');
 try{
  const r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/goal-review/auto-loop/runs');
  const d=await r.json().catch(()=>({}));
  if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'HTTP '+r.status);
  host.replaceChildren();
  const runs=(d&&d.runs)||[];
  if(!runs.length){addText(host,'p','自動評価ループはありません。送信範囲を確認してから開始できます。');return;}
  for(const run of runs.slice(-5))host.append(cardFor(run));
 }catch(e){host.replaceChildren();addText(host,'p','自動評価を取得できません: '+(e.message||'通信できませんでした'));}
}
function mount(){
 function init(){
  const anchor=document.getElementById('workflow-goal_plan')||document.getElementById('workflow-overview');
  if(!anchor){setTimeout(init,300);return;}
  if(document.getElementById('autoLoopCards'))return;
  const box=document.createElement('section');box.className='mission-list';
  addText(box,'h3','計画自動評価ループ（最大2回）');
  addText(box,'p','既存計画からローカル草案 → 構造検査 → 評価1/2 → 指摘取込（対応先不明なら人の確認待ち）→ 草案反映 → 評価2/2 → 人の承認待ち。計画承認・実行開始・公開・RAG登録は自動化しません。');
  const actor=document.createElement('input');actor.id='autoLoopActor';actor.placeholder='担当者名（必須）';actor.maxLength=100;
  let latestPreview=null;
  box.append(actor);
  const prov=document.createElement('div');
  addText(prov,'p','検証AI（プロジェクトで許可したAIと一致させる）');
  for(const row of PROVIDERS){
   const lab=document.createElement('label');
   const cb=document.createElement('input');cb.type='checkbox';cb.id='autoLoopProv-'+row[0];cb.value=row[0];
   lab.append(cb,document.createTextNode(' '+row[1]));
   prov.append(lab);
  }
  box.append(prov);
  const previewHost=document.createElement('div');previewHost.id='autoLoopPreview';
  const status=document.createElement('p');status.id='autoLoopStatus';
  const previewBtn=document.createElement('button');previewBtn.type='button';previewBtn.textContent='送信範囲を確認（外部送信なし）';
  const startBtn=document.createElement('button');startBtn.type='button';startBtn.textContent='許可して自動評価を開始';startBtn.disabled=true;
  actor.addEventListener('input',()=>{startBtn.disabled=!(latestPreview&&latestPreview.can_start&&actor.value.trim());});
  prov.addEventListener('change',()=>{latestPreview=null;startBtn.disabled=true;previewHost.replaceChildren();status.textContent='検証AIを変更したため、送信範囲を再確認してください。';});
  document.getElementById('projectSelect').addEventListener('change',()=>{latestPreview=null;startBtn.disabled=true;previewHost.replaceChildren();status.textContent='PJを変更したため、送信範囲を再確認してください。';});
  const cancelBtn=document.createElement('button');cancelBtn.type='button';cancelBtn.textContent='実行中を取消';
  previewBtn.onclick=async()=>{
   status.textContent='確認中…';
   try{const view=await api('/preview',{providers:selectedProviders()});latestPreview=view;renderPreview(previewHost,view);startBtn.disabled=!(view.can_start&&actor.value.trim());status.textContent=view.can_start?'この時点では外部送信していません。送信内容を確認してから開始してください。':(view.start_blockers||[]).join(' / ');}
   catch(e){status.textContent=e.message||'確認できませんでした';}
  };
  startBtn.onclick=async()=>{
   if(!latestPreview||!latestPreview.can_start||!actor.value.trim())return;
   status.textContent='開始中…';
   try{
    const started=await api('/start',{actor:actor.value.trim(),providers:selectedProviders(),public_summary:latestPreview.public_summary,expected_packet_hash:latestPreview.packet_hash});
    status.textContent='許可を記録しました。評価を実行します…';
    const out=await api('/'+encodeURIComponent(started.id)+'/run',{});
    status.textContent='状態: '+(out.state_ja||out.state||'');
    await load(currentPid(),'autoLoopCards');
   }catch(e){status.textContent=e.message||'開始できませんでした';await load(currentPid(),'autoLoopCards');}
  };
  cancelBtn.onclick=async()=>{
   status.textContent='取消中…';
   try{
    const d=await api('/runs');
    const runs=(d&&d.runs)||[];
    const open=runs.filter(x=>['permitted','drafting','organized','structure_checked','verifying','intake','proposing','applied'].indexOf(x.state)>=0).pop();
    if(!open){status.textContent='取り消す実行中のループはありません';return;}
    const out=await api('/'+encodeURIComponent(open.id)+'/cancel',{actor:(document.getElementById('autoLoopActor')||{}).value||'user'});
    status.textContent=out.stop_reason||'取り消しました';
    await load(currentPid(),'autoLoopCards');
   }catch(e){status.textContent=e.message||'取消できませんでした';}
  };
  box.append(previewBtn,startBtn,cancelBtn,status,previewHost);
  const host=document.createElement('div');host.id='autoLoopCards';box.append(host);
  anchor.append(box);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
mount();
async function refreshAll(){
 const pid=currentPid();
 await load(pid,'autoLoopCards');
}
if(typeof document!=='undefined'){
 document.addEventListener('change',e=>{if(e&&e.target&&e.target.id==='projectSelect')refreshAll();});
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>setTimeout(refreshAll,800));else setTimeout(refreshAll,800);
}
if(typeof module!=='undefined')module.exports={FLOW,cardFor};
})();
