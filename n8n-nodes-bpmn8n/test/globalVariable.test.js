const { test } = require('node:test');
const assert = require('node:assert/strict');
const { Bpmn8nGlobalVariable } = require('../dist/nodes/Bpmn8nGlobalVariable.node');

/** A node context whose bpmn8n server answers with `reply(request)` ({statusCode, body}). */
function context(params, reply, items = [{ json: { case: 1 } }]) {
	const calls = [];
	return {
		calls,
		getInputData: () => items, continueOnFail: () => false,
		getWorkflow: () => ({ id: 'wf1', name: 'Refunds' }), getExecutionId: () => '7',
		getNode: () => ({ id: 'node1', name: 'Count' }), getWorkflowDataProxy: () => ({ $runIndex: 0 }),
		getCredentials: async () => ({ baseUrl: 'http://bpmn8n/' }),
		getNodeParameter: (name, _i, fallback) => (name in params ? params[name] : fallback),
		helpers: { httpRequestWithAuthentication: async function (_, request) { calls.push(request); return reply(request); } },
	};
}

const ok = (body) => () => ({ statusCode: 200, body });

test('get puts the value on the item under its key, null when missing', async () => {
	const found = context({ operation: 'get', key: 'acme.limit', output: 'merge' }, ok({ key: 'acme.limit', value: { max: 3 } }));
	const [[out]] = await new Bpmn8nGlobalVariable().execute.call(found);
	assert.deepEqual(out.json, { case: 1, 'acme.limit': { max: 3 } });
	assert.deepEqual([found.calls[0].method, found.calls[0].url], ['GET', 'http://bpmn8n/api/variables/acme.limit']);
	const missing = context({ operation: 'get', key: 'none', output: 'replace' }, () => ({ statusCode: 404, body: { detail: 'no' } }));
	const [[none]] = await new Bpmn8nGlobalVariable().execute.call(missing);
	assert.deepEqual(none.json, { none: null });
});

test('set sends JSON values and the TTL; text that is not JSON stays text', async () => {
	for (const [typed, sent] of [['{"max": 3}', { max: 3 }], ['1000', 1000], ['plain words', 'plain words'], [42, 42]]) {
		const ctx = context({ operation: 'set', key: 'k', value: typed, ttl: 60, output: 'merge' }, ok({}));
		const [[out]] = await new Bpmn8nGlobalVariable().execute.call(ctx);
		assert.deepEqual([ctx.calls[0].method, ctx.calls[0].body], ['PUT', { value: sent, ttl: 60 }]);
		assert.deepEqual(out.json.k, sent);
	}
	const forever = context({ operation: 'set', key: 'k', value: '1', ttl: 0, output: 'merge' }, ok({}));
	await new Bpmn8nGlobalVariable().execute.call(forever);
	assert.equal(forever.calls[0].body.ttl, null);
});

test('increment posts with the case headers and passes the new value on', async () => {
	const ctx = context({ operation: 'increment', key: 'hits', by: 2, ttl: 0, output: 'merge' }, ok({ key: 'hits', value: 7 }));
	const [[out]] = await new Bpmn8nGlobalVariable().execute.call(ctx);
	assert.equal(ctx.calls[0].url, 'http://bpmn8n/api/variables/hits/increment');
	assert.deepEqual(ctx.calls[0].body, { by: 2, ttl: null });
	assert.equal(ctx.calls[0].headers['X-Bpmn8n-Case'], 'wf1%3A7');
	assert.equal(out.json.hits, 7);
});

test('delete tolerates a variable already gone; other errors fail the node', async () => {
	const gone = context({ operation: 'delete', key: 'k', output: 'merge' }, () => ({ statusCode: 404, body: { detail: 'no' } }));
	const [[out]] = await new Bpmn8nGlobalVariable().execute.call(gone);
	assert.equal(gone.calls[0].method, 'DELETE');
	assert.deepEqual(out.json, { case: 1, k: null });
	const bad = context({ operation: 'get', key: 'a:b', output: 'merge' }, () => ({ statusCode: 422, body: { detail: 'bad key' } }));
	await assert.rejects(new Bpmn8nGlobalVariable().execute.call(bad), /422 bad key/);
});
