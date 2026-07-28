/**
 * ChatGPT (chatgpt.com) capture.
 * Selectors centralized. Stable message id attrs preferred over ordinal.
 */
(function () {
  "use strict";
  const K = globalThis.KaizenLogBrowserAI;
  if (!K) return;

  // --- selectors (brittle; update when ChatGPT UI changes) ---
  const SELECTORS = {
    user: '[data-message-author-role="user"]',
    assistant: '[data-message-author-role="assistant"]',
  };
  // Prefer stable DOM ids when present (ChatGPT often sets data-message-id)
  const STABLE_ATTRS = ["data-message-id", "data-id", "id"];
  const CONV_PATTERNS = [/\/c\/([a-zA-Z0-9-]+)/, /\/g\/[^/]+\/c\/([a-zA-Z0-9-]+)/];
  const SITE = "chatgpt.com";

  function loadOpts(cb) {
    try {
      chrome.storage.local.get({ saveBody: true }, (o) => cb(!!o.saveBody));
    } catch (_e) {
      cb(true);
    }
  }

  function scanRole(nodes, role, saveBody) {
    nodes.forEach((n, i) => {
      K.emitMessageNode(n, {
        role: role,
        roleIndex: i,
        site: SITE,
        convPatterns: CONV_PATTERNS,
        saveBody: saveBody,
        preferredAttrs: STABLE_ATTRS,
      });
    });
  }

  loadOpts((saveBody) => {
    const root = document.documentElement;
    K.observeWithSelectorHealth(
      root,
      SELECTORS.user,
      (nodes) => scanRole(nodes, "user", saveBody),
      "chatgpt.user"
    );
    K.observeWithSelectorHealth(
      root,
      SELECTORS.assistant,
      (nodes) => scanRole(nodes, "assistant", saveBody),
      "chatgpt.assistant"
    );
  });
})();
