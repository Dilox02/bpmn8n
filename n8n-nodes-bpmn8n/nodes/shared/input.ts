import type { IDataObject, INodeProperties } from 'n8n-workflow';
import type { RunIn, RunOut } from './contract';

export type InputMode = 'fields' | 'text';

const TEXT_ONLY = { show: { inputFrom: ['text'] } };

/** Shown in every node that reads process data. */
export const VARIABLES_HINT =
	'Process variables: the data that started the case (e.g. the webhook body) plus every earlier result, form and message. Write {{name}}, e.g. {{book}} or "Due {{due_date}}".';

/** "Input From": the task's fields, or GLiNER2.5 extraction from free text. */
export const INPUT_PROPERTIES: INodeProperties[] = [
	{
		displayName: 'Input From',
		name: 'inputFrom',
		type: 'options',
		default: 'fields',
		options: [
			{ name: 'Fields', value: 'fields', description: 'Each field: a value, a {{variable}}, or empty to use the variable with its name' },
			{
				name: 'Text (GLiNER Extraction)',
				value: 'text',
				description: 'GLiNER2.5 reads free text (an email, a ticket) and fills the input fields',
			},
		],
	},
	{
		displayName: 'Text',
		name: 'text',
		type: 'string',
		typeOptions: { rows: 3 },
		default: '',
		placeholder: '{{message}}',
		description: 'The text the input fields are extracted from, e.g. {{message}}',
		displayOptions: TEXT_ONLY,
	},
	{
		displayName: 'Min Confidence',
		name: 'minConfidence',
		type: 'number',
		typeOptions: { minValue: 0, maxValue: 1, numberPrecision: 2 },
		default: 0.5,
		description: 'A field GLiNER is less sure about counts as not found (then its default, or an error)',
		displayOptions: TEXT_ONLY,
	},
	{
		displayName: 'Verify Values with Laya',
		name: 'verify',
		type: 'boolean',
		default: false,
		description:
			'Whether Laya double-checks each extracted value and drops the ones it is nearly sure are wrong. Off by default: in our evaluation it stopped no wrong value and lost a few right ones.',
		displayOptions: TEXT_ONLY,
	},
	{
		displayName:
			'Empty fields take the process variable with the same name. Write {{name}} to use another one, e.g. {{reader}}; with text input, filled-in fields win over the extracted ones.',
		name: 'variablesNotice',
		type: 'notice',
		default: '',
	},
];

export const EXTRACTION_DETAILS: INodeProperties = {
	displayName: 'Include Extraction Details',
	name: 'includeExtraction',
	type: 'boolean',
	default: false,
	description: 'Whether to add what GLiNER extracted, with confidences, to the output as "extraction"',
	displayOptions: TEXT_ONLY,
};

/** The bpmn8n call for one item: the task's /run endpoint with the filled-in fields (empty ones
 * are left to binding by name), the item (it becomes process data) and, for text input, the text. */
export function inputRequest(
	mode: InputMode,
	path: string,
	fields: IDataObject,
	item: IDataObject,
	text = '',
	minConfidence = 0.5,
	verify = false,
): { path: string; body: RunIn } {
	const filled = Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== null && v !== undefined && v !== ''));
	const extraction = mode === 'text' ? { text, min_confidence: minConfidence, verify } : {};
	return { path: `${path}/run`, body: { fields: filled, item, ...extraction } };
}

/** The task result and what GLiNER extracted (empty for field input). */
export function unpackReply(reply: RunOut) {
	return { result: reply.result as IDataObject, extraction: reply.extracted as IDataObject | undefined };
}
