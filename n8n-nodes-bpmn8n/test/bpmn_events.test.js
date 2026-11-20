// C2, C4, C5, C8: reminders, signals, terminate, call-activity data mapping.
const { test } = require('node:test');
const assert = require('node:assert');
const { Bpmn8nSignalCatch } = require('../dist/nodes/Bpmn8nSignalCatch.node.js');
const { Bpmn8nTerminateEndEvent } = require('../dist/nodes/Bpmn8nTerminateEndEvent.node.js');
const { Bpmn8nCallActivity } = require('../dist/nodes/Bpmn8nCallActivity.node.js');
const { Bpmn8nManualTask } = require('../dist/nodes/Bpmn8nManualTask.node.js');
const { mapInputs, mergeOutputs } = require('../dist/nodes/shared/callMapping.js');

/** An execution context: `parameters` by name, every post to bpmn8n kept in `posted` (path, body). */
function context(parameters, items = [{ json: { amount: 5 } }], extra = {}) {
	const posted = [], waited = [];
	const ctx = {
		getInputData: () => items,
		getNode: () => ({ id: 'n1', name: 'Node' }),
		getNodeParameter: (name, _i, fallback) => (name in parameters ? parameters[name] : fallback),
		evaluateExpression: (e) => (e.includes('resumeUrl') ? 'http://localhost:5678/webhook-waiting/9' : 0),
		putExecutionToWait: async (until) => waited.push(until),
		getCredentials: async () => ({ baseUrl: 'http://web:8000' }),
		getWorkflow: () => ({ id: 'wf', name: 'Flow' }),
		getExecutionId: () => '9',
		helpers: {
			httpRequestWithAuthentication: async (_c, options) => {
				posted.push([options.url, options.body]);
				return { statusCode: 200, body: [{ id: 1 }] };
			},
		},
		...extra,
	};
	return { ctx, posted, waited };
}

test('a signal catch subscribes as a broadcast with no correlation key, and waits for ever', async () => {
	const { ctx, posted, waited } = context({ signal: 'market-closed' });
	await new Bpmn8nSignalCatch().execute.call(ctx);
	const [url, body] = posted[0];
	assert.strictEqual(url, 'http://web:8000/api/messages/subscriptions');
	assert.deepStrictEqual([body.name, body.correlation_key, body.broadcast], ['market-closed', '', true]);
	assert.match(body.resume_url, /bpmn8n_wait=n1%3A0$/);
	assert.deepStrictEqual(waited, [new Date('3000-01-01T00:00:00.000Z')]);
});

test('a signal resumes the catch with its payload as one item', async () => {
	const ctx = { getBodyData: () => ({ price: 7, _signal: 'market-closed' }), getNode: () => ({ name: 'Node' }) };
	const { workflowData } = await new Bpmn8nSignalCatch().webhook.call(ctx);
	assert.deepStrictEqual(workflowData, [[{ json: { price: 7, _signal: 'market-closed' } }]]);
});

test('a terminate end tells bpmn8n which case ends and has no outputs', async () => {
	const node = new Bpmn8nTerminateEndEvent();
	assert.deepStrictEqual(node.description.outputs, []);
	const { ctx, posted } = context({ reason: 'customer left' });
	assert.deepStrictEqual(await node.execute.call(ctx), []);
	assert.deepStrictEqual(posted, [['http://web:8000/api/cases/wf%3A9/terminate', { reason: 'customer left' }]]);
});

test('a terminated case: bpmn8n\'s 409 fails the node with its explanation', async () => {
	const { ctx } = context({ reason: '' });
	ctx.helpers.httpRequestWithAuthentication = async () => ({ statusCode: 409, body: { detail: 'case wf:9 was terminated' } });
	await assert.rejects(new Bpmn8nTerminateEndEvent().execute.call(ctx), /409 case wf:9 was terminated/);
});

test('mapInputs sends only the mapped fields; a lone template keeps its type, text is filled in', () => {
	const item = { amount: 5, customer: { name: 'Ann' }, secret: 'x' };
	assert.deepStrictEqual(
		mapInputs(item, { total: '{{amount}}', who: '{{customer.name}}', label: 'for {{customer.name}}', fixed: 3, gone: '{{missing}}' }),
		{ total: 5, who: 'Ann', label: 'for Ann', fixed: 3 },
	);
});

test('mergeOutputs merges back only the listed outputs into the parent item', () => {
	assert.deepStrictEqual(
		mergeOutputs([{ id: 1 }, { id: 2 }], [{ answer: 'a', secret: 's' }, { answer: 'b' }], ['answer', 'absent']),
		[{ id: 1, answer: 'a' }, { id: 2, answer: 'b' }],
	);
	assert.deepStrictEqual(mergeOutputs([{ id: 1 }], [{ r: 1 }, { r: 2 }], ['r']), [{ id: 1, r: 1 }, { id: 1, r: 2 }]);
});

function callRun(parameters, child, extra = {}) {
	const sent = [];
	const { ctx, posted } = context({ workflowId: { value: '7' }, ...parameters }, [{ json: { amount: 5, text: 'hi' } }], {
		getWorkflowDataProxy: () => ({ $execution: { id: '9' }, $workflow: { id: 'wf' } }),
		getMode: () => 'trigger',
		executeWorkflow: async (_w, items) => (sent.push(items), { data: [child] }),
		...extra,
	});
	ctx.helpers.httpRequestWithAuthentication = async (_c, options) => (posted.push([options.url, options.body]),
		{ statusCode: 200, body: { message: '', event: 41 } });
	return { run: () => new Bpmn8nCallActivity().execute.call(ctx), sent, posted };
}

test('a call activity is a step of the event log: running while the sub-process works, then complete', async () => {
	const { run, posted } = callRun({}, [{ json: { out: 1 } }]);
	await run();
	assert.deepStrictEqual(posted.map(([url, body]) => [url, body.status, body.event]),
		[['http://web:8000/api/events', 'running', undefined], ['http://web:8000/api/events', 'complete', 41]]);
});

test('a failed sub-process ends its event as an error and still fails; a node without a credential logs nothing', async () => {
	const failing = callRun({}, [], { executeWorkflow: async () => { throw new Error('child broke'); } });
	await assert.rejects(failing.run(), /child broke/);
	assert.deepStrictEqual(failing.posted[1][1], { status: 'error', event: 41, message: 'child broke' });
	const bare = callRun({}, [{ json: { out: 1 } }], { getCredentials: async () => { throw new Error('no credentials'); } });
	assert.deepStrictEqual(await bare.run(), [[{ json: { out: 1 } }]]);
	assert.deepStrictEqual(bare.posted, []);
});

test('a call activity without a mapping sends the items and returns the result as before', async () => {
	const { run, sent } = callRun({}, [{ json: { out: 1 } }]);
	assert.deepStrictEqual(await run(), [[{ json: { out: 1 } }]]);
	assert.deepStrictEqual(sent, [[{ json: { amount: 5, text: 'hi' } }]]);
});

test('a call activity with inputs and outputs sends only the mapped fields and merges back only the outputs', async () => {
	const { run, sent } = callRun(
		{ inputs: '{"prompt": "{{text}}"}', outputs: ['answer'] },
		[{ json: { answer: 'yes', secret: 'no' } }],
	);
	assert.deepStrictEqual(await run(), [[{ json: { amount: 5, text: 'hi', answer: 'yes' } }]]);
	assert.deepStrictEqual(sent, [[{ json: { prompt: 'hi' } }]]);
});

test('a human task with Remind After sends when to remind and whom to escalate to; without it, nothing', async () => {
	const node = new Bpmn8nManualTask();
	const base = { title: 'Check', lane: 'clerks', description: '', form: { fields: [] }, context: '{}' };
	const run = async (parameters) => {
		const { ctx, posted } = context({ ...base, ...parameters });
		await node.execute.call(ctx);
		return posted[0][1];
	};
	const body = await run({ remindAmount: 2, remindUnit: 'hours', escalateTo: '/escalate-claims' });
	assert.ok(Math.abs(Date.parse(body.remind_at) - Date.now() - 2 * 3.6e6) < 60e3, body.remind_at);
	assert.strictEqual(body.escalate_to, '/escalate-claims');
	const none = await run({});
	assert.deepStrictEqual([none.remind_at, none.escalate_to], [undefined, undefined]);
});
