/**
 * KaizenLog Browser AI — service worker.
 *
 * E1: Day-keyed map in chrome.storage.local. Export rewrites full day JSONL
 * (overwrite). Today is kept after export so a second export is still complete;
 * only past days are dropped after successful export.
 *
 * No network requests. Permissions: storage / alarms / downloads only.
 */
"use strict";

importScripts("lib.js");

const ALARM = "kaizenlog-export";
const DAY_MAP_KEY = "eventDayMap";
const LEGACY_BUFFER_KEY = "eventBuffer";
const FOLDER = "kaizenlog-browser-ai";
const L = self.KaizenLogLib;

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM, { periodInMinutes: 5 });
  chrome.storage.local.get({ saveBody: true }, () => {});
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(ALARM, { periodInMinutes: 5 });
  exportBuffer();
});

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === ALARM) exportBuffer();
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "kaizenlog_browser_ai_event" && msg.record) {
    upsertRecord(msg.record).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg && msg.type === "kaizenlog_export_now") {
    exportBuffer().then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});

async function loadDayMap() {
  const data = await chrome.storage.local.get({
    [DAY_MAP_KEY]: {},
    [LEGACY_BUFFER_KEY]: null,
  });
  let map = data[DAY_MAP_KEY];
  if (!map || typeof map !== "object" || Array.isArray(map)) map = {};
  // One-shot migrate legacy array buffer
  if (Array.isArray(data[LEGACY_BUFFER_KEY]) && data[LEGACY_BUFFER_KEY].length) {
    map = L.migrateArrayBufferToDayMap(data[LEGACY_BUFFER_KEY]);
    await chrome.storage.local.set({
      [DAY_MAP_KEY]: map,
      [LEGACY_BUFFER_KEY]: [],
    });
  }
  return map;
}

async function upsertRecord(record) {
  const map = await loadDayMap();
  const result = L.upsertDayMap(map, record, L.MAX_RECORDS_PER_DAY);
  if (result.truncated) {
    // Keep under chrome.storage.local ~10MB without unlimitedStorage permission.
    console.warn(
      "[KaizenLog] day record cap reached; oldest records dropped for",
      result.day,
      "(max",
      L.MAX_RECORDS_PER_DAY,
      ")"
    );
  }
  await chrome.storage.local.set({ [DAY_MAP_KEY]: result.map });
}

/**
 * Export every day still in the map as full JSONL (overwrite).
 * Then drop past days; keep today so subsequent exports stay complete.
 */
async function exportBuffer() {
  const map = await loadDayMap();
  const days = Object.keys(map);
  if (!days.length) return;

  const today = L.dayKey(new Date().toISOString());
  for (let i = 0; i < days.length; i++) {
    const day = days[i];
    const rows = L.recordsForDay(map[day]);
    if (!rows.length) continue;
    const body = rows.map((r) => JSON.stringify(r)).join("\n") + "\n";
    const url =
      "data:application/jsonl;charset=utf-8," + encodeURIComponent(body);
    try {
      await chrome.downloads.download({
        url: url,
        filename: FOLDER + "/" + day + ".jsonl",
        conflictAction: "overwrite",
        saveAs: false,
      });
    } catch (_e) {
      /* keep map for next try — do not prune */
      return;
    }
  }
  const pruned = L.prunePastDays(map, today);
  await chrome.storage.local.set({ [DAY_MAP_KEY]: pruned });
}
