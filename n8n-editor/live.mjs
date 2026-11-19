// Shared transport and keyed DOM updates for the editor's local live views.
export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const numericFormat = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 4 });
const percentFormat = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 2 });
export const number = value => value == null ? '—' : numericFormat.format(value);
export const measurement = (value, unit = '') => unit === 'ratio' ? `${percentFormat.format(value * 100)} %` : `${number(value)}${unit ? ' ' + unit : ''}`;
export const date = value => value ? new Date(value).toLocaleString('en-GB') : '—';
export const metricKey = m => JSON.stringify([m.node_name, m.name, m.unit, m.direction, m.threshold]);
export const runKey = r => `${r.project}/${r.execution_id}`;
export const text = (el, value) => { const next = String(value ?? ''); if (el.textContent !== next) el.textContent = next; };
export function html(el, value) { if (el._html !== value) { el.innerHTML = value; el._html = value; } }
export function keyed(parent, items, keyOf, create, update) {
  const previous = new Map([...parent.children].map(el => [el.dataset.key, el]));
  let cursor = parent.firstElementChild;
  for (const item of items) {
    const key = String(keyOf(item));
    const el = previous.get(key) || create(item);
    el.dataset.key = key;
    previous.delete(key);
    update(el, item);
    if (el !== cursor) parent.insertBefore(el, cursor);
    cursor = el.nextElementSibling;
  }
  for (const el of previous.values()) el.remove();
}
export function localURL(configured, locationURL = globalThis.location.href) {
  const url = new URL(configured), location = new URL(locationURL);
  const loopback = new Set(['localhost', '127.0.0.1']);
  if (loopback.has(url.hostname) && loopback.has(location.hostname)) url.hostname = location.hostname;
  return url.href.replace(/\/$/, '');
}
/** n8n's base path: '/', or N8N_PATH behind the one-origin gateway ('/n8n/'); else from the hook's own URL (<base>static/…). */
export const editorBase = (scriptURL, basePath = globalThis.BASE_PATH) => basePath || new URL('../', scriptURL).pathname;
export function previewURL(configured) {
  const url = new URL(configured);
  // n8n permits local HTTP previews on localhost; the iframe isolates cookies with credentialless.
  if (['localhost', '127.0.0.1'].includes(url.hostname)) url.hostname = 'localhost';
  return url.href.replace(/\/$/, '');
}
// n8n binds UI sessions to this identifier; omitting it invalidates the auth cookie.
export const browserId = () => globalThis.localStorage?.getItem('n8n-browserId') || '';
export function sessionURL(value) {
  const id = browserId();
  if (!id) return value;
  const url = new URL(value, globalThis.location.href);
  url.searchParams.set('browserId', id);
  return url.href;
}
export class ApiClient {
  constructor(base, fetcher = globalThis.fetch.bind(globalThis)) {
    this.base = base; this.fetcher = fetcher; this.cache = new Map(); this.abort = new AbortController();
  }
  async get(path) {
    const cached = this.cache.get(path);
    const headers = cached?.etag ? { 'If-None-Match': cached.etag } : {};
    const id = browserId();
    if (id) headers['browser-id'] = id;
    const response = await this.fetcher(this.base + path, { credentials: 'include', signal: this.abort.signal,
      headers });
    if (response.status === 304 && cached) return { data: cached.data, changed: false };
    if (!response.ok) throw Error(response.status === 401 || response.status === 403
      ? 'Session expired or access denied: sign in again and refresh.' : `Could not read the data (HTTP ${response.status}): try Refresh.`);
    const data = await response.json();
    this.cache.delete(path);
    this.cache.set(path, { etag: response.headers.get('ETag'), data });
    if (this.cache.size > 12) this.cache.delete(this.cache.keys().next().value);
    return { data, changed: true };
  }
  destroy() { this.abort.abort(); this.cache.clear(); }
}
export class LiveUpdates {
  constructor(url, change, state = () => {}) {
    this.url = url; this.change = change; this.state = state;
    this.visible = () => document.hidden ? this.disconnect() : this.connect();
    document.addEventListener('visibilitychange', this.visible);
    this.visible();
  }
  connect() {
    if (this.events || this.closed) return;
    this.events = new EventSource(sessionURL(this.url), { withCredentials: true });
    this.events.onopen = () => this.state('Live updates connected');
    this.events.addEventListener('change', () => this.change());
    this.events.onerror = () => this.state('Reconnecting to live updates…');
  }
  disconnect() { this.events?.close(); this.events = null; }
  destroy() { this.closed = true; this.disconnect(); document.removeEventListener('visibilitychange', this.visible); }
}
export function viewComponent(name, factory, props, watchProps = props) {
  const mount = function () {
    this.localView?.destroy();
    this.localView = factory(this);
    this.$el.parentNode.insertBefore(this.localView.root, this.$el);
  };
  return { name, props, render() { return null; }, mounted: mount,
    beforeUnmount() { this.localView?.destroy(); },
    watch: Object.fromEntries(watchProps.map(prop => [prop, mount])) };
}
export function installRoute(name, outlet, component, nativeQuery = false) {
  let attempts = 0;
  const timer = setInterval(() => {
    const router = document.querySelector('#app')?.__vue_app__?.config.globalProperties.$router;
    const route = router?.getRoutes().find(r => r.name === name);
    if (!route?.components?.[outlet]) { if (++attempts > 600) clearInterval(timer); return; }
    clearInterval(timer);
    const original = route.components[outlet];
    if (nativeQuery) router.beforeEach(to => { if (to.name === name) route.components[outlet] = to.query.native === '1' ? original : component; });
    route.components[outlet] = nativeQuery && router.currentRoute.value.query.native === '1' ? original : component;
    if (router.currentRoute.value.matched.some(r => r.name === name)) router.replace({ path: router.currentRoute.value.fullPath, force: true });
  }, 50);
}
export function decodeExecution(value) {
  if (typeof value !== 'string') return value || {};
  const flat = JSON.parse(value), memo = new Map();
  if (!Array.isArray(flat)) return flat;
  function visit(index) {
    if (!Number.isInteger(index) || index < 0 || index >= flat.length) throw Error('Invalid execution reference');
    if (memo.has(index)) return memo.get(index);
    const item = flat[index];
    if (item === null || typeof item !== 'object') return item;
    const result = Array.isArray(item) ? [] : Object.create(null);
    memo.set(index, result);
    for (const [key, entry] of Object.entries(item)) result[key] = typeof entry === 'string' ? visit(Number(entry)) : entry;
    return result;
  }
  return visit(0);
}

// Each stack entry/input port/item is a separate token. Join queues never become active calls.
export function executionState(execution, activity = { active: [], recent: [] }) {
  const data = execution.data || {}, runtime = data.executionData || {};
  const terminal = !['new', 'running', 'waiting', 'unknown'].includes(execution.status);
  const states = {}, tokens = [];
  for (const [name, runs] of Object.entries(data.resultData?.runData || {})) {
    const last = runs.at(-1);
    states[name] = { state: last?.error ? 'error' : 'complete', count: 0, runs: runs.length };
  }
  function inputs(node, connections, prefix, state, runIndex, source) {
    for (const [kind, ports] of Object.entries(connections || {})) for (const [port, items] of (ports || []).entries()) {
      for (const [itemIndex, item] of (items || []).entries()) {
        if (!item || typeof item !== 'object' || !('json' in item)) continue;
        tokens.push({ id: `${prefix}/${kind}/${port}/${itemIndex}`, node, kind, port, itemIndex, state, runIndex,
          source, input: item.json, pairedItem: item.pairedItem, binary: Object.keys(item.binary || {}) });
      }
    }
  }
  if (!terminal) {
    for (const [i, frame] of (runtime.nodeExecutionStack || []).entries()) {
      const name = frame.node?.name || '?';
      const run = frame.runIndex ?? data.resultData?.runData?.[name]?.length ?? 0;
      inputs(name, frame.data, `stack/${i}`, 'ready', run, frame.source);
    }
    for (const [node, iterations] of Object.entries(runtime.waitingExecution || {})) {
      for (const [runIndex, connections] of Object.entries(iterations || {})) {
        inputs(node, connections, `join/${node}/${runIndex}`, 'waiting', Number(runIndex), runtime.waitingExecutionSource?.[node]?.[runIndex]);
      }
    }
    const traceKey = (node, run, item) => JSON.stringify([node, run, item]);
    const candidates = new Map();
    for (const token of tokens) {
      if (token.state !== 'ready' || token.kind !== 'main' || token.port !== 0) continue;
      const key = traceKey(token.node, token.runIndex, token.itemIndex);
      if (!candidates.has(key)) candidates.set(key, []);
      candidates.get(key).push(token);
    }
    for (const call of activity.recent || []) {
      if (call.status !== 'complete') continue;
      const trace = call.invocation;
      if (trace?.run_index == null || trace?.item_index == null) continue;
      for (const token of candidates.get(traceKey(call.node_name, trace.run_index, trace.item_index)) || []) token.state = 'buffered';
    }
    const matched = new Set();
    for (const call of activity.active) {
      const trace = call.invocation;
      const token = trace?.run_index != null && trace?.item_index != null
        ? candidates.get(traceKey(call.node_name, trace.run_index, trace.item_index))?.find(t => !matched.has(t.id)) : null;
      if (token) { token.state = 'active'; token.callId = call.id; matched.add(token.id); }
      else tokens.push({ id: `call/${call.id}`, node: call.node_name, state: 'active', callId: call.id,
        runIndex: trace?.run_index, itemIndex: trace?.item_index });
    }
    for (const token of tokens) {
      const previous = states[token.node];
      const priority = { active: 3, waiting: 2, buffered: 2, ready: 1, complete: 0, error: 0 };
      states[token.node] = { state: priority[token.state] > (priority[previous?.state] ?? -1) ? token.state : previous.state,
        count: (previous?.count || 0) + 1, runs: previous?.runs || 0,
        counts: { ...previous?.counts, [token.state]: (previous?.counts?.[token.state] || 0) + 1 } };
    }
  }
  return { states, tokens, active: terminal ? [] : activity.active, recent: activity.recent || [], terminal };
}
