const DRAFT_URL = "http://web:8000/api/google-sites/automation/draft";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "local-supporter-get-draft") return false;
  fetch(DRAFT_URL, { cache: "no-store" })
    .then(async (response) => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `下書き取得エラー (${response.status})`);
      sendResponse({ ok: true, data });
    })
    .catch((error) => sendResponse({ ok: false, error: String(error.message || error) }));
  return true;
});
