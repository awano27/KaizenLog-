"use strict";

const saveBodyEl = document.getElementById("saveBody");
const statusEl = document.getElementById("status");

chrome.storage.local.get({ saveBody: true }, (o) => {
  saveBodyEl.checked = o.saveBody !== false;
});

saveBodyEl.addEventListener("change", () => {
  chrome.storage.local.set({ saveBody: saveBodyEl.checked }, () => {
    statusEl.textContent = "保存しました";
  });
});

document.getElementById("exportNow").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "kaizenlog_export_now" }, () => {
    statusEl.textContent = "エクスポートを要求しました（Downloads/kaizenlog-browser-ai）";
  });
});
