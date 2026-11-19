import { NodeOperationError } from 'n8n-workflow';
import { branchOutputs, branchesProperty, readBranches } from './shared/branches';
import { deadline, durationProperties } from './shared/duration';
import type { SubscriptionIn } from './shared/contract';
import { CORRELATION_KEY, SUBSCRIPTIONS } from './shared/messages';
import { WaitingNode } from './shared/WaitingNode';

/** BPMN Event-Based Gateway: a race. The process waits for the first of several messages (one
 * output each) or, if none arrives in time, leaves on "timeout" (output 0: see WaitingNode). The
 * losers are discarded. */
export class Bpmn8nEventBasedGateway extends WaitingNode {
	constructor() {
		super({
			bpmnType: 'eventBasedGateway',
			summary: 'BPMN Event-Based Gateway: the first of several messages wins, or the timeout',
			subtitle: '={{ "first of " + ((($parameter.branches || {}).values || []).map(b => b.name).join(", ")) }}',
			outputs: branchOutputs({ first: 'timeout' }),
			properties: [
				{ ...branchesProperty(), displayName: 'Messages', placeholder: 'Add Message',
					description: 'After "timeout", one output per message name (senders use POST /api/messages/{name})' },
				CORRELATION_KEY,
				...durationProperties('Timeout'),
			],
			until: (ctx) => deadline(ctx),
			outputCount: (ctx) => readBranches(ctx, 'getExecutionId' in ctx ? 0 : undefined).length + 1,
			register: (ctx, client, resumeUrl, until, [item]) =>
				Promise.all(
					readBranches(ctx, 0).map((message) =>
						client.post(SUBSCRIPTIONS, {
							name: message.name,
							correlation_key: String(ctx.getNodeParameter('correlationKey', 0)),
							resume_url: resumeUrl,
							expires: until.toISOString(),
							item,
						} satisfies SubscriptionIn),
					),
				),
			route: (ctx, payload) => {
				const message = readBranches(ctx).findIndex((m) => m.name === payload._message);
				if (message < 0) throw new NodeOperationError(ctx.getNode(), `no output for message "${payload._message}"`);
				return 1 + message; // after "timeout"
			},
		});
	}
}
