import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function source(name) {
  return fs.readFileSync(path.join(root, 'browser-extension', name), 'utf8');
}

function loadContext(files, chrome) {
  const context = {
    chrome, console, URL, setTimeout, clearTimeout,
    location: { href: 'https://chatgpt.com/c/conversation-1' },
  };
  context.self = context;
  vm.createContext(context);
  context.importScripts = (...names) => {
    for (const name of names) {
      const file = name.replace(/^.*[\\/]/, '');
      vm.runInContext(source(file), context, { filename: file });
    }
  };
  for (const file of files) vm.runInContext(source(file), context, { filename: file });
  return context;
}

function createOptionsDocument() {
  const elements = new Map();
  for (const id of ['saveBody', 'status', 'exportNow']) {
    elements.set(id, {
      id,
      checked: false,
      textContent: '',
      listeners: {},
      addEventListener(type, fn) { this.listeners[type] = fn; },
    });
  }
  return {
    getElementById(id) { return elements.get(id); },
    elements,
  };
}

function eventHook() {
  const listeners = [];
  return {
    addListener(fn) { listeners.push(fn); },
    emit(...args) { return Promise.all(listeners.map((fn) => fn(...args))); },
    listeners,
  };
}

function createStorage(initial = {}) {
  const state = { ...initial };
  const gets = [];
  const pendingGets = [];
  let getError = null;
  let deferNextGet = false;
  return {
    state,
    gets,
    setGetError(error) { getError = error; },
    deferGet() { deferNextGet = true; },
    flushGet() { pendingGets.shift()?.(); },
    local: {
      get(defaults, callback) {
        gets.push(defaults);
        const buildResult = () => {
          const result = {};
          for (const [key, fallback] of Object.entries(defaults || {})) {
            result[key] = Object.prototype.hasOwnProperty.call(state, key) ? state[key] : fallback;
          }
          return result;
        };
        if (callback) {
          if (deferNextGet) {
            deferNextGet = false;
            pendingGets.push(() => {
              if (getError) throw getError;
              callback(buildResult());
            });
          } else {
            if (getError) throw getError;
            callback(buildResult());
          }
        } else if (getError) {
          throw getError;
        }
        return Promise.resolve(buildResult());
      },
      set(values, callback) {
        Object.assign(state, values);
        if (callback) callback();
        return Promise.resolve();
      },
    },
  };
}

function record(key, day, text = key) {
  return {
    key,
    ts: `${day}T12:00:00.000Z`,
    site: 'chatgpt.com',
    conversation_id: 'conversation-1',
    role: 'assistant',
    char_count: text.length,
    text,
  };
}

test('content rechecks saveBody for each event and fails closed while preserving metadata', async () => {
  const storage = createStorage({ saveBody: true });
  const sent = [];
  const chrome = {
    storage: storage,
    runtime: { sendMessage(message) { sent.push(message); } },
  };
  const context = loadContext(['lib.js', 'sites/common.js'], chrome);
  const node = {
    innerText: 'old body',
    textContent: '',
    getAttribute(name) { return name === 'data-message-id' ? 'msg-1' : null; },
  };

  context.KaizenLogBrowserAI.emitMessageNode(node, {
    site: 'chatgpt.com',
    role: 'assistant',
    roleIndex: 0,
    convPatterns: [/\/c\/([^/]+)/],
    saveBody: true,
    preferredAttrs: ['data-message-id'],
  });
  storage.deferGet();
  context.KaizenLogBrowserAI.emitMessageNode(node, {
    site: 'chatgpt.com', role: 'assistant', roleIndex: 0,
    convPatterns: [/\/c\/([^/]+)/], saveBody: true, preferredAttrs: ['data-message-id'],
  });
  storage.state.saveBody = false;
  storage.flushGet();
  await Promise.resolve();
  assert.equal(sent.length, 2);
  assert.equal(sent[1].record.text, undefined);
  assert.deepEqual(
    { key: sent[1].record.key, site: sent[1].record.site, role: sent[1].record.role, char_count: sent[1].record.char_count },
    { key: 'chatgpt.com|conversation-1|assistant|msg-1', site: 'chatgpt.com', role: 'assistant', char_count: 8 },
  );

  storage.state.saveBody = true;
  node.innerText = 'new body';
  context.KaizenLogBrowserAI.emitMessageNode(node, {
    site: 'chatgpt.com', role: 'assistant', roleIndex: 0,
    convPatterns: [], saveBody: false, preferredAttrs: ['data-message-id'],
  });
  await Promise.resolve();
  assert.equal(sent[2].record.text, 'new body');

  storage.setGetError(new Error('storage unavailable'));
  node.innerText = 'must be omitted';
  context.KaizenLogBrowserAI.emitMessageNode(node, {
    site: 'chatgpt.com', role: 'assistant', roleIndex: 0,
    convPatterns: [], saveBody: true, preferredAttrs: ['data-message-id'],
  });
  await Promise.resolve();
  assert.equal(sent[3].record.text, undefined);
  assert.equal(sent[3].record.key, 'chatgpt.com|/c/conversation-1|assistant|msg-1');
});

test('stable DOM ids and stream updates are last-record-wins in one day bucket', () => {
  const context = loadContext(['lib.js'], {});
  const first = context.KaizenLogLib.upsertDayMap({}, record('k', '2026-10-15', 'partial'), 5000).map;
  const second = context.KaizenLogLib.upsertDayMap(first, record('k', '2026-10-15', 'complete'), 5000).map;
  assert.deepEqual(Object.keys(second['2026-10-15']), ['k']);
  assert.equal(second['2026-10-15'].k.text, 'complete');
  assert.equal(context.KaizenLogLib.stableMessageKey('chatgpt.com', 'c', 'assistant', 'dom-1'), 'chatgpt.com|c|assistant|dom-1');
  assert.equal(context.KaizenLogLib.stableMessageKey('chatgpt.com', 'c', 'assistant', 'dom-1'), 'chatgpt.com|c|assistant|dom-1');
});

test('background queue serializes concurrent upserts and migrates legacy arrays', async () => {
  const storage = createStorage({
    eventBuffer: [record('legacy', '2026-10-14')],
    saveBody: false,
  });
  const runtimeOnMessage = eventHook();
  const chrome = {
    storage,
    runtime: { onInstalled: eventHook(), onStartup: eventHook(), onMessage: runtimeOnMessage },
    alarms: { create() {}, onAlarm: eventHook() },
    downloads: { download: async () => 1, onChanged: eventHook() },
  };
  loadContext(['background.js'], chrome);
  const messages = [record('a', '2026-10-15'), record('b', '2026-10-15'), record('a', '2026-10-15', 'new')];
  await Promise.all(messages.map((item) => new Promise((resolve) => {
    runtimeOnMessage.listeners[0]({ type: 'kaizenlog_browser_ai_event', record: item }, {}, (response) => {
      assert.equal(response.ok, true);
      resolve();
    });
  })));
  const map = storage.state.eventDayMap;
  assert.deepEqual(Object.keys(map['2026-10-15']).sort(), ['a', 'b']);
  assert.equal(map['2026-10-15'].a.text, undefined);
  assert.equal(map['2026-10-15'].a.char_count, 3);
  assert.equal(map['2026-10-14'].legacy.text, 'legacy');
});

test('background rechecks consent after delayed day-map read before persisting', async () => {
  const state = { saveBody: true, eventDayMap: {}, eventBuffer: null };
  let releaseMapRead;
  const mapRead = new Promise((resolve) => { releaseMapRead = resolve; });
  const storage = {
    state,
    local: {
      get(defaults, callback) {
        if (Object.prototype.hasOwnProperty.call(defaults, 'eventDayMap')) {
          return mapRead.then(() => {
            const result = { eventDayMap: state.eventDayMap, eventBuffer: state.eventBuffer };
            if (callback) callback(result);
            return result;
          });
        }
        const result = { saveBody: state.saveBody };
        if (callback) callback(result);
        return Promise.resolve(result);
      },
      set(values, callback) {
        Object.assign(state, values);
        if (callback) callback();
        return Promise.resolve();
      },
    },
  };
  const runtimeOnMessage = eventHook();
  const chrome = {
    storage,
    runtime: { onInstalled: eventHook(), onStartup: eventHook(), onMessage: runtimeOnMessage },
    alarms: { create() {}, onAlarm: eventHook() },
    downloads: { download: async () => 1, onChanged: eventHook() },
  };
  loadContext(['background.js'], chrome);
  let response;
  const message = new Promise((resolve) => {
    runtimeOnMessage.listeners[0](
      { type: 'kaizenlog_browser_ai_event', record: record('race', '2026-10-15', 'must not persist') },
      {},
      (value) => { response = value; resolve(); },
    );
  });
  await Promise.resolve();
  state.saveBody = false;
  releaseMapRead();
  await message;
  assert.equal(response.ok, true);
  const saved = state.eventDayMap['2026-10-15'].race;
  assert.equal(saved.text, undefined);
  assert.equal(saved.char_count, 'must not persist'.length);
  assert.equal(saved.key, 'race');
});

test('export keeps buffers until every download reports complete, then prunes only past days', async () => {
  const storage = createStorage({
    saveBody: false,
    eventDayMap: {
      '2026-10-14': { old: record('old', '2026-10-14') },
      '2026-10-15': { today: record('today', '2026-10-15') },
    },
  });
  const onChanged = eventHook();
  const runtimeOnMessage = eventHook();
  const downloads = [];
  const chrome = {
    storage,
    runtime: { onInstalled: eventHook(), onStartup: eventHook(), onMessage: runtimeOnMessage },
    alarms: { create() {}, onAlarm: eventHook() },
    downloads: {
      onChanged,
      async download(details) { downloads.push(details); return downloads.length; },
    },
  };
  loadContext(['background.js'], chrome);
  let response;
  const pending = new Promise((resolve) => {
    runtimeOnMessage.listeners[0]({ type: 'kaizenlog_export_now' }, {}, (value) => { response = value; resolve(); });
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(downloads.length, 2);
  await pending;
  assert.equal(response.status, 'pending');
  assert.deepEqual(Object.keys(storage.state.eventDayMap).sort(), ['2026-10-14', '2026-10-15']);
  await onChanged.emit({ id: 1, state: { current: 'in_progress' } });
  assert.deepEqual(Object.keys(storage.state.eventDayMap).sort(), ['2026-10-14', '2026-10-15']);
  await onChanged.emit({ id: 1, state: { current: 'complete' } });
  assert.deepEqual(Object.keys(storage.state.eventDayMap).sort(), ['2026-10-14', '2026-10-15']);
  await onChanged.emit({ id: 2, state: { current: 'complete' } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(Object.keys(storage.state.eventDayMap), ['2026-10-15']);
  assert.ok(storage.state.eventDayMap['2026-10-15'].today);
});

test('interrupted download leaves all buffers retryable', async () => {
  const storage = createStorage({
    saveBody: false,
    eventDayMap: { '2026-10-14': { old: record('old', '2026-10-14') } },
  });
  const onChanged = eventHook();
  const runtimeOnMessage = eventHook();
  const chrome = {
    storage,
    runtime: { onInstalled: eventHook(), onStartup: eventHook(), onMessage: runtimeOnMessage },
    alarms: { create() {}, onAlarm: eventHook() },
    downloads: { onChanged, async download() { return 7; } },
  };
  loadContext(['background.js'], chrome);
  runtimeOnMessage.listeners[0]({ type: 'kaizenlog_export_now' }, {}, () => {});
  await new Promise((resolve) => setImmediate(resolve));
  await onChanged.emit({ id: 7, state: { current: 'interrupted' } });
  assert.ok(storage.state.eventDayMap['2026-10-14'].old);
});

test('unknown or missing download state never prunes the retryable buffer', async () => {
  const storage = createStorage({
    saveBody: false,
    eventDayMap: { '2026-10-14': { old: record('old', '2026-10-14') } },
  });
  const onChanged = eventHook();
  const runtimeOnMessage = eventHook();
  const chrome = {
    storage,
    runtime: { onInstalled: eventHook(), onStartup: eventHook(), onMessage: runtimeOnMessage },
    alarms: { create() {}, onAlarm: eventHook() },
    downloads: { onChanged, async download() { return 8; } },
  };
  loadContext(['background.js'], chrome);
  runtimeOnMessage.listeners[0]({ type: 'kaizenlog_export_now' }, {}, () => {});
  await new Promise((resolve) => setImmediate(resolve));
  await onChanged.emit({ id: 999, state: { current: 'complete' } });
  await onChanged.emit({ id: 8 });
  assert.ok(storage.state.eventDayMap['2026-10-14'].old);
  await onChanged.emit({ id: 8, state: { current: 'in_progress' } });
  assert.ok(storage.state.eventDayMap['2026-10-14'].old);
});

test('export response and options status distinguish pending, failure, and complete', async () => {
  const storage = createStorage({
    saveBody: false,
    eventDayMap: { '2026-10-14': { old: record('old', '2026-10-14') } },
  });
  const onChanged = eventHook();
  const runtimeOnMessage = eventHook();
  const document = createOptionsDocument();
  const statusMessages = [];
  let downloadMode = 'pending';
  let nextDownloadId = 30;
  const chrome = {
    storage,
    runtime: {
      onInstalled: eventHook(),
      onStartup: eventHook(),
      onMessage: runtimeOnMessage,
      sendMessage(message, callback) {
        if (message.type === 'kaizenlog_export_now') {
          const backgroundListener = runtimeOnMessage.listeners.find((fn) => fn._background);
          backgroundListener(message, {}, callback);
          return;
        }
        if (message.type === 'kaizenlog_export_status') {
          statusMessages.push(message);
          for (const listener of runtimeOnMessage.listeners.filter((fn) => !fn._background)) {
            listener(message, {}, () => {});
          }
        }
      },
    },
    alarms: { create() {}, onAlarm: eventHook() },
    downloads: {
      onChanged,
      async download() {
        if (downloadMode === 'reject') throw new Error('download rejected');
        return nextDownloadId++;
      },
    },
  };
  const optionsContext = loadContext([], chrome);
  optionsContext.document = document;
  vm.runInContext(source('options.js'), optionsContext, { filename: 'options.js' });
  loadContext(['background.js'], chrome);
  const serviceWorkerListener = runtimeOnMessage.listeners.at(-1);
  serviceWorkerListener._background = true;

  document.elements.get('exportNow').listeners.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(statusMessages.length, 0);
  assert.match(document.elements.get('status').textContent, /中/);

  downloadMode = 'reject';
  storage.state.eventDayMap = { '2026-10-14': { old: record('old', '2026-10-14') } };
  document.elements.get('exportNow').listeners.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(document.elements.get('status').textContent, /失敗/);

  downloadMode = 'pending';
  storage.state.eventDayMap = { '2026-10-14': { old: record('old', '2026-10-14') } };
  document.elements.get('exportNow').listeners.click();
  await new Promise((resolve) => setImmediate(resolve));
  const acceptedId = nextDownloadId - 1;
  await onChanged.emit({ id: acceptedId, state: { current: 'complete' } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(document.elements.get('status').textContent, /完了/);
});
