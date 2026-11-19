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
