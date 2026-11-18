import { NodeOperationError } from 'n8n-workflow';
import type {
	IDataObject,
	IExecuteFunctions,
	INodeProperties,
	INodeType,
	INodeTypeDescription,
	IWebhookFunctions,
	IWebhookResponseData,
} from 'n8n-workflow';
import { Bpmn8nClient, bpmnDescription } from './Bpmn8nClient';

const WAIT_INDEFINITELY = new Date('3000-01-01T00:00:00.000Z'); // same sentinel as n8n-workflow

export interface WaitingSpec {
	bpmnType: string;
	summary: string;
	subtitle: string;
	properties: INodeProperties[];
	/** Tells bpmn8n what the execution now waits on, and where to resume it. */
	register: (
		ctx: IExecuteFunctions,
		client: Bpmn8nClient,
		resumeUrl: string,
		until: Date,
		items: IDataObject[], // the items that reached the node: they become process data
	) => Promise<unknown>;
	/** One wait per item (BPMN multi-instance: bpmn8n resumes once all are done); else one item only. */
	multiInstance?: boolean;
	/** What n8n shows when the node is opened while the execution waits on it (HTML, an expression). */
	waitingNodeTooltip?: string;
	/** Notes n8n shows in the node's panels (static HTML). */
	hints?: INodeTypeDescription['hints'];
	/** When to stop waiting (then the items leave on the first output); default never. */
	until?: (ctx: IExecuteFunctions) => Date;
	/** Or the moment bpmn8n answered the registration with (a due date it filled in from the case);
	 * undefined keeps `until`. */
	untilRegistered?: (ctx: IExecuteFunctions, registered: unknown) => Date | undefined;
	outputs?: INodeTypeDescription['outputs'];
	outputCount?: (ctx: IExecuteFunctions | IWebhookFunctions) => number;
	/** The output a resume (the posted payload) leaves on; default the first. */
	route?: (ctx: IWebhookFunctions, payload: Record<string, unknown>) => number;
}

/** A BPMN element the process waits on (people, a message, the first of several messages).
 * On execution it registers with bpmn8n together with its resume URL and pauses; bpmn8n calls
 * the URL when the wait is over and the posted payload becomes this node's output: one item, or
 * one per entry of `_items` (the tasks of a multi-instance wait, in the order of the items). */
export abstract class WaitingNode implements INodeType {
	description: INodeTypeDescription;
	execute: INodeType['execute'];
	webhook: INodeType['webhook'];

	protected constructor(spec: WaitingSpec) {
		const count = (ctx: IExecuteFunctions | IWebhookFunctions) => spec.outputCount?.(ctx) ?? 1;
		this.description = bpmnDescription(spec.bpmnType, spec.summary, {
			subtitle: spec.subtitle,
			webhooks: [{ name: 'default', httpMethod: 'POST', responseMode: 'onReceived', path: '', restartWebhook: true }],
			properties: spec.properties,
			...(spec.waitingNodeTooltip && { waitingNodeTooltip: spec.waitingNodeTooltip }),
			...(spec.hints && { hints: spec.hints }),
			...(spec.outputs && { outputs: spec.outputs }),
		});
		this.execute = async function (this: IExecuteFunctions) {
			const items = this.getInputData().map((item) => item.json);
			if (items.length > 1 && !spec.multiInstance) {
				throw new NodeOperationError(this.getNode(), `${items.length} items arrived: one wait resumes one execution`, {
					description: 'Split the items upstream (e.g. Loop Over Items) to wait once per item.',
				});
			}
			const until = spec.until?.(this) ?? WAIT_INDEFINITELY;
			const resumeUrl = new URL(this.evaluateExpression('{{ $execution.resumeUrl }}', 0) as string);
			// n8n reuses its URL across waits; each node run needs its own retry identity.
			resumeUrl.searchParams.set('bpmn8n_wait', `${this.getNode().id}:${this.evaluateExpression('{{ $runIndex }}', 0)}`);
			const registered = await spec.register(this, new Bpmn8nClient(this), resumeUrl.toString(), until, items.length ? items : [{}]);
			await this.putExecutionToWait(spec.untilRegistered?.(this, registered) ?? until);
			// When `until` passes, n8n re-runs this node *disabled*, which passes the items through its
			// first output (workflow-execute handleWaitingState): so "timed out" is always output 0.
			const outputs = Array.from({ length: count(this) }, () => [] as ReturnType<IExecuteFunctions['getInputData']>);
			outputs[0] = this.getInputData();
			return outputs;
		};
		this.webhook = async function (this: IWebhookFunctions): Promise<IWebhookResponseData> {
			const payload = this.getBodyData();
			const items = (Array.isArray(payload._items) ? payload._items : [payload]) as IDataObject[];
			const outputs = Array.from({ length: count(this) }, () => [] as Array<{ json: IDataObject }>);
			outputs[spec.route?.(this, payload) ?? 0] = items.map((json) => ({ json }));
			return { workflowData: outputs };
		};
	}
}
