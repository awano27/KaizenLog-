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

// All storage mutations and export scheduling share one service-worker queue.
// This prevents concurrent read -> modify -> set operations from losing rows.
let backgroundQueue = Promise.resolve();
function enqueueBackground(task) {
  const run = backgroundQueue.then(task, task);
  backgroundQueue = run.catch(() => {});
  return run;
}

// A download being accepted is not the same as its file being complete.
// Keep each accepted id until downloads.onChanged reports complete.
const pendingDownloads = new Map();

function notifyExportStatus(result) {
  try {
    chrome.runtime.sendMessage(
      Object.assign({ type: "kaizenlog_browser_ai_export_status" }, result),
      () => {
        void (chrome.runtime && chrome.runtime.lastError);
      }
    );
  } catch (_e) {
    /* no extension page is open — the buffer remains durable for retry */
  }
}

function scheduleExport() {
  enqueueBackground(exportBuffer).then(
    (result) => notifyExportStatus(result),
    (_e) => notifyExportStatus({ status: "failed", error: "export_failed" })
  );
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM, { periodInMinutes: 5 });
  chrome.storage.local.get({ saveBody: true }, () => {});
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(ALARM, { periodInMinutes: 5 });
  scheduleExport();
});

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === ALARM) scheduleExport();
});

chrome.downloads.onChanged.addListener((delta) => {
  const op = pendingDownloads.get(delta && delta.id);
  if (!op) return;
  const state = delta.state && delta.state.current;
  if (state === "in_progress") return;
  if (state === "interrupted") {
    op.failed = true;
    pendingDownloads.delete(delta.id);
    op.pending.delete(delta.id);
    if (!op.statusNotified) {
      op.statusNotified = true;
      notifyExportStatus({ status: "failed", error: "download_interrupted" });
    }
    return;
  }
  if (state !== "complete") return;
  pendingDownloads.delete(delta.id);
  op.pending.delete(delta.id);
  if (!op.pending.size && !op.failed) {
    enqueueBackground(() => finalizeExport(op)).then(
      (result) => {
        if (!op.statusNotified) {
          op.statusNotified = true;
          notifyExportStatus(result);
        }
      },
      (_e) => {
        if (!op.statusNotified) {
          op.statusNotified = true;
          notifyExportStatus({ status: "failed", error: "storage_finalize_failed" });
        }
      }
    );
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "kaizenlog_browser_ai_event" && msg.record) {
    enqueueBackground(() => upsertRecord(msg.record)).then(
      () => sendResponse({ ok: true }),
      (_e) => sendResponse({ ok: false, status: "failed", error: "upsert_failed" })
    );
    return true;
  }
  if (msg && msg.type === "kaizenlog_export_now") {
    enqueueBackground(exportBuffer).then(
      (result) => sendResponse(result),
      (_e) => sendResponse({ status: "failed", error: "export_failed" })
    );
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
  let saveBody = false;
  try {
    const options = await new Promise((resolve) => {
      chrome.storage.local.get({ saveBody: false }, (o) => {
        const lastError = chrome.runtime && chrome.runtime.lastError;
        resolve(lastError ? null : o);
      });
    });
    saveBody = !!options && options.saveBody === true;
  } catch (_e) {
    saveBody = false;
  }
  const storedRecord = Object.assign({}, record);
  if (!saveBody) delete storedRecord.text;
  const result = L.upsertDayMap(map, storedRecord, L.MAX_RECORDS_PER_DAY);
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
  if (!days.length) return { status: "complete", pending_count: 0 };

  const today = L.dayKey(new Date().toISOString());
  const op = {
    map: map,
    today: today,
    days: new Set(),
    pending: new Set(),
    failed: false,
    statusNotified: false,
  };
  for (let i = 0; i < days.length; i++) {
    const day = days[i];
    const rows = L.recordsForDay(map[day]);
    if (!rows.length) continue;
    const body = rows.map((r) => JSON.stringify(r)).join("\n") + "\n";
    const url =
      "data:application/jsonl;charset=utf-8," + encodeURIComponent(body);
    try {
      const downloadId = await chrome.downloads.download({
        url: url,
        filename: FOLDER + "/" + day + ".jsonl",
        conflictAction: "overwrite",
        saveAs: false,
      });
      if (downloadId == null) throw new Error("download id missing");
      op.days.add(day);
      op.pending.add(downloadId);
      pendingDownloads.set(downloadId, op);
    } catch (_e) {
      op.failed = true;
      /* keep map for next try — do not prune */
      return { status: "failed", error: "download_rejected" };
    }
  }
  if (!op.pending.size && !op.failed) return finalizeExport(op);
  return { status: "pending", pending_count: op.pending.size };
}

async function finalizeExport(op) {
  if (op.failed || op.pending.size) return;
  try {
    const current = await loadDayMap();
    const kept = {};
    for (const day of Object.keys(current)) {
      if (day === op.today || !op.days.has(day)) {
        kept[day] = current[day];
        continue;
      }
      // A new event may have arrived after the file was scheduled. Keep that
      // changed bucket for a later retry instead of pruning unexported data.
      if (JSON.stringify(current[day]) !== JSON.stringify(op.map[day])) {
        kept[day] = current[day];
      }
    }
    await chrome.storage.local.set({ [DAY_MAP_KEY]: kept });
    return { status: "complete", pending_count: 0 };
  } catch (_e) {
    return { status: "failed", error: "storage_finalize_failed" };
  }
}
