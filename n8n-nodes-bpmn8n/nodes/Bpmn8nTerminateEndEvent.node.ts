import type { IExecuteFunctions, INodeType, INodeTypeDescription } from 'n8n-workflow';
import { Bpmn8nClient, bpmnDescription } from './shared/Bpmn8nClient';

/** BPMN Terminate End Event: ends the whole case here and stops its other branches. bpmn8n records the
 * termination, cancels what the case still waits on (tasks, messages, signals) and asks n8n, through its
 * public API (POST /executions/{id}/stop), to stop the execution; every later bpmn8n call of the case
 * answers 409 "terminated". ponytail: n8n finishes the branch it is running before the stop takes hold. */
export class Bpmn8nTerminateEndEvent implements INodeType {
	description: INodeTypeDescription = bpmnDescription(
		'terminateEndEvent',
		'BPMN Terminate End Event: ends the process here and stops its other branches',
		{
			subtitle: '={{$parameter["reason"]}}',
			outputs: [],
			properties: [
				{ displayName: 'Reason', name: 'reason', type: 'string', default: '', placeholder: 'Order cancelled by the customer' },
			],
		},
	);

	async execute(this: IExecuteFunctions) {
		const caseId = `${this.getWorkflow().id}:${this.getExecutionId()}`;
		await new Bpmn8nClient(this).post(`/api/cases/${encodeURIComponent(caseId)}/terminate`, {
			reason: this.getNodeParameter('reason', 0, '') as string,
		});
		return [];
	}
}
