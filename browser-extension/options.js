"use strict";

const saveBodyEl = document.getElementById("saveBody");
const statusEl = document.getElementById("status");

function renderExportStatus(result) {
  const status = result && result.status;
  if (status === "pending") {
    const count = Number(result.pending_count || 0);
    statusEl.textContent = count
      ? `エクスポート中（${count}件の完了待ち）`
      : "エクスポート中（完了待ち）";
    return;
  }
  if (status === "complete") {
    statusEl.textContent = "エクスポート完了（past dayを削除し、todayは保持しました）";
    return;
  }
  if (status === "failed") {
    statusEl.textContent = `エクスポート失敗（${result.error || "unknown"}）。bufferを保持しています`;
    return;
  }
  statusEl.textContent = "エクスポート状態を確認できませんでした。bufferを保持しています";
}

chrome.storage.local.get({ saveBody: true }, (o) => {
  saveBodyEl.checked = o.saveBody !== false;
});

saveBodyEl.addEventListener("change", () => {
  chrome.storage.local.set({ saveBody: saveBodyEl.checked }, () => {
    statusEl.textContent = "保存しました";
  });
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "kaizenlog_browser_ai_export_status") {
    renderExportStatus(msg);
  }
});

document.getElementById("exportNow").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "kaizenlog_export_now" }, (result) => {
    const lastError = chrome.runtime && chrome.runtime.lastError;
    renderExportStatus(
      lastError ? { status: "failed", error: "runtime_error" } : result
    );
  });
});
