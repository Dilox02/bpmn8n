import type { IExecuteFunctions, INodeType, INodeTypeDescription } from 'n8n-workflow';
import { Bpmn8nClient, CREDENTIALS, bpmnDescription } from './shared/Bpmn8nClient';
import { mapInputs, mergeOutputs } from './shared/callMapping';

/** BPMN Call Activity: runs another workflow (a reusable sub-process, started by its "When
 * Executed by Another Workflow" trigger) with these items and continues with its result.
 * Without a mapping the items go in and its result comes back as they are. With Inputs ({child variable:
 * "{{parent variable}}"}) it gets only those fields; with Outputs, only those variables of its result are
 * merged back into the item.
 * With a bpmn8n credential the call is a step of the case's event log: running while the sub-process works
 * (the case is "here" on its canvas), then complete or error with its duration.
 * ponytail: the sub-process should not wait (user/receive tasks): it runs to the end in one go. */
export class Bpmn8nCallActivity implements INodeType {
	description: INodeTypeDescription = bpmnDescription(
		'callActivity',
		'BPMN Call Activity: runs a reusable sub-process (another workflow) and continues with its result',
		{
			subtitle: '={{ $parameter.workflowId.cachedResultName || $parameter.workflowId.value }}',
			credentials: [{ name: CREDENTIALS, required: false }], // optional: a node built before calls were logged runs as it did
			properties: [
				{ displayName: 'Sub-Process', name: 'workflowId', type: 'workflowSelector', default: '', required: true },
				{
					displayName: 'Inputs',
					name: 'inputs',
					type: 'json',
					default: '{}',
					description: 'The only fields the sub-process gets: {"child_variable": "{{parent_variable}}"}; empty: the whole item',
				},
				{
					displayName: 'Outputs',
					name: 'outputs',
					type: 'string',
					typeOptions: { multipleValues: true, multipleValueButtonText: 'Add Output' },
					default: [],
					placeholder: 'result_variable',
					description: 'The only variables of its result that come back, merged into the item; none: its result as it is',
				},
			],
		},
	);

	async execute(this: IExecuteFunctions) {
		const { value: id } = this.getNodeParameter('workflowId', 0, {}) as { value: string }; // as n8n's Execute Workflow
		const parent = this.getWorkflowDataProxy(0);
		const given = this.getNodeParameter('inputs', 0, '{}') as string | Record<string, unknown>;
		const inputs = typeof given === 'string' ? JSON.parse(given || '{}') : given;
		const outputs = this.getNodeParameter('outputs', 0, []) as string[];
		const sent = Object.keys(inputs).length
			? this.getInputData().map((item) => ({ json: mapInputs(item.json, inputs) }))
			: this.getInputData();
		const event = await started(this);
		let result;
		try {
			// like n8n's Execute Workflow: links parent and child executions; a manual run uses the draft
			result = await this.executeWorkflow({ id }, sent, undefined, {
				parentExecution: { executionId: parent.$execution.id, workflowId: parent.$workflow.id as string, shouldResume: true },
				executionMode: this.getMode(),
			});
		} catch (error) {
			await finished(this, event, 'error', (error as Error).message);
			throw error;
		}
		await finished(this, event, 'complete');
		if (!outputs.length) return result.data.map((items) => items ?? []);
		const parents = this.getInputData().map((item) => item.json);
		return result.data.map((items) =>
			mergeOutputs(parents, (items ?? []).map((item) => item.json), outputs).map((json) => ({ json })));
	}
}

/** The call's running event in bpmn8n (its id), or undefined for a node without a bpmn8n credential. */
async function started(ctx: IExecuteFunctions): Promise<number | undefined> {
	try {
		await ctx.getCredentials(CREDENTIALS);
	} catch {
		return undefined;
	}
	return (await new Bpmn8nClient(ctx).post<{ event: number }>('/api/events', { status: 'running' })).event;
}

/** Ends the running event. The sub-process's result is never lost to the log: a failure here is only a hint. */
async function finished(ctx: IExecuteFunctions, event: number | undefined, status: string, message = '') {
	if (event === undefined) return;
	try {
		await new Bpmn8nClient(ctx).post('/api/events', { status, event, message });
	} catch (error) {
		ctx.addExecutionHints?.({ message: `bpmn8n: the call was not logged (${(error as Error).message})`, type: 'warning' });
	}
}
