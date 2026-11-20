import type { SubscriptionIn } from './shared/contract';
import { SUBSCRIPTIONS } from './shared/messages';
import { WaitingNode } from './shared/WaitingNode';

/** BPMN Signal Catch Event: waits for a named signal, with no correlation key. One
 * POST /api/signals/<signal> {"payload": {...}} resumes every execution waiting for it (a message resumes
 * one); a Signal Throw step does the same from a process. Signals are not queued: a catch that starts
 * waiting after the throw does not get it. */
export class Bpmn8nSignalCatch extends WaitingNode {
	constructor() {
		super({
			bpmnType: 'signalCatch',
			summary: 'BPMN Signal Catch Event: waits for a signal that one throw delivers to every waiting process',
			subtitle: '={{$parameter["signal"]}}',
			properties: [
				{
					displayName: 'Signal',
					name: 'signal',
					type: 'string',
					default: '',
					required: true,
					placeholder: 'market-closed',
					description: 'Senders deliver it with POST /api/signals/{signal}, or a Signal Throw step',
				},
			],
			register: (ctx, client, resumeUrl, _until, [item]) =>
				client.post(SUBSCRIPTIONS, {
					item,
					name: ctx.getNodeParameter('signal', 0) as string,
					correlation_key: '',
					resume_url: resumeUrl,
					broadcast: true,
				} satisfies SubscriptionIn),
		});
	}
}
