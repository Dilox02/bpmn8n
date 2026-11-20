// Drift between the Python builder (bpmn8n/workflows/builder.py) and these nodes: n8n drops a parameter its
// node does not declare as soon as the node is edited, so every parameter the builder writes must be one.
// Fixture: regenerate with `python manage.py elements --builder-params` (a Django test says when it is stale).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const builder = require('./builder-params.json');
const pkg = require('../package.json');

const nodes = Object.fromEntries(pkg.n8n.nodes.map((file) => {
	const exported = require(path.join('..', file));
	const description = new (Object.values(exported)[0])().description;
	return [`CUSTOM.${description.name}`, description];
}));

test('every node the builder writes exists', () => {
	assert.deepEqual(Object.keys(builder).filter((type) => !nodes[type]), []);
});

test('every parameter the builder writes is a property of its node', () => {
	for (const [type, keys] of Object.entries(builder)) {
		const declared = new Set(nodes[type].properties.map((p) => p.name));
		assert.deepEqual(keys.filter((k) => !declared.has(k)), [], `${type} does not declare them`);
	}
});
