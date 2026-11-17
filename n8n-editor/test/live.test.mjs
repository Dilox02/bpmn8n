import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ApiClient, LiveUpdates, localURL, previewURL, sessionURL, decodeExecution, executionState } from '../live.mjs';

const frame = (name, runIndex, inputs) => ({ node: { name }, runIndex, data: { main: inputs.map(port => port.map(json => ({ json }))) } });
const execution = (stack, waiting = {}) => ({ status: 'running', data: { resultData: { runData: {} }, executionData: { nodeExecutionStack: stack, waitingExecution: waiting } } });
const call = (id, node_name, run_index = 0, item_index = 0) => ({ id, node_name, invocation: { run_index, item_index } });

test('parallel and inclusive splits keep every branch and input payload separate', () => {
  const e = execution([frame('A', 0, [[{ branch: 'left' }]]), frame('B', 0, [[{ branch: 'right' }]]), frame('C', 0, [[{ branch: 'queued' }]])]);
  const result = executionState(e, { active: [call(1, 'A'), call(2, 'B')], recent: [] });
  assert.equal(result.tokens.length, 3);
  assert.deepEqual(result.tokens.map(t => [t.node, t.state]), [['A', 'active'], ['B', 'active'], ['C', 'ready']]);
  assert.equal(result.tokens[0].input.branch, 'left');
  assert.equal(result.tokens[1].input.branch, 'right');
});

test('simultaneous invocations of the same node match run and item, never name alone', () => {
  const e = execution([frame('Task', 1, [[{ item: 'one' }]]), frame('Task', 2, [[{ item: 'two' }, { item: 'three' }]])]);
  const result = executionState(e, { active: [call(10, 'Task', 1), call(11, 'Task', 2, 1)], recent: [] });
  assert.equal(result.tokens.length, 3);
  assert.equal(result.states.Task.count, 3);
  assert.deepEqual(result.tokens.filter(t => t.state === 'active').map(t => [t.input.item, t.callId]), [['one', 10], ['three', 11]]);
});

test('join buffers expose each input port, stay waiting and disappear at termination', () => {
  const e = execution([], { Join: { 0: { main: [[{ json: { from: 'A' } }], [{ json: { from: 'B' } }]] } } });
  const result = executionState(e, { active: [], recent: [] });
  assert.deepEqual(result.tokens.map(t => [t.port, t.state]), [[0, 'waiting'], [1, 'waiting']]);
  assert.equal(result.active.length, 0);
  e.status = 'error';
  assert.equal(executionState(e, { active: [call(1, 'stale')], recent: [] }).tokens.length, 0);
});

test('per-node badges distinguish active, buffered and unconfirmed items', () => {
  const e = execution([frame('Task', 0, [[{ n: 0 }, { n: 1 }, { n: 2 }]])]);
  const result = executionState(e, { active: [call(11, 'Task', 0, 1)], recent: [{ ...call(10, 'Task', 0, 0), status: 'complete' }] });
  assert.deepEqual(result.tokens.map(t => t.state), ['buffered', 'active', 'ready']);
  assert.deepEqual(result.states.Task.counts, { buffered: 1, active: 1, ready: 1 });
  assert.equal(result.states.Task.count, 3);
});

test('flatted execution decoding preserves shared/circular references without expanding indefinitely', () => {
  const data = decodeExecution('[{"a":"1","self":"0"},"literal"]');
  assert.equal(data.a, 'literal'); assert.equal(data.self, data);
  assert.throws(() => decodeExecution('[{"a":"99"}]'), /reference/);
});

test('conditional reads reuse data for 304 and retain a bounded cache', async () => {
  const calls = [];
  const api = new ApiClient('http://backend', async (url, options) => {
    calls.push([url, options]);
    if (calls.length === 2) return { status: 304 };
    return { status: 200, ok: true, headers: { get: () => '"v1"' }, json: async () => ({ value: 1 }) };
  });
  assert.equal((await api.get('/runs')).changed, true);
  assert.equal((await api.get('/runs')).changed, false);
  assert.equal(calls[1][1].headers['If-None-Match'], '"v1"');
  for (let i = 0; i < 20; i++) await api.get(`/${i}`);
  assert.equal(api.cache.size, 12); api.destroy();
});

test('UI-bound sessions retain the native browser identifier for REST, SSE and exports', async () => {
  const previousStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const oldLocation = globalThis.location;
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: key => key === 'n8n-browserId' ? 'ui-browser-123' : null } });
  globalThis.location = { href: 'http://localhost:5678/workflow/one/executions/2' };
  try {
    const api = new ApiClient('/rest', async (url, options) => {
      assert.equal(url, '/rest/executions/2');
      assert.equal(options.headers['browser-id'], 'ui-browser-123');
      assert.equal(options.credentials, 'include');
      return { ok: true, status: 200, headers: { get: () => null }, json: async () => ({}) };
    });
    await api.get('/executions/2'); api.destroy();
    const url = new URL(sessionURL('http://localhost:8000/api/evaluations/one/stream?existing=1'));
    assert.equal(url.searchParams.get('browserId'), 'ui-browser-123');
    assert.equal(url.searchParams.get('existing'), '1');
    assert.equal(new URL(sessionURL('/export')).pathname, '/export');
  } finally {
    if (previousStorage) Object.defineProperty(globalThis, 'localStorage', previousStorage);
    else delete globalThis.localStorage;
    globalThis.location = oldLocation;
  }
});

test('SSE pushes trigger changes, disconnect while hidden, and clean up on unmount', () => {
  const listeners = {}, sources = [];
  const oldDocument = globalThis.document, oldEventSource = globalThis.EventSource;
  globalThis.document = { hidden: false, addEventListener: (key, fn) => listeners[key] = fn, removeEventListener: key => delete listeners[key] };
  globalThis.EventSource = class { constructor(url, options) { this.url = url; this.options = options; this.listeners = {}; sources.push(this); } addEventListener(key, fn) { this.listeners[key] = fn; } close() { this.closed = true; } };
  try {
    let changed = 0;
    const live = new LiveUpdates('/stream', () => changed++);
    sources[0].listeners.change(); assert.equal(changed, 1);
    document.hidden = true; listeners.visibilitychange(); assert.equal(sources[0].closed, true);
    document.hidden = false; listeners.visibilitychange(); assert.equal(sources.length, 2);
    live.destroy(); assert.equal(sources[1].closed, true); assert.equal(listeners.visibilitychange, undefined);
  } finally { globalThis.document = oldDocument; globalThis.EventSource = oldEventSource; }
});

test('local backend addresses preserve the editor hostname and cookie scope', () => {
  assert.equal(localURL('http://localhost:8000', 'http://127.0.0.1:5678'), 'http://127.0.0.1:8000');
  assert.equal(localURL('https://backend.example', 'http://127.0.0.1:5678'), 'https://backend.example');
});

test('credentialless preview uses the n8n-supported local HTTP hostname', () => {
  assert.equal(previewURL('http://localhost:5679'), 'http://localhost:5679');
  assert.equal(previewURL('http://127.0.0.1:5679'), 'http://localhost:5679');
  assert.equal(previewURL('https://preview.example.com'), 'https://preview.example.com');
});
