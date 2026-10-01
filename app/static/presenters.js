(function(root,factory){
  'use strict';
  var api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  root.MmiPresenters=api;
  root.presenters=api;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  var UNKNOWN='不明';
  function present(value){
    if(value===null||value===undefined||value==='')return UNKNOWN;
    if(value===true)return '必須';
    if(value===false)return '不要';
    if(Array.isArray(value))return value.length?value.map(present).join('、'):UNKNOWN;
    if(typeof value==='object')return Object.keys(value).length?Object.keys(value).map(function(k){return k+': '+present(value[k]);}).join('、'):UNKNOWN;
    return String(value);
  }
  function parse(value){
    if(value&&typeof value==='object')return value;
    if(typeof value==='string'){try{return JSON.parse(value);}catch(e){return {criterion:value};}}
    return {};
  }
  function rawJson(value){
    if(typeof value==='string'){try{return JSON.stringify(JSON.parse(value),null,2);}catch(e){return value;}}
    try{return JSON.stringify(value,null,2);}catch(e){return String(value);}
  }
  function first(source,keys){for(var i=0;i<keys.length;i++){if(Object.prototype.hasOwnProperty.call(source,keys[i])&&source[keys[i]]!==null&&source[keys[i]]!=='')return source[keys[i]];}return null;}
  function outputNames(value){var rows=Array.isArray(value)?value:value?[value]:[];return rows.map(function(x){return x&&typeof x==='object'?first(x,['title','name','path','description','type']):x;});}
  function planContractPresenter(input,context){
    var data=parse(input),task=context||{},outputs=first(data,['outputs','artifacts','deliverables']);
    return {kind:'plan-contract',cards:[
      {title:'この工程で作るもの',value:present(outputNames(outputs).length?outputNames(outputs):first(data,['criterion','objective','scope','description'])||task.title)},
      {title:'必要な入力',value:present(first(data,['inputs','required_inputs','sources','input_requirements']))},
      {title:'先に終える工程',value:present(first(data,['depends_on','dependencies','prerequisites'])||task.depends_on)},
      {title:'人間確認',value:present(first(data,['human_confirmation_required','semantic_review_required','human_review','human_confirmation','approval_required','review_required']))},
      {title:'完了の証拠',value:present(first(data,['evidence','completion_evidence','validation','acceptance','criterion']))},
      {title:'未達ならどうなるか',value:present(first(data,['failure_policy','on_failure','failure_action','if_unmet','fallback']))}
    ],raw:input};
  }
  var STATUS={draft:'目標編集中',planning:'計画確認待ち',ready:'実行待ち',running:'実行中',paused:'一時停止',completed:'全工程完了・目標達成は未確定',failed:'未達成・エラー停止',cancelled:'中止',pending:'待機',needs_review:'人間確認待ち',blocked:'依存工程で停止',skipped:'スキップ',passed:'合格',pass:'合格',not_passed:'指摘あり',conditional:'指摘あり',fail:'指摘あり',unverifiable:'未検証',unverified:'未検証',connection_failed:'接続できない',connection_error:'接続できない',waiting_budget:'利用枠の回復待ち'};
  function statusPresenter(input){
    if(input===null||input===undefined||input==='')return UNKNOWN;
    var key=typeof input==='object'?input.status:input;
    return Object.prototype.hasOwnProperty.call(STATUS,key)?STATUS[key]:UNKNOWN;
  }
  function issueText(issue){return present(issue&&typeof issue==='object'?first(issue,['text','issue','message','detail','unmet_goal','reason']):issue);}
  function reviewRows(data){
    var reviews=first(data,['reviews','provider_reviews','results'])||[],outcomes=data.provider_outcomes||[];
    if(!Array.isArray(reviews))reviews=[];
    if(!Array.isArray(outcomes))outcomes=[];
    return reviews.map(function(row){var issues=row.issues;if(!Array.isArray(issues))issues=issues?[issues]:[];return {ai:present(first(row,['provider','id','label','name'])),verdict:statusPresenter(first(row,['verdict','status','outcome'])),severity:present(first(row,['severity','importance','priority'])||first(issues[0]||{},['severity'])),issue:present(issues.map(issueText)),proposal:present(first(row,['suggestion','proposal','fix','recommendation'])||issues.map(function(issue){return issue&&typeof issue==='object'?first(issue,['remedy']):null;}).filter(Boolean))};}).concat(outcomes.filter(function(row){var id=first(row,['provider','id']);return !reviews.some(function(r){return first(r,['provider','id'])===id;});}).map(function(row){return {ai:present(first(row,['provider','id','label'])),verdict:statusPresenter(first(row,['status','outcome'])),severity:UNKNOWN,issue:present(first(row,['error','message'])),proposal:UNKNOWN};}));
  }
  function reviewPresenter(input){
    var data=parse(input),rows=reviewRows(data),total=first(data,['required_count'])||rows.length,passed=first(data,['success_count']);if(passed===null)passed=rows.filter(function(row){return row.verdict==='合格';}).length;var errors=data.connection_errors||[];
    return {kind:'review',cards:[
      {title:'全体判定',value:statusPresenter(data.status||data.verdict)},
      {title:'合格 n/m',value:(total?passed+'/'+total:UNKNOWN)},
      {title:'内容の指摘',value:present(rows.map(function(row){return row.issue;}).filter(function(x){return x!==UNKNOWN;}))},
      {title:'接続できなかったAI',value:present(errors.map(function(x){return first(x,['provider','id','label']);}))}
    ],rows:rows,raw:input};
  }
  function taskResultPresenter(task){
    task=task||{};var result=parse(task.result),error=task.error;
    return {kind:'task-result',cards:[
      {title:'処理結果',value:present(first(result,['summary','result','message','status'])||(!error&&task.result))},
      {title:'作成物',value:present(outputNames(first(result,['artifacts','outputs','files','deliverables'])))},
      {title:'失敗理由',value:present(error||first(result,['error','failure_reason']))},
      {title:'再実行できる範囲',value:present(first(result,['retry_scope','rerun_scope','recoverable_from']))},
      {title:'次の操作',value:present(first(result,['next_action','next_step','action']))}
    ],raw:{result:task.result,error:task.error}};
  }
  function flatten(value,prefix,out){
    out=out||{};prefix=prefix||'';
    if(value&&typeof value==='object'&&!Array.isArray(value)){Object.keys(value).forEach(function(k){flatten(value[k],prefix?prefix+'.'+k:k,out);});}
    else out[prefix||'値']=value;
    return out;
  }
  var DIFF_LABELS={human_review:'人間確認',human_confirmation:'人間確認',approval_required:'人間確認',review_required:'人間確認',depends_on:'先に終える工程',inputs:'必要な入力',outputs:'作成物',criterion:'完了の証拠'};
  function diffPresenter(before,after){
    var left=flatten(parse(before)),right=flatten(parse(after)),keys=Object.keys(Object.assign({},left,right));
    var changes=keys.filter(function(k){return rawJson(left[k])!==rawJson(right[k]);}).map(function(k){var leaf=k.split('.').pop(),label=DIFF_LABELS[leaf]||leaf||'項目';return label+': '+present(left[k])+'→'+present(right[k]);});
    return {kind:'diff',changes:changes.length?changes:[UNKNOWN],raw:{before:before,after:after}};
  }
  function revisionHistoryPresenter(input){
    var rows=Array.isArray(input)?input:[];
    return {kind:'revision-history',rows:rows.map(function(row){return {date:present(first(row,['created_at','updated_at','applied_at','timestamp'])),version:present(first(row,['plan_version','version','revision'])),target:present(first(row,['task_id','target','scope'])),summary:present(first(row,['summary','reason','change','status'])),applied:row.applied===true||row.status==='applied'?'反映済み':row.applied===false||row.status&&row.status!=='applied'?'未反映':UNKNOWN,diffs:(row.changes||[]).map(function(x){return diffPresenter(x.before,x.after);})};}),raw:input};
  }
  function httpErrorPresenter(status,detail){
    var code=Number(status)||Number((String(detail||'').match(/\b(401|403|429|500|502|503|504)\b/)||[])[1]);
    var message=code===401||code===403?'Google接続などの認証を確認し、必要なら再認証してください':code===429?'利用枠に達しました。回復後に再試行してください':code===503||code===502||code===504?'一時的な接続エラーです。後で再試行してください':code===500?'処理中に問題が発生しました。後で再試行してください':'処理に失敗しました。技術情報を確認してください';
    return {message:message,raw:{status:status,detail:detail}};
  }
  return {UNKNOWN:UNKNOWN,present:present,rawJson:rawJson,statusPresenter:statusPresenter,planContractPresenter:planContractPresenter,reviewPresenter:reviewPresenter,taskResultPresenter:taskResultPresenter,diffPresenter:diffPresenter,revisionHistoryPresenter:revisionHistoryPresenter,httpErrorPresenter:httpErrorPresenter};
});
