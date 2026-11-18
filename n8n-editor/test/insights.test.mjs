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
