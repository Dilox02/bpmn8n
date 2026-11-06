import type { IDataObject, IExecuteFunctions, INodeType, INodeTypeDescription } from 'n8n-workflow';
import { Bpmn8nClient, bpmnDescription, routeItems } from './Bpmn8nClient';
import { branchOutputs, branchesProperty, readBranches } from './branches';

/** A BPMN split gateway, decided in bpmn8n on the case's process variables: by Laya reading text
 * (POST /api/gateways/<kind>) or by conditions such as {{amount}} > 1000, one per branch
 * (exclusive: the first that holds; inclusive: all). No branch -> the default output. Items pass
 * through unchanged. */
export abstract class GatewayNode implements INodeType {
	description: INodeTypeDescription;
	execute: INodeType['execute'];

	protected constructor(kind: 'exclusive' | 'inclusive', summary: string, minConfidence: number) {
		this.description = bpmnDescription(`${kind}Gateway`, summary, {
			subtitle: '={{ $parameter.decider === "conditions" ? "by conditions" : $parameter.question }}',
			outputs: branchOutputs({ last: 'default' }), // + default: taken when Laya picks no branch
			properties: [
				{
					displayName: 'Decide With',
					name: 'decider',
					type: 'options',
					default: 'laya',
					options: [
						{ name: 'Laya (Reads Text)', value: 'laya', description: 'For tickets, emails, requests written by people' },
						{ name: 'Conditions (Data)', value: 'conditions', description: 'A true/false expression per branch' },
					],
				},
				{
					displayName: 'Question',
					name: 'question',
					type: 'string',
					typeOptions: { rows: 2 },
					default: '',
					required: kind === 'exclusive',
					displayOptions: { show: { decider: ['laya'] } },
					placeholder: kind === 'exclusive' ? 'Which team should handle this ticket?' : '',
					description:
						kind === 'exclusive'
							? 'Laya picks the branch that best answers it'
							: 'Optional prefix for every branch question; short full questions per branch work best',
				},
				branchesProperty(
					kind === 'exclusive'
						? "The situation this branch is for, e.g. 'wants to buy more, asks for a quote'"
						: "A short yes/no question, e.g. 'Was personal data leaked?'",
				),
				{
					displayName: 'State',
					name: 'state',
					type: 'string',
					typeOptions: { rows: 3 },
					default: '',
					placeholder: '{{message}}',
					description: 'What Laya reads, e.g. {{message}}; empty: all the process variables',
					displayOptions: { show: { decider: ['laya'] } },
				},
				{
					displayName: 'Min Confidence',
					name: 'minConfidence',
					type: 'number',
					typeOptions: { minValue: 0, maxValue: 1, numberPrecision: 2 },
					default: minConfidence,
					description:
						'Below this a branch is not taken and the item goes to "default". 0.7 worked well in our tests, with "default" connected to a User Task.',
					displayOptions: { show: { decider: ['laya'] } },
				},
			],
		});
		this.execute = async function (this: IExecuteFunctions) {
			const client = new Bpmn8nClient(this);
			const names = readBranches(this, 0).map((b) => b.name);
			const defaultOutput = names.length;
			return routeItems(this, names.length + 1, defaultOutput, async (item, i) => {
				const branches = readBranches(this, i);
				// both deciders run in bpmn8n, on the case's process variables (+ this item)
				const reply =
					this.getNodeParameter('decider', i) === 'conditions'
						? await client.post('/api/gateways/conditions', {
								kind,
								item: item.json,
								conditions: Object.fromEntries(branches.map((b) => [b.name, b.condition ?? ''])) as IDataObject,
							})
						: await client.post(`/api/gateways/${kind}`, {
								item: item.json,
								question: this.getNodeParameter('question', i, '') as string,
								state: this.getNodeParameter('state', i, '') as string,
								min_confidence: this.getNodeParameter('minConfidence', i) as number,
								branches: Object.fromEntries(branches.map((b) => [b.name, b.description || b.name])) as IDataObject,
							});
				const taken = (reply.branches as string[]).map((b) => names.indexOf(b)).filter((o) => o >= 0);
				return { json: item.json, outputs: taken.length ? taken : [defaultOutput] };
			});
		};
	}
}
