(()=>{'use strict';
const ACTION_BUTTONS={prepare:'vehicleAutoPrepare',import_feedback:'goalFeedbackImport',propose_feedback:'goalFeedbackPropose',apply:'goalFeedbackApply',external_review:'goalPlanSend',approve_plan:'missionApprove',start:'missionStart',cancel_queue:'goalQueueCancel',approve_result:'goalResultApprove'};
const HEADINGS=['不明点','止まる条件','確認済み事実','候補と影響','再実行範囲'];
const OVERVIEW_FIELDS=['現在','止まっている理由','次の操作','担当','達成の証拠'];
const STATUS_CODE_JA={not_passed:'計画内容の指摘あり',connection_failed:'外部AIに接続できない',waiting_budget:'利用枠の回復待ち',unverified:'未検証'};
const OWNER_SEBASTIAN='セバスが自動処理';
const OWNER_HUMAN='人間が確認・承認';
const OWNER_EXTERNAL='外部サービスの復旧待ち';
const NEXT_ACTION_TAB={prepare:'execute',import_feedback:'goal_plan',propose_feedback:'goal_plan',apply:'goal_plan',external_review:'goal_plan',approve_plan:'goal_plan',start:'execute',cancel_queue:'goal_plan',approve_result:'artifacts',review_source_difference:'execute',resolve_source_reads:'execute',confirm_allocation:'execute',apply_prior_answers:'execute',resolve_development:'goal_plan',review_feedback:'goal_plan',review_artifacts:'artifacts',wait_budget:'goal_plan',wait:'execute',idle:'overview'};
function statusCodeJa(code){if(code==null||code==='')return '不明';if(Object.prototype.hasOwnProperty.call(STATUS_CODE_JA,code))return STATUS_CODE_JA[code];return '不明';}
function formatCount(value){if(value==null)return '未取得';if(typeof value==='number'&&isFinite(value))return String(value)+'件';if(typeof value==='string'&&/^-?\d+$/.test(value.trim()))return value.trim()+'件';return '不明';}
function actionUnlockHint(reason){const text=String(reason||'');if(!text)return '解除方法: 条件を満たすと操作できます。';if(text.indexOf('実行中')>=0)return '解除方法: 処理の完了を待ってください。';if(text.indexOf('予算')>=0||text.indexOf('利用枠')>=0)return '解除方法: 利用枠の回復を待つか、予算待ちを取り消してください。';if(text.indexOf('接続')>=0||text.indexOf('認証')>=0||/\b(401|429|503)\b/.test(text))return '解除方法: 外部AIの接続・認証を復旧してください。';if(text.indexOf('外部')>=0||text.indexOf('検証')>=0)return '解除方法: 「計画内容」タブで外部検証または接続設定を確認してください。';if(text.indexOf('指摘')>=0||text.indexOf('修正')>=0)return '解除方法: 「計画内容」タブで指摘を確認し、修正案を作成してください。';if(text.indexOf('承認')>=0)return '解除方法: 計画または結果を確認して承認してください。';return '解除方法: 表示された理由を解消してください。';}
function showActionBlockReason(button,rule){if(!button||!button.parentNode)return;let hint=button.nextElementSibling;if(!hint||!(hint.classList&&hint.classList.contains('action-block-reason'))){hint=document.createElement('small');hint.className='action-block-reason';button.insertAdjacentElement('afterend',hint);}if(rule&&rule.allowed){hint.textContent='';hint.hidden=true;return;}hint.hidden=false;const reason=(rule&&rule.reason)||'現在は実行できません';hint.textContent='利用できない理由: '+reason+'。'+actionUnlockHint(reason);}
function formatCurrent(data){if(data==null)return '未取得';if(data.final_completed===true)return '目標達成(確定)';const phase=data.phase;if(phase==null||phase==='')return '不明';const mapped={running:'処理を実行中です',accuracy_blocked:'原本の正確性確認が必要です',fact_confirm:'配賦・業務事実の確認待ちです',dev_blocked:'追加開発が必要な指摘があります',plan_conflict:'修正案の対象と根拠を確認してください',plan_fact_confirm:'計画に必要な業務事実を確認してください',issues_open:'計画を作成済み。外部AIの確認で修正が必要',waiting_budget:'計画を作成済み。利用枠の回復待ちです',unverified:'計画を作成済み。外部検証は未検証です',connection_failed:'計画を作成済み。外部AIに接続できないため停止しています',provisional:'暫定成果があります。確定完了ではありません',complete:'目標と原本照合を満たしています。確定完了には人間確認が必要です',idle:'待機中です'};if(Object.prototype.hasOwnProperty.call(mapped,phase))return mapped[phase];return '不明';}
function classifyOwner(data){if(data==null)return '不明';const phase=data.phase;const next=(data.next_action)||{};const klass=next.class||'';if(phase==='waiting_budget'||phase==='connection_failed')return OWNER_EXTERNAL;if(klass==='human_fact'||klass==='human_approval'||klass==='development')return OWNER_HUMAN;if(phase==='unverified'||phase==='provisional'||phase==='fact_confirm'||phase==='dev_blocked'||phase==='plan_conflict'||phase==='plan_fact_confirm'||phase==='accuracy_blocked'||phase==='issues_open'||phase==='complete')return OWNER_HUMAN;if(klass==='external')return OWNER_EXTERNAL;if(next.auto_executable||phase==='running')return OWNER_SEBASTIAN;if(klass==='local_safe'&&next.manual_executable===false)return OWNER_SEBASTIAN;if(klass==='local_safe')return OWNER_HUMAN;return '不明';}
function collectStopReasonCards(data){const plan=[],connection=[],human=[],budget=[];function addItem(list,item){if(!item)return;if(list.indexOf(item)<0)list.push(item);}if(data==null)return[];const phase=data.phase;const stop=data.stop_reason||'';const blocking=data.blocking_error||'';const gate=data.gate||{};const failed=gate.failed_criteria;const extra=[blocking,gate.error,gate.reason,stop,phase].filter(Boolean).join(' ');const http=(extra.match(/\b(401|429|503)\b/)||[])[0];const replan=data.replan_failure||{};const hasPlan=phase==='issues_open'||phase==='dev_blocked'||phase==='plan_conflict'||phase==='plan_fact_confirm'||(Array.isArray(failed)&&failed.length>0)||/指摘/.test(stop)||replan.code==='conflict_unresolved'||replan.code==='development_required';const hasConnection=phase==='connection_failed'||!!http||/再認証/.test(extra)||/connection_failed/.test(extra);const hasBudget=phase==='waiting_budget'||/利用枠/.test(stop)||/予算回復/.test(stop);const hasHuman=phase==='unverified'||phase==='provisional'||phase==='fact_confirm'||phase==='accuracy_blocked'||(phase==='complete'&&data.final_completed!==true);if(hasPlan){if(Array.isArray(failed)&&failed.length)addItem(plan,'計画内容の指摘が'+failed.length+'件');if(stop&&(/指摘/.test(stop)||phase==='issues_open'||phase==='dev_blocked'||phase==='plan_conflict'||phase==='plan_fact_confirm'))addItem(plan,stop);if(!plan.length)addItem(plan,statusCodeJa('not_passed'));}if(hasConnection){if(/再認証/.test(extra))addItem(connection,/再認証/.test(stop)?stop:'再認証が必要');if(http)addItem(connection,'外部AIに接続できない（HTTP '+http+'）');if(!connection.length)addItem(connection,(phase==='connection_failed'&&stop)?stop:statusCodeJa('connection_failed'));}if(hasBudget)addItem(budget,stop||statusCodeJa('waiting_budget'));if(hasHuman){if(phase==='unverified')addItem(human,stop||statusCodeJa('unverified'));else addItem(human,stop||'人間の確認待ちです');}const cards=[];if(plan.length)cards.push({category:'計画内容',items:plan});if(connection.length)cards.push({category:'接続・認証',items:connection});if(human.length)cards.push({category:'人間確認',items:human});if(budget.length)cards.push({category:'利用枠',items:budget});return cards;}
function formatNextOperation(data){if(data==null)return{label:'未取得',id:'',tab:'goal_plan',allowed:false,reason:'状態を取得できていません',unlock:actionUnlockHint('状態を取得できていません')};const next=data.next_action;if(next==null)return{label:'未取得',id:'',tab:'goal_plan',allowed:false,reason:'次の操作は未取得です',unlock:actionUnlockHint('次の操作は未取得です')};const id=next.id||'';const label=next.label||id||'不明';const reason=next.reason||(next.manual_executable?'':'現在は実行できません');return{label:label,id:id,tab:NEXT_ACTION_TAB[id]||'goal_plan',allowed:next.manual_executable===true,reason:reason,unlock:actionUnlockHint(reason)};}
function formatEvidence(data){if(data==null)return[{label:'成果物',text:'未取得'},{label:'公開URL',text:'未取得'},{label:'投稿URL',text:'未取得'},{label:'回答同期',text:'未取得'},{label:'人間確認',text:'未取得'},{label:'未達条件',text:'未取得'}];const gate=data.gate;let artifact='未取得';if(data.final_completed===true)artifact='確認済み 1件 / 必要 1件';else if(data.artifact_class==='provisional'||data.provisional)artifact='確認済み 0件 / 必要 1件（暫定・未確定）';else if(data.artifact_class==='final')artifact='確認済み 0件 / 必要 1件（未確定）';else if(data.artifact_class==='none')artifact='確認済み 0件 / 必要 0件';else if(data.artifact_class==null)artifact='未取得';else artifact='確認済み 0件 / 必要 1件（未確定）';let human='未取得';if(data.final_completed===true)human='確認済み 1件 / 必要 1件';else if(data.phase==null||data.phase==='')human='不明';else if(data.phase==='provisional'||data.phase==='complete'||data.phase==='unverified'||data.phase==='fact_confirm')human='確認済み 0件 / 必要 1件';else human='確認済み 0件 / 必要 0件';let unmet='未取得';if(gate==null)unmet='未取得';else if(!Object.prototype.hasOwnProperty.call(gate,'failed_criteria')||gate.failed_criteria==null)unmet='未取得';else unmet=formatCount(gate.failed_criteria.length);return[{label:'成果物',text:artifact},{label:'公開URL',text:'未取得'},{label:'投稿URL',text:'未取得'},{label:'回答同期',text:'未取得'},{label:'人間確認',text:human},{label:'未達条件',text:unmet}];}
function buildOverviewModel(data){if(data==null)return{current:'未取得',stopCards:[],stopMissing:true,next:formatNextOperation(null),owner:'不明',evidence:formatEvidence(null)};return{current:formatCurrent(data),stopCards:collectStopReasonCards(data),stopMissing:false,next:formatNextOperation(data),owner:classifyOwner(data),evidence:formatEvidence(data)};}
function openOverviewTarget(tab,actionId){if(typeof window!=='undefined'&&typeof window.selectWorkflowTab==='function')window.selectWorkflowTab(tab||'goal_plan');if(typeof document==='undefined')return;const id=ACTION_BUTTONS[actionId];const button=id?document.getElementById(id):null;if(button&&typeof button.focus==='function')button.focus();else{const panel=document.getElementById('workflow-'+(tab||'goal_plan'));if(panel&&typeof panel.focus==='function')panel.focus();}}
function renderOverview(container,data,error){if(!container)return;const model=buildOverviewModel(error?null:data);const current=container.querySelector('#workflowOverviewCurrent');if(current)current.textContent=model.current;const stop=container.querySelector('#workflowOverviewStop');if(stop){stop.replaceChildren();const heading=document.createElement('h3');heading.textContent='止まっている理由';stop.appendChild(heading);if(model.stopMissing){const p=document.createElement('p');p.textContent='未取得';stop.appendChild(p);}else if(!model.stopCards.length){const p=document.createElement('p');p.textContent='なし';stop.appendChild(p);}else{model.stopCards.forEach(card=>{const art=document.createElement('article');art.className='workflow-overview-card workflow-stop-card';art.dataset.stopCategory=card.category;const h=document.createElement('h4');h.textContent=card.category;art.appendChild(h);const ul=document.createElement('ul');(card.items||[]).forEach(item=>{const li=document.createElement('li');li.textContent=item;ul.appendChild(li);});art.appendChild(ul);stop.appendChild(art);});}}const nextLabel=container.querySelector('#workflowOverviewNext');if(nextLabel)nextLabel.textContent=model.next.label;const nextBtn=container.querySelector('#workflowOverviewNextButton');if(nextBtn){nextBtn.disabled=!model.next.id&&!model.next.label;nextBtn.dataset.tab=model.next.tab||'goal_plan';nextBtn.dataset.actionId=model.next.id||'';showActionBlockReason(nextBtn,model.next.allowed?{allowed:true,reason:''}:{allowed:false,reason:model.next.reason});}const owner=container.querySelector('#workflowOverviewOwner');if(owner)owner.textContent=model.owner;const triz=container.querySelector('#workflowOverviewTrizRag');if(triz){const t=data&&data.triz||{};const labels={ready:'RAG有効・まだ参照なし',matched:'承認済み経験を参照',no_match:'一致する承認済み経験なし',unavailable:'RAG検索不可',vehicle_recipe_path:'車両は承認済みレシピで照合',not_applicable:'発明対象外',off:'RAG未使用'};const ragLabel=labels[t.rag_status]||'RAG未取得';triz.textContent=(t.label||'TRIZ記録なし')+' / '+ragLabel+(t.rag_reference_count?'（'+t.rag_reference_count+'件）':'')+'。候補・試験だけでは業務回復ではありません。';}const evidence=container.querySelector('#workflowOverviewEvidence');if(evidence){evidence.replaceChildren();model.evidence.forEach(row=>{const li=document.createElement('li');li.textContent=row.label+': '+row.text;evidence.appendChild(li);});}}
function add(parent,tag,text){const node=document.createElement(tag);node.textContent=text==null?'':String(text);parent.appendChild(node);return node;}
function describe(value){if(value==null)return '不明';if(typeof value==='string')return value;try{return JSON.stringify(value);}catch(_){return String(value);}}
function unknownMetric(value,label){return value==null?label:String(value);}
function presenceMetric(value){if(value==null)return '不明';return value?'あり':'なし';}
function formatUnknown(unknown){if(unknown==null)return{message:'不明',note:''};if(typeof unknown==='string')return{message:unknown,note:''};const message=unknown.message||'';const parts=[];if(unknown.source_ref)parts.push('原本: '+unknown.source_ref);if(unknown.locator)parts.push(unknown.locator);return{message:message||'不明',note:parts.join(' ')};}
function formatStopCondition(stop){if(stop==null||stop==='')return 'なし';return typeof stop==='string'?stop:String(stop);}
function formatCandidatesSummary(candidates){if(!candidates||!candidates.length)return '候補なし';const count=candidates.length;const firstImpact=(candidates[0]&&candidates[0].impact)||{};let summary='候補 '+count+'件';const amountStr=firstImpact.amount!=null?'¥'+Number(firstImpact.amount).toLocaleString():'';const impactDetails=[firstImpact.month,firstImpact.count!=null?firstImpact.count+'件':''].filter(Boolean).join('、');if(amountStr){summary+='、この確認事項の影響額 '+amountStr;if(impactDetails)summary+='（'+impactDetails+'）';}return summary;}
function formatRerunScope(scope){if(!scope)return '再実行範囲: 未定';const months=(scope.months||[]).join(',');const count=scope.count!=null?scope.count:0;return '対象月 '+(months||'未定')+'、'+count+'件を再計算';}
function questionCard(question,context,onAnswered){
 const card=document.createElement('article');card.className='workflow-question';
 if(question&&question.id)card.dataset.questionId=question.id;
 add(card,'h4','不明点');
 const unknownInfo=formatUnknown(question.unknown);
 const pUnknown=add(card,'p',unknownInfo.message);
 if(unknownInfo.note){const note=document.createElement('small');note.textContent=' ('+unknownInfo.note+')';pUnknown.appendChild(note);}
 add(card,'h4','止まる条件');
 add(card,'p',formatStopCondition(question.stop_condition));
 add(card,'h4','確認済み事実');
 const factsDiv=document.createElement('div');card.appendChild(factsDiv);
 add(card,'h4','候補と影響');
 add(card,'p',formatCandidatesSummary(question.candidates));
 if(question.candidates&&question.candidates.length){
  const details=document.createElement('details');
  const summary=document.createElement('summary');summary.textContent='候補一覧を表示';details.appendChild(summary);
  const ul=document.createElement('ul');
  question.candidates.forEach(row=>{const c=row.candidate||row;const label=(c.company&&c.id)?(c.company+' / '+c.id):(c.company||c.id||c.value||describe(c));add(ul,'li',label);});
  details.appendChild(ul);card.appendChild(details);
 }
 add(card,'h4','再実行範囲');
 add(card,'p',formatRerunScope(question.rerun_scope));
 if(!question.answerable){add(card,'p','再抽出が必要');return card;}
 const form=document.createElement('form');
 const select=add(form,'select','');
 select.name='vehicle';
 const defaultOpt=add(select,'option','車両を選択');defaultOpt.value='';
 const excludeOpt=add(select,'option','除外（計算対象外）');excludeOpt.value='__exclude__';
 const candidateIds=new Set();
 const candidateOptions=[];
 (question.candidates||[]).forEach(row=>{
  const c=row.candidate||row;const id=c.id||(typeof c==='string'?c:null);
  if(id&&!candidateIds.has(id)){candidateIds.add(id);candidateOptions.push({id,label:(c.company&&c.id)?(c.company+' / '+c.id):(c.company||id)});}
 });
 const otherOptions=[];
 (context.vehicleChoices||[]).forEach(choice=>{
  if(choice&&choice.id&&!candidateIds.has(choice.id)){otherOptions.push({id:choice.id,label:choice.company?(choice.company+' / '+choice.id):choice.id});}
 });
 if(candidateOptions.length>0){
  const grp=add(select,'option','候補');grp.disabled=true;
  candidateOptions.forEach(opt=>{const o=add(select,'option',opt.label);o.value=opt.id;});
  if(otherOptions.length>0){const sep=add(select,'option','――');sep.disabled=true;}
 }
 otherOptions.forEach(opt=>{const o=add(select,'option',opt.label);o.value=opt.id;});
 const facts=question.confirmed_facts||[];
 if(facts.length===0){
  add(factsDiv,'p','過去の回答はありません');
 }else{
  facts.forEach(fact=>{
   const val=fact.value||{};
   const pFact=document.createElement('p');
   const months=(val.months||[]).join(',');
   const label=val.exclude?'除外':(val.vehicle_id||'');
   pFact.textContent='過去の回答: '+label+(months?' ('+months+')':'');
   const useBtn=document.createElement('button');
   useBtn.type='button';useBtn.textContent='この回答を使う';
   useBtn.onclick=()=>{if(val.exclude){select.value='__exclude__';}else if(val.vehicle_id){select.value=val.vehicle_id;}};
   pFact.appendChild(document.createTextNode(' '));pFact.appendChild(useBtn);factsDiv.appendChild(pFact);
  });
 }
 const reason=add(form,'input','');reason.placeholder='理由';reason.name='reason';
 const apply=add(form,'select','');apply.name='apply_to';[['this_month','この月'],['date_range','期間指定']].forEach(row=>{const option=add(apply,'option',row[1]);option.value=row[0];});
 const dateFrom=add(form,'input','');dateFrom.placeholder='開始月 YYYY-MM';dateFrom.name='date_from';
 const dateTo=add(form,'input','');dateTo.placeholder='終了月 YYYY-MM';dateTo.name='date_to';
 const updateDateInputs=()=>{const show=apply.value==='date_range';dateFrom.hidden=!show;dateTo.hidden=!show;};
 apply.addEventListener('change',updateDateInputs);updateDateInputs();
 const answeredBy=add(form,'input','');answeredBy.placeholder='回答者名（必須）';answeredBy.name='answered_by';
 const submit=add(form,'button','回答を保存');submit.type='submit';
 const status=add(form,'p','');status.setAttribute('role','status');
 form.addEventListener('submit',async event=>{
  event.preventDefault();if(!answeredBy.value.trim()){status.textContent='回答者名を入力してください';return;}
  submit.disabled=true;
  const isExclude=select.value==='__exclude__';
  const vehicleId=isExclude?'':select.value;
  const date_from=(apply.value==='date_range'&&dateFrom.value.trim())?dateFrom.value.trim():null;
  const date_to=(apply.value==='date_range'&&dateTo.value.trim())?dateTo.value.trim():null;
  try{
   const response=await fetch('/api/projects/'+encodeURIComponent(context.projectId)+'/questions/answer',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
     version:context.planVersion,
     input_hash:context.inputHash,
     issue_id:question.id,
     vehicle_id:vehicleId,
     exclude:isExclude,
     reason:reason.value,
     apply_to:apply.value,
     allocations:[],
     date_from:date_from,
     date_to:date_to,
     answered_by:answeredBy.value.trim()
    })
   });
   const body=await response.json().catch(()=>({}));
   if(!response.ok){if(response.status===409&&body.detail&&body.detail.code==='FLAG_OFF')throw Error('この機能は無効です。既存の回答画面を利用してください');throw Error(typeof body.detail==='string'?body.detail:(body.detail&&body.detail.message)||'HTTP '+response.status);}
   status.textContent=body.fact_error?'回答は適用されましたが履歴の記録に失敗しました: '+body.fact_error:'保存しました';if(onAnswered)onAnswered();
  }catch(error){status.textContent=error.message||'保存できませんでした';}finally{submit.disabled=false;}
 });
 card.appendChild(form);return card;
}
function init(){
 const host=document.getElementById('workflow');if(!host){setTimeout(init,300);return;}
 const card=document.createElement('section');card.id='workflowReadiness';card.className='workflow-readiness';
 add(card,'h2','進行状態');const status=add(card,'p','');status.id='workflowReadinessStatus';status.setAttribute('role','status');
 const overview=add(card,'section','');overview.id='workflowOverview';overview.className='workflow-overview';overview.setAttribute('aria-live','polite');overview.setAttribute('aria-atomic','true');
 add(overview,'h2','概要');
 const currentBlock=add(overview,'div','');currentBlock.className='workflow-overview-item';add(currentBlock,'h3','現在');const current=add(currentBlock,'p','');current.id='workflowOverviewCurrent';
 const stopBlock=add(overview,'div','');stopBlock.id='workflowOverviewStop';stopBlock.className='workflow-overview-item workflow-overview-stop';add(stopBlock,'h3','止まっている理由');
 const nextBlock=add(overview,'div','');nextBlock.className='workflow-overview-item';add(nextBlock,'h3','次の操作');const nextLabel=add(nextBlock,'p','');nextLabel.id='workflowOverviewNext';
 const nextBtn=add(nextBlock,'button','この画面を開く');nextBtn.type='button';nextBtn.id='workflowOverviewNextButton';
 const ownerBlock=add(overview,'div','');ownerBlock.className='workflow-overview-item';add(ownerBlock,'h3','担当');const owner=add(ownerBlock,'p','');owner.id='workflowOverviewOwner';
 const evidenceBlock=add(overview,'div','');evidenceBlock.className='workflow-overview-item';add(evidenceBlock,'h3','達成の証拠');const evidence=add(evidenceBlock,'ul','');evidence.id='workflowOverviewEvidence';
 const trizBlock=add(overview,'div','');trizBlock.className='workflow-overview-item';add(trizBlock,'h3','TRIZ・承認済みRAG');const trizStatus=add(trizBlock,'p','未取得');trizStatus.id='workflowOverviewTrizRag';
 const links=add(overview,'p','');links.className='workflow-overview-links';
 const planLink=add(links,'button','計画内容の詳細');planLink.type='button';planLink.id='workflowOverviewPlanLink';planLink.dataset.tab='goal_plan';
 const monitorLink=add(links,'button','実行モニタリングの詳細');monitorLink.type='button';monitorLink.id='workflowOverviewMonitorLink';monitorLink.dataset.tab='execute';
 const artifactsLink=add(links,'button','成果物を開く');artifactsLink.type='button';artifactsLink.id='workflowOverviewArtifactsLink';artifactsLink.dataset.tab='artifacts';
 const historyLink=add(links,'button','履歴を開く');historyLink.type='button';historyLink.id='workflowOverviewHistoryLink';historyLink.dataset.tab='history';
 const dl=add(card,'dl','');[['目標','workflowReadinessGoal'],['計画版','workflowReadinessVersion'],['停止理由','workflowReadinessStop'],['次の操作','workflowReadinessNext'],['正本状態','workflowReadinessCanonical'],['未達条件','workflowReadinessGate'],['成果','workflowReadinessArtifact']].forEach(row=>{add(dl,'dt',row[0]);const dd=add(dl,'dd','');dd.id=row[1];});
 const retry=add(card,'button','状態を再取得');retry.type='button';retry.id='workflowReadinessRetry';
 const qsection=add(card,'section','');add(qsection,'h3','確認事項（最大5件）');const qstatus=add(qsection,'p','');const qlist=add(qsection,'div','');
 const msection=add(card,'section','');add(msection,'h3','指標');
 const mstatus=add(msection,'p','');mstatus.id='workflowGoalMetricsStatus';mstatus.setAttribute('role','status');
 const mretry=add(msection,'button','指標を更新');mretry.type='button';mretry.id='workflowGoalMetricsRefresh';
 const mdl=add(msection,'dl','');[['計画の版数','workflowGoalMetricsPlanVersions'],['未解決の配賦','workflowGoalMetricsAllocations'],['未解決の読取','workflowGoalMetricsReads'],['Gate','workflowGoalMetricsGate'],['再停止回数','workflowGoalMetricsStopCount'],['暫定成果物の有無','workflowGoalMetricsProvisional'],['人間の確定の有無','workflowGoalMetricsAcceptance']].forEach(row=>{add(mdl,'dt',row[0]);const dd=add(mdl,'dd','');dd.id=row[1];});
 const overviewPanel=document.getElementById('workflow-overview');
 const executePanel=document.getElementById('workflow-execute');
 if(overviewPanel)overviewPanel.appendChild(card);
 else{const nav=host.querySelector('nav');if(nav)nav.before(card);else host.prepend(card);}
 if(executePanel){executePanel.appendChild(qsection);executePanel.appendChild(msection);}
 const el=id=>document.getElementById(id);let project='',epoch=0,timer=null,state=null,metricsEpoch=0;
 function selected(){return (document.getElementById('projectSelect')||{}).value||'';}
 function applyActions(data){const allowed=(data&&data.allowed_actions)||{};for(const [action,id] of Object.entries(ACTION_BUTTONS)){const button=el(id);if(!button)continue;const rule=allowed[action];if(!rule){button.disabled=true;button.title='';showActionBlockReason(button,{allowed:false,reason:'現在は実行できません'});continue;}button.disabled=!rule.allowed;button.title=rule.allowed?'':(rule.reason||'');showActionBlockReason(button,rule);}}
 window.applyWorkflowReadiness=function(){if(state)applyActions(state);};window.reloadWorkflowReadiness=function(){return load(true);};
 window.workflowReadinessSnapshot=function(){return state;};
 function notifyGoalLabels(){if(typeof window.refreshGoalCompletionLabels==='function')window.refreshGoalCompletionLabels();if(typeof window.refreshArtifactGoalLabels==='function')window.refreshArtifactGoalLabels();}
 function render(data,error){if(error){status.textContent='状態取得失敗: '+error;status.className='fetch-error';retry.disabled=false;renderOverview(overview,null,error);return;}status.textContent='';status.className='';el('workflowReadinessGoal').textContent=data.goal||'（未設定）';el('workflowReadinessVersion').textContent='v'+(data.plan_version||0)+(data.mission_status?' / '+data.mission_status:'');el('workflowReadinessStop').textContent=data.stop_reason||'';el('workflowReadinessCanonical').textContent=data.canonical_state||data.phase||'';const failed=((data.gate||{}).failed_criteria)||[];el('workflowReadinessGate').textContent=failed.length?failed.join(', '):'なし';const next=data.next_action||{};el('workflowReadinessNext').textContent=(next.label||next.id||'')+(next.endpoint?' ('+next.endpoint+')':'');const klass=data.artifact_class||'none';const artifact=el('workflowReadinessArtifact');artifact.className=klass==='final'?'artifact-final':klass==='provisional'?'artifact-provisional':'';artifact.textContent=data.final_completed?'確定':(data.provisional||klass==='provisional'?'暫定':'なし');applyActions(data);renderOverview(overview,data);}
 function renderMetrics(data){
  el('workflowGoalMetricsPlanVersions').textContent=unknownMetric(data.plan_versions,'不明');
  el('workflowGoalMetricsAllocations').textContent=unknownMetric(data.unresolved_allocations,'不明');
  el('workflowGoalMetricsReads').textContent=unknownMetric(data.unresolved_reads,'不明');
  const gate=data.gate;let gateText='不明';
  if(gate!=null){
   const head=gate.achieved==null?'不明':(gate.achieved?'達成':'未達');
   const counts=gate.criteria_counts;
   if(counts==null)gateText=head+'、条件別件数: 不明';
   else gateText=head+'、PASS '+unknownMetric(counts.PASS,'不明')+' / FAIL '+unknownMetric(counts.FAIL,'不明')+' / UNTESTABLE '+unknownMetric(counts.UNTESTABLE,'不明');
  }
  el('workflowGoalMetricsGate').textContent=gateText;
  el('workflowGoalMetricsStopCount').textContent=data.stop_count==null?'未計測':String(data.stop_count);
  el('workflowGoalMetricsProvisional').textContent=presenceMetric(data.provisional_artifact);
  el('workflowGoalMetricsAcceptance').textContent=presenceMetric(data.human_acceptance);
 }
 function isEditingQuestions(){
  const active=document.activeElement;
  return Boolean(active&&qlist&&qlist.contains(active)&&(active.tagName==='INPUT'||active.tagName==='SELECT'||active.tagName==='TEXTAREA'||active.closest('form')));
 }
 function snapshotQuestionsState(){
  const state={};
  qlist.querySelectorAll('.workflow-question').forEach(card=>{
   const qid=card.dataset.questionId;
   if(!qid)return;
   const item={};
   const details=card.querySelector('details');
   if(details)item.detailsOpen=details.open;
   const form=card.querySelector('form');
   if(form){
    const v=form.querySelector('select[name="vehicle"]');
    const r=form.querySelector('input[name="reason"]');
    const a=form.querySelector('select[name="apply_to"]');
    const df=form.querySelector('input[name="date_from"]');
    const dt=form.querySelector('input[name="date_to"]');
    const ab=form.querySelector('input[name="answered_by"]');
    if(v&&v.value)item.vehicle=v.value;
    if(r&&r.value)item.reason=r.value;
    if(a&&a.value)item.apply_to=a.value;
    if(df&&df.value)item.date_from=df.value;
    if(dt&&dt.value)item.date_to=dt.value;
    if(ab&&ab.value)item.answered_by=ab.value;
   }
   state[qid]=item;
  });
  return state;
 }
 function restoreQuestionState(card,saved){
  if(!card||!saved)return;
  if(saved.detailsOpen!=null){
   const details=card.querySelector('details');
   if(details)details.open=saved.detailsOpen;
  }
  const form=card.querySelector('form');
  if(!form)return;
  if(saved.vehicle!=null&&saved.vehicle!==''){
   const el=form.querySelector('select[name="vehicle"]');
   if(el)el.value=saved.vehicle;
  }
  if(saved.reason!=null&&saved.reason!==''){
   const el=form.querySelector('input[name="reason"]');
   if(el)el.value=saved.reason;
  }
  if(saved.apply_to!=null&&saved.apply_to!==''){
   const el=form.querySelector('select[name="apply_to"]');
   if(el){el.value=saved.apply_to;el.dispatchEvent(new Event('change'));}
  }
  if(saved.date_from!=null&&saved.date_from!==''){
   const el=form.querySelector('input[name="date_from"]');
   if(el)el.value=saved.date_from;
  }
  if(saved.date_to!=null&&saved.date_to!==''){
   const el=form.querySelector('input[name="date_to"]');
   if(el)el.value=saved.date_to;
  }
  if(saved.answered_by!=null&&saved.answered_by!==''){
   const el=form.querySelector('input[name="answered_by"]');
   if(el)el.value=saved.answered_by;
  }
 }
 async function loadQuestions(pid,ticket,forced){
  if(!forced&&isEditingQuestions())return;
  const savedState=!forced?snapshotQuestionsState():{};
  try{
   const response=await fetch('/api/projects/'+encodeURIComponent(pid)+'/questions?limit=5');
   const body=await response.json().catch(()=>({}));
   if(ticket!==epoch)return;
   if(!response.ok)throw Error(typeof body.detail==='string'?body.detail:'HTTP '+response.status);
   qstatus.textContent=body.prepared?'':'質問を準備できません';
   qlist.replaceChildren();
   (body.questions||[]).forEach(question=>{
    const card=questionCard(question,{projectId:pid,planVersion:body.plan_version,inputHash:body.input_hash,vehicleChoices:body.vehicle_choices||[]},()=>load(true));
    if(!forced&&savedState[question.id]){
     restoreQuestionState(card,savedState[question.id]);
    }
    qlist.appendChild(card);
   });
   if(!(body.questions||[]).length)add(qlist,'p','確認事項はまだありません。計画を生成・実行すると、確認が必要な項目がここに表示されます。');
   if(body.omitted>0)add(qlist,'p','ほか '+body.omitted+'件');
  }catch(error){
   qstatus.textContent='確認事項の取得失敗: '+(error.message||'通信できませんでした');
  }
 }
 function busyPoll(data){return !!(data&&(data.active_job||data.phase==='waiting_budget'||data.phase==='running'||data.mission_status==='running'));}
 function schedule(data){if(timer){clearTimeout(timer);timer=null;}if(document.hidden)return;timer=setTimeout(load,busyPoll(data)?4000:15000);}
 async function load(){
  const force=Boolean(arguments[0]);
  const pid=selected();
  const projectChanged=project!==pid;
  project=pid;
  const ticket=++epoch;
  if(!pid){render(null,'プロジェクトが選択されていません');notifyGoalLabels();return;}
  loadQuestions(pid,ticket,force||projectChanged);
  try{
   const response=await fetch('/api/projects/'+encodeURIComponent(pid)+'/workflow-readiness');
   const body=await response.json().catch(()=>({}));
   if(ticket!==epoch)return;
   if(!response.ok)throw Error(typeof body.detail==='string'?body.detail:'HTTP '+response.status);
   state=body;
   render(body);
   notifyGoalLabels();
   schedule(body);
  }catch(error){
   if(ticket!==epoch)return;
   state=null;
   render(null,error.message||'通信できませんでした');
   notifyGoalLabels();
   schedule({phase:'waiting_budget'});
  }
 }
 async function loadMetrics(){const pid=selected();const ticket=++metricsEpoch;const button=el('workflowGoalMetricsRefresh');if(button)button.disabled=true;if(!pid){mstatus.textContent='プロジェクトが選択されていません';if(ticket===metricsEpoch&&button)button.disabled=false;return;}try{const response=await fetch('/api/projects/'+encodeURIComponent(pid)+'/goal-metrics');const body=await response.json().catch(()=>({}));if(ticket!==metricsEpoch)return;if(!response.ok)throw Error(typeof body.detail==='string'?body.detail:'HTTP '+response.status);mstatus.textContent='';renderMetrics(body);}catch(error){if(ticket!==metricsEpoch)return;mstatus.textContent='指標を取得できませんでした: '+(error.message||'通信できませんでした');}finally{if(ticket===metricsEpoch&&button)button.disabled=false;}}
 retry.onclick=()=>load(true);mretry.onclick=loadMetrics;nextBtn.onclick=function(){openOverviewTarget(nextBtn.dataset.tab,nextBtn.dataset.actionId);};planLink.onclick=function(){openOverviewTarget('goal_plan','');};monitorLink.onclick=function(){openOverviewTarget('execute','');};artifactsLink.onclick=function(){openOverviewTarget('artifacts','');};historyLink.onclick=function(){openOverviewTarget('history','');};document.getElementById('projectSelect').addEventListener('change',()=>{load(true);});document.getElementById('projectSelect').addEventListener('change',loadMetrics);document.addEventListener('visibilitychange',()=>{if(document.hidden){if(timer){clearTimeout(timer);timer=null;}return;}load();});load(true);loadMetrics();
}
if(typeof module!=='undefined')module.exports={HEADINGS,OVERVIEW_FIELDS,STATUS_CODE_JA,statusCodeJa,formatCount,collectStopReasonCards,buildOverviewModel,formatCurrent,formatNextOperation,formatEvidence,classifyOwner,questionCard};
if(typeof document!=='undefined'){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();}
})();
