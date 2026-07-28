/**
 * Gemini (gemini.google.com) capture. Stable attrs preferred over ordinal.
 */
(function () {
  "use strict";
  const K = globalThis.KaizenLogBrowserAI;
  if (!K) return;

  const SELECTORS = {
    user: "user-query, .user-query, [data-message-author-role='user']",
    assistant: "model-response, .model-response, [data-message-author-role='model']",
  };
  const STABLE_ATTRS = ["data-message-id", "data-id", "id"];
  const CONV_PATTERNS = [/\/app\/([a-zA-Z0-9_-]+)/, /\/u\/\d+\/app\/([a-zA-Z0-9_-]+)/];
  const SITE = "gemini.google.com";

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
      "gemini.user"
    );
    K.observeWithSelectorHealth(
      root,
      SELECTORS.assistant,
      (nodes) => scanRole(nodes, "assistant", saveBody),
      "gemini.assistant"
    );
  });
})();
