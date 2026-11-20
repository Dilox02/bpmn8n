import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const Strip = createRequire(import.meta.url)('../strip.js');

test('the links follow bpmn8n\'s public URL, with or without a path prefix', () => {
  assert.deepEqual(Strip.links('http://localhost:8000').map(l => l.href), [
    'http://localhost:8000/inbox/', 'http://localhost:8000/cases/', 'http://localhost:8000/insights/',
    'http://localhost:8000/simulation/', 'http://localhost:8000/variables/', 'http://localhost:8000/docs/']);
  assert.equal(Strip.links('https://x.org/bpmn8n/')[0].href, 'https://x.org/bpmn8n/inbox/');
  assert.deepEqual(Strip.links('http://a').map(l => l.label), ['Inbox', 'Cases', 'Insights', 'Simulation', 'Variables', 'Docs']);
});

test('the workflow is read from the editor\'s URL under any base path', () => {
  assert.equal(Strip.workflowId('/workflow/abc123'), 'abc123');
  assert.equal(Strip.workflowId('/n8n/workflow/abc123/executions/9'), 'abc123');
  assert.equal(Strip.workflowId('/home/workflows'), null);
});

test('the waiting count is read for a saved workflow only', () => {
  assert.equal(Strip.waitingUrl('https://x.org/b/', 'a b'), 'https://x.org/b/api/executions/a%20b/waiting');
  assert.equal(Strip.waitingUrl('http://x', null), null);
  assert.equal(Strip.waitingUrl('http://x', 'new'), null);
});

test('the badge says nothing for none and caps at 99+', () => {
  assert.deepEqual([0, undefined, 3, 99, 100].map(Strip.badge), ['', '', '3', '99', '99+']);
});

test('localhost and 127.0.0.1 are the same machine, other hosts are left alone', () => {
  assert.equal(Strip.sameHost('http://localhost:8000', 'http://127.0.0.1:5678/workflow/1'), 'http://127.0.0.1:8000');
  assert.equal(Strip.sameHost('https://b.org/x', 'http://127.0.0.1:5678/'), 'https://b.org/x');
});

test('n8n\'s unused entries are hidden by its own ids, and the strip waits for <body>', async () => {
  for (const id of ['#settings-sso', '#settings-log-streaming', '#settings-source-control']) assert.ok(Strip.HIDDEN.includes(id), id);
  assert.ok(Strip.HIDDEN.some(s => s.includes('main-sidebar-help')));
  const source = (await import('node:fs')).readFileSync(new URL('../strip.js', import.meta.url), 'utf8');
  assert.match(source, /if \(document\.body\) document\.body\.appendChild\(strip\);\s*else addEventListener\('DOMContentLoaded'/);
  // inside bpmn8n (an iframe) the entries stay hidden; only the strip itself is left out
  assert.ok(source.indexOf("hide.textContent") < source.indexOf("if (window.top !== window) {"));
});

test('inside bpmn8n n8n shows no side bar and no Executions tab, and its canvas takes the preview\'s dark look', async () => {
  assert.ok(Strip.FRAMED_HIDDEN.includes('#sidebar'));
  assert.ok(Strip.FRAMED_HIDDEN.some(s => s.includes('radio-button-executions')));
  const source = (await import('node:fs')).readFileSync(new URL('../strip.js', import.meta.url), 'utf8');
  assert.match(source, /document\.body\.dataset\.theme = 'dark'/);
  assert.doesNotMatch(source, /localStorage\.setItem\('N8N_THEME'/);  // the person's own n8n keeps its theme
});
