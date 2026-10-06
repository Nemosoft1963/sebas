(()=>{'use strict';
const FIELDS=['対象条件','停止理由','確認した証拠','試した処置','セバスが次に行えること','人間に必要な判断','再評価結果'];
const EXTRA=['状態','承認','技術試験と業務達成の区別'];
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
function addCandidates(parent,detail){
 const wrap=document.createElement('div');wrap.className='resolution-candidates';parent.append(wrap);
 addText(wrap,'h5','参考にできた事例/採用しなかった事例と理由');
 const rag=(detail&&detail.rag_references)||{};
 const eligible=Array.isArray(rag.eligible)?rag.eligible:[];
 const rejected=Array.isArray(rag.rejected)?rag.rejected:[];
 if(rag.summary_ja)addText(wrap,'p',rag.summary_ja,'mission-note');
 if(!eligible.length&&!rejected.length)addText(wrap,'p','適格性の参照はありません。');
 const list=document.createElement('dl');wrap.append(list);
 for(const item of eligible.slice(0,10)){
  addText(list,'dt','参考にできた事例 '+String(item.case_id||'').slice(0,8));
  addText(list,'dd','要旨: '+String(item.excerpt||'未取得').slice(0,400)+' / 理由: '+String(item.reason||''));
 }
 for(const item of rejected.slice(0,10)){
  addText(list,'dt','採用しなかった事例 '+String(item.case_id||'').slice(0,8));
  addText(list,'dd','理由('+String(item.verdict||'不明')+'): '+String(item.reason||''));
 }
 if(eligible.length>10||rejected.length>10)addText(wrap,'p','ほか '+(eligible.length+rejected.length-20)+'件');
 addText(wrap,'h5','TRIZ候補の順位と理由');
 const triz=(detail&&detail.triz_candidates)||{};
 const ranked=Array.isArray(triz.ranked)?triz.ranked:[];
 const excluded=Array.isArray(triz.excluded)?triz.excluded:[];
 if(triz.note_ja)addText(wrap,'p',triz.note_ja,'mission-note');
 if(!ranked.length&&!excluded.length)addText(wrap,'p','順位付けできる候補はありません。');
 const order=document.createElement('ol');wrap.append(order);
 for(const item of ranked.slice(0,10)){
  const line=document.createElement('li');
  line.textContent='第'+String(item.rank)+'位 '+String(item.candidate_id||'').slice(0,8)+' 合計'+String(item.score)+'点: '+String(item.reason_ja||'').slice(0,400);
  order.append(line);
 }
 if(ranked.length>10)addText(wrap,'p','ほか '+(ranked.length-10)+'件');
 for(const item of excluded.slice(0,10)){
  addText(wrap,'p','対象外: '+String(item.candidate_id||'').slice(0,8)+' / '+String(item.reason_ja||''));
 }
 addText(wrap,'p','業務未達: 候補・試験は業務達成ではありません。業務達成は条件PASSと人間確認で別に判定します。','mission-note');
}
function cardFor(item){
 const card=document.createElement('article');card.className='mission-event resolution-card';
 addText(card,'h4','残件 '+String(item.id||'').slice(0,8)+' / '+String(item['状態']||''));
 const list=document.createElement('dl');card.append(list);
 for(const key of FIELDS.concat(EXTRA)){addText(list,'dt',key);addText(list,'dd',item[key]||'未取得');}
 const detailHost=document.createElement('div');card.append(detailHost);
 detailHost.dataset.resolutionCandidates=String(item.id||'');
 return card;
}
async function loadCandidates(host,pid,items){
 for(const item of (items||[]).slice(0,20)){
  if(!item||!item.id)continue;
  try{
   const r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/resolutions/'+encodeURIComponent(item.id)+'/candidates');
   const d=await r.json().catch(()=>({}));
   if(!r.ok)continue;
   const blocks=host.querySelectorAll('[data-resolution-candidates="'+String(item.id)+'"]');
   for(const block of blocks){
    if(block.dataset.resolutionCandidatesLoaded)continue;
    block.dataset.resolutionCandidatesLoaded='1';
    addCandidates(block,d);
   }
  }catch(e){/* 候補の追加表示に失敗しても残件カード自体は維持する */}
 }
}
async function load(pid,hostId){
 const host=document.getElementById(hostId);if(!host||!pid)return;
 host.replaceChildren();addText(host,'p','読込中…');
 try{
  const r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/resolutions');
  const d=await r.json().catch(()=>({}));
  if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'HTTP '+r.status);
  host.replaceChildren();
  const summary=(d&&d.summary)||{};
  const line=addText(host,'p','');
  const openCount=summary.open_count==null?'未取得':String(summary.open_count)+'件';
  const byCause=summary.by_cause||{};
  const parts=Object.keys(byCause).sort().map(k=>k+' '+byCause[k]+'件');
  line.textContent='未達条件の残件: '+openCount+(parts.length?'（'+parts.join(' / ')+'）':'');
  line.className='mission-note';
  const cards=(d&&d.cards)||[];
  if(!cards.length){addText(host,'p','残件はありません。未達条件がある場合は検出するとここに表示されます。');return;}
  const note=addText(host,'p','技術試験と業務達成は別欄です。JSONだけを見せません。未診断は成功扱いにしません。');
  note.className='mission-note';
  for(const item of cards.slice(0,20))host.append(cardFor(item));
  if(cards.length>20)addText(host,'p','ほか '+(cards.length-20)+'件');
  loadCandidates(host,pid,cards);
 }catch(e){host.replaceChildren();addText(host,'p','残件カードを取得できません: '+(e.message||'通信できませんでした'));}
}
function mount(hostId,title){
 function init(){
  let anchor=null;
  if(hostId==='resolutionCardsGoal')anchor=document.getElementById('workflow-goal_plan');
  else if(hostId==='resolutionCardsExec')anchor=document.getElementById('workflow-execute');
  else if(hostId==='resolutionCardsMission')anchor=document.querySelector('.mission-shell .mission-card')||document.querySelector('.mission-shell');
  else anchor=document.getElementById('workflow-overview');
  if(!anchor){setTimeout(init,300);return;}
  if(document.getElementById(hostId))return;
  const box=document.createElement('section');box.className='mission-list';
  addText(box,'h3',title);
  const host=document.createElement('div');host.id=hostId;box.append(host);
  anchor.append(box);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
}
mount('resolutionCardsGoal','未達条件の残件（原因と案内）');
mount('resolutionCardsExec','未達条件の残件（実行前確認）');
mount('resolutionCardsMission','未達条件の残件（概要）');
async function refreshAll(){
 const pid=(document.getElementById('projectSelect')||{}).value||'';
 await load(pid,'resolutionCardsGoal');await load(pid,'resolutionCardsExec');await load(pid,'resolutionCardsMission');
}
if(typeof document!=='undefined'){
 document.addEventListener('change',e=>{if(e&&e.target&&e.target.id==='projectSelect')refreshAll();});
 setInterval(()=>{const pid=(document.getElementById('projectSelect')||{}).value||'';if(pid)load(pid,'resolutionCardsGoal');},15000);
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>setTimeout(refreshAll,800));else setTimeout(refreshAll,800);
}
if(typeof module!=='undefined')module.exports={FIELDS,cardFor};
})();
