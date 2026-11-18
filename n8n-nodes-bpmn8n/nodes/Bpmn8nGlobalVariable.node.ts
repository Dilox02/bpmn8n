import type { IDataObject, IExecuteFunctions, INodeType, INodeTypeDescription } from 'n8n-workflow';
import { Bpmn8nClient, bpmnDescription, routeItems } from './shared/Bpmn8nClient';

type Operation = 'get' | 'set' | 'increment' | 'delete';

/** A variable every workflow shares, kept by bpmn8n (Redis or its database): n8n's paid `$vars`, open.
 * Get, Set (with an optional TTL), Increment (atomic, for counters) or Delete it; the value goes on with the
 * item under the variable's key, so the next bpmn8n step finds it as a case variable too. Fields of any
 * bpmn8n node read it as {{vars.<key>}}. */
export class Bpmn8nGlobalVariable implements INodeType {
	description: INodeTypeDescription = bpmnDescription('globalVariable', 'Get, set or count a variable shared by every workflow', {
		subtitle: '={{$parameter["operation"] + ": " + $parameter["key"]}}',
		properties: [
			{ displayName: 'Operation', name: 'operation', type: 'options', noDataExpression: true, default: 'get', options: [
				{ name: 'Get', value: 'get', description: 'Read the value (null when the variable does not exist)' },
				{ name: 'Set', value: 'set', description: 'Write a value' },
				{ name: 'Increment', value: 'increment', description: 'Add to a number atomically (a missing variable counts as 0)' },
				{ name: 'Delete', value: 'delete', description: 'Remove the variable' },
			] },
			{ displayName: 'Key', name: 'key', type: 'string', default: '', required: true, placeholder: 'acme.max_refund',
				description: 'Letters, digits, _ . and -; templates read it as {{vars.<key>}}' },
			{ displayName: 'Value', name: 'value', type: 'json', default: '""', displayOptions: { show: { operation: ['set'] } },
				description: 'A JSON value: 1000, "text", true, {"limit": 3}; an expression may return any value' },
			{ displayName: 'By', name: 'by', type: 'number', default: 1, displayOptions: { show: { operation: ['increment'] } },
				description: 'What to add; negative to subtract' },
			{ displayName: 'Expires After (Seconds)', name: 'ttl', type: 'number', default: 0, typeOptions: { minValue: 0 },
				displayOptions: { show: { operation: ['set', 'increment'] } },
				description: '0: never. For Increment it applies when the increment creates the variable (a counter per time window).' },
			{ displayName: 'Output', name: 'output', type: 'options', default: 'merge', options: [
				{ name: 'Input + Value Under the Key', value: 'merge', description: 'The incoming item, plus the value in a field named like the key' },
				{ name: 'Value Only', value: 'replace', description: 'Only the value, in a field named like the key' },
			] },
		],
	});

	async execute(this: IExecuteFunctions) {
		const client = new Bpmn8nClient(this);
		const outputs = await routeItems(this, 1, 0, async (item, index) => {
			const operation = this.getNodeParameter('operation', index) as Operation;
			const key = String(this.getNodeParameter('key', index)).trim();
			const path = `/api/variables/${encodeURIComponent(key)}`;
			const ttl = (this.getNodeParameter('ttl', index, 0) as number) || null;
			let value: unknown = null;
			if (operation === 'get') {
				try {
					value = ((await client.request('GET', path)) as IDataObject).value;
				} catch (error) {
					if (!/: 404 /.test((error as Error).message)) throw error; // the client's message carries the status
				}
			} else if (operation === 'set') {
				value = parsed(this.getNodeParameter('value', index));
				await client.request('PUT', path, { value, ttl } as IDataObject);
			} else if (operation === 'increment') {
				value = ((await client.post(`${path}/increment`, { by: this.getNodeParameter('by', index) as number, ttl }, index)) as IDataObject).value;
			} else {
				await client.request('DELETE', path).catch((error: Error) => {
					if (!/: 404 /.test(error.message)) throw error; // already gone: what Delete wanted
				});
			}
			const merge = this.getNodeParameter('output', index) === 'merge';
			return { json: { ...(merge ? item.json : {}), [key]: value } as IDataObject, outputs: [0] };
		});
		for (const [index, item] of outputs[0].entries()) {
			const { binary } = this.getInputData()[index];
			if (binary && this.getNodeParameter('output', index) === 'merge') item.binary = binary;
		}
		return outputs;
	}
}

/** A JSON field's value: n8n hands over text when it is typed in, the value itself from an expression. */
function parsed(value: unknown): unknown {
	if (typeof value !== 'string') return value;
	try {
		return JSON.parse(value);
	} catch {
		return value; // plain text
	}
}
