import type { SubscriptionIn } from './shared/contract';
import { CORRELATION_KEY, SUBSCRIPTIONS } from './shared/messages';
import { WaitingNode } from './shared/WaitingNode';

/** BPMN Receive Task: waits until another system posts the message to
 * POST /api/messages/<message> {"correlation_key": ..., "payload": {...}}. */
export class Bpmn8nReceiveTask extends WaitingNode {
	constructor() {
		super({
			bpmnType: 'receiveTask',
			summary: 'BPMN Receive Task: waits for a message from another system (e.g. payment confirmed)',
			subtitle: '={{$parameter["message"]}}',
			properties: [
				{
					displayName: 'Message',
					name: 'message',
					type: 'string',
					default: '',
					required: true,
					placeholder: 'payment-confirmed',
					description: 'Senders deliver it with POST /api/messages/{message}',
				},
				CORRELATION_KEY,
			],
			register: (ctx, client, resumeUrl, _until, [item]) =>
				client.post(SUBSCRIPTIONS, {
					item,
					name: ctx.getNodeParameter('message', 0) as string,
					correlation_key: String(ctx.getNodeParameter('correlationKey', 0)),
					resume_url: resumeUrl,
				} satisfies SubscriptionIn),
		});
	}
}
