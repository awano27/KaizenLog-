/**
 * Pure helpers for KaizenLog browser-extension buffer/export.
 * No chrome.* APIs — loadable from service worker (importScripts) and content scripts.
 *
 * Design (E1/E2):
 * - Day map { "YYYY-MM-DD": { recordKey: record } } keeps full day state.
 * - Export always rewrites the whole day file (overwrite) → idempotent.
 * - Stable keys (not text fingerprint) so streaming updates overwrite one row;
 *   no separate "response complete" detector is required.
 */
(function (global) {
  "use strict";

  // chrome.storage.local ~10MB quota; per-day cap without unlimitedStorage.
  const MAX_RECORDS_PER_DAY = 5000;

  function dayKey(isoTs) {
    try {
      const d = isoTs ? new Date(isoTs) : new Date();
      if (Number.isNaN(d.getTime())) return new Date().toISOString().slice(0, 10);
      return d.toISOString().slice(0, 10);
    } catch (_e) {
      return new Date().toISOString().slice(0, 10);
    }
  }

  /**
   * Content-independent stable key: site|conversation_id|role|messageRef
   * messageRef is DOM stable id or "ord:N".
   */
  function stableMessageKey(site, conversationId, role, messageRef) {
    return [
      String(site || ""),
      String(conversationId || "unknown"),
      String(role || ""),
      String(messageRef || "ord:0"),
    ].join("|");
  }

  /**
   * Prefer data-* / id attributes; fall back to role-local ordinal.
   * Site modules pass preferredAttrNames for that site's DOM.
   */
  function messageRefFromElement(el, roleIndex, preferredAttrNames) {
    const attrs =
      preferredAttrNames && preferredAttrNames.length
        ? preferredAttrNames
        : ["data-message-id", "data-id", "id"];
    if (el && el.getAttribute) {
      for (let i = 0; i < attrs.length; i++) {
        const v = el.getAttribute(attrs[i]);
        if (v && String(v).trim()) return String(v).trim();
      }
    }
    return "ord:" + String(roleIndex);
  }

  /**
   * Upsert record into day map. Mutates a copy-friendly structure.
   * Returns { map, truncated: boolean }.
   *
   * Overwrite same key → streaming partials collapse to final state.
   */
  function upsertDayMap(dayMap, record, maxPerDay) {
    const cap = typeof maxPerDay === "number" ? maxPerDay : MAX_RECORDS_PER_DAY;
    const map = dayMap && typeof dayMap === "object" ? dayMap : {};
    const day = dayKey(record && record.ts);
    const key =
      (record && record.key) ||
      stableMessageKey(
        record && record.site,
        record && record.conversation_id,
        record && record.role,
        "unknown"
      );
    const dayBucket = Object.assign({}, map[day] || {});
    const nextRec = Object.assign({}, record, { key: key });
    dayBucket[key] = nextRec;

    let truncated = false;
    const keys = Object.keys(dayBucket);
    if (keys.length > cap) {
      // Drop oldest by ts (then key) — keep storage under ~10MB without unlimitedStorage.
      const sorted = keys
        .map((k) => ({ k: k, ts: (dayBucket[k] && dayBucket[k].ts) || "" }))
        .sort((a, b) => {
          if (a.ts < b.ts) return -1;
          if (a.ts > b.ts) return 1;
          return a.k < b.k ? -1 : a.k > b.k ? 1 : 0;
        });
      const drop = sorted.length - cap;
      for (let i = 0; i < drop; i++) {
        delete dayBucket[sorted[i].k];
      }
      truncated = true;
    }

    const out = Object.assign({}, map);
    out[day] = dayBucket;
    return { map: out, truncated: truncated, day: day, key: key };
  }

  /**
   * After successful export: keep only today; drop past days.
   */
  function prunePastDays(dayMap, todayKey) {
    const map = dayMap && typeof dayMap === "object" ? dayMap : {};
    const today = todayKey || dayKey(new Date().toISOString());
    const out = {};
    if (map[today]) out[today] = map[today];
    return out;
  }

  /**
   * Flatten one day bucket to sorted record list for JSONL.
   */
  function recordsForDay(dayBucket) {
    const bucket = dayBucket && typeof dayBucket === "object" ? dayBucket : {};
    return Object.keys(bucket)
      .map((k) => bucket[k])
      .filter(Boolean)
      .sort((a, b) => {
        const ta = (a && a.ts) || "";
        const tb = (b && b.ts) || "";
        if (ta < tb) return -1;
        if (ta > tb) return 1;
        return String((a && a.key) || "") < String((b && b.key) || "") ? -1 : 1;
      });
  }

  /**
   * Migrate legacy array buffer → day map (one-shot).
   */
  function migrateArrayBufferToDayMap(arr) {
    let map = {};
    if (!Array.isArray(arr)) return map;
    for (let i = 0; i < arr.length; i++) {
      const rec = arr[i];
      if (!rec || typeof rec !== "object") continue;
      const r = upsertDayMap(map, rec, MAX_RECORDS_PER_DAY);
      map = r.map;
    }
    return map;
  }

  global.KaizenLogLib = {
    MAX_RECORDS_PER_DAY: MAX_RECORDS_PER_DAY,
    dayKey: dayKey,
    stableMessageKey: stableMessageKey,
    messageRefFromElement: messageRefFromElement,
    upsertDayMap: upsertDayMap,
    prunePastDays: prunePastDays,
    recordsForDay: recordsForDay,
    migrateArrayBufferToDayMap: migrateArrayBufferToDayMap,
  };
})(typeof globalThis !== "undefined" ? globalThis : self);
