(() => {
  if (window.top !== window || document.getElementById("local-supporter-sites-assistant")) return;
  const panel = document.createElement("div");
  panel.id = "local-supporter-sites-assistant";
  panel.style.cssText = "position:fixed;right:18px;bottom:18px;z-index:2147483647;padding:12px;border-radius:10px;background:#172033;color:#fff;font:14px system-ui;box-shadow:0 6px 24px #0005;max-width:320px";
  panel.innerHTML = '<button type="button" style="padding:9px 13px;border:0;border-radius:7px;background:#4f8cff;color:white;font-weight:700;cursor:pointer">Local Supporter：下書きを配置</button><div data-status style="margin-top:7px;font-size:12px;line-height:1.4">公開操作は行いません</div>';
  document.documentElement.appendChild(panel);
  const button = panel.querySelector("button");
  const status = panel.querySelector("[data-status]");
  // Keep the Sites publish controls clear; this panel is only an optional helper.
  panel.style.right = "auto";
  panel.style.left = "18px";
  const controls = document.createElement("div");
  controls.style.cssText = "display:flex;gap:8px;justify-content:flex-end;margin-bottom:8px";
  const collapse = document.createElement("button");
  collapse.type = "button";
  collapse.textContent = "折りたたむ";
  collapse.setAttribute("aria-expanded", "true");
  collapse.onclick = () => {
    const expanded = collapse.getAttribute("aria-expanded") === "true";
    button.hidden = expanded;
    status.hidden = expanded;
    collapse.setAttribute("aria-expanded", String(!expanded));
    collapse.textContent = expanded ? "補助パネルを開く" : "折りたたむ";
  };
  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "閉じる ×";
  close.title = "補助パネルを非表示にします。ページ再読み込みで戻せます。";
  close.onclick = () => { panel.hidden = true; };
  for (const control of [collapse, close]) {
    control.style.cssText = "padding:6px 9px;border:1px solid #aab7cc;border-radius:5px;background:#172033;color:white;cursor:pointer";
    controls.appendChild(control);
  }
  panel.prepend(controls);
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const PENDING_KEY = "localSupporterSitesDraftPendingAt";
  const visible = (node) => node && !node.hidden && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== "hidden";
  const enabled = (node) => visible(node) && !node.disabled && node.getAttribute("aria-disabled") !== "true";
  const textEditable = (node) => {
    if (!node) return false;
    if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
      return !node.readOnly && !node.disabled;
    }
    return node.isContentEditable || node.getAttribute("contenteditable") === "true";
  };
  let running = false;
  let phase = "待機";
  let activeProof = null;
  let renderedVersion = null;
  const observedVersions = new Map();
  window.addEventListener("message", (event) => {
    if (embedDialog()) return;
    let host;
    try { host = new URL(event.origin).hostname; } catch (_) { return; }
    if (!(host === "www.gstatic.com" || host.endsWith(".googleusercontent.com"))) return;
    const data = event.data;
    if (data?.type === "local-supporter-rendered" && /^[0-9a-f]{12}$/.test(String(data.version || "")) &&
        data.visible === true) observedVersions.set(data.version, Date.now());
    if (!activeProof) return;
    if (data?.type === "local-supporter-rendered" && data.nonce === activeProof.nonce &&
        data.version === activeProof.version && data.visible === true) renderedVersion = data.version;
  });
  function activate(node) {
    node.scrollIntoView({block: "nearest"});
    node.focus();
    for (const type of ["mousedown", "mouseup", "click"]) {
      node.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window, button: 0, buttons: type === "mousedown" ? 1 : 0}));
    }
  }
  const checkpointKey = "localSupporterPlacement:" + location.pathname;
  function progress(value) { phase = value; status.textContent = value + "…"; }
  function unique(nodes, label) {
    const found = nodes.filter((node) => visible(node) && !panel.contains(node));
    if (found.length > 1) throw new Error(label + "が複数あります。対象を特定できないため停止しました");
    return found[0];
  }
  function bodyDocuments() {
    const docs = [];
    function walk(doc) {
      docs.push(doc);
      for (const frame of doc.querySelectorAll("iframe")) {
        if (frame.closest("[role=dialog]") || !visible(frame)) continue;
        try { if (frame.contentDocument) walk(frame.contentDocument); } catch (_) {}
      }
    }
    walk(document);
    return docs;
  }
  function placed(version) {
    if (renderedVersion === version && !embedDialog()) return true;
    return bodyDocuments().some((doc) =>
      [...doc.querySelectorAll("[data-local-supporter-version]")].some((node) =>
        node.getAttribute("data-local-supporter-version") === version &&
        !node.closest("[role=dialog]") && visible(node)));
  }
  async function ensurePreviousVersionRemoved(previousVersion) {
    observedVersions.delete(previousVersion);
    await wait(3500);
    const seenAt = observedVersions.get(previousVersion) || 0;
    if (seenAt) {
      throw new Error("旧版 " + previousVersion + " がGoogle Sites上に残っています。旧Local Supporter埋込みを削除し、3秒待ってから再実行してください");
    }
  }
  async function clickReady(getter, label) {
    const target = await waitFor(() => { const node = getter(); return enabled(node) ? node : null; }, label);
    activate(target);
  }
  const normalized = (node) => (node.innerText || node.textContent || "").trim().toLowerCase();

  async function waitFor(getter, label, timeout = 15000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const found = getter();
      if (found) return found;
      await wait(250);
    }
    throw new Error(`${label}が見つかりません。Google Sitesの画面構成を確認してください`);
  }
  function byText(words, selector = "button,[role=button],[role=tab],[role=menuitem]") {
    const targets = words.map((word) => word.toLowerCase());
    return unique([...document.querySelectorAll(selector)].filter((node) => targets.includes(normalized(node))), words.join("/"));
  }
  function byTextWithin(root, words, selector = "button,[role=button],[role=tab],[role=menuitem]") {
    const targets = words.map((word) => word.toLowerCase());
    return unique([...root.querySelectorAll(selector)].filter((node) => targets.includes(normalized(node))), words.join("/"));
  }
  function embedDialog() {
    return [...document.querySelectorAll("[role=dialog]")].find((node) => visible(node) && /ウェブからの埋め込み|embed from the web/i.test(node.innerText || ""));
  }
  function previewOpen() {
    return [...document.querySelectorAll(
      '[aria-label="プレビューを終了"],[aria-label="Exit preview"],' +
      '[aria-label="プレビューを閉じて編集"],[aria-label="Close preview and edit"]'
    )].some(visible);
  }
  function driveSaved() {
    return [...document.querySelectorAll("[role=status],[role=button]")].some((node) =>
      visible(node) && /変更内容をすべてドライブに保存しました|All changes saved in Drive/i.test(node.textContent));
  }
  function byLabel(words, selector = "input,textarea,[contenteditable=true]") {
    const targets = words.map((word) => word.toLowerCase());
    return [...document.querySelectorAll(selector)].find((node) => {
      const label = `${node.getAttribute("aria-label") || ""} ${node.getAttribute("data-tooltip") || ""} ${node.getAttribute("placeholder") || ""}`.toLowerCase();
      return visible(node) && targets.some((word) => label.includes(word));
    });
  }
  function byAncestorLabel(words, selector = "input,textarea,[contenteditable=true],[role=textbox]") {
    const targets = words.map((word) => word.toLowerCase());
    return [...document.querySelectorAll(selector)].find((node) => {
      const labelledAncestor = node.closest("[aria-label]");
      const label = (labelledAncestor?.getAttribute("aria-label") || "").toLowerCase();
      return visible(node) && targets.some((word) => label.includes(word));
    });
  }
  function enterText(node, value) {
    if (!textEditable(node)) throw new Error("選択した項目は編集可能な入力欄ではありません");
    node.focus();
    if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
      const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node), "value")?.set;
      if (setter) setter.call(node, value); else node.value = value;
    } else {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(node);
      selection.removeAllRanges();
      selection.addRange(range);
      if (!document.execCommand("insertText", false, value)) {
        throw new Error("編集欄への入力に失敗しました");
      }
    }
    node.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    node.dispatchEvent(new Event("change", { bubbles: true }));
    node.blur();
  }
  function getDraft() {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("下書き取得がタイムアウトしました")), 20000);
      chrome.runtime.sendMessage({ type: "local-supporter-get-draft" }, (response) => {
        clearTimeout(timer);
        if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
        if (!response?.ok) return reject(new Error(response?.error || "承認済み下書きを取得できません"));
        resolve(response.data);
      });
    });
  }
  function storageGet(key) {
    return new Promise((resolve, reject) => chrome.storage.local.get(key, (data) => {
      if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
      else resolve(data || {});
    }));
  }
  function storageSet(value) {
    return new Promise((resolve, reject) => chrome.storage.local.set(value, () => {
      if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
      else resolve();
    }));
  }
  function storageRemove(key) {
    return new Promise((resolve, reject) => chrome.storage.local.remove(key, () => {
      if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
      else resolve();
    }));
  }
  function isEditor() {
    return /\/d\/[^/]+\/p\/[^/]+\/edit(?:$|[?#])/.test(location.pathname + location.search);
  }
  async function openBlankSite() {
    await storageSet({ [PENDING_KEY]: Date.now() });
    status.textContent = "空白サイトを開いています。編集画面で自動的に再開します…";
    location.assign("https://sites.new");
  }
  async function placeDraft() {
    if (previewOpen()) {
      throw new Error("プレビューを終了して編集画面へ戻ってから配置してください。開いたままでは古いプレビューの背後を編集してしまいます");
    }
    progress("承認済み原稿を確認");
    const draft = await getDraft();
    if (!draft.title || !draft.embed_html || !draft.asset_version) throw new Error("下書きデータが不完全です");
    if (!isEditor()) {
      throw new Error("既存の下書きサイトを開いてから配置してください。自動で別サイトは作成しません");
    }
    const version = String(draft.asset_version);
    const checkpoint = (await storageGet(checkpointKey))[checkpointKey];
    const previousVersion = checkpoint?.previousVersion ||
      (checkpoint?.state === "verified" && checkpoint.version !== version ? checkpoint.version : "");
    if (draft.is_update && previousVersion && previousVersion !== version) {
      progress("旧版の削除を検証");
      try {
        await ensurePreviousVersionRemoved(previousVersion);
      } catch (error) {
        await storageSet({ [checkpointKey]: {
          state: "replacement_required", version, previousVersion, at: Date.now()
        } });
        throw error;
      }
      await storageSet({ [checkpointKey]: {
        state: "replacement_confirmed", version, previousVersion, at: Date.now()
      } });
    }
    if (placed(version)) {
      await waitFor(driveSaved, "Google Driveへの保存完了", 20000);
      await storageSet({ [checkpointKey]: { state: "verified", version, at: Date.now() } });
      status.textContent = "最新版本文の配置と保存を確認しました。プレビューを新しく開き、デスクトップ表示で縦にスクロールして確認してください";
      return;
    }
    if (checkpoint?.version === version && checkpoint.state === "verified") {
      status.textContent = "最新版本文は配置確認済みです。プレビューを新しく開き、デスクトップ表示で縦にスクロールして確認してください";
      panel.style.background = "#176b3a";
      return;
    }
    if (checkpoint?.version === version && checkpoint.state === "inserted_pending_preview") {
      status.textContent = "挿入とGoogle Drive保存は完了しています。プレビューで本文とフォームを確認してください";
      panel.style.background = "#8a5a00";
      return;
    }
    if (checkpoint?.version === version && ["inserting", "unverified"].includes(checkpoint.state) && !embedDialog()) {
      progress("Google Driveへの保存を確認");
      await waitFor(driveSaved, "Google Driveへの保存完了", 20000);
      await storageSet({ [checkpointKey]: { state: "inserted_pending_preview", version, at: Date.now() } });
      status.textContent = "挿入とGoogle Drive保存は完了しています。埋め込み内部を編集画面から自動確認できないため、プレビューで本文とフォームを確認してください";
      panel.style.background = "#8a5a00";
      return;
    }
    if (checkpoint && ["inserting", "unverified", "verified"].includes(checkpoint.state)) {
      throw new Error("前回の挿入結果を確認できません。重複防止のため再挿入を停止しています。Google Sitesのプレビューを確認してください");
    }
    const parsed = new DOMParser().parseFromString(draft.embed_html, "text/html");
    const marker = parsed.createElement("div");
    marker.setAttribute("data-local-supporter-version", version);
    while (parsed.body.firstChild) marker.appendChild(parsed.body.firstChild);
    parsed.body.appendChild(marker);
    activeProof = {version, nonce: crypto.randomUUID()};
    renderedVersion = null;
    const proof = parsed.createElement("script");
    const expected = JSON.stringify({
      ...activeProof,
      requiredTexts: Array.isArray(draft.required_texts) ? draft.required_texts : [draft.title],
      requiredLinks: Array.isArray(draft.required_links) ? draft.required_links : [draft.inquiry_url],
    }).replace(/</g, "\\u003c");
    proof.textContent = "(() => {const p=" + expected + ";setInterval(() => {const m=document.querySelector('[data-local-supporter-version]'); const text=m?.innerText||''; const texts=p.requiredTexts.every(v=>text.includes(v)); const links=p.requiredLinks.every(v=>[...document.querySelectorAll('a')].some(a=>a.href===v)); if(m && texts && links && m.getBoundingClientRect().height>0) top.postMessage({type:'local-supporter-rendered',nonce:p.nonce,version:p.version,visible:true},'https://sites.google.com');},1000);})();";
    parsed.body.appendChild(proof);
    const html = "<!doctype html>" + parsed.documentElement.outerHTML;
    progress("タイトルを配置");
    const documentName = byAncestorLabel(["サイトのドキュメント名", "document name"], "input");
    if (documentName) enterText(documentName, draft.title);
    const siteName = await waitFor(() => byLabel(["サイト名", "site name"], "input,[contenteditable=true]"), "サイト名欄");
    enterText(siteName, draft.title);
    const pageTitleAnchor = await waitFor(
      () => byText(["ページのタイトル", "page title"], "[role=textbox],[contenteditable=true]") ||
        byLabel(["ページのタイトル", "page title"], "[role=textbox],[contenteditable=true],textarea,input") ||
        [...document.querySelectorAll('[role="textbox"][aria-label="テキスト"],[role="textbox"][aria-label="Text"]')].find(visible),
      "ページタイトル欄"
    );
    let pageTitle = pageTitleAnchor;
    if (!textEditable(pageTitle)) {
      activate(pageTitleAnchor);
      pageTitle = await waitFor(() => {
        if (textEditable(pageTitleAnchor) && visible(pageTitleAnchor)) return pageTitleAnchor;
        const candidates = [...document.querySelectorAll(
          '[role="textbox"][contenteditable="true"][aria-label="テキスト"],' +
          '[role="textbox"][contenteditable="true"][aria-label="Text"]'
        )].filter((node) => visible(node) && !panel.contains(node));
        return candidates.length === 1 ? candidates[0] : null;
      }, "ページタイトル編集欄");
    }
    if (pageTitle !== siteName && normalized(pageTitle) !== draft.title.trim().toLowerCase()) enterText(pageTitle, draft.title);
    let dialog = embedDialog();
    progress("本文入力を準備");
    if (!dialog) {
      const embed = await waitFor(() => byText(["埋め込む", "embed"]), "埋め込みボタン");
      activate(embed);
      dialog = await waitFor(embedDialog, "埋め込みダイアログ");
    }
    const codeTab = await waitFor(() => byTextWithin(dialog, ["埋め込みコード", "embed code"], "[role=tab]"), "埋め込みコードタブ");
    if (codeTab.getAttribute("aria-selected") !== "true") {
      activate(codeTab);
      await wait(500);
    }
    const previewReady = () =>
      [...dialog.querySelectorAll('[aria-label="コードを編集"],[aria-label="Edit code"],[aria-label="カスタム埋め込みのプレビュー"]')].some(visible);
    if (previewReady()) {
      await clickReady(() => dialog.querySelector('[aria-label="コードを編集"],[aria-label="Edit code"]'), "コードを編集");
    }
    {
      const editor = await waitFor(() => [...dialog.querySelectorAll("textarea")].find(visible), "HTML入力欄");
      enterText(editor, html);
      if (editor.value !== html) throw new Error("本文の入力内容が原稿と一致しません");
      progress("最新版のプレビューを生成");
      await clickReady(() => byTextWithin(dialog, ["次へ", "next"]), "次へボタン");
      await waitFor(previewReady, "埋め込みプレビュー", 20000);
    }
    const insert = await waitFor(
      () => { const node = byTextWithin(dialog, ["挿入", "insert"], '[role="button"],button'); return enabled(node) ? node : null; },
      "挿入ボタン",
      20000
    );
    await storageSet({ [checkpointKey]: { state: "awaiting_user_insert", version, at: Date.now() } });
    phase = "Googleの挿入操作を待機";
    renderedVersion = null;
    insert.scrollIntoView({block: "nearest"});
    insert.focus();
    status.textContent = "Googleダイアログ右下の「挿入」を1回押してください。押した後の配置と保存は自動確認します";
    await waitFor(() => !visible(dialog), "Googleの「挿入」操作", 300000);
    await storageSet({ [checkpointKey]: { state: "inserting", version, at: Date.now() } });
    progress("本文と保存状態を検証");
    await waitFor(driveSaved, "Google Driveへの保存完了", 20000);
    try {
      await waitFor(() => placed(version), "最新版本文の配置証拠", 20000);
    } catch (error) {
      await storageSet({ [checkpointKey]: { state: "inserted_pending_preview", version, at: Date.now() } });
      status.textContent = "挿入とGoogle Drive保存は完了しています。埋め込み内部を編集画面から自動確認できないため、プレビューで本文とフォームを確認してください";
      panel.style.background = "#8a5a00";
      return;
    }
    await storageSet({ [checkpointKey]: { state: "verified", version, at: Date.now() } });
    await storageRemove(PENDING_KEY);
    status.textContent = "最新版本文の配置と保存を確認しました。プレビューで表示を確認してください。";
    panel.style.background = "#176b3a";
  }
  button.addEventListener("click", async () => {
    if (running) return;
    running = true;
    button.disabled = true;
    status.textContent = "承認済み原稿を配置しています…";
    try { await placeDraft(); }
    catch (error) { status.textContent = phase + "： " + String(error.message || error); panel.style.background = "#8b1e2d"; }
    finally { button.disabled = false; running = false; }
  });
  (async () => {
    const pending = (await storageGet(PENDING_KEY))[PENDING_KEY];
    if (pending) {
      await storageRemove(PENDING_KEY);
      status.textContent = "前回の処理は停止しています。編集画面で配置ボタンを押してください";
    }
  })();
  const syncPanelWithPreview = () => {
    panel.style.display = previewOpen() ? "none" : "";
  };
  new MutationObserver(syncPanelWithPreview).observe(document.documentElement, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["aria-label", "style", "class"],
  });
  syncPanelWithPreview();
})();
