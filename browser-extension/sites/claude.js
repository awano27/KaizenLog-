/**
 * Claude.ai capture. Stable attrs preferred over ordinal.
 */
(function () {
  "use strict";
  const K = globalThis.KaizenLogBrowserAI;
  if (!K) return;

  const SELECTORS = {
    user: '[data-testid="user-message"], .font-user-message',
    assistant: '[data-testid="assistant-message"], .font-claude-message',
  };
  // data-testid は値がセレクタ定数("user-message"等)で全メッセージ共通のため
  // キーに使うと1件に衝突する。メッセージ固有IDになり得る属性のみ列挙する。
  const STABLE_ATTRS = ["data-id", "id"];
  const CONV_PATTERNS = [/\/chat\/([a-zA-Z0-9-]+)/];
  const SITE = "claude.ai";

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
      "claude.user"
    );
    K.observeWithSelectorHealth(
      root,
      SELECTORS.assistant,
      (nodes) => scanRole(nodes, "assistant", saveBody),
      "claude.assistant"
    );
  });
})();
