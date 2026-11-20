import { NodeOperationError } from 'n8n-workflow';
import type {
	IDataObject,
	IExecuteFunctions,
	IHttpRequestMethods,
	ILoadOptionsFunctions,
	INodeExecutionData,
	INodeTypeDescription,
	NodeConnectionType,
} from 'n8n-workflow';
import { CONTRACT_VERSION, HEADERS } from './contract';
import { OpenApiSpec, type OpenApiDoc } from './OpenApiSpec';

type Context = IExecuteFunctions | ILoadOptionsFunctions;

export const CREDENTIALS = 'bpmn8nApi';

/** HTTP client for a bpmn8n server, authenticated with the node's bpmn8nApi credentials. */
/** n8n's default (300 s) aborts a task that calls a local LLM for longer, while bpmn8n finishes the work
 * anyway; 15 minutes is gunicorn's graceful timeout (docker-compose.yml), the longest a task can run. */
export const REQUEST_TIMEOUT_MS = 15 * 60 * 1000;

export class Bpmn8nClient {
	private warned = false;

	constructor(private readonly ctx: Context) {}

	async spec(): Promise<OpenApiSpec> {
		return new OpenApiSpec((await this.request('GET', '/api/openapi.json')) as OpenApiDoc);
	}

	/** POSTs `body`; during an execution it also tells bpmn8n which process (workflow), case and
	 * activity (node) the call belongs to, for the event log. The case is "<workflow id>:<execution
	 * id>": execution ids alone repeat across workflows and after a database migration. Values
	 * are URI-encoded, since node and workflow names need not be ASCII. `body` is one of the server's request
	 * shapes (./contract, generated from them), `T` its reply's. */
	async post<T = IDataObject>(path: string, body: object, itemIndex = 0): Promise<T> {
		const ctx = this.ctx;
		const headers: IDataObject =
			'getExecutionId' in ctx
				? {
						[HEADERS.case]: encodeURIComponent(`${ctx.getWorkflow().id}:${ctx.getExecutionId()}`),
						[HEADERS.process]: encodeURIComponent(ctx.getWorkflow().name ?? ''),
						[HEADERS.activity]: encodeURIComponent(ctx.getNode().name),
						[HEADERS.node]: encodeURIComponent(ctx.getNode().id),
						[HEADERS.item]: String(itemIndex),
						[HEADERS.run]: String('getWorkflowDataProxy' in ctx ? ctx.getWorkflowDataProxy(itemIndex).$runIndex ?? '' : ''),
					}
				: {};
		return (await this.request('POST', path, body as IDataObject, headers)) as T;
	}

	/** The reply's body; an error reply fails with bpmn8n's own explanation (its `detail`), which
	 * n8n's generic HTTP error would hide. Every call says which contract the nodes were built for: a server
	 * that speaks another one still answers, with a warning shown once in the node's output. */
	async request(method: IHttpRequestMethods, path: string, body?: IDataObject, headers: IDataObject = {}): Promise<unknown> {
		const { baseUrl } = await this.ctx.getCredentials(CREDENTIALS);
		const reply = (await this.ctx.helpers.httpRequestWithAuthentication.call(this.ctx, CREDENTIALS, {
			method,
			url: String(baseUrl).replace(/\/$/, '') + path,
			body,
			headers: { ...headers, [HEADERS.contract]: CONTRACT_VERSION },
			json: true,
			timeout: REQUEST_TIMEOUT_MS,
			returnFullResponse: true,
			ignoreHttpStatusErrors: true,
		})) as { statusCode: number; body: IDataObject | string; headers?: Record<string, string | undefined> };
		const warning = reply.headers?.[HEADERS.warning.toLowerCase()];
		if (warning && !this.warned && 'addExecutionHints' in this.ctx) {
			this.warned = true;
			this.ctx.addExecutionHints({ message: `bpmn8n: ${warning}`, type: 'warning', location: 'outputPane' });
		}
		if (reply.statusCode >= 400) {
			const detail = typeof reply.body === 'object' ? (reply.body.detail ?? JSON.stringify(reply.body)) : reply.body;
			throw new NodeOperationError(this.ctx.getNode(), `bpmn8n ${method} ${path}: ${reply.statusCode} ${detail}`);
		}
		return reply.body;
	}
}

export interface Routed {
	json: IDataObject;
	outputs: number[];
}

/** Runs `route` for every input item and puts the item on the outputs it returns.
 * With "Continue On Fail" a failing item goes to `errorOutput` carrying the error message; it also carries
 * the error itself, which is how n8n tells it apart when On Error is "Continue (using error output)" and
 * moves it to the node's "error" output (an item with other fields than `error` would stay on `main`). */
export async function routeItems(
	ctx: IExecuteFunctions,
	outputCount: number,
	errorOutput: number,
	route: (item: INodeExecutionData, index: number) => Promise<Routed>,
): Promise<INodeExecutionData[][]> {
	const outputs: INodeExecutionData[][] = Array.from({ length: outputCount }, () => []);
	for (const [index, item] of ctx.getInputData().entries()) {
		try {
			const { json, outputs: targets } = await route(item, index);
			for (const target of targets) outputs[target].push({ json, pairedItem: { item: index } });
		} catch (error) {
			if (!ctx.continueOnFail()) throw error;
			const failure = error instanceof NodeOperationError ? error
				: new NodeOperationError(ctx.getNode(), error as Error, { itemIndex: index });
			outputs[errorOutput].push({
				json: { ...item.json, error: failure.message }, error: failure, pairedItem: { item: index },
			});
		}
	}
	return outputs;
}

/** "serviceTask" -> "Service Task" */
export function titleCase(camel: string): string {
	return camel.replace(/([A-Z])/g, ' $1').replace(/^./, (c) => c.toUpperCase());
}

/** Description shared by every bpmn8n node; `bpmnType` is the BPMN element, e.g. "serviceTask". */
export function bpmnDescription(
	bpmnType: string,
	summary: string,
	rest: Pick<INodeTypeDescription, 'properties'> & Partial<INodeTypeDescription>,
): INodeTypeDescription {
	const displayName = titleCase(bpmnType);
	return {
		displayName,
		name: `bpmn8n${displayName.replace(/ /g, '')}`,
		icon: { light: `file:icons/${bpmnType}.svg`, dark: `file:icons/${bpmnType}.dark.svg` },
		group: ['transform'],
		version: 1,
		description: summary,
		defaults: { name: displayName },
		inputs: ['main' as NodeConnectionType],
		outputs: ['main' as NodeConnectionType],
		credentials: [{ name: CREDENTIALS, required: true }],
		codex: { categories: ['BPMN'], alias: ['bpmn', 'bpmn8n', bpmnType, displayName.toLowerCase()] }, // node search
		...rest,
	};
}
