// Fixture: regenerate with `python manage.py export_openapi_schema --api bpmn8n.urls.api --output n8n-nodes-bpmn8n/test/openapi.json`
const { test } = require('node:test');
const assert = require('node:assert');
const { OpenApiSpec } = require('../dist/nodes/shared/OpenApiSpec');

const spec = new OpenApiSpec(require('./openapi.json'));
const fieldsOf = (path) => Object.fromEntries(spec.fields(path).map((f) => [f.id, f]));

test('lists only the tasks of the requested BPMN kind', () => {
	assert.deepStrictEqual(spec.tasks('scriptTask').map((t) => t.value), ['/api/tasks/normalize_alert']);
	assert.ok(spec.tasks('serviceTask').some((t) => t.value === '/api/tasks/register_ticket'));
	assert.deepStrictEqual(spec.tasks('userTask'), []); // user tasks are not endpoints
	const refund = spec.tasks('serviceTask').find((t) => t.value === '/api/tasks/register_ticket');
	assert.ok(!refund.description.includes('\n')); // one line: what it does (and, live, where it is written)
});

test('maps the request body schema to optional text fields (choices keep a dropdown)', () => {
	const fields = fieldsOf('/api/tasks/check_expense_policy');
	assert.deepStrictEqual(Object.keys(fields), ['employee', 'category', 'amount', 'attendees']);
	assert.ok(Object.values(fields).every((f) => f.required === false)); // empty = variable with its name
	assert.strictEqual(fields.amount.type, 'string'); // so that {{total}} fits a number
	assert.strictEqual(fields.amount.defaultValue, undefined); // a default would beat the variable
	assert.strictEqual(fields.category.type, 'options'); // Literal[...] -> dropdown
	assert.deepStrictEqual(fields.category.options.map((o) => o.value), ['meal', 'travel', 'hotel', 'other']);
	assert.strictEqual(fieldsOf('/api/tasks/calculate_refund').duplicate_charge.type, 'string');
});

test('unknown path gives no fields', () => {
	assert.deepStrictEqual(spec.fields('/nope'), []);
});
