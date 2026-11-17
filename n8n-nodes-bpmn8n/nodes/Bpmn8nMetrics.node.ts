import type { IExecuteFunctions, INodeType, INodeTypeDescription, IDataObject } from 'n8n-workflow';
import { Bpmn8nClient, bpmnDescription, routeItems } from './shared/Bpmn8nClient';

/** Records explicitly configured measurements, preserving the workflow's input items. */
export class Bpmn8nMetrics implements INodeType {
	description: INodeTypeDescription = bpmnDescription('metrics', 'Record local workflow evaluations in the project run folder', {
		displayName: 'Metrics',
		icon: { light: 'file:icons/metrics.svg', dark: 'file:icons/metrics.dark.svg' },
		properties: [
			{ displayName: 'Project Folder', name: 'project', type: 'string', default: '', required: true,
				description: 'Folder under projects/ containing workflow.json. Samples go to run/<workflow ID>/<execution ID>/metrics/.' },
			{ displayName: 'Run Label', name: 'label', type: 'string', default: '',
				description: 'Optional dataset or experiment name, to distinguish test fixtures from real measurements' },
			{ displayName: 'Metrics', name: 'metrics', type: 'fixedCollection', default: { values: [] },
				typeOptions: { multipleValues: true }, placeholder: 'Add Metric', options: [
					{ displayName: 'Metric', name: 'values', values: [
						{ displayName: 'Name', name: 'name', type: 'string', default: '', required: true },
						{ displayName: 'Value', name: 'value', type: 'number', default: 0, required: true,
							description: 'A finite number, usually an expression such as {{ $json.accepted }}' },
						{ displayName: 'Unit', name: 'unit', type: 'string', default: '' },
						{ displayName: 'Direction', name: 'direction', type: 'options', default: 'higher', options: [
							{ name: 'Higher Is Better', value: 'higher' }, { name: 'Lower Is Better', value: 'lower' },
						] },
						{ displayName: 'Check Threshold', name: 'checkThreshold', type: 'boolean', default: false },
						{ displayName: 'Threshold', name: 'threshold', type: 'number', default: 0,
							displayOptions: { show: { checkThreshold: [true] } } },
					] },
				] },
		],
	});

	async execute(this: IExecuteFunctions) {
		const client = new Bpmn8nClient(this);
		const runIndex = this.getWorkflowDataProxy(0).$runIndex as number;
		const outputs = await routeItems(this, 1, 0, async (item, index) => {
			const configured = this.getNodeParameter('metrics', index) as { values?: IDataObject[] };
			// A value the step did not measure this time (null, empty) is left out, not recorded as 0:
			// a 0 would fail or pass a threshold for a measurement that never happened.
			const measured = (configured.values ?? []).filter(({ value }) => value !== null && value !== undefined && value !== '');
			if (!measured.length) return { json: item.json, outputs: [0] };
			await client.post('/api/evaluations/samples', {
				project: this.getNodeParameter('project', index) as string,
				label: this.getNodeParameter('label', index, '') as string,
				workflow_id: this.getWorkflow().id ?? '', execution_id: this.getExecutionId(),
				node_id: this.getNode().id, node_name: this.getNode().name, sample_id: `${runIndex}:${index}`,
				metrics: measured.map(({ name, value, unit, direction, checkThreshold, threshold }) => ({
					name, value, unit: unit ?? '', direction: direction ?? 'higher', threshold: checkThreshold ? threshold : null,
				})),
			});
			return { json: item.json, outputs: [0] };
		});
		// One output item per input, including Continue On Fail: retain binary attachments too.
		for (const [index, item] of outputs[0].entries()) {
			if (this.getInputData()[index].binary) item.binary = this.getInputData()[index].binary;
		}
		return outputs;
	}
}
