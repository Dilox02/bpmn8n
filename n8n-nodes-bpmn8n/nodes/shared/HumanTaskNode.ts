import type { IDataObject, IExecuteFunctions, INodeProperties, IWebhookFunctions } from 'n8n-workflow';
import type { FormField, UserTaskIn } from './contract';
import { UNIT_MS, deadline, durationProperties } from './duration';
import { VARIABLES_HINT } from './input';
import { WaitingNode } from './WaitingNode';

declare const process: { env: Record<string, string | undefined> };

/** Where people open bpmn8n's pages (not the Docker-internal URL of the credential). */
const PAGES = (process.env.BPMN8N_PUBLIC_URL ?? 'http://localhost:8000').replace(/\/$/, '');

/** With an interrupting deadline the node has two outputs: "timeout" (0: n8n times a wait out through
 * output 0, see WaitingNode) and "done"; without, its single output as before. */
const HAS_DEADLINE = '$parameter.due && $parameter.due !== "none" && $parameter.deadline';
const OUTPUTS = `={{ ${HAS_DEADLINE} ? [{ type: 'main', displayName: 'timeout' }, { type: 'main', displayName: 'done' }] : ['main'] }}`;

/** A node parameter, from an execution (item 0) or a webhook (no item). */
function param(ctx: IExecuteFunctions | IWebhookFunctions, name: string, fallback: unknown): unknown {
	return 'getInputData' in ctx ? ctx.getNodeParameter(name, 0, fallback) : ctx.getNodeParameter(name, fallback);
}

export function hasDeadline(ctx: IExecuteFunctions | IWebhookFunctions): boolean {
	return ['for', 'until'].includes(param(ctx, 'due', 'none') as string) && Boolean(param(ctx, 'deadline', false));
}

const [dueMode, dueAmount, dueUnit, dueUntil] = durationProperties('Due', 'due');

const PROPERTIES: INodeProperties[] = [
	{ displayName: 'Title', name: 'title', type: 'string', default: '', required: true,
		placeholder: 'Approve {{amount}} EUR for {{employee}}' },
	{
		displayName: 'Lane',
		name: 'lane',
		type: 'string',
		default: '',
		placeholder: 'managers',
		description: 'Role (Django group) whose members can complete it; empty = anyone',
	},
	{ displayName: 'Description', name: 'description', type: 'string', typeOptions: { rows: 2 }, default: '' },
	{
		displayName: 'Form Fields',
		name: 'form',
		type: 'fixedCollection',
		typeOptions: { multipleValues: true, sortable: true },
		default: {},
		placeholder: 'Add Field',
		options: [
			{
				displayName: 'Field',
				name: 'fields',
				values: [
					{ displayName: 'Name', name: 'name', type: 'string', default: '', required: true },
					{ displayName: 'Label', name: 'label', type: 'string', default: '' },
					{
						displayName: 'Type',
						name: 'type',
						type: 'options',
						default: 'string',
						options: [
							{ name: 'Text', value: 'string' },
							{ name: 'Long Text', value: 'text' },
							{ name: 'Number', value: 'number' },
							{ name: 'Yes/No', value: 'boolean' },
							{ name: 'Choice', value: 'choice' },
							{ name: 'Date', value: 'date' },
							{ name: 'Email', value: 'email' },
						],
					},
					{ displayName: 'Options', name: 'options', type: 'string', default: '', placeholder: 'approve, reject, ask for more',
						description: 'The choices, separated by commas', displayOptions: { show: { type: ['choice'] } } },
					{ displayName: 'Required', name: 'required', type: 'boolean', default: true,
						displayOptions: { hide: { type: ['boolean'] } } },
					{ displayName: 'Help', name: 'help', type: 'string', default: '', description: 'Shown under the field' },
				],
			},
		],
	},
	{
		displayName: 'Context',
		name: 'context',
		type: 'json',
		default: '{}',
		description: 'Data shown to the person, e.g. {"amount": "{{amount}}"}; empty: all the process variables',
	},
	{
		displayName: 'Priority',
		name: 'priority',
		type: 'options',
		default: 'normal',
		description: 'Orders the inbox: overdue first, then by priority, then by due date',
		options: ['low', 'normal', 'high', 'urgent'].map((p) => ({ name: p[0].toUpperCase() + p.slice(1), value: p })),
	},
	{
		...dueMode,
		default: 'none',
		description: 'The SLA: the inbox orders by it and marks the task overdue',
		options: [{ name: 'No Due Date', value: 'none' }, ...(dueMode.options ?? [])],
	},
	dueAmount,
	dueUnit,
	{ ...dueUntil, type: 'string', placeholder: '2026-12-31T17:00 or {{due_date}}',
		description: 'An ISO date (the end of that day) or date-time, or a {{variable}} holding one' },
	{
		displayName: 'Deadline',
		name: 'deadline',
		type: 'boolean',
		default: false,
		displayOptions: { show: { due: ['for', 'until'] } },
		description:
			'Whether the due time interrupts the wait: the tasks expire and the process leaves on "timeout"; completed in time, on "done"',
	},
	{
		displayName: 'Remind After',
		name: 'remindAmount',
		type: 'number',
		typeOptions: { minValue: 0 },
		default: 0,
		description: 'A non-interrupting reminder: if the task still waits after this long, bpmn8n reminds its lane by e-mail (manage.py run_deadlines); the task stays open. 0: none',
	},
	{
		displayName: 'Reminder Unit',
		name: 'remindUnit',
		type: 'options',
		default: 'hours',
		displayOptions: { hide: { remindAmount: [0] } },
		options: Object.keys(UNIT_MS).map((u) => ({ name: u[0].toUpperCase() + u.slice(1), value: u })),
	},
	{
		displayName: 'Escalate To',
		name: 'escalateTo',
		type: 'string',
		default: '',
		displayOptions: { hide: { remindAmount: [0] } },
		placeholder: 'supervisors or /escalate-claims',
		description: "With the reminder: a lane to e-mail instead of this task's, or the /path of an escalation workflow's webhook (started with the case id)",
	},
	{
		displayName: 'Assignee',
		name: 'assignee',
		type: 'string',
		default: '',
		placeholder: 'anna or {{approver}}',
		description: 'Created already claimed by this person (a username, or a {{variable}} holding one) if they may work it; else open to the lane',
	},
	{
		displayName: 'Not Same As',
		name: 'notSameAs',
		type: 'string',
		typeOptions: { multipleValues: true, multipleValueButtonText: 'Add Activity' },
		default: [],
		placeholder: 'Submit Expense',
		description: 'Four-eyes: whoever completed one of these activities (node names) in the same case may not work this one',
	},
	{
		displayName: 'Completion',
		name: 'completion',
		type: 'options',
		default: 'all',
		description: 'With several items (one task each): how many completed tasks resume the process; the others are then cancelled',
		options: [
			{ name: 'All Tasks', value: 'all' },
			{ name: 'Any Task (the First Wins)', value: 'any' },
			{ name: 'At Least', value: 'atLeast' },
		],
	},
	{ displayName: 'Quorum', name: 'quorum', type: 'number', typeOptions: { minValue: 1 }, default: 2,
		displayOptions: { show: { completion: ['atLeast'] } }, description: 'How many completed tasks resume the process' },
	{ displayName: VARIABLES_HINT, name: 'variablesNotice', type: 'notice', default: '' },
];

/** The non-interrupting reminder: when it is due (a time span from now) and who or what it escalates to. */
function remindRequest(ctx: IExecuteFunctions): Partial<UserTaskIn> {
	const amount = ctx.getNodeParameter('remindAmount', 0, 0) as number;
	if (!amount) return {};
	const unit = ctx.getNodeParameter('remindUnit', 0, 'hours') as keyof typeof UNIT_MS;
	return {
		remind_at: new Date(Date.now() + amount * UNIT_MS[unit]).toISOString(),
		escalate_to: ctx.getNodeParameter('escalateTo', 0, '') as string,
	};
}

/** What the node sends bpmn8n besides title, form and context: the SLA, priority and actor rules. */
export function peopleRequest(ctx: IExecuteFunctions): Partial<UserTaskIn> {
	const mode = ctx.getNodeParameter('due', 0, 'none') as string;
	const completion = ctx.getNodeParameter('completion', 0, 'all') as 'all' | 'any' | 'atLeast';
	return {
		priority: ctx.getNodeParameter('priority', 0, 'normal') as UserTaskIn['priority'],
		// a time span ends from now; a date may be a {{variable}}, which bpmn8n fills in from the case
		due: mode === 'none' ? '' : mode === 'until' ? String(ctx.getNodeParameter('dueUntil', 0, '')) : deadline(ctx, 0, 'due').toISOString(),
		deadline: hasDeadline(ctx),
		...remindRequest(ctx),
		assignee: ctx.getNodeParameter('assignee', 0, '') as string,
		not_same_as: ctx.getNodeParameter('notSameAs', 0, []) as string[],
		completion: completion === 'atLeast' ? (ctx.getNodeParameter('quorum', 0, 1) as number) : completion,
	};
}

/** BPMN User Task / Manual Task: a work item in the bpmn8n inbox for a lane (Django group), one per
 * item; the submitted forms are this node's output. Opened, the node links to the pages where people
 * work its tasks: while the editor follows a run, n8n's waiting message links to that run's task;
 * always, a hint links to the inbox (n8n 2.42 hides a waiting node's run when an execution is reopened
 * from the executions list, its own Wait node too). With a deadline (BPMN's interrupting boundary
 * timer) it waits until the due time bpmn8n answers with, then leaves on "timeout". */
export abstract class HumanTaskNode extends WaitingNode {
	protected constructor(kind: 'userTask' | 'manualTask', summary: string) {
		super({
			bpmnType: kind,
			summary,
			subtitle: '={{$parameter["lane"]}}',
			properties: PROPERTIES,
			multiInstance: true,
			outputs: OUTPUTS,
			outputCount: (ctx) => (hasDeadline(ctx) ? 2 : 1),
			route: (ctx) => (hasDeadline(ctx) ? 1 : 0),
			untilRegistered: (ctx, registered) => {
				const due = (registered as Array<{ due?: string | null }>)[0]?.due;
				return hasDeadline(ctx) && due ? new Date(due) : undefined;
			},
			waitingNodeTooltip:
				`=This step waits for a person. <a href="${PAGES}/inbox/?workflow={{ $workflow.id }}` +
				`&execution={{ $execution.id }}" target="_blank">Open the task page</a>`,
			hints: [{
				message: `People work these tasks in the inbox (execution number shown): <a href="${PAGES}/inbox/" target="_blank">open the inbox</a>`,
				type: 'info',
				location: 'outputPane',
				whenToDisplay: 'always',
			}],
			register: (ctx, client, resumeUrl, _until, items) => {
				const context = ctx.getNodeParameter('context', 0) as string | IDataObject;
				return client.post('/api/usertasks', {
					kind,
					title: ctx.getNodeParameter('title', 0) as string,
					lane: ctx.getNodeParameter('lane', 0) as string,
					description: ctx.getNodeParameter('description', 0) as string,
					form: (ctx.getNodeParameter('form', 0) as { fields?: FormField[] }).fields ?? [],
					context: typeof context === 'string' ? JSON.parse(context || '{}') : context ?? {},
					resume_url: resumeUrl,
					items,
					...peopleRequest(ctx),
				} satisfies UserTaskIn);
			},
		});
	}
}
