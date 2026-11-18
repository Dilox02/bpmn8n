import type { INodeProperties } from 'n8n-workflow';

export const UNIT_MS = { seconds: 1e3, minutes: 6e4, hours: 3.6e6, days: 8.64e7, weeks: 6.048e8 } as const;

type Parameters = { getNodeParameter(name: string, itemIndex: number): unknown };

/** The parameter names of a duration: mode, amount, unit, until; with a prefix (e.g. "due"): due,
 * dueAmount, dueUnit, dueUntil, for a node that has other parameters with those names. */
export function durationNames(prefix?: string) {
	const name = (n: string) => (prefix ? prefix + n[0].toUpperCase() + n.slice(1) : n);
	return { mode: prefix ?? 'mode', amount: name('amount'), unit: name('unit'), until: name('until') };
}

/** "Wait for a time span / until a date" fields, labelled `label` (e.g. "Wait", "Timeout"). */
export function durationProperties(label: string, prefix?: string): INodeProperties[] {
	const names = durationNames(prefix);
	return [
		{
			displayName: label,
			name: names.mode,
			type: 'options',
			default: 'for',
			options: [
				{ name: 'For a Time Span', value: 'for' },
				{ name: 'Until a Date', value: 'until' },
			],
		},
		{ displayName: 'Amount', name: names.amount, type: 'number', typeOptions: { minValue: 0 }, default: 1,
			displayOptions: { show: { [names.mode]: ['for'] } } },
		{
			displayName: 'Unit',
			name: names.unit,
			type: 'options',
			default: 'hours',
			displayOptions: { show: { [names.mode]: ['for'] } },
			options: Object.keys(UNIT_MS).map((u) => ({ name: u[0].toUpperCase() + u.slice(1), value: u })),
		},
		{ displayName: 'Until', name: names.until, type: 'dateTime', default: '', required: true,
			displayOptions: { show: { [names.mode]: ['until'] } } },
	];
}

export const DURATION_SUBTITLE =
	'={{ $parameter.mode === "until" ? "until " + $parameter.until : $parameter.amount + " " + $parameter.unit }}';

/** The moment the configured duration ends. */
export function deadline(ctx: Parameters, i = 0, prefix?: string): Date {
	const names = durationNames(prefix);
	if (ctx.getNodeParameter(names.mode, i) === 'until') return new Date(ctx.getNodeParameter(names.until, i) as string);
	const unit = ctx.getNodeParameter(names.unit, i) as keyof typeof UNIT_MS;
	return new Date(Date.now() + (ctx.getNodeParameter(names.amount, i) as number) * UNIT_MS[unit]);
}
