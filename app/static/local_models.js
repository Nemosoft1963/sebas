(() => {
  const grid = document.querySelector(".grid");
  if (!grid) return;

  const panel = document.createElement("section");
  panel.className = "card local-model-panel";
  panel.innerHTML = [
    '<div class="local-model-head">',
    '<div><h2>ローカルLLM</h2>',
    '<p>インストール済みの会話用Ollamaモデルを選択します。埋め込み専用モデルは表示されません。</p></div>',
    '<span id="localModelSwitchState" class="local-model-state">一覧を取得中...</span>',
    "</div>",
    '<div class="local-model-controls">',
    '<label for="localModelSelect">使用モデル</label>',
    '<select id="localModelSelect" aria-label="使用するローカルLLM"></select>',
    '<button id="localModelApply" type="button">このモデルに切替</button>',
    '<button id="localModelRefresh" class="secondary" type="button">一覧更新</button>',
    "</div>",
    '<div id="localModelDetails" class="local-model-details"></div>',
  ].join("");
  grid.insertAdjacentElement("afterend", panel);

  const select = panel.querySelector("#localModelSelect");
  const apply = panel.querySelector("#localModelApply");
  const refresh = panel.querySelector("#localModelRefresh");
  const state = panel.querySelector("#localModelSwitchState");
  const details = panel.querySelector("#localModelDetails");
  let catalog = [];
  let selected = "";

  function sizeLabel(bytes) {
    if (!bytes) return "サイズ不明";
    return (bytes / 1024 / 1024 / 1024).toFixed(1) + " GB";
  }

  function renderDetails() {
    const model = catalog.find((item) => item.name === select.value);
    if (!model) {
      details.textContent = "";
      apply.disabled = true;
      return;
    }
    const parts = [
      model.parameter_size || model.family || "規模不明",
      model.quantization_level || "量子化情報なし",
      sizeLabel(model.size),
      model.thinking_supported
        ? "thinking対応"
        : "thinking非対応（自動で互換モードに補正）",
      model.loaded ? "メモリ読込済み" : "未読込",
    ];
    details.textContent = parts.join(" / ");
    apply.disabled = model.name === selected;
  }

  async function loadModels() {
    refresh.disabled = true;
    apply.disabled = true;
    state.classList.remove("error");
    state.textContent = "一覧を取得中...";
    try {
      const response = await fetch("/api/local-models", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "モデル一覧を取得できません");
      catalog = body.models || [];
      selected = body.selected || "";
      select.replaceChildren();
      for (const model of catalog) {
        const option = document.createElement("option");
        option.value = model.name;
        option.textContent =
          model.name +
          (model.thinking_supported ? " / thinking" : "") +
          (model.loaded ? " / 読込済み" : "");
        select.append(option);
      }
      if (catalog.some((item) => item.name === selected)) select.value = selected;
      state.textContent = catalog.length
        ? "現在: " + selected
        : "選択可能な会話モデルがありません";
      renderDetails();
    } catch (error) {
      state.classList.add("error");
      state.textContent = "一覧取得エラー: " + error.message;
      select.replaceChildren();
      details.textContent = "Ollamaの起動状態を確認してください。";
    } finally {
      refresh.disabled = false;
    }
  }

  async function switchModel() {
    const requested = select.value;
    if (!requested || requested === selected) return;
    if (!confirm(
      "ローカルLLMを「" + requested +
      "」へ切り替えます。モデル読込に時間がかかる場合があります。続行しますか？"
    )) return;
    apply.disabled = true;
    refresh.disabled = true;
    select.disabled = true;
    state.classList.remove("error");
    state.textContent = "切替中: " + requested;
    try {
      const response = await fetch("/api/local-models/selected", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: requested }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "モデル切替に失敗しました");
      selected = body.selected;
      state.textContent = "切替完了: " + selected;
      await loadModels();
      if (typeof window.health === "function") await window.health();
    } catch (error) {
      state.classList.add("error");
      state.textContent = "切替エラー: " + error.message;
      renderDetails();
    } finally {
      refresh.disabled = false;
      select.disabled = false;
    }
  }

  async function syncSelectedModel() {
    if (document.hidden || refresh.disabled || select.disabled || document.activeElement === select) return;
    try {
      const response = await fetch("/api/local-models", { cache: "no-store" });
      if (!response.ok) return;
      const body = await response.json();
      if ((body.selected || "") !== selected) await loadModels();
    } catch (_) {
      // The regular refresh control remains available during transient API errors.
    }
  }
  select.addEventListener("change", renderDetails);
  apply.addEventListener("click", switchModel);
  refresh.addEventListener("click", loadModels);
  document.addEventListener("visibilitychange", syncSelectedModel);
  setInterval(syncSelectedModel, 10000);
  loadModels();
})();
