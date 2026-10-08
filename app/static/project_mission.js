(function(){
"use strict";
document.addEventListener("DOMContentLoaded",function(){
  var mission=null,missionProject="",missionDirty=false,missionBusy=false,providerCatalog=[],externalActions=[],premarketing={campaigns:[],leads:[]},premarketingLoading=false;
  function el(id){return document.getElementById(id)}
  function escapeHtml(value){var d=document.createElement("div");d.textContent=value==null?"":String(value);return d.innerHTML}
  var presenters=window.MmiPresenters;
  function technicalInfo(value,label){return '<details class="technical-info"><summary>'+(label||'技術情報を表示')+'</summary><pre>'+escapeHtml(presenters.rawJson(value))+'</pre></details>'}
  function cardsHtml(model){return '<div class="presenter-cards">'+model.cards.map(function(card){return '<section class="presenter-card"><h4>'+escapeHtml(card.title)+'</h4><p>'+escapeHtml(card.value)+'</p></section>'}).join('')+'</div>'+technicalInfo(model.raw)}
  function userError(error){var shown=presenters.httpErrorPresenter(null,error&&error.message);return shown.message+' '+technicalInfo(shown.raw)}
  function statusLabel(value){return({draft:"目標編集中",planning:"計画確認待ち",ready:"実行待ち",running:"実行中",paused:"一時停止",completed:"全工程完了・目標達成は未確定",failed:"未達成・エラー停止",cancelled:"中止"})[value]||presenters.statusPresenter(value)}
  function completedGoalLabel(){var snap=typeof window.workflowReadinessSnapshot==="function"?window.workflowReadinessSnapshot():null;if(snap&&snap.final_completed===true)return "目標達成(確定)";return "全工程完了・目標達成は未確定"}
  function missionStateText(){var state=mission&&mission.status||"draft";if(state==="ready"&&mission.execution_gate&&mission.execution_gate.blocked)return "承認済み・外部検証待ち";if(state==="completed")return completedGoalLabel();return statusLabel(state)}
  window.refreshGoalCompletionLabels=function(){if(mission&&el("missionState"))el("missionState").textContent=missionStateText()};
  function taskStatus(value){return({pending:"待機",running:"実行中",completed:"完了",failed:"失敗",needs_review:"資料・計算の確認待ち",blocked:"依存失敗で停止",skipped:"スキップ"})[value]||value}
  async function jsonRequest(url,options){
    var response=await fetch(url,options),body=await response.json().catch(function(){return{}});
    if(!response.ok){var detail=typeof body.detail==="string"?body.detail:presenters.rawJson(body.detail||body);var shown=presenters.httpErrorPresenter(response.status,detail),error=Error(shown.message);error.technical=shown.raw;throw error;}
    return body;
  }
  function renderExternalActions(){
    var box=el("externalActionList");if(!box)return;
    box.innerHTML=externalActions.length?externalActions.map(function(a){
      var controls=a.status==="pending_approval"?'<button data-action-approve="'+escapeHtml(a.id)+'">承認</button>':a.status==="approved"?'<button data-action-execute="'+escapeHtml(a.id)+'">実行</button><button data-action-complete="'+escapeHtml(a.id)+'">外部実施証拠を登録</button>':'';
      return '<article class="mission-event"><strong>'+escapeHtml(a.kind)+' → '+escapeHtml(a.target)+' / '+escapeHtml(a.status)+'</strong><pre>'+escapeHtml(a.content)+'</pre>'+(a.evidence?'<p>証拠: '+escapeHtml(a.evidence)+'</p>':'')+(a.error?'<p class="error">'+escapeHtml(a.error)+'</p>':'')+controls+'</article>';
    }).join(""):"<span class=\"mission-empty\">外部実行アクションは未登録です</span>";
    box.querySelectorAll("[data-action-approve]").forEach(function(b){b.onclick=async function(){if(!confirm("対象と内容を確認し、この外部アクションを承認しますか？"))return;await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/actions/"+b.dataset.actionApprove+"/approve",{method:"POST"});await loadExternalActions()}});
    box.querySelectorAll("[data-action-execute]").forEach(function(b){b.onclick=async function(){if(!confirm("承認済みアクションを実際に実行します。emailの場合は外部へ送信されます。続行しますか？"))return;try{await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/actions/"+b.dataset.actionExecute+"/execute",{method:"POST"})}catch(e){el("missionMessage").textContent=e.message}await loadExternalActions()}});
    box.querySelectorAll("[data-action-complete]").forEach(function(b){b.onclick=async function(){var evidence=prompt("実施日時、相手、結果、確認可能な記録を入力してください");if(!evidence)return;await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/actions/"+b.dataset.actionComplete+"/complete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({evidence:evidence})});await loadExternalActions()}});
  }
  async function loadExternalActions(){
    try{externalActions=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/actions");renderExternalActions()}catch(e){if(el("externalActionList"))el("externalActionList").textContent=e.message}
  }
  function renderCampaignMonitor(c){
    var m=c.monitor||{},counts=m.counts||{},stages=m.stages||[],progress=Number(m.progress_percent||0);
    return '<section class="premonitor"><div class="premonitor-head"><div><strong>実行モニター</strong><small> 最終活動 '+escapeHtml(m.last_activity_at||"-")+'</small></div><b>'+progress+'%</b></div><div class="premonitor-progress"><i style="width:'+Math.max(0,Math.min(100,progress))+'%"></i></div><div class="premonitor-stages">'+stages.map(function(s){return '<div class="premonitor-stage '+escapeHtml(s.state)+'"><span></span><strong>'+escapeHtml(s.label)+'</strong><small>'+escapeHtml(s.detail)+'</small></div>'}).join('')+'</div><div class="premonitor-counts"><span>SNS媒体 <b>'+Number(counts.social_channels||0)+'</b></span><span>投稿操作 <b>'+Number(counts.composer_opens||0)+'</b></span><span>公開投稿 <b>'+Number(counts.published_posts||0)+'</b></span><span>リード <b>'+Number(counts.leads||0)+'</b></span><span>有望 <b>'+Number(counts.qualified_leads||0)+'</b></span></div><p class="premonitor-next"><strong>次の操作:</strong> '+escapeHtml(m.next_action||"状態を確認する")+'</p>'+(m.attention?'<p class="error">要確認: '+escapeHtml(m.attention)+'</p>':'')+'</section>';
  }
  function renderPremarketing(){
    var box=el("premarketingList");if(!box)return;
    var signature=currentProject+JSON.stringify(premarketing);
    if(box.dataset.renderSignature===signature)return;
    var expanded=Array.from(box.querySelectorAll("details")).map(function(d){return d.open});
    var sameProject=box.dataset.renderProject===currentProject;
    box.dataset.renderSignature=signature;box.dataset.renderProject=currentProject;
    var campaigns=premarketing.campaigns||[],leads=premarketing.leads||[],google=premarketing.google||{},production=premarketing.creative_production||{},canva=production.canva||{},canvaReady=!!canva.configured;
    var browserLabel=google.browser_state==="ready"?"稼働":google.browser_state==="setup_required"?"初期設定待ち":"停止";
    var googleState='<div class="premonitor-toolbar"><p>Google連携: '+(google.configured?'設定済み':'認証設定待ち')+' / 専用ブラウザ: '+browserLabel+(google.account?' / '+escapeHtml(google.account):'')+' / 制作実行: '+(production.codex_required===false?'Local Supporter（Codex不要）':'確認中')+' / Canva: '+(canvaReady?'直接連携済み':escapeHtml(canva.message||'未設定（アクセストークンとブランドテンプレートIDが必要）'))+'</p><span id="premarketingMonitorUpdated">更新 '+new Date().toLocaleTimeString()+'（15秒間隔）</span><button type="button" data-premarketing-refresh>今すぐ更新</button></div>';
    box.innerHTML=googleState+(campaigns.length?campaigns.map(function(c){
      var absolute=location.origin+c.capture_url,status=c.publication_status||"local_only",controls='';
      var creative=c.creative||{},creativeStatus=creative.status||"not_started",creativeHtml='';
      if(creativeStatus==="not_started"||creativeStatus==="failed")controls+='<button data-creative-package="'+escapeHtml(c.id)+'">デザイン品質向上</button>';
      else if(creativeStatus==="brief_ready")controls+='<button data-creative-orchestrate="'+escapeHtml(c.id)+'">'+(canvaReady?'各AI＋Canvaで自動制作':'各AI＋ローカルで自動制作（Canva未設定）')+'</button><button data-creative-auto="'+escapeHtml(c.id)+'">ローカルのみで自動作成</button><button data-creative-package="'+escapeHtml(c.id)+'">制作仕様を再生成・コピー</button><button data-creative-review="'+escapeHtml(c.id)+'">外部デザイン結果を登録</button>';
      else if(creativeStatus==="reviewed")controls+='<a class="button" href="'+escapeHtml(c.capture_url)+'/creative/preview" target="_blank">自動デザイン確認</a><button data-creative-orchestrate="'+escapeHtml(c.id)+'">'+(canvaReady?'各AI＋Canvaで再制作':'各AI＋ローカルで再制作（Canva未設定）')+'</button><button data-creative-auto="'+escapeHtml(c.id)+'">ローカルのみで再生成</button><button data-creative-review="'+escapeHtml(c.id)+'">レビューを更新</button><button data-creative-approve="'+escapeHtml(c.id)+'">品質承認</button>';
      else if(creativeStatus==="approved")creativeHtml='<p>デザイン品質: 承認済み / '+Number(creative.quality_score||0)+'点 / <a href="'+escapeHtml(c.capture_url)+'/creative/preview" target="_blank">デザインを確認</a>'+(creative.design_url?' / <a href="'+escapeHtml(creative.design_url)+'" target="_blank" rel="noopener noreferrer">Canvaを確認</a>':'')+'</p>';
      if(status==="local_only")controls='<button data-google-request="'+escapeHtml(c.id)+'">Google公開申請</button>';
      else if(status==="awaiting_approval")controls='<button data-google-approve="'+escapeHtml(c.id)+'">公開承認</button>';
      else if(status==="approved")controls='<button data-google-publish="'+escapeHtml(c.id)+'">Googleフォーム公開</button>';
      else if(status==="failed"||status==="reauth_required")controls='<button data-google-publish="'+escapeHtml(c.id)+'">再試行</button>';
      if(c.google_form_id)controls+='<button data-google-sync="'+escapeHtml(c.id)+'">回答同期</button>';
      var siteStatus=c.site_publication_status||"not_requested",revisionStatus=c.landing_revision_status||"",hasLiveSite=!!c.google_site_url;
      var draftVersion=c.landing_asset_version||"",publishedVersion=c.published_asset_version||"";
      var versionMatches=!!draftVersion&&draftVersion===publishedVersion&&siteStatus==="published";
      var versionHtml=draftVersion?'<p class="'+(versionMatches?'success':'warning')+'">LP版: 原案 '+escapeHtml(draftVersion)+' / 公開 '+escapeHtml(publishedVersion||"未照合")+(versionMatches?'（一致）':'（不一致・再配置と公開検証が必要）')+'</p>':'<p class="warning">LP版: 未生成（LP資材を再生成してください）</p>';
      var publisherTarget=c.google_site_edit_url||"https://sites.google.com/new";
      var publisherLaunchUrl="http://127.0.0.1:8010/?intent=newBrowser&url="+encodeURIComponent(publisherTarget);
      if(c.google_form_id){
        if(!hasLiveSite&&creativeStatus==="approved")controls+='<button data-google-site-package="'+escapeHtml(c.id)+'">'+(c.landing_assets_path?'品質反映でLP資材再生成':'品質反映済みLP資材生成')+'</button>';
        if(hasLiveSite&&siteStatus==="published"&&(["","published","failed"].indexOf(revisionStatus)>=0))controls+=(canvaReady?'<button data-landing-recompose="'+escapeHtml(c.id)+'" data-recompose-mode="ai_canva">LPをAI＋Canvaで再構成</button>':'<button disabled title="CANVA_ACCESS_TOKENとCANVA_BRAND_TEMPLATE_IDの設定が必要です">LPをAI＋Canvaで再構成（未設定）</button>')+'<button data-landing-recompose="'+escapeHtml(c.id)+'" data-recompose-mode="local">LPをローカルで再構成</button>';
        if(hasLiveSite&&revisionStatus==="creative_approved")controls+='<button data-google-site-package="'+escapeHtml(c.id)+'">承認デザインでLP更新資材を生成</button>';
        if(c.landing_assets_path&&(siteStatus==="draft_ready"||(!hasLiveSite&&siteStatus==="not_requested")))controls+='<button type="button" data-google-site-request="'+escapeHtml(c.id)+'">'+(hasLiveSite?'LP更新申請':'LP公開申請')+'</button>';
        else if(siteStatus==="awaiting_approval")controls+='';
        else if(siteStatus==="approved"||siteStatus==="failed")controls+='<a class="button" href="'+escapeHtml(c.landing_preview_url)+'" target="_blank">最新版LPを確認</a><a class="button" href="'+escapeHtml(publisherLaunchUrl)+'" target="_blank">'+(hasLiveSite?'専用Chromeで既存Google Sitesを更新':'専用ChromeでGoogle Sitesへ配置')+'</a><button data-google-site="'+escapeHtml(c.id)+'" data-site-edit-url="'+escapeHtml(c.google_site_edit_url||"")+'" data-site-public-url="'+escapeHtml(c.google_site_url||"")+'">公開結果を検証・登録</button>';
      }
      var social=c.social_shares||[],socialHtml='';
      if(c.google_site_url){
        if(!social.length)controls+='<button data-social-package="'+escapeHtml(c.id)+'">SNS投稿キット生成</button>';
        else{
          var hasSocialEvidence=social.some(function(s){return s.status==="evidence_registered"||!!s.evidence_url});
          controls+=hasSocialEvidence?'<button disabled title="公開投稿URLが登録済みです。新しいキャンペーン版を作成してください">SNS投稿キット再生成（投稿証拠あり）</button>':'<button data-social-regenerate="'+escapeHtml(c.id)+'">SNS投稿キット再生成</button>';
          if(social.every(function(s){return s.status==="draft_ready"}))controls+='<button data-social-request="'+escapeHtml(c.id)+'">SNS文案承認申請</button>';
          else if(social.every(function(s){return s.status==="awaiting_approval"}))controls+='<button data-social-approve="'+escapeHtml(c.id)+'">SNS文案承認</button>';
        }
      }
      if(social.length)socialHtml='<details><summary>SNS投稿キット（'+social.length+'媒体）</summary>'+social.map(function(s){
        var usable=["approved","composer_opened","evidence_registered"].indexOf(s.status)>=0;
        var action=usable?'<button data-social-open="'+escapeHtml(c.id)+'" data-social-channel="'+escapeHtml(s.channel)+'" data-social-mode="'+escapeHtml(s.mode)+'">'+(s.mode==="copy"?'本文・URLをコピー':escapeHtml(s.label)+'投稿画面')+'</button>':'';
        if(usable)action+='<button data-social-evidence="'+escapeHtml(c.id)+'" data-social-channel="'+escapeHtml(s.channel)+'">投稿URL登録</button>';
        return '<article class="mission-event"><strong>'+escapeHtml(s.label)+' / '+escapeHtml(s.status)+'</strong><pre>'+escapeHtml(s.post_text)+'</pre><p>計測URL: '+escapeHtml(s.tracking_url)+'</p><p>投稿操作開始: '+Number(s.open_count||0)+'回</p>'+(s.evidence_url?'<p>公開投稿: <a href="'+escapeHtml(s.evidence_url)+'" target="_blank" rel="noopener noreferrer">確認</a></p>':'')+action+'</article>';
      }).join('')+'</details>';
      var approvalBanner=siteStatus==="awaiting_approval"?'<section class="site-approval-banner"><strong>Google Sites LPは公開承認待ちです</strong><p>公開内容を確認し、承認後に専用ブラウザで手動公開します。</p><button type="button" data-google-site-approve="'+escapeHtml(c.id)+'">LP公開を承認する</button></section>':'';
      var placementBanner=versionHtml+(siteStatus==="approved"?'<section class="site-placement-banner"><strong>最新版デザインの配置が必要です</strong><p>'+(hasLiveSite?'既存Google Sitesの編集画面を専用ブラウザで開き、':'専用ブラウザでGoogle Sitesを開き、')+'「Local Supporter：下書きを配置」を押して内容を確認してください。Google Sitesの公開は確認後に手動で行います。</p></section>':'');
      return '<article class="mission-event"><strong>'+escapeHtml(c.title)+' / '+escapeHtml(status)+'</strong>'+approvalBanner+placementBanner+renderCampaignMonitor(c)+'<p>対象: '+escapeHtml(c.audience)+'</p>'+creativeHtml+(creativeStatus!=="approved"?'<p>デザイン品質: '+escapeHtml(creativeStatus)+'（LP資材生成前に品質承認が必要）</p>':'')+(Number(c.landing_revision||0)?'<p>LP改訂: '+Number(c.landing_revision)+' / '+escapeHtml(revisionStatus)+(c.landing_revision_instruction?' / 指示: '+escapeHtml(c.landing_revision_instruction):'')+'</p>':'')+'<p>ローカルフォーム: <a href="'+escapeHtml(c.capture_url)+'" target="_blank">'+escapeHtml(absolute)+'</a></p>'+(c.google_form_url?'<p>Googleフォーム: <a href="'+escapeHtml(c.google_form_url)+'" target="_blank">開く</a></p>':'')+(c.landing_assets_path?'<p>LP資材: '+escapeHtml(c.landing_assets_path)+' / 状態 '+escapeHtml(siteStatus)+'</p>':'')+(c.google_site_url?'<p>現在公開中のLP: <a href="'+escapeHtml(c.google_site_url)+'" target="_blank">開く</a></p>':'')+'<p>資材: '+escapeHtml(c.assets_path)+'</p>'+(c.site_publication_error?'<p class="error">'+escapeHtml(c.site_publication_error)+'</p>':'')+(c.publication_error?'<p class="error">'+escapeHtml(c.publication_error)+'</p>':'')+controls+socialHtml+'</article>';
    }).join(""):"<span class=\"mission-empty\">キャンペーンは未作成です</span>")+'<h4>獲得リード '+leads.length+'件</h4>'+(leads.length?leads.map(function(l){return '<article class="mission-event"><strong>'+escapeHtml(l.company||l.name)+' / score '+l.score+' / '+escapeHtml(l.status)+'</strong><p>'+escapeHtml(l.role)+' '+escapeHtml(l.problem)+'</p><p>取得元: '+escapeHtml(l.source)+'</p></article>'}).join(""):"<span class=\"mission-empty\">同意付きリードはまだありません</span>");
    box.querySelectorAll("[data-landing-recompose]").forEach(function(b){b.onclick=async function(){var instruction=prompt("LPをどのように再構成しますか？ 公開してよい内容だけを入力してください。","要点を短くし、相談ボタンまでの流れを分かりやすくする")||"";if(!instruction.trim())return;var mode=b.dataset.recomposeMode;if(mode==="ai_canva"&&!confirm("キャンペーンの公開用情報とこの再構成指示を、許可済み外部AIとCanvaへ送り制作しますか？ 現在公開中のLPは変更しません。"))return;var popup=window.open("about:blank","_blank"),original=b.textContent;b.disabled=true;b.textContent="再構成中...";el("missionMessage").textContent="公開中LPを保持して改訂案を生成しています...";try{var r=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.landingRecompose+"/landing/recompose",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({instruction:instruction.trim(),mode:mode})});if(popup){popup.opener=null;popup.location.replace(r.preview_url)}el("missionMessage").textContent="LP改訂"+r.revision+"のプレビューを生成しました。確認後にデザイン品質を承認してください"}catch(e){if(popup)popup.close();el("missionMessage").textContent="LP再構成エラー: "+e.message;b.disabled=false;b.textContent=original}await loadPremarketing()}});
    box.querySelectorAll("[data-creative-package]").forEach(function(b){b.onclick=async function(){var popup=window.open("about:blank","_blank");try{var r=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.creativePackage+"/creative/package",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({provider:"canva"})});try{await navigator.clipboard.writeText(r.brief)}catch(e){}if(popup){popup.opener=null;popup.location.replace(r.canva_url)}el("missionMessage").textContent="制作仕様を生成しクリップボードへコピーしました。Canvaで制作後、デザイン結果を登録してください"}catch(e){if(popup)popup.close();el("missionMessage").textContent=e.message}await loadPremarketing()}});
    box.querySelectorAll("[data-creative-auto]").forEach(function(b){b.onclick=async function(){var popup=window.open("about:blank","_blank");try{var r=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.creativeAuto+"/creative/auto-generate",{method:"POST"});if(popup){popup.opener=null;popup.location.replace(r.preview_url)}el("missionMessage").textContent="AIデザインと自動品質レビューを生成しました。確認後に品質承認してください"}catch(e){if(popup)popup.close();el("missionMessage").textContent=e.message}await loadPremarketing()}});
    box.querySelectorAll("[data-creative-orchestrate]").forEach(function(b){b.onclick=async function(){if(!confirm("キャンペーンの公開用タイトル・対象・提供価値・CTA・ブランド設定だけを、計画で許可済みの外部AIへ送り、制作部品を生成しますか？"+(canvaReady?" Canva組版も実行します。":" Canvaは未設定のためローカル組版を使います。")))return;var popup=window.open("about:blank","_blank");el("missionMessage").textContent="各AIへ部品製作を依頼し、"+(canvaReady?"Canva":"ローカル")+"で組版しています...";try{var r=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.creativeOrchestrate+"/creative/orchestrate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({use_canva:canvaReady})});if(popup){popup.opener=null;popup.location.replace(r.preview_url)}var ok=(r.components||[]).filter(function(x){return x.ok}).length;el("missionMessage").textContent="Codexを使わず制作完了: AI部品 "+ok+"件 / "+(r.canva?"Canva組版":"ローカル組版")+(r.canva_error?" / "+r.canva_error:"")+"。確認後に品質承認してください"}catch(e){if(popup)popup.close();el("missionMessage").textContent=e.message}await loadPremarketing()}});
    box.querySelectorAll("[data-creative-review]").forEach(function(b){b.onclick=async function(){var design=prompt("Canvaデザインの共有URL（ローカル制作の場合は空欄可）")||"",image=prompt("LPに表示する公開HTTPS画像URL（任意）")||"",review=prompt("ブランド、可読性、CTA、モバイル表示、事実性のレビュー結果");if(!review)return;var score=Number(prompt("品質スコア（0〜100、70以上で承認可能）","80"));if(!Number.isInteger(score)||score<0||score>100){el("missionMessage").textContent="品質スコアは0〜100の整数で入力してください";return}try{await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.creativeReview+"/creative/review",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({design_url:design,image_url:image,review_text:review,quality_score:score})});el("missionMessage").textContent="デザインレビューを登録しました"}catch(e){el("missionMessage").textContent=e.message}await loadPremarketing()}});
    box.querySelectorAll("[data-creative-approve]").forEach(function(b){b.onclick=async function(){if(!confirm("登録されたデザイン、レビュー、品質スコアを確認し、LPへの利用を承認しますか？"))return;try{await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.creativeApprove+"/creative/approve",{method:"POST"});el("missionMessage").textContent="デザイン品質を承認しました。品質反映済みLP資材を生成できます"}catch(e){el("missionMessage").textContent=e.message}await loadPremarketing()}});
    box.querySelectorAll("[data-google-request]").forEach(function(b){b.onclick=async function(){await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.googleRequest+"/google/request",{method:"POST"});await loadPremarketing()}});
    box.querySelectorAll("[data-google-approve]").forEach(function(b){b.onclick=async function(){if(!confirm("Googleフォームを外部公開し、Google Sitesへ掲載することを承認しますか？"))return;await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.googleApprove+"/google/approve",{method:"POST"});await loadPremarketing()}});
    box.querySelectorAll("[data-google-publish]").forEach(function(b){b.onclick=async function(){try{await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.googlePublish+"/google/publish-form",{method:"POST"})}catch(e){el("missionMessage").textContent=e.message}await loadPremarketing()}});
    box.querySelectorAll("[data-google-sync]").forEach(function(b){b.onclick=async function(){try{var r=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.googleSync+"/google/sync",{method:"POST"});el("missionMessage").textContent="Google回答: 取込"+r.imported+"件 / 重複"+r.duplicates+"件 / 除外"+r.rejected+"件"}catch(e){el("missionMessage").textContent=e.message}await loadPremarketing()}});
    box.querySelectorAll("[data-google-site-package]").forEach(function(b){b.onclick=async function(){try{var r=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.googleSitePackage+"/google/site/package",{method:"POST"});window.open(r.preview_url,"_blank");el("missionMessage").textContent="LP資材を生成しました。内容を確認して公開申請してください"}catch(e){el("missionMessage").textContent=e.message}await loadPremarketing()}});
    box.querySelectorAll("[data-google-site-request]").forEach(function(b){b.addEventListener("click",async function(event){
      event.preventDefault();event.stopPropagation();if(b.disabled)return;
      b.disabled=true;var original=b.textContent;b.textContent="申請中...";
      el("missionMessage").textContent="Google SitesのLP公開申請を登録しています...";el("missionMessage").classList.remove("error");
      try{
        await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.googleSiteRequest+"/google/site/request",{method:"POST"});
        el("missionMessage").textContent="LP公開申請を登録しました。次に「LP公開承認」を押してください";
        premarketingLoading=false;await loadPremarketing();
      }catch(e){
        el("missionMessage").textContent="LP公開申請エラー: "+e.message;el("missionMessage").classList.add("error");
        b.disabled=false;b.textContent=original;
      }
    })});
    box.querySelectorAll("[data-google-site-approve]").forEach(function(b){b.onclick=async function(){if(!confirm("このLP内容をGoogle Sitesへ外部公開し、問い合わせボタンを公開済みGoogleフォームへ接続することを承認しますか？"))return;var original=b.textContent;b.disabled=true;b.textContent="承認処理中...";try{await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.googleSiteApprove+"/google/site/approve",{method:"POST"});el("missionMessage").textContent="LP公開を承認しました。専用ブラウザでGoogle Sitesを公開してください";el("missionMessage").classList.remove("error");premarketingLoading=false;await loadPremarketing()}catch(e){el("missionMessage").textContent="LP公開承認エラー: "+e.message;el("missionMessage").classList.add("error");b.disabled=false;b.textContent=original}}});
    box.querySelectorAll("[data-google-site]").forEach(function(b){b.onclick=async function(){var edit=prompt("Google Sites編集URL（省略可）",b.dataset.siteEditUrl||"")||"",published=prompt("公開済みGoogle Sites HTTPS URL（必須）",b.dataset.sitePublicUrl||"")||"";if(!published)return;try{await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.googleSite+"/google/site",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({edit_url:edit,public_url:published})});el("missionMessage").textContent="公開LPのタイトル・CTA・フォーム導線を検証して登録しました"}catch(e){el("missionMessage").textContent=e.message}await loadPremarketing()}});
    box.querySelectorAll("[data-social-package]").forEach(function(b){b.onclick=async function(){try{await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.socialPackage+"/social/package",{method:"POST"});el("missionMessage").textContent="SNS投稿キットを生成しました。文案と計測URLを確認してください"}catch(e){el("missionMessage").textContent=e.message}await loadPremarketing()}});
    box.querySelectorAll("[data-social-regenerate]").forEach(function(b){b.onclick=async function(){if(!confirm("現在のSNS文案・承認状態・投稿画面の起動回数を履歴へ保存し、公開中LPのURLで投稿キットを再生成しますか？ 自動投稿は行いません。"))return;var original=b.textContent;b.disabled=true;b.textContent="再生成中...";try{var r=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.socialRegenerate+"/social/package",{method:"POST"});el("missionMessage").textContent="SNS投稿キットを再生成しました。旧版はアーカイブ済みです。文案と計測URLを確認してください"}catch(e){el("missionMessage").textContent="SNS投稿キット再生成エラー: "+e.message;b.disabled=false;b.textContent=original}await loadPremarketing()}});
    box.querySelectorAll("[data-social-request]").forEach(function(b){b.onclick=async function(){await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.socialRequest+"/social/request",{method:"POST"});await loadPremarketing()}});
    box.querySelectorAll("[data-social-approve]").forEach(function(b){b.onclick=async function(){if(!confirm("表示中のSNS文案と計測URLを承認しますか？ 投稿はまだ行われません。"))return;await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.socialApprove+"/social/approve",{method:"POST"});await loadPremarketing()}});
    box.querySelectorAll("[data-social-open]").forEach(function(b){b.onclick=async function(){var popup=b.dataset.socialMode==="copy"?null:window.open("about:blank","_blank");try{var r=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.socialOpen+"/social/"+encodeURIComponent(b.dataset.socialChannel)+"/open",{method:"POST"}),copied=true;if(r.mode==="copy"||r.mode==="composer_copy"){try{await navigator.clipboard.writeText(r.post_text+"\n\n"+r.tracking_url)}catch(copyError){copied=false}}if(r.compose_url&&popup){popup.opener=null;popup.location.replace(r.compose_url)}else if(popup)popup.close();el("missionMessage").textContent=r.compose_url?r.label+"の投稿画面を開きました。最終確認後に手動投稿してください":copied?"投稿本文と計測URLをコピーしました":"クリップボードを利用できません。画面上の文案とURLを手動コピーしてください"}catch(e){if(popup)popup.close();el("missionMessage").textContent=e.message}await loadPremarketing()}});
    box.querySelectorAll("[data-social-evidence]").forEach(function(b){b.onclick=async function(){var published=prompt("実際に公開された投稿のHTTPS URLを入力してください")||"";if(!published)return;try{await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns/"+b.dataset.socialEvidence+"/social/"+encodeURIComponent(b.dataset.socialChannel)+"/evidence",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({public_post_url:published})});el("missionMessage").textContent="公開投稿URLを証拠として登録しました"}catch(e){el("missionMessage").textContent=e.message}await loadPremarketing()}});
    box.querySelectorAll("button").forEach(function(b){b.type="button"});
    if(sameProject)box.querySelectorAll("details").forEach(function(d,i){if(expanded[i]!==undefined)d.open=expanded[i]});
    box.querySelectorAll("[data-premarketing-refresh]").forEach(function(b){b.onclick=function(){loadPremarketing()}});
  }
  async function loadPremarketing(){
    if(premarketingLoading)return;premarketingLoading=true;var target=currentProject;
    try{var data=await jsonRequest("/api/projects/"+encodeURIComponent(target)+"/premarketing");if(target===currentProject){premarketing=data;renderPremarketing()}}catch(e){if(target===currentProject&&el("premarketingList"))el("premarketingList").textContent=e.message}finally{premarketingLoading=false}
  }
  function selectedProviders(){
    return Array.prototype.slice.call(document.querySelectorAll("#missionProviderChecks input:checked")).map(function(x){return x.value});
  }
  function renderProviders(selected){
    var box=el("missionProviderChecks");
    if(!providerCatalog.length){box.innerHTML="<span class=\"mission-empty\">接続済み外部AIはありません</span>";return}
    box.innerHTML=providerCatalog.map(function(p){
      var checked=selected.indexOf(p.id)>=0?" checked":"";
      var disabled=p.configured?"":" disabled";
      return "<label><input type=\"checkbox\" value=\""+escapeHtml(p.id)+"\""+checked+disabled+"> "+escapeHtml(p.label)+(p.configured?"":"（未接続）")+"</label>";
    }).join("");
    box.querySelectorAll("input").forEach(function(input){input.addEventListener("change",function(){missionDirty=true})});
  }
  async function loadProviders(){
    try{
      providerCatalog=await jsonRequest("/api/research/providers");
      renderProviders(mission&&mission.external_providers||[]);
    }catch(error){el("missionProviderChecks").textContent="外部AI状態を取得できません"}
  }
  function populateFields(force){
    if(!mission)return;
    var changed=missionProject!==currentProject;
    if(force||changed||!missionDirty){
      el("missionGoal").value=mission.goal||"";
      el("missionCriteria").value=mission.success_criteria||"";
      el("missionConstraints").value=mission.constraints_text||"";
      el("missionExternal").checked=!!mission.allow_external_ai;
      if(el("missionMaxParallel"))el("missionMaxParallel").value=String(mission.max_parallel_tasks||2);
      renderProviders(mission.external_providers||[]);
      missionDirty=false;
    }
    missionProject=currentProject;
  }
  var detailDrafts={};
  function detailHtml(task){
    var info=(mission.detailed_tasks||[]).find(function(x){return x.task_id===task.id});
    if(!info)return "";
    if(!info.plan)return '<p>2段階計画: 実行直前に詳細計画を作成します。</p>';
    var p=info.plan,b=info.budget||{},total=(b.planning||0)+(b.execution||0)+(b.repair||0);
    var html='<section class="detail-plan" data-detail-task="'+escapeHtml(task.id)+'"><h4>詳細計画 第'+Number(p.revision)+'版</h4><p>実要求 '+total+'/12回・計画 '+Number(b.planning||0)+'/2・実行 '+Number(b.execution||0)+'/8・修正 '+Number(b.repair||0)+'/2</p>';
    if(info.stale)html+='<p class="error">入力または親契約が変更されています。再開時に詳細計画を更新します。</p>';
    (p.steps||[]).forEach(function(step){
      var spec=p.payload.steps.find(function(x){return x.id===step.step_id})||{};
      html+='<details><summary>'+escapeHtml(step.step_id+' '+(spec.title||'')+' — '+taskStatus(step.state))+'</summary><p>'+escapeHtml(spec.objective||presenters.UNKNOWN)+'</p><p>完了条件: '+escapeHtml(spec.validation||presenters.UNKNOWN)+'</p>'+cardsHtml(presenters.taskResultPresenter({result:step.output,error:step.error}))+'</details>';
    });
    var end=p.steps.find(function(x){return x.step_id==='D06'});
    if(task.status==='needs_review'&&end&&end.state==='needs_review'&&!info.stale){
      var key=p.id+':'+end.output_hash,draft=detailDrafts[key]||{notes:'',checks:[false,false,false]};
      html+='<p>上の工程結果とD05の候補本文を確認してください。確認結果はこの候補だけに適用します。</p>';
      ['主張の意味と根拠を確認した','対象年度・適用版を確認した','親タスクの要求充足を確認した'].forEach(function(label,i){html+='<label><input type="checkbox" data-detail-check="'+i+'" '+(draft.checks[i]?'checked':'')+'>'+label+'</label><br>'});
      html+='<textarea data-detail-notes placeholder="確認した根拠・判断理由">'+escapeHtml(draft.notes)+'</textarea><button data-detail-review="approved">確認結果を承認として記録</button><button data-detail-review="rejected">要修正として記録</button>'+(end.approved?'<p>この候補の確認結果は承認済みです。</p>':'');
    }
    if(task.status!=='running'&&task.status!=='completed')html+='<button data-detail-resume>保存工程から再開準備</button><button data-detail-replan>理由を付けて再計画</button><p>再開準備後に「実行再開」を押してください。</p>';
    return html+'</section>';
  }
  function bindDetailControls(){
    document.querySelectorAll('[data-detail-task]').forEach(function(box){
      var tid=box.dataset.detailTask,info=(mission.detailed_tasks||[]).find(function(x){return x.task_id===tid});if(!info||!info.plan)return;
      var p=info.plan,end=p.steps.find(function(x){return x.step_id==='D06'}),key=p.id+':'+(end&&end.output_hash);
      function draft(){return {notes:(box.querySelector('[data-detail-notes]')||{}).value||'',checks:Array.from(box.querySelectorAll('[data-detail-check]')).map(function(x){return x.checked})}}
      box.querySelectorAll('input,textarea').forEach(function(e){e.oninput=function(){detailDrafts[key]=draft()}});
      async function send(action,payload){try{await jsonRequest('/api/projects/'+encodeURIComponent(currentProject)+'/tasks/'+encodeURIComponent(tid)+'/details/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});await loadMission(false)}catch(e){el('missionMessage').textContent=e.message}}
      box.querySelectorAll('[data-detail-review]').forEach(function(button){button.onclick=function(){var d=draft();return send('review',{plan_id:p.id,candidate_hash:end.output_hash,decision:button.dataset.detailReview,notes:d.notes,meaning_checked:!!d.checks[0],applicability_checked:!!d.checks[1],requirements_checked:!!d.checks[2]})}});
      var resume=box.querySelector('[data-detail-resume]');if(resume)resume.onclick=function(){return send('resume',{})};
      var replan=box.querySelector('[data-detail-replan]');if(replan)replan.onclick=function(){var reason=prompt('詳細計画を作り直す理由を入力してください。既存の要求予算を引き継ぎます。');if(reason&&reason.trim())return send('resume',{replan_reason:reason})};
    });
  }
  function renderMission(){
    if(!mission)return;
    renderWorkflow();
    var state=mission.status||"draft",progress=mission.progress||{completed:0,total:0,percent:0};
    el("missionState").textContent=missionStateText();
    el("missionProgressBar").style.width=progress.percent+"%";
    var active=(mission.tasks||[]).filter(function(x){return x.status==="running"}).length,waiting=(mission.tasks||[]).filter(function(x){return x.status==="pending"}).length;
    el("missionProgressText").textContent=progress.completed+" / "+progress.total+" 完了（"+progress.percent+"%）・実行中 "+active+" / "+(mission.max_parallel_tasks||2)+"・待機 "+waiting;
    el("missionPlanSummary").textContent=mission.plan_summary||"目標を保存し、「AIで計画生成」を押してください。";
    renderInstructionChat();
    var tasks=mission.tasks||[];
    var reviews=mission.plan_reviews||[];
    if(el("missionPlanReviews")){var reviewHtml=reviews.length?"<h3>複数AIによる計画評価</h3>"+reviews.map(function(review){var model=presenters.reviewPresenter({status:review.ok?'passed':'not_passed',reviews:[{provider:review.label||review.id,status:review.ok?'passed':'not_passed',issues:review.ok?review.review:review.error}]});return "<article class=\"mission-event\"><strong>"+escapeHtml(review.label||review.id||presenters.UNKNOWN)+" — "+(review.ok?"評価完了":"評価失敗")+"</strong>"+cardsHtml(model)+"</article>"}).join(""):"<span class=\"mission-empty\">外部AI評価は未実施です</span>";if(state==="failed"&&mission.final_report)reviewHtml+="<article class=\"mission-event\"><strong>未解決事項レポート</strong><p>未解決事項があります。技術情報を確認してください。</p>"+technicalInfo(mission.final_report)+"</article>";el("missionPlanReviews").innerHTML=reviewHtml}
    el("missionTasks").innerHTML=tasks.length?tasks.map(function(task){
      var details=task.result||task.error,deps=task.depends_on||[];
      var quality=(mission.quality_attempts||[]).find(function(x){return x.task_id===task.id});
      var route=(mission.task_routes||[]).find(function(x){return x.task_id===task.id});
      var routeHtml=route?"<p>現在の設定: "+escapeHtml(({off:"無効",shadow:"観測",enforce:"強制検証"})[route.mode]||"設定不正")+" / 実行経路: "+escapeHtml(({legacy:"従来経路",document:"文書契約経路",detailed:"2段階計画",blocked:"停止"})[route.route]||route.route)+(route.reason?"（"+escapeHtml(route.reason)+"）":"")+"</p>":"";
      if(quality)routeHtml+="<p>直近の実要求: "+escapeHtml((quality.actual_models||[]).join(", ")||"モデル未記録")+" / "+Number(quality.actual_requests||0)+"回</p>";
      var qualityHtml=quality?"<details><summary>品質検証: "+escapeHtml(quality.state==="needs_review"?"内容確認待ち":quality.state)+"</summary><pre>"+escapeHtml(JSON.stringify(quality,null,2))+"</pre></details>":"";
      return "<article class=\"mission-task "+escapeHtml(task.status)+"\"><div class=\"mission-task-head\"><span>"+task.position+". "+escapeHtml(task.title)+"</span><span class=\"mission-badge\">"+escapeHtml(taskStatus(task.status))+" / "+escapeHtml(task.mode)+"</span></div>"+
        detailHtml(task)+routeHtml+qualityHtml+(deps.length?"<p>依存: "+escapeHtml(deps.join(", "))+"</p>":"")+
        (task.agent_label?"<p>担当: "+escapeHtml(task.agent_label)+"</p>":"")+
        (task.description?"<p>"+escapeHtml(task.description)+"</p>":"")+
        cardsHtml(presenters.planContractPresenter(task.acceptance_criteria,task))+
        (details?"<section class=\"task-result\"><h4>"+(task.error?"エラー":"中途成果・実行結果")+"</h4>"+cardsHtml(presenters.taskResultPresenter(task))+"</section>":"")+"</article>";
    }).join(""):"<span class=\"mission-empty\">計画はまだありません。目標を入力して計画を生成してください。</span>";
    bindDetailControls();
    var events=mission.events||[];
    el("missionEvents").innerHTML=events.length?events.slice(0,40).map(function(event){
      var when=new Date(event.created_at).toLocaleString("ja-JP");
      return "<article class=\"mission-event\"><time>"+escapeHtml(when)+"</time><strong>"+escapeHtml(event.message)+"</strong>"+
        (event.detail?"<details><summary>中途報告の詳細</summary><pre>"+escapeHtml(event.detail)+"</pre></details>":"")+"</article>";
    }).join(""):"<span class=\"mission-empty\">履歴はまだありません。計画を実行すると判断・操作の記録が表示されます。</span>";
    var running=state==="running";
    el("missionSave").disabled=running||missionBusy;
    el("missionGenerate").disabled=running||missionBusy||!el("missionGoal").value.trim();
    el("missionApprove").disabled=state!=="planning"||missionBusy;
    el("missionStart").disabled=["ready","paused"].indexOf(state)<0||missionBusy||!!mission.execution_gate?.blocked;
    el("missionStart").title=mission.execution_gate?.reason||"";
    var gateNotice=el("missionGateNotice");if(!gateNotice){gateNotice=document.createElement("p");gateNotice.id="missionGateNotice";gateNotice.className="action-block-reason";gateNotice.setAttribute("aria-live","polite");el("missionStart").insertAdjacentElement("afterend",gateNotice);}gateNotice.hidden=!mission.execution_gate?.blocked;gateNotice.textContent=mission.execution_gate?.blocked?"利用できない理由: "+mission.execution_gate.reason+"。解除方法: 外部検証または必要な確認を完了してください。":"";
    el("missionStart").textContent=state==="paused"?"実行再開":"実行開始";
    el("missionPause").disabled=!running||missionBusy;
    el("missionRetry").disabled=state!=="failed"||missionBusy;
    if(el("missionVerify"))el("missionVerify").disabled=running||missionBusy||!tasks.length;
    el("missionCancel").disabled=["planning","ready","running","paused","failed"].indexOf(state)<0||missionBusy;
    el("missionDownload").disabled=!tasks.length&&!mission.final_report;
    el("missionArtifactsDownload").disabled=mission.status!=="completed";
    [el("missionGoal"),el("missionCriteria"),el("missionConstraints"),el("missionExternal"),el("missionMaxParallel")].forEach(function(x){if(x)x.disabled=running});
  }
  function renderInstructionChat(){
    var box=el("missionInstructionChat");if(!box||!mission)return;
    var messages=mission.instruction_messages||[];
    box.innerHTML=messages.length?messages.map(function(item){
      var user=item.kind==="mission_instruction_user",when=new Date(item.created_at).toLocaleString("ja-JP");
      return '<article class="instruction-bubble '+(user?'instruction-user':'instruction-system')+'"><strong>'+(user?'あなた':'システム')+'</strong><p>'+escapeHtml(item.message)+'</p><time>'+escapeHtml(when)+'</time></article>';
    }).join(""):'<span class="mission-empty">追加指示はまだありません</span>';
    box.scrollTop=box.scrollHeight;
  }
  async function submitMissionInstruction(){
    var input=el("missionInstructionInput"),button=el("missionInstructionSend"),text=input.value.trim();
    if(!text){el("missionMessage").textContent="追加指示を入力してください";return}
    button.disabled=true;input.disabled=true;el("missionMessage").textContent="追加指示を記録しています...";
    try{
      mission=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/mission/instructions",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({instruction:text})});
      input.value="";populateFields(true);renderMission();el("missionMessage").textContent="追加指示を記録し、今後の処理条件へ反映しました";el("missionMessage").classList.remove("error");
    }catch(error){el("missionMessage").textContent="追加指示エラー: "+error.message;el("missionMessage").classList.add("error")}
    finally{button.disabled=false;input.disabled=false;input.focus()}
  }
  async function loadMission(forceFields){
    var target=currentProject;
    try{
      var body=await jsonRequest("/api/projects/"+encodeURIComponent(target)+"/mission");
      if(target!==currentProject)return;
      mission=body;
      populateFields(!!forceFields);
      renderMission();
      loadExternalActions();
      loadCaseReferences(target);
      if(forceFields||!el("premarketingList")||el("premarketingList").dataset.renderProject!==target)loadPremarketing();
    }catch(error){
      el("missionMessage").textContent="目標・計画の取得エラー: "+error.message;
      el("missionMessage").classList.add("error");
    }
  }
  async function saveMission(silent){
    var allow=el("missionExternal").checked,providers=selectedProviders();
    if(!el("missionGoal").value.trim())throw Error("プロジェクト目標を入力してください");
    if(allow&&!silent&&!confirm("許可した外部AIタスクでは、プロジェクトの目標・コンテキストが外部APIへ送信されます。保存しますか？"))throw Error("保存を中止しました");
    mission=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/mission",{
      method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({goal:el("missionGoal").value,success_criteria:el("missionCriteria").value,
        constraints_text:el("missionConstraints").value,allow_external_ai:allow,external_providers:providers,
        max_parallel_tasks:Number(el("missionMaxParallel")&&el("missionMaxParallel").value||2)})
    });
    missionDirty=false;populateFields(true);renderMission();
    el("missionMessage").textContent="目標と達成条件を保存しました";
    el("missionMessage").classList.remove("error");
    return mission;
  }
  async function action(path,message){
    missionBusy=true;renderMission();el("missionMessage").textContent=message;el("missionMessage").classList.remove("error");
    try{
      mission=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/mission/"+path,{method:"POST"});
      renderMission();
    }catch(error){
      el("missionMessage").textContent="エラー: "+error.message;el("missionMessage").classList.add("error");
      await loadMission(false);
      throw error;
    }finally{missionBusy=false;renderMission()}
  }
  async function loadCaseReferences(target){
    var box=el("missionCaseReferenceList");if(!box||target!==currentProject)return;
    try{
      var data=await jsonRequest("/api/projects/"+encodeURIComponent(target)+"/plan/case-references");
      if(target!==currentProject)return;
      renderCaseReferences(data);
    }catch(error){box.replaceChildren();var errLine=document.createElement("span");errLine.className="error";errLine.textContent="事例参照の取得エラー: "+(error&&error.message?error.message:String(error&&error.message||""));box.appendChild(errLine)}
  }
  function caseSafeText(value){return value==null?"":String(value)}
  function appendCaseLine(parent,tag,text,className){var node=document.createElement(tag);if(className)node.className=className;node.textContent=text;parent.appendChild(node);return node}
  function renderCaseReferences(data){
    var box=el("missionCaseReferenceList");if(!box)return;
    var items=(data&&data.criteria)||[];
    box.replaceChildren();
    if(!items.length){appendCaseLine(box,"span","参考にした事例はまだありません。計画生成時に照合されます。","mission-empty");return}
    items.forEach(function(criterion){
      var refs=criterion.references||[];
      var used=refs.filter(function(r){return r.used_or_rejected==="used"});
      var rejected=refs.filter(function(r){return r.used_or_rejected==="rejected"});
      var article=document.createElement("article");article.className="mission-event";
      appendCaseLine(article,"h4",caseSafeText(criterion.criterion_id||"条件")+" — "+caseSafeText(criterion.status||""));
      appendCaseLine(article,"p","参考にした事例");
      if(used.length){var usedList=document.createElement("ul");used.forEach(function(r){var li=document.createElement("li");li.textContent="参照ID "+caseSafeText(r.case_id)+"（一致: "+caseSafeText(r.applicability_verdict||"")+"）";li.appendChild(document.createElement("br"));li.appendChild(document.createTextNode(caseSafeText(r.excerpt||"").slice(0,400)));usedList.appendChild(li)});article.appendChild(usedList)}
      else{appendCaseLine(article,"p","参考にした事例はありません。")}
      appendCaseLine(article,"p","採用しなかった理由");
      if(rejected.length){var rejectedList=document.createElement("ul");rejected.forEach(function(r){appendCaseLine(rejectedList,"li",caseSafeText(r.case_id)+" — "+caseSafeText(r.reason||r.applicability_verdict||"不採用"))});article.appendChild(rejectedList)}
      else{appendCaseLine(article,"p","採用しなかった候補はありません。")}
      box.appendChild(article);
    });
    appendCaseLine(box,"p","事例本文は最小限であり、達成証拠として表示しません。RAGによる品質改善は主張しません。","mission-note");
  }
  async function loadWorkspace(){
    var box=el("projectWorkspaceList"),target=currentProject;if(!box)return;
    try{
      var data=await jsonRequest("/api/projects/"+encodeURIComponent(target)+"/workspace");if(target!==currentProject)return;
      el("projectWorkspaceCurrent").textContent="/workspace/"+data.workspace_path+(data.exists?"":"（未作成）");
      box.innerHTML=data.entries&&data.entries.length?data.entries.map(function(item){var link=item.kind==="file"?" <button type=\"button\" data-workspace-download=\""+escapeHtml(item.path)+"\">保存</button>":"";return "<div class=\"context-file\"><span>"+(item.kind==="directory"?"📁 ":"📄 ")+escapeHtml(item.path)+(item.kind==="file"?" / "+item.size_bytes+" bytes":"")+"</span>"+link+"</div>"}).join(""):"<span class=\"mission-empty\">作業ファイルはありません</span>";
      box.querySelectorAll("button[data-workspace-download]").forEach(function(button){button.onclick=function(){var path=button.dataset.workspaceDownload.split("/").map(encodeURIComponent).join("/");downloadSavedResult("/api/projects/"+encodeURIComponent(currentProject)+"/workspace/files/"+path+"/download")}});
    }catch(error){box.innerHTML="<span class=\"error\">Workspace取得エラー: "+escapeHtml(error.message)+"</span>"}
  }
  function setupWorkspaceUI(){
    var fields=document.querySelector(".project-fields"),contexts=document.querySelector(".context-files");if(!fields||!contexts||el("projectWorkspace"))return;
    var label=document.createElement("label");label.innerHTML="作業フォルダ（/workspace 内）<input id=\"projectWorkspace\" maxlength=\"500\" placeholder=\"projects/プロジェクトID（空欄で自動設定）\"><small>この専用範囲だけをローカルAIが参照・更新します。削除と任意コマンド実行は禁止です。</small>";fields.appendChild(label);
    var panel=document.createElement("div");panel.className="context-files";panel.innerHTML='<h3>プロジェクト作業フォルダ</h3><div id="projectWorkspaceCurrent" class="project-note"></div><div class="project-toolbar"><button id="projectWorkspaceRefresh" type="button" class="secondary">更新</button><button id="projectWorkspaceMkdir" type="button" class="secondary">フォルダ作成</button><button id="projectWorkspaceText" type="button" class="secondary">テキスト作成</button><input id="projectWorkspaceFile" type="file" hidden><button id="projectWorkspaceUpload" type="button" class="secondary">ファイル追加</button></div><div id="projectWorkspaceList" class="context-file-list"></div>';contexts.insertAdjacentElement("beforebegin",panel);
    el("projectWorkspaceRefresh").onclick=loadWorkspace;
    el("projectWorkspaceMkdir").onclick=async function(){var path=prompt("作成する相対フォルダ名（例: output/reports）");if(!path)return;try{await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/workspace/operations",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({operations:[{action:"mkdir",path:path}]})});await loadWorkspace()}catch(error){alert("作成エラー: "+error.message)}};
    el("projectWorkspaceText").onclick=async function(){var path=prompt("作成・更新する相対ファイル名（例: output/memo.md）");if(!path)return;var content=prompt("UTF-8テキスト内容");if(content===null)return;try{await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/workspace/operations",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({operations:[{action:"write_text",path:path,content:content}]})});await loadWorkspace()}catch(error){alert("保存エラー: "+error.message)}};
    el("projectWorkspaceUpload").onclick=function(){el("projectWorkspaceFile").click()};
    el("projectWorkspaceFile").onchange=async function(){var file=this.files&&this.files[0];if(!file)return;var path=prompt("保存先の相対パス",file.name);if(!path){this.value="";return}var form=new FormData();form.append("file",file,file.name);form.append("relative_path",path);try{await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/workspace/upload",{method:"POST",body:form});await loadWorkspace()}catch(error){alert("アップロードエラー: "+error.message)}this.value=""};
    el("projectSave").onclick=async function(){var name=el("projectName").value.trim();if(!name){el("projectStatus").textContent="プロジェクト名を入力してください";return}try{await jsonRequest("/api/projects/"+encodeURIComponent(currentProject),{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:name,context_text:el("projectContext").value,workspace_path:el("projectWorkspace").value})});await loadProjects(currentProject);el("projectStatus").textContent="プロジェクト設定を保存しました"}catch(error){el("projectStatus").textContent="保存エラー: "+error.message}};
  }
  function accountingTable(preview){
    var headers=preview.headers||[],rows=preview.rows||[];
    if(!headers.length)return '<span class="mission-empty">表示できるセルがありません</span>';
    return '<div class="accounting-table-wrap"><table class="accounting-table"><thead><tr>'+headers.map(function(value){return '<th>'+escapeHtml(value||'-')+'</th>'}).join('')+'</tr></thead><tbody>'+rows.map(function(row){return '<tr>'+headers.map(function(_,index){return '<td title="'+escapeHtml(row[index] == null?'':row[index])+'">'+escapeHtml(row[index] == null?'':row[index])+'</td>'}).join('')+'</tr>'}).join('')+'</tbody></table></div>';
  }
  async function showAccountingPreview(fileId){
    var panel=el("accountingPreview"),target=currentProject;
    panel.hidden=false;panel.innerHTML='<div class="accounting-preview-head"><h3>会計Excelを解析中...</h3><button type="button" class="secondary" data-accounting-close>閉じる</button></div>';
    panel.querySelector("[data-accounting-close]").onclick=function(){panel.hidden=true};
    try{
      var data=await jsonRequest("/api/projects/"+encodeURIComponent(target)+"/context-files/"+encodeURIComponent(fileId)+"/accounting-preview");if(target!==currentProject)return;
      var html='<div class="accounting-preview-head"><h3>'+escapeHtml(data.label)+' — '+escapeHtml(data.filename)+'</h3><button type="button" class="secondary" data-accounting-close>閉じる</button></div><p class="accounting-note">シート '+data.sheet_count+'件 / '+escapeHtml(data.note)+'</p>';
      (data.warnings||[]).forEach(function(warning){html+='<p class="accounting-warning">'+escapeHtml(warning)+'</p>'});
      (data.sections||[]).forEach(function(section){html+='<section class="accounting-section"><h4>'+escapeHtml(section.sheet_name)+' — '+escapeHtml(section.label)+'（'+section.row_count+'行）</h4><div class="accounting-summary">'+Object.keys(section.summary||{}).map(function(key){return '<span>'+escapeHtml(key)+': '+escapeHtml(section.summary[key])+'</span>'}).join('')+'</div>'+(section.checks||[]).map(function(check){return '<p class="accounting-check '+escapeHtml(check.status)+'">'+escapeHtml(check.name)+': '+escapeHtml(check.detail)+'</p>'}).join('')+accountingTable(section.preview||{})+'</section>'});
      panel.innerHTML=html;panel.querySelector("[data-accounting-close]").onclick=function(){panel.hidden=true};panel.scrollIntoView({behavior:"smooth",block:"nearest"});
    }catch(error){panel.innerHTML='<div class="accounting-preview-head"><h3>会計Excelの確認エラー</h3><button type="button" class="secondary" data-accounting-close>閉じる</button></div><p class="error">'+escapeHtml(error.message)+'</p>';panel.querySelector("[data-accounting-close]").onclick=function(){panel.hidden=true}}
  }
  function setupContextFolderUpload(){
    var fileButton=el("projectMd");
    if(!fileButton||el("projectFolder"))return;
    var heading=el("contextFileList")&&el("contextFileList").closest(".context-files").querySelector("h3");
    if(heading){heading.textContent="登録済みコンテキスト・備忘録";var preview=document.createElement("div");preview.id="accountingPreview";preview.className="accounting-preview";preview.hidden=true;heading.closest(".context-files").appendChild(preview)}
    loadContextFiles=async function(){
      var box=el("contextFileList");
      try{
        contextFiles=await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/context-files");
        if(!contextFiles.length){box.innerHTML='<span class="context-empty">コンテキストファイルはありません</span>';return}
        box.innerHTML=contextFiles.map(function(f){
          var note=f.extraction_note?" / "+f.extraction_note:"";
          var deleteButton=f.source==="memo"?'<button type="button" disabled title="計画と実行に合わせて自動更新されます">自動更新</button>':'<button type="button" data-context-delete="'+escapeHtml(f.id)+'">削除</button>';
          var previewButton=["yayoi_journal","yayoi_trial_balance","spreadsheet"].indexOf(f.file_kind)>=0?'<button type="button" class="context-preview" data-context-preview="'+escapeHtml(f.id)+'">内容確認</button>':'';
          return '<div class="context-file"><span title="'+escapeHtml(f.filename+note)+'">'+escapeHtml(f.filename)+' / '+escapeHtml(f.file_kind)+' / '+f.char_count+'字 / '+f.size_bytes+' bytes</span>'+previewButton+'<button type="button" class="context-download" data-context-download="'+escapeHtml(f.id)+'">原本保存</button>'+deleteButton+'</div>';
        }).join("");
        box.querySelectorAll("button[data-context-preview]").forEach(function(button){button.onclick=function(){showAccountingPreview(button.dataset.contextPreview)}});
        box.querySelectorAll("button[data-context-download]").forEach(function(button){button.onclick=function(){downloadSavedResult("/api/projects/"+encodeURIComponent(currentProject)+"/context-files/"+encodeURIComponent(button.dataset.contextDownload)+"/download")}});
        box.querySelectorAll("button[data-context-delete]").forEach(function(button){button.onclick=function(){deleteContextFile(button.dataset.contextDelete)}});
      }catch(error){box.innerHTML='<span class="error">ファイル一覧エラー: '+escapeHtml(error.message)+'</span>'}
    };
    deleteContextFile=async function(fileId){
      var item=contextFiles.find(function(f){return f.id===fileId});
      if(!item||!confirm('ファイル「'+item.filename+'」をこのプロジェクトから削除しますか？'))return;
      try{
        await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/context-files/"+encodeURIComponent(fileId),{method:"DELETE"});
        await loadProjects(currentProject);el("projectStatus").textContent='「'+item.filename+'」を削除しました';
      }catch(error){el("projectStatus").textContent="ファイル削除エラー: "+error.message}
    };
    var singleInput=el("projectMdFile");
    fileButton.textContent="ファイル追加";
    singleInput.accept=".md,.txt,.csv,.xlsx,.xlsm,text/markdown,text/plain,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel.sheet.macroEnabled.12";
    singleInput.onchange=async function(){
      var files=Array.prototype.slice.call(singleInput.files||[]);if(!files.length)return;
      var unsupported=files.find(function(file){return !/\.(md|txt|csv|xlsx|xlsm)$/i.test(file.name)});
      if(unsupported){el("projectStatus").textContent="対応形式はMD/TXT/CSV/XLSX/XLSMです: "+unsupported.name;singleInput.value="";return}
      fileButton.disabled=true;var uploaded=0;
      try{
        for(var i=0;i<files.length;i++){
          var file=files[i],form=new FormData();el("projectStatus").textContent="「"+file.name+"」を登録中... ("+(i+1)+"/"+files.length+")";form.append("file",file,file.name);
          await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/context-files",{method:"POST",body:form});uploaded++;
        }
        await loadProjects(currentProject);el("projectStatus").textContent=uploaded+"件を追加しました。会計Excelは「内容確認」で表示できます";
      }catch(error){await loadProjects(currentProject);el("projectStatus").textContent="ファイル追加エラー（"+uploaded+"件追加済み）: "+error.message}
      finally{fileButton.disabled=false;singleInput.value=""}
    };
    var folderButton=document.createElement("button"),folderInput=document.createElement("input");
    folderButton.id="projectFolder";folderButton.type="button";folderButton.className="secondary";folderButton.textContent="調査フォルダ追加";
    folderInput.id="projectFolderInput";folderInput.type="file";folderInput.multiple=true;folderInput.hidden=true;
    folderInput.setAttribute("webkitdirectory","");folderInput.setAttribute("directory","");
    fileButton.insertAdjacentElement("afterend",folderButton);
    folderButton.insertAdjacentElement("afterend",folderInput);
    folderButton.addEventListener("click",function(){folderInput.click()});
    folderInput.addEventListener("change",async function(){
      var selected=Array.prototype.slice.call(folderInput.files||[]);
      var files=selected;
      var ignored=selected.length-files.length;
      if(!files.length){el("projectStatus").textContent="選択フォルダにMDファイルがありません";folderInput.value="";return}
      if(files.length>200){el("projectStatus").textContent="一度に登録できるファイルは200件までです";folderInput.value="";return}
      folderButton.disabled=true;fileButton.disabled=true;
      var uploaded=0;
      try{
        for(var i=0;i<files.length;i++){
          var file=files[i],relativePath=file.webkitRelativePath||file.name,form=new FormData();
          el("projectStatus").textContent="フォルダから登録中: "+relativePath+" ("+(i+1)+"/"+files.length+")";
          form.append("file",file,file.name);form.append("relative_path",relativePath);
          await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/context-files",{method:"POST",body:form});
          uploaded++;
        }
        await loadProjects(currentProject);
        el("projectStatus").textContent="調査フォルダから "+uploaded+"件を追加しました。対応形式は本文抽出し、その他も原本と目録を保存します";
      }catch(error){
        await loadProjects(currentProject);
        el("projectStatus").textContent="フォルダ追加エラー（"+uploaded+"件追加済み）: "+error.message;
      }finally{
        folderButton.disabled=false;fileButton.disabled=false;folderInput.value="";
      }
    });
  }

  var missionNote=document.querySelector(".mission-note");if(missionNote)missionNote.textContent="ローカルLLMが工程を設計し、選択した複数AIの評価を統合して高度化します。実行中に不足機能を検出した場合も実現手段を再評価・再実行し、未解決事項はレポートします。";
  var externalBox=document.querySelector(".mission-external");
  if(externalBox){var externalLabel=externalBox.querySelector("label");if(externalLabel)externalLabel.lastChild.textContent=" 外部AIを計画評価・実現手段検討・調査タスクで利用する（明示許可）";var parallel=document.createElement("label");parallel.innerHTML='最大同時実行 <select id="missionMaxParallel"><option value="1">1</option><option value="2" selected>2</option><option value="3">3</option><option value="4">4</option></select>（PC負荷に合わせて設定）';externalBox.appendChild(parallel)}
  var summary=el("missionPlanSummary");if(summary){var reviewBox=document.createElement("div");reviewBox.id="missionPlanReviews";reviewBox.className="mission-list";summary.insertAdjacentElement("afterend",reviewBox)}
  if(summary){var caseBox=document.createElement("section");caseBox.className="mission-list";caseBox.id="missionCaseReferences";var caseTitle=document.createElement("h3");caseTitle.textContent="参考にした事例・採用しなかった理由";caseBox.appendChild(caseTitle);var caseDesc=document.createElement("p");caseDesc.textContent="事例は工程候補の参考であり、達成証拠として表示しません。RAGによる品質改善は主張しません。事例本文は最小限のみ表示します。";caseBox.appendChild(caseDesc);var caseList=document.createElement("div");caseList.id="missionCaseReferenceList";var caseLoading=document.createElement("span");caseLoading.className="mission-empty";caseLoading.textContent="読込中";caseList.appendChild(caseLoading);caseBox.appendChild(caseList);reviewBox.insertAdjacentElement("afterend",caseBox)}
  if(summary){var actionBox=document.createElement("section");actionBox.className="mission-list";actionBox.innerHTML='<h3>承認付き外部実行</h3><p>計画だけで終わらせず、対象と内容を登録して承認後に実行します。emailはSMTP設定時に自動送信でき、その他は実施証拠を登録します。</p><div><select id="externalActionKind"><option value="email">メール送信</option><option value="proposal">提案</option><option value="meeting">商談</option><option value="poc">PoC</option><option value="contract">契約</option><option value="manual">その他</option></select><input id="externalActionTarget" placeholder="送信先メールまたは実行対象"><textarea id="externalActionContent" placeholder="emailは1行目を件名、2行目以降を本文として送信"></textarea><button id="externalActionCreate" type="button">承認待ちへ登録</button></div><div id="externalActionList"><span class="mission-empty">読込中</span></div>';reviewBox.insertAdjacentElement("afterend",actionBox);el("externalActionCreate").onclick=async function(){var target=el("externalActionTarget").value.trim(),content=el("externalActionContent").value.trim();if(!target||!content){el("missionMessage").textContent="実行対象と内容を入力してください";return}await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/actions",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({kind:el("externalActionKind").value,target:target,content:content})});el("externalActionTarget").value="";el("externalActionContent").value="";await loadExternalActions()}}
  if(summary){var pre=document.createElement("section");pre.className="mission-list";pre.innerHTML='<h3>プレマーケティング・リード獲得</h3><p>キャンペーン資材と同意フォームを生成し、回答を自動評価して有望リードを承認待ちメールへ送ります。</p><input id="premarketingTitle" placeholder="キャンペーン名"><textarea id="premarketingAudience" placeholder="対象顧客・課題"></textarea><textarea id="premarketingOffer" placeholder="提供価値・オファー"></textarea><input id="premarketingCta" placeholder="CTA（例: 30分の無料相談）"><button id="premarketingCreate" type="button">キャンペーン生成</button><div id="premarketingList"><span class="mission-empty">読込中</span></div>';actionBox.insertAdjacentElement("afterend",pre);el("premarketingCreate").onclick=async function(){var payload={title:el("premarketingTitle").value.trim(),audience:el("premarketingAudience").value.trim(),offer:el("premarketingOffer").value.trim(),call_to_action:el("premarketingCta").value.trim()};if(!payload.title||!payload.audience||!payload.offer||!payload.call_to_action){el("missionMessage").textContent="キャンペーン名・対象・オファー・CTAを入力してください";return}await jsonRequest("/api/projects/"+encodeURIComponent(currentProject)+"/premarketing/campaigns",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});await loadPremarketing()}}
  window.setInterval(function(){if(el("premarketingList")&&!document.hidden)loadPremarketing()},15000);
  var retryButton=el("missionRetry");if(retryButton&&!el("missionVerify")){var verifyButton=document.createElement("button");verifyButton.id="missionVerify";verifyButton.type="button";verifyButton.className="secondary";verifyButton.textContent="完了検証・補正";retryButton.insertAdjacentElement("beforebegin",verifyButton)}
  ["missionGoal","missionCriteria","missionConstraints","missionExternal","missionMaxParallel"].forEach(function(id){
    el(id).addEventListener("input",function(){missionDirty=true;renderMission()});
    el(id).addEventListener("change",function(){missionDirty=true;renderMission()});
  });
  el("missionSave").addEventListener("click",function(){saveMission(false).catch(function(error){el("missionMessage").textContent=error.message})});
  el("missionGenerate").addEventListener("click",async function(){
    try{var reviewers=selectedProviders();if(el("missionExternal").checked&&reviewers.length&&!confirm("選択した外部AI（"+reviewers.join(", ")+"）へ、目標・達成条件・制約とローカル草案を送って計画を評価させます。秘密・個人情報が含まれないことを確認して続行しますか？"))return;if(missionDirty||!mission||!mission.goal)await saveMission(true);await action("plan/generate","ローカル草案を作成し、複数AI評価後にローカルLLMが高度化しています...")}
    catch(error){el("missionMessage").textContent=error.message;el("missionMessage").classList.add("error")}
  });
  el("missionApprove").addEventListener("click",function(){action("plan/approve","計画を承認しています...").catch(function(){})});
  el("missionStart").addEventListener("click",function(){var providers=selectedProviders();if(mission&&mission.allow_external_ai&&providers.length&&!confirm("実行中に不足機能を発見した場合、目標・制約・対象タスク・不足内容を選択AIへ送り、実現手段を評価させます。調査タスクでは許可済みコンテキストも送信されます。秘密・個人情報が含まれないことを確認して開始しますか？"))return;action("start","依存関係を確認し、不足機能の検出を有効にして計画実行を開始します...").catch(function(){})});
  el("missionPause").addEventListener("click",function(){action("pause","安全に一時停止しています...").catch(function(){})});
  el("missionVerify").addEventListener("click",function(){if(!confirm("完了済みタスクを実ファイル・表処理監査・最終報告で再検証します。不合格タスクとその後続を補正待ちへ戻しますか？"))return;action("verify","完了実績を検証し、必要な補正タスクを準備しています...").then(function(){el("missionMessage").textContent=mission.status==="paused"?"未実証のタスクを補正待ちへ戻しました。「実行再開」でローカルAIが補正します。":"実操作と成果物の検証に合格しました"}).catch(function(){})});
  el("missionRetry").addEventListener("click",function(){action("retry","失敗タスクを再試行可能にしています...").catch(function(){})});
  el("missionCancel").addEventListener("click",function(){if(confirm("この計画の実行を中止しますか？ 完了済み成果は残ります。"))action("cancel","実行を中止しています...").catch(function(){})});
  el("missionDownload").addEventListener("click",function(){downloadSavedResult("/api/projects/"+encodeURIComponent(currentProject)+"/mission/report/download")});
  el("missionArtifactsDownload").addEventListener("click",function(){downloadSavedResult("/api/projects/"+encodeURIComponent(currentProject)+"/mission/artifacts/download")});
  var originalRenderProject=renderProject,lastRenderedProject='';
  renderProject=function(){originalRenderProject();var selected=projects.find(function(p){return p.id===currentProject});if(el("projectTargetName"))el("projectTargetName").textContent=selected&&selected.name||"未選択";if(el("projectWorkspace"))el("projectWorkspace").value=selected&&selected.workspace_path||"";var switched=lastRenderedProject!==currentProject;lastRenderedProject=currentProject;if(switched)clearUnsentDrafts();missionProject="";applyProjectTabMemory();loadMission(true);loadWorkspace()};
  var WORKFLOW_TAB_KEYS=['overview','goal_plan','execute','artifacts','history','manage'];
  var WORKFLOW_TAB_ALIASES={requirements:'goal_plan',plan:'goal_plan',flow:'goal_plan',monitor:'execute',examples:'history'};
  function workflowTabStorageKey(pid){return 'localWorkflowTab:'+String(pid||'')}
  function canonicalWorkflowTab(key){if(WORKFLOW_TAB_ALIASES[key])key=WORKFLOW_TAB_ALIASES[key];if(WORKFLOW_TAB_KEYS.indexOf(key)<0)key='overview';return key}
  function workflowPanel(key){key=canonicalWorkflowTab(key);return el('workflow-'+key)}
  function readWorkflowTab(pid){try{var saved=localStorage.getItem(workflowTabStorageKey(pid));if(saved)return canonicalWorkflowTab(saved)}catch(e){}return 'overview'}
  function writeWorkflowTab(pid,key){if(!pid)return;try{localStorage.setItem(workflowTabStorageKey(pid),canonicalWorkflowTab(key))}catch(e){}}
  function clearUnsentDrafts(){
    ['missionGoal','missionCriteria','missionConstraints','missionInstructionInput','externalActionTarget','externalActionContent','premarketingTitle','premarketingAudience','premarketingOffer','premarketingCta','researchTopic','input'].forEach(function(id){var node=el(id);if(node)node.value='';});
    if(el('missionExternal'))el('missionExternal').checked=false;
    if(el('missionMaxParallel'))el('missionMaxParallel').value='2';
    detailDrafts={};workflowSignature='';selectedWorkflowTask='';
    var list=el('premarketingList');if(list){list.dataset.renderSignature='';list.dataset.renderProject='';}
  }
  function updateWorkflowEmptyState(){
    var guide=el('workflowEmptyGuide'),has=!!currentProject;
    if(guide)guide.hidden=has;
    document.querySelectorAll('#workflow [role=tab]').forEach(function(button){button.disabled=!has&&button.dataset.view!=='manage'});
    if(!has){selectWorkflowTab('manage');var status=document.querySelector('#workflow .workflow-status');if(status)status.textContent='プロジェクトが未選択です。PJ管理から新規作成してください。';}
  }
  function applyProjectTabMemory(){
    updateWorkflowEmptyState();
    if(currentProject)selectWorkflowTab(readWorkflowTab(currentProject));
  }
  function setupWorkflowTabs(){
    var main=document.querySelector('body>main'),host=document.createElement('section');
    host.id='workflow';host.className='card workflow';
    host.innerHTML='<nav class="workflow-tabs" role="tablist" aria-label="プロジェクト作業"><button type="button" role="tab" aria-selected="false" tabindex="-1" data-view="overview">概要</button><button type="button" role="tab" aria-selected="false" tabindex="-1" data-view="goal_plan">目標と計画</button><button type="button" role="tab" aria-selected="false" tabindex="-1" data-view="execute">実行と確認</button><button type="button" role="tab" aria-selected="false" tabindex="-1" data-view="artifacts">成果物</button><button type="button" role="tab" aria-selected="false" tabindex="-1" data-view="history">履歴</button><button type="button" role="tab" aria-selected="false" tabindex="-1" data-view="manage">PJ管理</button></nav><div class="workflow-status" role="status" aria-live="polite" aria-atomic="true"></div><div id="workflowEmptyGuide" class="workflow-empty-guide" hidden><h2>プロジェクトを選択してください</h2><p>作業を始めるには、上の「作業対象」からプロジェクトを選ぶか、「PJ管理」タブで新規作成してください。未選択のままでは、前のプロジェクトの入力内容は使いません。</p></div>';
    var targetBar=el('projectTargetBar'),settingsPanel=el('projectSettingsPanel'),settingsToggle=el('projectSettingsToggle');
    targetBar.insertAdjacentElement('afterend',host);
    var projectCard=document.querySelector('.card.project');
    if(projectCard)settingsPanel.appendChild(projectCard);
    settingsToggle.onclick=function(){var opening=settingsPanel.hidden;settingsPanel.hidden=!opening;settingsToggle.setAttribute('aria-expanded',String(opening));settingsToggle.textContent=opening?'設定を閉じる':'設定'};
    host.querySelector('.workflow-status').append(el('missionState'),el('missionMessage'));
    WORKFLOW_TAB_KEYS.forEach(function(key){var panel=document.createElement('section');panel.id='workflow-'+key;panel.setAttribute('role','tabpanel');panel.setAttribute('aria-labelledby','workflow-tab-'+key);panel.tabIndex=0;host.appendChild(panel)});
    var examplesHost=document.createElement('div');examplesHost.id='workflow-examples';workflowPanel('history').appendChild(examplesHost);
    function move(selector,key){var node=document.querySelector(selector);if(node)workflowPanel(key).appendChild(node)}
    function buttons(key,ids){var row=document.createElement('div');row.className='mission-actions';workflowPanel(key).appendChild(row);ids.forEach(function(id){if(el(id))row.appendChild(el(id))})}
    move('.mission-note','goal_plan');move('.mission-fields','goal_plan');
    var instruction=document.createElement('section');instruction.className='mission-instructions';instruction.innerHTML='<h3>追加指示</h3><p>現在の進捗を保持したまま、今後の処理条件へ指示を追加します。公開Web調査を依頼するときは「Webを検索」と明記してください。検索語や外部AIへ社内原本・個人情報・認証情報は送らず、公式一次資料を保存してローカルLLMが分析します。</p><div id="missionInstructionChat" class="instruction-chat" aria-live="polite"><span class="mission-empty">追加指示はまだありません</span></div><form id="missionInstructionForm" class="instruction-composer"><textarea id="missionInstructionInput" maxlength="4000" rows="3" placeholder="例：Webを検索して現行の公募要領を公式一次資料から収集し、URL・取得日時・版を保存する"></textarea><button id="missionInstructionSend" type="submit">追加指示を送信</button></form>';
    workflowPanel('goal_plan').appendChild(instruction);el('missionInstructionForm').onsubmit=function(event){event.preventDefault();submitMissionInstruction()};
    move('.mission-external','goal_plan');
    buttons('goal_plan',['missionSave','missionGenerate']);
    buttons('goal_plan',['missionApprove']);
    var approvalBar=el('missionApprove').parentElement;approvalBar.classList.add('mission-approval-bar');el('missionApprove').textContent='この計画を承認して実行準備へ';
    var approvalHint=document.createElement('span');approvalHint.textContent='計画内容を確認後、このボタンで承認してください。';approvalBar.appendChild(approvalHint);
    move('#missionPlanSummary','goal_plan');move('#missionPlanReviews','goal_plan');
    var planList=document.createElement('div');planList.id='workflowPlanTasks';workflowPanel('goal_plan').appendChild(planList);
    var graphHosts={flow:'goal_plan',monitor:'execute'};
    ['flow','monitor'].forEach(function(key){var intro=document.createElement('p');intro.textContent=key==='flow'?'工程依存図（補助表示）。矢印は先行工程から後続工程への依存関係です。同じ段の工程は依存関係上、並行実行が可能です。':'工程を選択すると結果とエラーを表示します。状態は自動更新されます。';workflowPanel(graphHosts[key]).appendChild(intro);var graph=document.createElement('div');graph.id='workflowGraph-'+key;graph.className='workflow-graph';workflowPanel(graphHosts[key]).appendChild(graph)});
    buttons('execute',['missionStart','missionPause','missionRetry','missionVerify','missionCancel','missionDownload','missionArtifactsDownload']);
    var resumeHint=document.createElement('p');resumeHint.id='workflowResumeHint';resumeHint.textContent='障害や一時停止のあとは「実行再開」で保存工程から続けられます。失敗した工程は「失敗タスクを再試行」を使います。';workflowPanel('execute').appendChild(resumeHint);
    move('.mission-progress-wrap','execute');
    var detail=document.createElement('div');detail.id='workflowTaskDetail';detail.className='mission-event';workflowPanel('execute').appendChild(detail);
    move('.mission-columns','execute');
    var eventsCol=el('missionEvents')&&el('missionEvents').closest('.mission-column');
    var historyHead=document.createElement('h2');historyHead.textContent='判断・操作の記録';workflowPanel('history').appendChild(historyHead);
    if(eventsCol)workflowPanel('history').appendChild(eventsCol);
    if(actionBox)workflowPanel('execute').appendChild(actionBox);
    if(pre)workflowPanel('execute').appendChild(pre);
    move('.card.monitor','execute');
    var artIntro=document.createElement('p');artIntro.className='mission-note';artIntro.textContent='生成済み成果物を下書き・暫定・検証済み・人間承認済みで区別します。RAG登録は人間承認済みの結果からのみ進められます。';workflowPanel('artifacts').appendChild(artIntro);
    var experiencePanel=el('experienceImportPanel');if(experiencePanel)workflowPanel('artifacts').appendChild(experiencePanel);
    var tools=document.createElement('details');tools.className='workflow-tools';tools.innerHTML='<summary>AI会話・外部AI調査</summary>';workflowPanel('goal_plan').appendChild(tools);
    var chat=document.querySelector('main>.grid'),research=document.querySelector('.card.research');if(chat)tools.appendChild(chat);if(research)tools.appendChild(research);
    document.querySelector('.mission-shell').hidden=true;
    host.querySelectorAll('[role=tab]').forEach(function(button,index){
      button.id='workflow-tab-'+button.dataset.view;button.setAttribute('aria-controls','workflow-'+button.dataset.view);
      button.onclick=function(){selectWorkflowTab(button.dataset.view)};
      button.onkeydown=function(event){var tabs=Array.from(host.querySelectorAll('[role=tab]')),count=tabs.length,next=event.key==='ArrowRight'?(index+1)%count:event.key==='ArrowLeft'?(index+count-1)%count:event.key==='Home'?0:event.key==='End'?count-1:-1;if(event.key==='Enter'||event.key===' '){event.preventDefault();selectWorkflowTab(button.dataset.view);return;}if(next>=0){event.preventDefault();tabs[next].click();tabs[next].focus()}};
    });
    applyProjectTabMemory();
  }
  function selectWorkflowTab(key){
    key=canonicalWorkflowTab(key);
    document.querySelectorAll('#workflow [role=tab]').forEach(function(button){var active=button.dataset.view===key;button.setAttribute('aria-selected',String(active));button.tabIndex=active?0:-1;var panel=workflowPanel(button.dataset.view);if(panel)panel.hidden=!active});
    var examples=el('workflow-examples');if(examples)examples.hidden=key!=='history';
    writeWorkflowTab(currentProject,key);
  }
  window.selectWorkflowTab=selectWorkflowTab;
  var workflowSignature='',selectedWorkflowTask='';
  function renderWorkflow(){
    if(!el('workflow'))return;
    var tasks=mission.tasks||[],signature=JSON.stringify([currentProject,mission.status,tasks]);
    if(signature===workflowSignature)return;workflowSignature=signature;
    el('workflowPlanTasks').innerHTML=tasks.length?tasks.map(function(t){return '<article class="mission-task"><h3>'+escapeHtml(t.position+'. '+t.title)+'</h3><p>'+escapeHtml(t.description||presenters.UNKNOWN)+'</p><p>担当: '+escapeHtml(t.agent_label||t.mode||presenters.UNKNOWN)+'</p>'+cardsHtml(presenters.planContractPresenter(t.acceptance_criteria,t))+'</article>'}).join(''):'<p>計画はまだありません。「プロジェクト・要求」で計画を生成してください。</p>';
    var nodes=new Map(),unresolved=new Set(),levels=new Map();
    tasks.forEach(function(t,i){nodes.set(String(t.task_key||t.id||i),t)});
    function level(key,visiting){if(levels.has(key))return levels.get(key);if(visiting.has(key)){unresolved.add(key);return 0}var next=new Set(visiting);next.add(key);var depth=0;(nodes.get(key).depends_on||[]).forEach(function(dep){dep=String(dep);if(nodes.has(dep))depth=Math.max(depth,level(dep,next)+1);else unresolved.add(key)});levels.set(key,depth);return depth}
    nodes.forEach(function(_,key){level(key,new Set())});
    var positions=new Map(),columns=new Map();nodes.forEach(function(t,key){var depth=levels.get(key),row=columns.get(depth)||0;columns.set(depth,row+1);positions.set(key,{x:depth*290+15,y:row*135+15})});
    ['flow','monitor'].forEach(function(view){
      var graph=el('workflowGraph-'+view);graph.replaceChildren();
      if(!tasks.length){graph.textContent='計画生成後にフローを表示します。';return}
      var canvas=document.createElement('div');canvas.className='workflow-canvas';canvas.style.width=((Math.max.apply(null,Array.from(levels.values()))+1)*290)+'px';canvas.style.height=(Math.max.apply(null,Array.from(columns.values()))*135+20)+'px';graph.appendChild(canvas);
      var ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('width','100%');svg.setAttribute('height','100%');svg.setAttribute('aria-hidden','true');canvas.appendChild(svg);
      nodes.forEach(function(t,key){var p=positions.get(key);(t.depends_on||[]).forEach(function(dep){var from=positions.get(String(dep));if(!from)return;var path=document.createElementNS(ns,'path'),x=from.x+250,y=from.y+48;path.setAttribute('d','M '+x+' '+y+' H '+(x+18)+' V '+(p.y+48)+' H '+p.x+' m -7 -5 l 7 5 l -7 5');path.setAttribute('fill','none');path.setAttribute('stroke','#7b92b7');path.setAttribute('stroke-width','2');svg.appendChild(path)});
        var node=document.createElement('button');node.type='button';node.className='workflow-node '+(view==='monitor'?(['pending','running','completed','failed','blocked','skipped'].includes(t.status)?t.status:'pending'):'');node.style.left=p.x+'px';node.style.top=p.y+'px';node.innerHTML='<strong>'+escapeHtml(t.position+'. '+t.title)+'</strong><small>'+escapeHtml(view==='monitor'?taskStatus(t.status):(t.agent_label||t.mode||'工程'))+'</small>'+(unresolved.has(key)?'<small>依存関係を要確認</small>':'');node.onclick=function(){selectedWorkflowTask=key;selectWorkflowTab('monitor');showWorkflowTask()};canvas.appendChild(node);
      });
    });
    if(!nodes.has(selectedWorkflowTask))selectedWorkflowTask=Array.from(nodes.keys()).find(function(key){return nodes.get(key).status==='running'})||Array.from(nodes.keys())[0]||'';
    showWorkflowTask();
  }
  function showWorkflowTask(){
    var task=(mission.tasks||[]).find(function(t,i){return String(t.task_key||t.id||i)===selectedWorkflowTask});
    el('workflowTaskDetail').innerHTML=task?'<h3>'+escapeHtml(task.title)+' — '+escapeHtml(taskStatus(task.status))+'</h3><p>'+escapeHtml(task.description||presenters.UNKNOWN)+'</p>'+cardsHtml(presenters.planContractPresenter(task.acceptance_criteria,task))+cardsHtml(presenters.taskResultPresenter(task)):'工程を選択すると詳細を表示します。';
  }
  setupWorkflowTabs();
  loadProviders();
  loadMission(true);
  setInterval(function(){loadMission(false)},1000);
  setupWorkspaceUI();
  var selectedProject=projects.find(function(p){return p.id===currentProject});if(el("projectWorkspace"))el("projectWorkspace").value=selectedProject&&selectedProject.workspace_path||"";
  loadWorkspace();
  setupContextFolderUpload();
});
})();
