import type { IDataObject } from 'n8n-workflow';

const TEMPLATE = /\{\{\s*([\w.]+)\s*\}\}/g;

function lookup(item: IDataObject, path: string): unknown {
	return path.split('.').reduce<unknown>((value, key) => (value as IDataObject | undefined)?.[key], item);
}

/** A mapped value for one item: a lone "{{variable}}" is that variable's value (any type); text with templates
 * has them filled in; a missing variable leaves the field out. */
function render(value: unknown, item: IDataObject): unknown {
	if (typeof value !== 'string') return value;
	const lone = value.trim().match(/^\{\{\s*([\w.]+)\s*\}\}$/);
	if (lone) return lookup(item, lone[1]);
	return value.replace(TEMPLATE, (_, path: string) => String(lookup(item, path) ?? ''));
}

/** What the sub-process gets from an item: only the mapped fields, {child variable: "{{parent variable}}"}.
 * ponytail: the parent's variables are read from the incoming item (n8n's data), not from bpmn8n's case store. */
export function mapInputs(item: IDataObject, inputs: Record<string, unknown>): IDataObject {
	const mapped: IDataObject = {};
	for (const [name, template] of Object.entries(inputs)) {
		const value = render(template, item);
		if (value !== undefined) mapped[name] = value as IDataObject[string];
	}
	return mapped;
}

/** The parent item with only the listed outputs of the sub-process's item merged in. Pairing: the sub-process
 * item i goes with parent item i (or the first one when it returns more items than the node got). */
export function mergeOutputs(parents: IDataObject[], results: IDataObject[], outputs: string[]): IDataObject[] {
	return results.map((result, i) => ({
		...(parents[i] ?? parents[0] ?? {}),
		...Object.fromEntries(outputs.filter((name) => name in result).map((name) => [name, result[name]])),
	}));
}
