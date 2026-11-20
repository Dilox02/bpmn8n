import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import components from '../../bpmn8n/static/bpmn8n/components.js';

const { tone, timelineItems, timelineHTML, related, CSS } = components;

test('a pill takes one of the five tones, anything else is neutral', () => {
  assert.deepEqual(['positive', 'negative', 'warning', 'accent', 'neutral', 'purple', undefined].map(tone),
    ['positive', 'negative', 'warning', 'accent', 'neutral', 'neutral', 'neutral']);
});

test('a timeline reads its JSON attribute and drops what is not an entry', () => {
  const items = timelineItems('[{"label": "Register", "meta": "automatic", "at": "10:00", "status": "error"}, {"meta": "no label"}, 3, {"label": 7, "status": "purple"}]');
  assert.deepEqual(items, [{ label: 'Register', meta: 'automatic', at: '10:00', status: 'error' }, { label: '7', meta: '', at: '', status: 'complete' }]);
  assert.deepEqual(timelineItems('not json'), []);
  assert.deepEqual(timelineItems('{"label": "an object, not a list"}'), []);
});

test('a timeline escapes what it shows', () => {
  const html = timelineHTML(timelineItems([{ label: '<img src=x onerror=alert(1)>', meta: 'a "quote"', status: 'running' }]));
  assert.ok(html.startsWith('<ol><li data-status="running">'));
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;') && html.includes('a &quot;quote&quot;'));
  assert.ok(!html.includes('<img') && !html.includes('<time>'));  // no time given, none drawn
});

test('pointing at a workflow lights it, the edges that touch it and the workflows at their ends', () => {
  const edges = [{ source: 'Outside', target: 'Claim' }, { source: 'Claim', target: 'Payout' }, { source: 'Intake', target: 'Triage' }];
  const lit = related(edges, 'Claim');
  assert.deepEqual([...lit.nodes].sort(), ['Claim', 'Outside', 'Payout']);
  assert.deepEqual([...lit.edges], [0, 1]);
  assert.deepEqual([...related(edges, 'Alone').nodes], ['Alone']);
  assert.equal(related(edges, null).nodes.size, 0);
});

test('the components use only the tokens tokens.css defines, and no colour of their own', () => {
  const tokens = readFileSync(new URL('../../bpmn8n/static/bpmn8n/tokens.css', import.meta.url), 'utf8');
  const defined = new Set([...tokens.matchAll(/(--b8-[\w-]+):/g)].map(m => m[1]));
  const used = [...CSS.matchAll(/var\((--[\w-]+)/g)].map(m => m[1]);
  assert.ok(used.length > 10);
  assert.deepEqual(used.filter(name => !defined.has(name)), []);
  assert.equal(CSS.match(/#[0-9a-f]{3,8}\b/gi), null);
});
