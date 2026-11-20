import type {
	IExecuteFunctions,
	ILoadOptionsFunctions,
	INodeType,
	INodeTypeDescription,
	ResourceMapperValue,
} from 'n8n-workflow';
import { Bpmn8nClient, bpmnDescription, routeItems } from './Bpmn8nClient';
import type { RunOut } from './contract';
import { EXTRACTION_DETAILS, INPUT_PROPERTIES, inputRequest, unpackReply, type InputMode } from './input';
import { OUTPUT_PROPERTIES, shapeOutput, type OutputMode } from './output';

/** A BPMN task whose implementations are the bpmn8n endpoints tagged `x-bpmn-task: <kind>`.
 * The Task dropdown and the Input fields are read live from the server's OpenAPI document,
 * so a new @task shows up without rebuilding the node. */
export abstract class TaskNode implements INodeType {
	description: INodeTypeDescription;
	methods: INodeType['methods'];

	protected constructor(kind: string, summary: string) {
		this.description = bpmnDescription(kind, summary, {
			subtitle: '={{$parameter["task"].split("/").pop()}}',
			properties: [
				{
					displayName: 'Task',
					name: 'task',
					type: 'options',
					typeOptions: { loadOptionsMethod: 'getTasks' },
					default: '',
					required: true,
					description: `Python functions registered with @task("${kind}") in bpmn8n; new ones appear here at once`,
				},
				...INPUT_PROPERTIES,
				{
					displayName: 'Input',
					name: 'input',
					type: 'resourceMapper',
					default: { mappingMode: 'defineBelow', value: null },
					noDataExpression: true,
					displayOptions: { hide: { task: [''] } }, // nothing to map until a task is picked
					typeOptions: {
						loadOptionsDependsOn: ['task'],
						resourceMapper: {
							resourceMapperMethod: 'getFields',
							mode: 'add',
							valuesLabel: 'Input',
							fieldWords: { singular: 'field', plural: 'fields' },
							addAllFields: true,
							multiKeyMatch: false,
							supportAutoMap: false,
							noFieldsError: 'This task takes no input.',
						},
					},
				},
				...OUTPUT_PROPERTIES,
				EXTRACTION_DETAILS,
			],
		});
		// n8n calls these with its own `this`, so the kind travels by closure.
		this.methods = {
			loadOptions: {
				async getTasks(this: ILoadOptionsFunctions) {
					return (await new Bpmn8nClient(this).spec()).tasks(kind);
				},
			},
			resourceMapping: {
				async getFields(this: ILoadOptionsFunctions) {
					const path = this.getNodeParameter('task') as string;
					const fields = path ? (await new Bpmn8nClient(this).spec()).fields(path) : [];
					return { fields }; // all optional: an empty field takes the variable with its name
				},
			},
		};
	}

	async execute(this: IExecuteFunctions) {
		const client = new Bpmn8nClient(this);
		return routeItems(this, 1, 0, async (item, i) => {
			const { path, body } = inputRequest(
				this.getNodeParameter('inputFrom', i, 'fields') as InputMode,
				this.getNodeParameter('task', i) as string,
				(this.getNodeParameter('input', i) as ResourceMapperValue).value ?? {},
				item.json,
				this.getNodeParameter('text', i, '') as string,
				this.getNodeParameter('minConfidence', i, 0.5) as number,
				this.getNodeParameter('verify', i, false) as boolean,
			);
			const { result, extraction } = unpackReply(await client.post<RunOut>(path, body, i));
			const json = shapeOutput(
				this.getNodeParameter('output', i, 'result') as OutputMode,
				item.json,
				result,
				this.getNodeParameter('outputKey', i, 'result') as string,
			);
			if (this.getNodeParameter('includeExtraction', i, false)) json.extraction = extraction;
			return { json, outputs: [0] };
		});
	}
}
