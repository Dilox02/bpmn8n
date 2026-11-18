const { test } = require('node:test');
const assert = require('node:assert');
const { Bpmn8nUserTask } = require('../dist/nodes/Bpmn8nUserTask.node.js');
const { Bpmn8nReceiveTask } = require('../dist/nodes/Bpmn8nReceiveTask.node.js');
const { Bpmn8nEventBasedGateway } = require('../dist/nodes/Bpmn8nEventBasedGateway.node.js');

const webhook = (body, parameters = {}) => ({
	getBodyData: () => body,
	getNode: () => ({ name: 'Node' }),
	getNodeParameter: (name) => parameters[name],
});

test('a multi-instance resume brings one item per task, in order', async () => {
	const { workflowData } = await new Bpmn8nUserTask().webhook.call(webhook({ _items: [{ ok: true }, { ok: false }] }));
	assert.deepStrictEqual(workflowData, [[{ json: { ok: true } }, { json: { ok: false } }]]);
});

test('a human task links to its pages: the run\'s task while waiting, the inbox always', () => {
	const { waitingNodeTooltip, hints } = new Bpmn8nUserTask().description;
	assert.match(waitingNodeTooltip, /inbox\/\?workflow=\{\{ \$workflow\.id \}\}&execution=\{\{ \$execution\.id \}\}/);
	assert.deepStrictEqual([hints[0].whenToDisplay, hints[0].location], ['always', 'outputPane']);
	assert.match(hints[0].message, /href="http:\/\/localhost:8000\/inbox\/"/);
});

test('a message resume is one item', async () => {
	const { workflowData } = await new Bpmn8nReceiveTask().webhook.call(webhook({ amount: 9 }));
	assert.deepStrictEqual(workflowData, [[{ json: { amount: 9 } }]]);
});

test('several items cannot wait for one message', async () => {
	const ctx = { getInputData: () => [{ json: {} }, { json: {} }], getNode: () => ({ name: 'Receive' }) };
	await assert.rejects(new Bpmn8nReceiveTask().execute.call(ctx), /2 items arrived/);
});

test('an unknown message is an error, not a timeout', async () => {
	const branches = { branches: { values: [{ name: 'hold-book' }] } };
	const gateway = new Bpmn8nEventBasedGateway();
	const { workflowData } = await gateway.webhook.call(webhook({ _message: 'hold-book' }, branches));
	assert.strictEqual(workflowData[1].length, 1);
	await assert.rejects(gateway.webhook.call(webhook({ _message: 'spam' }, branches)), /no output for message "spam"/);
});

/** A User Task run: what it posted to bpmn8n, until when it waited, with bpmn8n answering `reply`. */
function userTaskRun(parameters, reply) {
	const posted = [], waited = [];
	const base = { title: 'Approve', lane: 'managers', description: '', form: { fields: [] }, context: '{}' };
	const all = { ...base, ...parameters };
	const ctx = {
		getInputData: () => [{ json: { amount: 5 } }],
		getNode: () => ({ id: 'n1', name: 'Approve' }),
		getNodeParameter: (name, _i, fallback) => (name in all ? all[name] : fallback),
		evaluateExpression: (e) => (e.includes('resumeUrl') ? 'http://localhost:5678/webhook-waiting/9' : 0),
		putExecutionToWait: async (until) => waited.push(until),
		getCredentials: async () => ({ baseUrl: 'http://web:8000' }),
		getWorkflow: () => ({ id: 'wf', name: 'Expenses' }),
		getExecutionId: () => '9',
		helpers: {
			httpRequestWithAuthentication: async (_credentials, options) => {
				posted.push(options.body);
				return { statusCode: 201, body: reply };
			},
		},
	};
	return { run: () => new Bpmn8nUserTask().execute.call(ctx), posted, waited };
}

test('a deadline waits until the due time bpmn8n answers with, and times out through output 0', async () => {
	const deadline = { due: 'for', dueAmount: 2, dueUnit: 'days', deadline: true };
	const { run, posted, waited } = userTaskRun(
		{ ...deadline, priority: 'high', assignee: '{{approver}}', notSameAs: ['Submit Expense'], completion: 'atLeast', quorum: 2 },
		[{ id: 1, due: '2026-12-01T10:00:00Z' }],
	);
	const outputs = await run();
	assert.deepStrictEqual(outputs, [[{ json: { amount: 5 } }], []]); // a disabled re-run passes them on: "timeout"
	assert.deepStrictEqual(waited, [new Date('2026-12-01T10:00:00Z')]);
	const { due, ...rest } = posted[0];
	assert.ok(Math.abs(Date.parse(due) - Date.now() - 2 * 8.64e7) < 60e3, due);
	assert.deepStrictEqual(
		[rest.priority, rest.deadline, rest.assignee, rest.not_same_as, rest.completion],
		['high', true, '{{approver}}', ['Submit Expense'], 2],
	);
});

test('without a deadline it waits for people only, on its single output', async () => {
	const { run, posted, waited } = userTaskRun({ due: 'until', dueUntil: '{{due_date}}' }, [{ id: 1, due: '2026-12-01T10:00:00Z' }]);
	assert.strictEqual((await run()).length, 1);
	assert.deepStrictEqual(waited, [new Date('3000-01-01T00:00:00.000Z')]);
	assert.deepStrictEqual([posted[0].due, posted[0].deadline, posted[0].completion, posted[0].priority],
		['{{due_date}}', false, 'all', 'normal']); // bpmn8n fills the variable in from the case
});

test('people completing in time leave on "done"; the outputs say so only with a deadline', async () => {
	const deadline = { due: 'for', deadline: true };
	const done = await new Bpmn8nUserTask().webhook.call(webhook({ _items: [{ ok: true }] }, deadline));
	assert.deepStrictEqual(done.workflowData, [[], [{ json: { ok: true } }]]);
	const plain = await new Bpmn8nUserTask().webhook.call(webhook({ _items: [{ ok: true }] }, { deadline: true }));
	assert.deepStrictEqual(plain.workflowData, [[{ json: { ok: true } }]]); // no due time: the deadline is off
	const outputs = new Function('$parameter', `return ${new Bpmn8nUserTask().description.outputs.slice(3, -2)}`);
	assert.deepStrictEqual(outputs(deadline).map((o) => o.displayName), ['timeout', 'done']);
	assert.deepStrictEqual(outputs({ due: 'none', deadline: true }), ['main']);
});

test("bpmn8n's explanation reaches the n8n error", async () => {
	const { Bpmn8nClient } = require('../dist/nodes/shared/Bpmn8nClient.js');
	const ctx = {
		getCredentials: async () => ({ baseUrl: 'http://web:8000' }),
		getNode: () => ({ name: 'Check Amount' }),
		helpers: { httpRequestWithAuthentication: async () => ({ statusCode: 422, body: { detail: "'{{amount}} < 1000': no" } }) },
	};
	await assert.rejects(new Bpmn8nClient(ctx).post('/api/gateways/conditions', {}), /422 '\{\{amount\}\} < 1000': no/);
});
