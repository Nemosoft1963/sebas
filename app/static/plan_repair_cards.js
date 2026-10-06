(()=>{'use strict';
const FIELDS=['対象条件','停止理由','確認した証拠','試した処置','セバスが次に行えること','人間に必要な判断','再評価結果'];
function addText(parent,tag,text,className){const node=document.createElement(tag);node.textContent=text==null?'':String(text);if(className)node.className=className;parent.append(node);return node;}
function cardFor(item){
 const card=document.createElement('article');card.className='mission-event repair-card';
 addText(card,'h4','残件 '+String(item.issue_id||'')+' / '+String(item.run_state||''));
 const excerpt=document.createElement('p');excerpt.textContent=String(item.issue_excerpt||'');card.append(excerpt);
 const list=document.createElement('dl');card.append(list);
 for(const key of FIELDS){addText(list,'dt',key);addText(list,'dd',item[key]||'未取得');}
 addText(card,'p',String(item['技術試験と業務達成の区別']||''));
 return card;
}
async function load(pid,hostId){
 const host=document.getElementById(hostId);if(!host||!pid)return;
 host.replaceChildren();addText(host,'p','読込中…');
 try{
  const r=await fetch('/api/projects/'+encodeURIComponent(pid)+'/plan/repair-runs/cards');
  const d=await r.json().catch(()=>({}));
  if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'HTTP '+r.status);
  host.replaceChildren();
  const cards=(d&&d.cards)||[];
  if(!cards.length){addText(host,'p','修復ランの残件カードはまだありません。修復ランを開始するとここに表示されます。');return;}
  const note=addText(host,'p','技術試験と業務達成は別欄です。JSONだけを見せません。');
  note.className='mission-note';
  for(const item of cards.slice(0,20))host.append(cardFor(item));
  if(cards.length>20)addText(host,'p','ほか '+(cards.length-20)+'件');
 }catch(e){host.replaceChildren();addText(host,'p','残件カードを取得できません: '+(e.message||'通信できませんでした'));}
}
function mount(hostId,title){
 function init(){
  let anchor=null;
  if(hostId==='repairCardsGoal')anchor=document.getElementById('workflow-goal_plan');
  else if(hostId==='repairCardsExec')anchor=document.getElementById('workflow-execute');
  else if(hostId==='repairCardsMission')anchor=document.querySelector('.mission-shell .mission-card')||document.querySelector('.mission-shell');
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
mount('repairCardsGoal','修復ランの残件（指摘→条件→工程→差分）');
mount('repairCardsExec','修復ランの残件（実行前確認）');
mount('repairCardsMission','修復ランの残件（概要）');
async function refreshAll(){
 const pid=(document.getElementById('projectSelect')||{}).value||'';
 await load(pid,'repairCardsGoal');await load(pid,'repairCardsExec');await load(pid,'repairCardsMission');
}
if(typeof document!=='undefined'){
 document.addEventListener('change',e=>{if(e&&e.target&&e.target.id==='projectSelect')refreshAll();});
 setInterval(()=>{const pid=(document.getElementById('projectSelect')||{}).value||'';if(pid)load(pid,'repairCardsGoal');},15000);
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>setTimeout(refreshAll,800));else setTimeout(refreshAll,800);
}
if(typeof module!=='undefined')module.exports={FIELDS,cardFor};
})();
