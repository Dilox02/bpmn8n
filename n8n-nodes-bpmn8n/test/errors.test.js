// A failing item under "Continue On Fail" must carry the error itself: n8n moves an item to a node's "error"
// output (On Error: "Continue (using error output)", the builder's on_error="branch") only when it has one.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { routeItems } = require('../dist/nodes/shared/Bpmn8nClient.js');

const ctx = (continueOnFail) => ({
	getInputData: () => [{ json: { a: 1 } }, { json: { a: 2 } }],
	continueOnFail: () => continueOnFail,
	getNode: () => ({ name: 'Book Slot', type: 'CUSTOM.bpmn8nServiceTask' }),
});
const route = async (item) => {
	if (item.json.a === 2) throw new Error('bpmn8n POST /api/tasks/book_slot/run: 503 down');
	return { json: { booked: true }, outputs: [0] };
};

test('a failure goes on with its message and the error n8n routes by', async () => {
	const [main] = await routeItems(ctx(true), 1, 0, route);
	assert.deepEqual(main[0], { json: { booked: true }, pairedItem: { item: 0 } });
	assert.deepEqual(main[1].json, { a: 2, error: 'bpmn8n POST /api/tasks/book_slot/run: 503 down' });
	assert.equal(main[1].error.message, 'bpmn8n POST /api/tasks/book_slot/run: 503 down');
	assert.deepEqual(main[1].pairedItem, { item: 1 });
});

test('without Continue On Fail the error stops the node', async () => {
	await assert.rejects(routeItems(ctx(false), 1, 0, route), /503 down/);
});
