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
function targetLabel(b){
 const t=String((b||{}).target_type||'');
 if(t==='preparation_gate')return '準備工程';
 if(t==='goal_criterion')return '目標SC';
 if(t==='task_only')return '工程のみ';
 if(t==='plan_level')return '計画全体';
 const k=String((b||{}).issue_kind||'');
 if(k==='preparation')return '準備工程';
 if(k==='requirement_item')return '目標SC';
 if(k==='final_verification')return '最終検証';
 return '計画全体';
}
const NO_CANDIDATE_FALLBACK=[
 {id:'reask_evaluator',label_ja:'評価者への再確認',detail_ja:'指摘の対象・根拠を評価者に再確認する'},
 {id:'fix_public_packet',label_ja:'公開packetの修正',detail_ja:'公開packetの目的・構造を修正して再評価する'},
 {id:'unsupported_or_missing_capability',label_ja:'対応不可／不足機能',detail_ja:'対応不可または不足機能として記録し、追加開発・業務確認へ回す'}
];
function noCandidateOptionsFor(run,b){
 const cands=(((run||{}).awaiting_decision||{}).candidates||[]);
 for(const c of cands){
  if(c&&String(c.issue_id||'')===String((b||{}).issue_id||'')&&Array.isArray(c.no_candidate_options)&&c.no_candidate_options.length)return c.no_candidate_options;
 }
 return NO_CANDIDATE_FALLBACK;
}
function awaitingSummary(card,run){
 const ad=((run||{}).awaiting_decision||{});
 const count=Number(ad.unconfirmed_count||0);
 const cands=(ad.candidates||[]);
 if(String(ad.status||'')!=='awaiting_decision'&&!count&&!cands.length)return;
 const box=document.createElement('div');
 addText(box,'h4','判断待ち: 未確定の指摘'+String(count)+'件');
 const dest=(ad.destination||{});
 if(dest.note_ja)addText(box,'p','遷移先: '+String(dest.note_ja));
 const ul=document.createElement('ul');box.append(ul);
 for(const c of cands){
  const li=document.createElement('li');
  li.textContent='未確定 '+String(c.issue_id||'')+': '+String(c.issue_text||'');
  ul.append(li);
 }
 const confirmed=((run.issue_bindings||[]).filter(x=>x&&String(x.selection_status||'')==='confirmed'));
 if(confirmed.length){
  addText(box,'p','確定済み '+String(confirmed.length)+'件（候補に再び出しません）');
  const ul2=document.createElement('ul');box.append(ul2);
  for(const x of confirmed){
   const li=document.createElement('li');
   li.textContent='確定済み '+String(x.issue_id||'');
   ul2.append(li);
  }
 }
 card.append(box);
}
function cardFor(run){
 const card=document.createElement('article');card.className='mission-event resolution-card';
 addText(card,'h4','自動評価 '+String(run.id||'').slice(0,8)+' / '+String(run.state_ja||run.state||''));
 flowLine(card,run);
 awaitingSummary(card,run);
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
  const resolved=(b&&b.resolved_preparation&&b.resolved_preparation.passed)?'（準備ゲート一意）':'';
  line.textContent='指摘 '+String(b.issue_id||'').slice(0,8)+' 対象:'+targetLabel(b)+' 状態:'+String(b.state||'')+' 種別:'+String(b.issue_kind_ja||b.issue_kind||'')+' 対応先種別:'+String(b.target_type||'未確定')+' 対象工程:'+((b.target_task_keys||b.candidate_task_keys||[]).join('、')||'なし')+' 対象ID:'+((b.target_ids||[]).join('、')||'なし')+' 根拠:'+String(b.basis||'')+resolved;
  bindings.append(line);
  const detail=document.createElement('div');
  addText(detail,'p','対象: '+targetLabel(b)+'（準備工程/目標SC/計画全体を区別。PREP00を目標達成件数の代用品にしません）');
  addText(detail,'p','根拠: '+String(b.basis||'未取得'));
  if(b&&b.resolved_preparation&&Array.isArray(b.resolved_preparation.reasons)&&b.resolved_preparation.reasons.length){
   addText(detail,'p','未解決の理由: '+(b.resolved_preparation.reasons.join(' / ')||'未取得'));
  }else if(String(b.state||'')!=='resolved'){
   addText(detail,'p','未解決の理由: '+String(b.basis||'未取得'));
  }
  addText(detail,'p','セバスの修正案: 未作成（対応付け確定後にローカルで作成します）');
  card.append(detail);
  const selStatus=String(b.selection_status||'unselected');
  if(selStatus==='confirmed'){
   const doneBox=document.createElement('div');
   addText(doneBox,'p','確定済み: この指摘は人が確定しました（候補に再び出しません）。確定は人の判断の記録であり、検証合格・外部評価合格ではありません。');
   card.append(doneBox);
  }else if(String(b.state||'')==='ambiguous'||String(b.state||'')==='invalid_reference'||String(b.state||'')==='multiple_targets'){
   const guide=document.createElement('div');
   addText(guide,'p','元の指摘: '+String(b.issue_text||b.unmet_goal||b.reason||'未取得'));
   addText(guide,'p','指摘種別: '+String(b.issue_kind_ja||b.issue_kind||'未取得')+'（準備・実行・最終検証・計画全体を区別。実行工程へ無理に割り当てません）');
   addText(guide,'p','候補工程: '+(((b.candidate_task_keys||[]).join('、'))||'なし'));
   addText(guide,'p','候補達成条件: '+(((b.candidate_criterion_ids||[]).join('、'))||'なし'));
   addText(guide,'p','候補理由: '+(((b.candidate_reasons||[]).join(' / '))||String(b.basis||'未取得')));
   addText(guide,'p','選択状態: '+String(b.selection_status||'unselected')+'（未選択なら停止のまま。選択・差分確認後だけ再評価へ進めます）');
   const noCand=((b.candidate_task_keys||[]).length===0&&(b.candidate_criterion_ids||[]).length===0);
   let taskSelect=null,critSelect=null;
   if(!noCand){
   const taskLabel=document.createElement('label');
   taskLabel.textContent='対応先の工程';
   taskSelect=document.createElement('select');
   const emptyTask=document.createElement('option');emptyTask.value='';emptyTask.textContent='選択してください';taskSelect.append(emptyTask);
   for(const key of (b.candidate_task_keys||[])){const opt=document.createElement('option');opt.value=String(key);opt.textContent=String(key);taskSelect.append(opt);}
   taskLabel.append(taskSelect);
   guide.append(taskLabel);
   const critLabel=document.createElement('label');
   critLabel.textContent='対応先の達成条件';
   critSelect=document.createElement('select');
   const emptyCrit=document.createElement('option');emptyCrit.value='';emptyCrit.textContent='選択してください';critSelect.append(emptyCrit);
   for(const key of (b.candidate_criterion_ids||[])){const opt=document.createElement('option');opt.value=String(key);opt.textContent=String(key);critSelect.append(opt);}
   critLabel.append(critSelect);
   guide.append(critLabel);
   }
   if(noCand){
    addText(guide,'p','候補なし: この指摘は任意の工程へ割り付けできません。判断の選択肢から選び、理由を記入してください。');
    const optionBox=document.createElement('div');
    for(const opt of noCandidateOptionsFor(run,b)){
     const lab=document.createElement('label');
     const radio=document.createElement('input');radio.type='radio';radio.name='no-cand-'+String(b.issue_id||'');
     radio.value=String(opt.id||'');
     lab.append(radio,document.createTextNode(' '+String(opt.label_ja||opt.id||'')+' / '+String(opt.detail_ja||'')));
     optionBox.append(lab);
    }
    guide.append(optionBox);
   }
   const reasonLabel=document.createElement('label');
   reasonLabel.textContent='理由（必須）';
   const reasonInput=document.createElement('textarea');reasonInput.rows=2;reasonInput.cols=60;reasonInput.maxLength=2000;
   reasonInput.placeholder='判断の理由を記入（必須。未記入では確定できません）';
   reasonLabel.append(reasonInput);
   guide.append(reasonLabel);
   const previewBtn=document.createElement('button');previewBtn.type='button';previewBtn.textContent=noCand?'判断内容をプレビュー（外部送信なし）':'修正差分プレビューを確認（外部送信なし）';
   const confirmBtn=document.createElement('button');confirmBtn.type='button';confirmBtn.textContent=noCand?'判断を確定する':'選択を確定して再評価へ進む';confirmBtn.disabled=true;
   const msg=document.createElement('p');
   let previewConfirmed=false;
   function currentDecisionOption(){
    if(!noCand)return '';
    const checked=(guide.querySelectorAll('input[type="radio"]')||[]);
    for(const r of checked){if(r.checked)return String(r.value||'');}
    return '';
   }
   previewBtn.onclick=async()=>{
    previewBtn.disabled=true;confirmBtn.disabled=true;previewConfirmed=false;
    msg.textContent='差分を確認しています…';
    try{
     const pid=currentPid();
     const body={issue_id:String(b.issue_id||'')};
     if(noCand){
      const opt=currentDecisionOption();
      if(!opt)throw Error('判断の選択肢を選んでください');
      body.decision_option=opt;
     }else{
      body.task_key=taskSelect.value;body.criterion_id=critSelect.value;
     }
     const r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/goal-review/auto-loop/'+encodeURIComponent(run.id)+'/binding-preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
     const d=await r.json().catch((parseErr)=>{throw Error('対応先を確認できず。再読み込みしてください');});
     if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'HTTP '+r.status);
     if(noCand){
      msg.textContent='判断の記録: '+String(d.before||'')+' → '+String(d.after||'')+' / '+String(d.note||d.diff||'')+'（この判断は計画を変えず、判断の記録のみ。検証合格・外部評価合格ではありません）';
     }else{
      msg.textContent='修正差分: '+String(d.before||'')+' → '+String(d.after||'')+' / '+String(d.note||'');
     }
     previewConfirmed=true;confirmBtn.disabled=false;
    }catch(e){msg.textContent='差分を確認できません: '+(e.message||'通信できませんでした');}
    finally{previewBtn.disabled=false;}
   };
   if(noCand){
    guide.querySelectorAll('input[type="radio"]').forEach(r=>{r.addEventListener('change',()=>{previewConfirmed=false;confirmBtn.disabled=true;});});
   }else{
    taskSelect.addEventListener('change',()=>{previewConfirmed=false;confirmBtn.disabled=true;});
    critSelect.addEventListener('change',()=>{previewConfirmed=false;confirmBtn.disabled=true;});
   }
   reasonInput.addEventListener('input',()=>{previewConfirmed=false;confirmBtn.disabled=true;});
   confirmBtn.onclick=async()=>{
    if(!previewConfirmed)return;
    confirmBtn.disabled=true;
    msg.textContent='選択を確定しています…';
    try{
     const pid=currentPid();
     const actor=((document.getElementById('autoLoopActor')||{}).value||'').trim();
     if(!actor)throw Error('担当者名を入力してください');
     const reason=String(reasonInput.value||'').trim();
     if(!reason)throw Error('理由を記入してください（必須）');
     const body={issue_id:String(b.issue_id||''),actor:actor,reason:reason,confirmed_preview:true};
     if(noCand){
      const opt=currentDecisionOption();
      if(!opt)throw Error('判断の選択肢を選んでください');
      body.decision_option=opt;
     }else{
      body.task_key=taskSelect.value;body.criterion_id=critSelect.value;
     }
     const r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/goal-review/auto-loop/'+encodeURIComponent(run.id)+'/binding-confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
     const d=await r.json().catch((parseErr)=>{throw Error('選択を確定できず。再読み込みしてください');});
     if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'HTTP '+r.status);
     msg.textContent=noCand?'判断を記録しました（人の判断の記録であり、検証合格・外部評価合格ではありません）。再読込で最新状態を確認できます。':'対応先を選択しました。修正後の構造検査と外部再評価が必要です。';
     await load(pid,'autoLoopCards');
    }catch(e){msg.textContent='選択を確定できません: '+(e.message||'通信できませんでした');const actorNow=((document.getElementById('autoLoopActor')||{}).value||'').trim();confirmBtn.disabled=!previewConfirmed||!actorNow;}
   };
   const actorBox=document.getElementById('autoLoopActor');
   if(actorBox)actorBox.addEventListener('input',()=>{const a=String(actorBox.value||'').trim();confirmBtn.disabled=!previewConfirmed||!a;});
   guide.append(previewBtn,confirmBtn,msg);
   card.append(guide);
  }
 }
 if(run.state==='stopped'&&run.stop_kind==='human_required'){
  const note=document.createElement('p');note.className='mission-note';
  note.textContent='同じ指摘が未処理のため、新規開始より対応付け再開を先に検討してください。';
  card.append(note);
  addText(card,'p','保存済みレビューを使い、外部AI呼出0回（ローカル修正案へ）で再開します。実呼出数は増えません。');
  const btn=document.createElement('button');btn.type='button';
  btn.textContent='保存済み指摘を対応付け直す（外部送信なし）';
  btn.onclick=async()=>{
   btn.disabled=true;
   try{
    const pid=currentPid();
    const prev=await fetch('/api/projects/'+encodeURIComponent(pid)+'/goal-review/auto-loop/'+encodeURIComponent(run.id)+'/remap-preview').then(async r=>{const d=await r.json().catch(()=>{throw Error('対応先を確認できず。再読み込みしてください');});if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'HTTP '+r.status);return d;});
    const msg=document.createElement('p');
    msg.textContent='対応付けプレビュー: '+String((prev.bindings||[]).length)+'件 / 外部呼出0回（保存済みレビューを使います）';
    card.append(msg);
    for(const b of (prev.bindings||[])){
     const line=document.createElement('p');
     line.textContent='指摘 '+String(b.issue_id||'').slice(0,8)+' 対象:'+targetLabel(b)+' 状態:'+String(b.state||'')+' 候補:'+((b.target_task_keys||b.candidate_task_keys||[]).join('、')||'なし')+' 根拠:'+String(b.basis||'');
     card.append(line);
    }
    const done=await api('/'+encodeURIComponent(run.id)+'/remap',{actor:((document.getElementById('autoLoopActor')||{}).value||'').trim(),idempotency_key:'remap-'+String(run.id).slice(0,8)});
    const fin=document.createElement('p');
    fin.textContent='対応付け直しを記録しました。状態:'+String(done.state_ja||done.state||'')+' / 外部呼出0回';
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
 const d=await r.json().catch((parseErr)=>{throw Error('自動評価を確認できず。再読み込みしてください');});
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
  ['公開目的の要約',(view.public_purpose&&view.public_purpose.purpose_text)||'未取得'],
  ['達成条件ID',((view.public_purpose&&view.public_purpose.criterion_ids)||[]).join('、')||'なし'],
  ['利用者向け成果物',String((view.public_purpose&&view.public_purpose.deliverable_count)??'不明')],
  ['内部記録・検証報告',String((view.public_purpose&&view.public_purpose.internal_record_count)??'不明')],
  ['送信前検査',(view.consistency&&view.consistency.passed)?'一致':'不一致: '+(((view.consistency&&view.consistency.reasons)||[]).join(' / ')||'未取得')],
  ['非公開件数',String((view.consistency&&view.consistency.nonpublic_count)??'不明')],
  ['評価不能範囲',(view.consistency&&view.consistency.unevaluable_scope)||'なし'],
  ['開始条件',view.can_start?'満たしています':(view.start_blockers||[]).join(' / ')],
  ['公開用説明の文字数',String((view.public_summary||'').length)],
  ['最終送信ハッシュ',view.packet_hash||'未取得'],
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
  const d=await r.json().catch((parseErr)=>{throw Error('自動評価を取得できず。再読み込みしてください');});
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
  const summaryBox=document.createElement('textarea');summaryBox.id='autoLoopSummary';summaryBox.rows=4;summaryBox.cols=80;summaryBox.maxLength=12000;
  summaryBox.placeholder='編集後の公開用説明（必須。具体例: 誰向け・何を作る・成果物の種類と数・達成条件の要約）';
  summaryBox.value='';
  box.append(summaryBox);
  summaryBox.addEventListener('input',()=>{latestPreview=null;startBtn.disabled=true;previewHost.replaceChildren();status.textContent='説明を変更しました。送信範囲を再確認してください。';});
  addText(box,'p','プレビューは編集後の説明そのもので最終送信ハッシュを作ります。古いハッシュ・空・不正形式は開始APIが409で拒否します。');
  const prov=document.createElement('div');
  addText(prov,'p','検証AI（プロジェクトで許可したAIと一致させる）');
  for(const row of PROVIDERS){
   const lab=document.createElement('label');
   const cb=document.createElement('input');cb.type='checkbox';cb.id='autoLoopProv-'+row[0];cb.value=row[0];
   lab.append(cb,document.createTextNode(' '+row[1]));
   prov.append(lab);
  }
  box.append(prov);
  let providerEpoch=0,providerTouched=false;
  async function selectProjectDefault(pid){
   const ticket=++providerEpoch;
   if(!pid)return;
   try{
    const [missionResponse,catalogResponse]=await Promise.all([
     fetch('/api/projects/'+encodeURIComponent(pid)+'/mission'),
     fetch('/api/research/providers')
    ]);
    if(!missionResponse.ok||!catalogResponse.ok)throw Error('外部AI設定を取得できません');
    const [mission,catalog]=await Promise.all([missionResponse.json(),catalogResponse.json()]);
    if(ticket!==providerEpoch||pid!==currentPid()||providerTouched)return;
    const allowed=new Set(mission.allow_external_ai?(mission.external_providers||[]):[]);
    const configured=new Set((Array.isArray(catalog)?catalog:[]).filter(x=>x.configured).map(x=>x.id));
    const preferred=['chatgpt','claude','grok','meta'].find(id=>allowed.has(id)&&configured.has(id))||'';
    for(const [id] of PROVIDERS)document.getElementById('autoLoopProv-'+id).checked=id===preferred;
    latestPreview=null;startBtn.disabled=true;previewHost.replaceChildren();
    status.textContent=preferred?
     '検証AI「'+PROVIDERS.find(x=>x[0]===preferred)[1]+'」を初期選択しました。送信範囲を確認してから開始してください。':
     'このPJで許可済み・接続済みの検証AIがありません。目標と計画の外部AI設定を確認してください。';
   }catch(e){if(ticket===providerEpoch)status.textContent=e.message||'外部AIの初期設定を確認できませんでした';}
  }
  const previewHost=document.createElement('div');previewHost.id='autoLoopPreview';
  const status=document.createElement('p');status.id='autoLoopStatus';
  const previewBtn=document.createElement('button');previewBtn.type='button';previewBtn.textContent='送信範囲を確認（外部送信なし）';
  const startBtn=document.createElement('button');startBtn.type='button';startBtn.textContent='許可して自動評価を開始';startBtn.disabled=true;
  actor.addEventListener('input',()=>{startBtn.disabled=!(latestPreview&&latestPreview.can_start&&actor.value.trim());});
  prov.addEventListener('change',()=>{providerTouched=true;latestPreview=null;startBtn.disabled=true;previewHost.replaceChildren();status.textContent='検証AIを変更したため、送信範囲を再確認してください。';});
  document.getElementById('projectSelect').addEventListener('change',()=>{providerTouched=false;latestPreview=null;startBtn.disabled=true;previewHost.replaceChildren();status.textContent='PJを変更したため、送信範囲を再確認してください。';selectProjectDefault(currentPid());});
  const cancelBtn=document.createElement('button');cancelBtn.type='button';cancelBtn.textContent='実行中を取消';
  previewBtn.onclick=async()=>{
   status.textContent='確認中…';
   try{const summaryText=((document.getElementById('autoLoopSummary')||{}).value||'').trim();const view=await api('/preview',{providers:selectedProviders(),public_summary:summaryText});latestPreview=view;renderPreview(previewHost,view);startBtn.disabled=!(view.can_start&&actor.value.trim());status.textContent=view.can_start?'この時点では外部送信していません。編集後の説明と最終ハッシュを確認してから開始してください。':((view.packet_status?'['+String(view.packet_status)+'] ':'')+(view.start_blockers||[]).join(' / '));}
   catch(e){status.textContent=e.message||'確認できませんでした';}
  };
  startBtn.onclick=async()=>{
   if(!latestPreview||!latestPreview.can_start||!actor.value.trim())return;
   status.textContent='開始中…';
   try{
    const summaryText=((document.getElementById('autoLoopSummary')||{}).value||'').trim();
    if(summaryText!==String(latestPreview.public_summary||'')){status.textContent='説明を編集した後は再プレビューしてください。古いハッシュでは開始できません。';return;}
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
  (function waitForInitialProject(remaining){
   const pid=currentPid();
   if(pid){selectProjectDefault(pid);return;}
   if(remaining>0)setTimeout(()=>waitForInitialProject(remaining-1),500);
   else status.textContent='PJ一覧を取得できません。画面を再読み込みしてください。';
  })(30);
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
