import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const Heat = createRequire(import.meta.url)('../insights.js');
const nodes = {
  Register: { frequency: 400, cases: 400, mean: 0.3, median: 0.25, errors: 0 },
  'Assess Damage': { frequency: 187, cases: 138, mean: 4380, median: 2940, errors: 0 },
  Pay: { frequency: 330, cases: 330, mean: 1.2, median: 1.1, errors: 14 },
};

test('heat is a node against the hottest one, clamped to 0–1', () => {
  assert.equal(Heat.scale(5, 10), 0.5);
  assert.equal(Heat.scale(20, 10), 1);
  assert.equal(Heat.scale(5, 0), 0);
  assert.equal(Heat.scale(null, 10), 0);
  assert.equal(Heat.heat(nodes, 'Register', 'frequency'), 1);
  assert.equal(Heat.heat(nodes, 'Pay', 'frequency'), 330 / 400);
  assert.equal(Heat.heat(nodes, 'Assess Damage', 'duration'), 1);
  assert.equal(Heat.heat(nodes, 'Missing', 'duration'), 0);
});

test('colours run cold to hot in one hue per measure', () => {
  assert.equal(Heat.colour(0, 'frequency'), 'hsl(212 25% 70%)');
  assert.equal(Heat.colour(1, 'frequency'), 'hsl(212 90% 48%)');
  assert.equal(Heat.colour(1, 'duration'), 'hsl(8 90% 48%)');
  assert.equal(Heat.colour(7, 'duration'), Heat.colour(1, 'duration'));  // clamped
});

test('badges and legends say counts and durations briefly', () => {
  assert.deepEqual([0, 950, 1234, 12_500, 1_300_000].map(Heat.count), ['0', '950', '1.2k', '13k', '1.3M']);
  assert.deepEqual([0.35, 4.2, 42, 300, 900, 12_240, 190_000].map(Heat.duration),
    ['350 ms', '4.2 s', '42 s', '5.0 min', '15 min', '3.4 h', '2.2 d']);
  assert.equal(Heat.duration(null), '—');
  assert.equal(Heat.badge(nodes.Pay, 'frequency'), '330');
  assert.equal(Heat.badge(nodes['Assess Damage'], 'duration'), '1.2 h');
  assert.deepEqual(Heat.legend(nodes, 'frequency'), { low: '187', high: '400' });
  assert.deepEqual(Heat.legend(nodes, 'duration'), { low: '300 ms', high: '1.2 h' });
  assert.equal(Heat.legend({}, 'duration'), null);
  assert.match(Heat.describe('Pay', nodes.Pay), /ran 330 times in 330 cases · mean 1\.2 s, median 1\.1 s · 14 errors/);
});

test('the workflow comes from the editor URL, and bpmn8n is read on the same loopback host', () => {
  assert.equal(Heat.workflowId('/workflow/aB3x9'), 'aB3x9');
  assert.equal(Heat.workflowId('/workflow/aB3x9/executions/12'), 'aB3x9');
  assert.equal(Heat.workflowId('/workflow/new'), null);
  assert.equal(Heat.workflowId('/home/workflows'), null);
  assert.equal(Heat.overlayURL('http://localhost:8000/', 'a b'), 'http://localhost:8000/api/analytics/overlay?workflow_id=a%20b');
  assert.equal(Heat.sameHost('http://localhost:8000', 'http://127.0.0.1:5678/workflow/x'), 'http://127.0.0.1:8000');
  assert.equal(Heat.sameHost('https://bpmn8n.example.com', 'http://localhost:5678/'), 'https://bpmn8n.example.com');
});

const capacity = {
  bottleneck: 'managers', stable: true, page: '/simulation/?project=acme',
  nodes: {
    'Approve Expense': { visits: 0.9, resource: 'managers', utilisation: 0.54, servers: 2, stable: true, wait: 216, response: 936 },
    Review: { visits: 1, resource: 'clerks', utilisation: 1.3, servers: 1, stable: false, wait: null, response: null },
    Record: { visits: 0.9, resource: null, utilisation: null, servers: null, stable: true, wait: 0, response: 1.2 },
  },
};

test('capacity mode reads the closed forms of the simulation API', () => {
  assert.ok(Heat.MODES.includes('capacity'));
  assert.equal(Heat.overlayURL('http://localhost:8000/', 'a b', 'capacity'), 'http://localhost:8000/api/simulation/overlay?workflow_id=a%20b');
  assert.equal(Heat.overlayURL('http://localhost:8000', 'w', 'duration'), 'http://localhost:8000/api/analytics/overlay?workflow_id=w');
});

test('a lane is coloured by its utilisation, red from 85 %, and steps nobody waits for get none', () => {
  assert.equal(Heat.capacityColour(0), 'hsl(190 25% 70%)');
  assert.equal(Heat.capacityColour(0.85), 'hsl(0 85% 58%)');
  assert.equal(Heat.capacityColour(1.3), Heat.capacityColour(Heat.NEAR));
  assert.notEqual(Heat.capacityColour(0.84), Heat.capacityColour(0.85));
  assert.equal(Heat.capacityColour(null), null);
  assert.equal(Heat.capacityColour(capacity.nodes.Record.utilisation), null);
});

test('capacity badges, titles and legend say ρ, people, the wait and the bottleneck', () => {
  assert.equal(Heat.capacityBadge(capacity.nodes['Approve Expense']), '54 %');
  assert.equal(Heat.capacityBadge(capacity.nodes.Record), '');
  assert.equal(Heat.capacityDescribe('Approve Expense', capacity.nodes['Approve Expense']),
    'Approve Expense: managers · 2 people, busy 54 % · waits 3.6 min · 0.9 visits per case');
  assert.match(Heat.capacityDescribe('Review', capacity.nodes.Review), /clerks · 1 person, busy 130 % · cannot keep up/);
  assert.equal(Heat.capacityLegend(capacity), 'bottleneck managers');
  assert.equal(Heat.capacityLegend({ ...capacity, stable: false }), 'bottleneck managers, cannot keep up');
  assert.equal(Heat.capacityLegend({ nodes: {}, note: 'a peak of cases at once has no steady state: simulate it' }),
    'a peak of cases at once has no steady state: simulate it');
});

const cases = [
  { case_id: 'w:1', steps: [{ node: 'A', start: 1000, end: 1010 }, { node: 'B', start: 1030, end: 1040 }] },
  { case_id: 'w:2', steps: [{ node: 'A', start: 1050, end: 1060 }] },
];
const centres = { A: { x: 0, y: 0 }, B: { x: 100, y: 200 } };

test('replay compresses the window into the animation length and keeps every step visible', () => {
  const r = Heat.compress(cases, 10);  // 60 real seconds -> 10
  assert.equal(r.length, 10);
  assert.deepEqual(r.cases[0].steps.map(s => [s.node, +s.start.toFixed(3)]), [['A', 0], ['B', 5]]);
  assert.equal(+r.cases[1].steps[0].start.toFixed(3), 8.333);
  const tiny = Heat.compress([{ case_id: 'x', steps: [{ node: 'A', start: 5, end: 5 }, { node: 'B', start: 6, end: 6 }] }], 10);
  assert.ok(tiny.cases[0].steps[0].end - tiny.cases[0].steps[0].start >= Heat.DWELL);
  assert.deepEqual(Heat.compress([], 10), { length: 0, cases: [] });
});

test('a token sits on its node, travels between two, and is gone before and after', () => {
  const all = Heat.compress(cases, 60).cases;  // 1 s per real second: A 0-10, B 30-40
  assert.equal(Heat.positionAt(all[0], 5, centres).node, 'A');
  assert.deepEqual(Heat.positionAt(all[0], 20, centres), { x: 50, y: 100, node: 'B', moving: true });  // halfway
  assert.equal(Heat.positionAt(all[0], 35, centres).moving, false);
  assert.equal(Heat.positionAt(all[0], 45, centres), null);
  assert.equal(Heat.positionAt(all[1], 10, centres), null);  // w:2 has not started
  assert.equal(Heat.positionAt(all[0], 5, { B: centres.B }), null);  // node not on the canvas
});

test('the clock advances by speed and wraps; step mode jumps to the next boundary', () => {
  assert.equal(Heat.advance(1, 500, 2, 10), 2);
  assert.equal(Heat.advance(10.5, 1000, 1, 10), 0);
  const r = Heat.compress(cases, 60);
  assert.equal(Heat.nextBoundary(r, 0), 10);
  assert.equal(Heat.nextBoundary(r, 10), 30);
  assert.equal(Heat.nextBoundary(r, 60), null);
});

test('replay reads the replay endpoint of the workflow', () => {
  assert.equal(Heat.replayURL('http://b/', 'wf 1', { since: '2026-01-01', limit: 20 }),
    'http://b/api/analytics/replay?workflow_id=wf%201&limit=20&since=2026-01-01');
});

test('the switch and its replay controls really hide (their display: flex would beat the hidden attribute)', async () => {
  const source = (await import('node:fs')).readFileSync(new URL('../insights.js', import.meta.url), 'utf8');
  assert.match(source, /\.bpmn8n-heat\[hidden\], \.bpmn8n-heat \[hidden\] \{ display: none; \}/);
});
