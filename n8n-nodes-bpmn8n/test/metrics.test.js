const { test } = require('node:test');
const assert = require('node:assert/strict');
const { Bpmn8nMetrics } = require('../dist/nodes/Bpmn8nMetrics.node');

test('metrics keep items paired, evaluate each item, and distinguish loop iterations', async () => {
	const calls = [];
	const items = [{ json: { score: .9 }, binary: { file: { data: 'dGVzdA==', mimeType: 'text/plain' } } }, { json: { score: .5 } }];
	const context = {
		getWorkflowDataProxy: () => ({ $runIndex: 3 }), getInputData: () => items,
		getWorkflow: () => ({ id: 'workflow1', name: 'Workflow' }), getExecutionId: () => '123',
		getNode: () => ({ id: 'node1', name: 'Quality' }), getCredentials: async () => ({ baseUrl: 'http://backend' }),
		continueOnFail: () => false,
		getNodeParameter: (name, i) => name === 'label' ? 'test fixture' : name === 'project' ? 'example' : { values: [
			{ name: 'score', value: items[i].json.score, checkThreshold: true, threshold: .8 },
		] },
		helpers: { httpRequestWithAuthentication: async function (_, request) { calls.push(request); return { statusCode: 200, body: {} }; } },
	};
	const [output] = await new Bpmn8nMetrics().execute.call(context);
	assert.deepEqual(output.map(o => o.json), items.map(i => i.json));
	assert.deepEqual(output[0].binary, items[0].binary);
	assert.deepEqual(output.map(o => o.pairedItem), [{ item: 0 }, { item: 1 }]);
	assert.deepEqual(calls.map(c => c.body.sample_id), ['3:0', '3:1']);
	assert.equal(calls[0].body.metrics[0].threshold, .8);
	assert.equal(calls[1].body.metrics[0].value, .5);
	assert.equal(calls[0].body.workflow_id, 'workflow1');
	assert.equal(calls[0].timeout, 15 * 60 * 1000);  // a long task is not aborted at n8n's 300 s
});

test('a value not measured in this item is left out, and an item with none posts nothing', async () => {
	const calls = [];
	const items = [{ json: { agreement: null, speed: 14 } }, { json: { agreement: null, speed: null } }];
	const context = {
		getWorkflowDataProxy: () => ({ $runIndex: 0 }), getInputData: () => items,
		getWorkflow: () => ({ id: 'workflow1', name: 'Workflow' }), getExecutionId: () => '7',
		getNode: () => ({ id: 'node1', name: 'Health' }), getCredentials: async () => ({ baseUrl: 'http://backend' }),
		continueOnFail: () => false,
		getNodeParameter: (name, i) => name === 'label' ? '' : name === 'project' ? 'example' : { values: [
			{ name: 'agreement', value: items[i].json.agreement, checkThreshold: true, threshold: .4 },
			{ name: 'speed', value: items[i].json.speed, checkThreshold: true, threshold: 8 },
		] },
		helpers: { httpRequestWithAuthentication: async function (_, request) { calls.push(request); return { statusCode: 200, body: {} }; } },
	};
	const [output] = await new Bpmn8nMetrics().execute.call(context);
	assert.equal(output.length, 2);
	assert.equal(calls.length, 1);
	assert.deepEqual(calls[0].body.metrics.map(m => m.name), ['speed']);
});
