/**
 * Shared helpers for KaizenLog browser AI capture.
 * Depends on lib.js (KaizenLogLib) loaded first.
 *
 * E2: Streaming does not create new rows — stable keys overwrite the same
 * record as char_count/text grow. No separate "response complete" event.
 */
(function (global) {
  "use strict";

  const Lib = global.KaizenLogLib;

  /**
   * Build a telemetry record. text may be omitted when saveBody is false.
   * @param {object} p
   * @returns {object}
   */
  function buildRecord(p) {
    const site = p.site;
    const conversationId = p.conversationId || "unknown";
    const role = p.role;
    const messageRef = p.messageRef != null ? p.messageRef : "ord:0";
    const key =
      p.key ||
      (Lib
        ? Lib.stableMessageKey(site, conversationId, role, messageRef)
        : [site, conversationId, role, messageRef].join("|"));
    const rec = {
      key: key,
      ts: p.ts || new Date().toISOString(),
      site: site,
      conversation_id: conversationId,
      role: role,
      char_count:
        typeof p.charCount === "number" ? p.charCount : (p.text || "").length,
    };
    if (p.saveBody !== false && p.text != null && String(p.text).length) {
      rec.text = String(p.text);
    }
    return rec;
  }

  function conversationIdFromUrl(url, patterns) {
    try {
      const u = new URL(url || location.href);
      for (const re of patterns || []) {
        const m = u.pathname.match(re);
        if (m && m[1]) return m[1];
      }
      return u.pathname || "unknown";
    } catch (_e) {
      return "unknown";
    }
  }

  function enqueue(record) {
    try {
      chrome.runtime.sendMessage(
        { type: "kaizenlog_browser_ai_event", record: record },
        () => {
          void chrome.runtime.lastError;
        }
      );
    } catch (_e) {
      /* never throw into page */
    }
  }

  /**
   * Observe DOM. On zero matches for warnAfterMs, console.warn once.
   * Selectors are brittle — never throw into the host page.
   */
  function observeWithSelectorHealth(root, selector, onNodes, warnLabel) {
    let matchedOnce = false;
    let warned = false;
    const scan = () => {
      try {
        const nodes = root.querySelectorAll(selector);
        if (nodes && nodes.length) {
          matchedOnce = true;
          onNodes(nodes);
        }
      } catch (_e) {
        /* swallow */
      }
    };
    scan();
    const mo = new MutationObserver(() => scan());
    try {
      mo.observe(root, { childList: true, subtree: true, characterData: true });
    } catch (_e) {
      return () => {};
    }
    setTimeout(() => {
      if (!matchedOnce && !warned) {
        warned = true;
        console.warn(
          "[KaizenLog] selector matched 0 nodes for",
          warnLabel || selector,
          "— site DOM may have changed; capture paused for this selector"
        );
      }
    }, 15000);
    return () => mo.disconnect();
  }

  /**
   * Emit/update one message node by stable key (ordinal or data-id).
   * Same key + longer text overwrites — streaming collapses to final.
   */
  function emitMessageNode(el, opts) {
    const role = opts.role;
    const roleIndex = opts.roleIndex;
    const site = opts.site;
    const convPatterns = opts.convPatterns;
    const saveBody = opts.saveBody;
    const preferredAttrs = opts.preferredAttrs || [];
    const text = (el.innerText || el.textContent || "").trim();
    if (!text) return;
    const cid = conversationIdFromUrl(location.href, convPatterns);
    const messageRef = Lib
      ? Lib.messageRefFromElement(el, roleIndex, preferredAttrs)
      : "ord:" + roleIndex;
    enqueue(
      buildRecord({
        site: site,
        conversationId: cid,
        role: role,
        messageRef: messageRef,
        text: text,
        charCount: text.length,
        saveBody: saveBody,
      })
    );
  }

  global.KaizenLogBrowserAI = {
    buildRecord: buildRecord,
    conversationIdFromUrl: conversationIdFromUrl,
    enqueue: enqueue,
    observeWithSelectorHealth: observeWithSelectorHealth,
    emitMessageNode: emitMessageNode,
  };
})(typeof globalThis !== "undefined" ? globalThis : window);
