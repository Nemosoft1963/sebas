(function(){
'use strict';
document.addEventListener('DOMContentLoaded',()=>{
 const host=document.getElementById('workflow-examples');if(!host)return;
 const root=document.createElement('section');root.className='card';root.id='reviewQueue';
 root.innerHTML='<h3>要確認一覧</h3><p>このプロジェクトの実行例・手順学習・分野別TRIZをまとめて確認できます。</p><details><summary>状態の意味と確認の進め方</summary><dl><dt>要確認</dt><dd>内容や根拠を人が確認する段階です。</dd><dt>再検証</dt><dd>入力・条件・試験が変わったため、過去の確認をそのまま使えません。</dd><dt>検証準備</dt><dd>原文の整理や候補の試験が必要です。</dd><dt>未接続・不合格</dt><dd>必要能力や試験結果を確認します。計画保存を実行成功とは扱いません。</dd><dt>機械検査合格</dt><dd>内容の正しさ、コードの動作、実業務での効果を保証しません。</dd><dt>採用済み</dt><dd>確認した入力・版・適用範囲に対する採用です。条件が変われば再確認します。</dd></dl><p>一覧は保存状態の読取専用表示です。原文や証拠を元画面で確認して操作してください。業務タスク全体の承認待ちは含みません。</p></details><label>表示区分<select id="reviewQueueFilter"><option value="">すべて</option><option value="review">要確認</option><option value="revalidate">再検証</option><option value="prepare">検証準備</option><option value="blocked">未接続・不合格</option></select></label><button type="button" id="reviewQueueRefresh">一覧を更新</button><p id="reviewQueueStatus" role="status" aria-live="polite"></p><div id="reviewQueueItems"></div><button type="button" id="reviewQueuePrev">前の実行例</button><button type="button" id="reviewQueueNext">次の実行例</button>';
 host.prepend(root);
 const el=id=>document.getElementById(id);const labels={review:'要確認',revalidate:'再検証',prepare:'検証準備',blocked:'未接続・不合格'};
 let project='',offset=0,next=null,rows=[],epoch=0,requestId=0;
 function clear(){rows=[];next=null;el('reviewQueueItems').replaceChildren();el('reviewQueuePrev').disabled=true;el('reviewQueueNext').disabled=true;}
 function render(){
  const list=el('reviewQueueItems');list.replaceChildren();const selected=rows.filter(x=>!el('reviewQueueFilter').value||x.kind===el('reviewQueueFilter').value);
  for(const item of selected){
   const article=document.createElement('article');article.className='card';article.style.overflowWrap='anywhere';
   const title=document.createElement('strong');title.textContent=labels[item.kind]+'：'+item.title+'（'+item.count+'件）';
   const name=document.createElement('p');name.textContent=item.filename;
   const reason=document.createElement('p');reason.textContent=item.reason;
   const button=document.createElement('button');button.type='button';button.textContent='この実行例で確認';button.onclick=()=>window.dispatchEvent(new CustomEvent('review-open-example',{detail:{project,example:item.example_id}}));
   article.append(title,name,reason,button);list.append(article);
  }
  if(!selected.length){const p=document.createElement('p');p.textContent='表示中の実行例に、この区分の確認対象はありません。';list.append(p);}
  el('reviewQueuePrev').disabled=offset===0;el('reviewQueueNext').disabled=next===null;
 }
 async function refresh(){
  if(!project){clear();el('reviewQueueStatus').textContent='プロジェクトを選択してください。';return;}
  const token=epoch,id=++requestId,pid=project,page=offset;clear();el('reviewQueueStatus').textContent='読込中…';
  try{const r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/review-queue?offset='+page);const d=await r.json();if(token!==epoch||id!==requestId)return;if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'読込に失敗しました');rows=d.items;next=d.next_offset;render();el('reviewQueueStatus').textContent='実行例 '+(page+1)+'件目からの一覧 / 確認対象 '+rows.reduce((n,x)=>n+x.count,0)+'件。 '+d.notice;}
  catch(e){if(token===epoch&&id===requestId){clear();el('reviewQueueStatus').textContent=e.message;}}
 }
 function changed(){const pid=typeof currentProject!=='undefined'?currentProject:'';if(pid===project)return;project=pid;epoch++;offset=0;el('reviewQueueFilter').value='';clear();refresh();}
 el('reviewQueueRefresh').onclick=refresh;el('reviewQueueFilter').onchange=render;
 el('reviewQueuePrev').onclick=()=>{offset=Math.max(0,offset-30);refresh();};el('reviewQueueNext').onclick=()=>{if(next!==null){offset=next;refresh();}};
 window.addEventListener('example-selected',changed);
 // Refresh on entering the tab, without polling or disturbing form drafts.
 const observer=new MutationObserver(()=>{changed();if(!host.hidden)refresh();});observer.observe(host,{attributes:true,attributeFilter:['hidden']});
 const prior=renderProject;renderProject=function(){prior();changed();};changed();if(!project)refresh();
});
})();
