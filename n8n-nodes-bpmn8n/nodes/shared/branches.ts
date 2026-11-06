import type { INodeProperties } from 'n8n-workflow';

export interface Branch {
	name: string;
	description?: string;
	condition?: string | boolean;
}

type Expression = `={{${string}}}`;

/** One n8n output per configured branch (its name is the label), plus optional extra outputs
 * before them (e.g. "timeout") and after them (e.g. "default", BPMN's default flow). */
export function branchOutputs(extra: { first?: string; last?: string } = {}): Expression {
	const output = (label: string) => `[{ type: 'main', displayName: '${label}' }]`;
	const branches = `(($parameter.branches || {}).values || []).map(b => ({ type: 'main', displayName: b.name }))`;
	const head = extra.first ? `${output(extra.first)}.concat(${branches})` : branches;
	return `={{ ${head}${extra.last ? `.concat(${output(extra.last)})` : ''} }}`;
}

/** The "Branches" list; with `describe` each branch also gets a description for Laya and a
 * condition for data-based decisions (shown according to the node's "Decide With"). */
export function branchesProperty(describe?: string): INodeProperties {
	const name: INodeProperties = { displayName: 'Name', name: 'name', type: 'string', default: '', required: true };
	const condition: INodeProperties = {
		displayName: 'Condition',
		name: 'condition',
		type: 'string',
		default: '',
		placeholder: '{{available}}',
		description:
			'When this branch applies, on the process variables: {{available}}, not {{available}}, {{amount}} > 1000 and {{vip}}, {{category}} == "meal"',
		displayOptions: { show: { '/decider': ['conditions'] } },
	};
	return {
		displayName: 'Branches',
		name: 'branches',
		type: 'fixedCollection',
		// each branch titled by its name (n8n's default: "Branch 1", "Branch 2"...)
		typeOptions: { multipleValues: true, sortable: true, fixedCollection: { itemTitle: '={{ $collection.item.value.name }}' } },
		default: { values: [{ name: 'Branch 1' }, { name: 'Branch 2' }] },
		placeholder: 'Add Branch',
		description: 'Each branch is an output of this node, in this order',
		options: [
			{
				displayName: 'Branch',
				name: 'values',
				values: describe
					? [
							name,
							{
								displayName: 'Description',
								name: 'description',
								type: 'string',
								default: '',
								description: describe,
								displayOptions: { show: { '/decider': ['laya'] } },
							},
							condition,
						]
					: [name],
			},
		],
	};
}

/** The configured branches, from an execution (item `i`) or a webhook (no item). */
export function readBranches(ctx: { getNodeParameter(name: string, ...rest: never[]): unknown }, i?: number): Branch[] {
	const read = ctx.getNodeParameter as (name: string, i?: number) => unknown;
	const value = (i === undefined ? read.call(ctx, 'branches') : read.call(ctx, 'branches', i)) as { values?: Branch[] };
	return value.values ?? [];
}
