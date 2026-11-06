import type { FieldType, INodePropertyOptions, ResourceMapperField } from 'n8n-workflow';

export interface JsonSchema {
	type?: string;
	format?: string;
	title?: string;
	enum?: Array<string | number | boolean>;
	default?: string | number | boolean | null;
	anyOf?: JsonSchema[];
	$ref?: string;
	properties?: Record<string, JsonSchema>;
	required?: string[];
}

interface Operation {
	summary?: string;
	description?: string;
	'x-bpmn-task'?: string;
	'x-bpmn-source'?: string; // path:line of the Python function
	requestBody?: { content?: { 'application/json'?: { schema?: JsonSchema } } };
}

export interface OpenApiDoc {
	paths: Record<string, { post?: Operation }>;
	components?: { schemas?: Record<string, JsonSchema> };
}

/** Reads the bpmn8n OpenAPI document: which endpoints are BPMN tasks, and what they take. */
export class OpenApiSpec {
	constructor(private readonly doc: OpenApiDoc) {}

	/** POST endpoints tagged `x-bpmn-task: <kind>`, as dropdown options whose value is the path and whose
	 * description says what the function does and where it is written. */
	tasks(kind: string): INodePropertyOptions[] {
		return Object.entries(this.doc.paths).flatMap(([path, { post }]) =>
			post?.['x-bpmn-task'] === kind
				? [{
						name: post.summary ?? path,
						value: path,
						description: [post.description?.split('\n')[0], post['x-bpmn-source']].filter(Boolean).join(' · '),
					}]
				: [],
		);
	}

	/** Request-body properties of the task at `path`, as resource-mapper fields. All optional
	 * (an empty field takes the process variable with its name) and text, so that {{variable}}
	 * templates fit numbers and yes/no too; choices keep their dropdown. */
	fields(path: string): ResourceMapperField[] {
		const body = this.resolve(this.doc.paths[path]?.post?.requestBody?.content?.['application/json']?.schema);
		return Object.entries(body.properties ?? {}).map(([id, raw]) => {
			const schema = this.resolve(raw);
			return {
				id,
				displayName: schema.title ?? id,
				required: false,
				defaultMatch: false,
				display: true,
				type: schema.enum ? ('options' as FieldType) : ('string' as FieldType),
				options: schema.enum?.map((value) => ({ name: String(value), value })),
			};
		});
	}

	/** Follows `$ref`s and unwraps `Optional[X]` (anyOf with null). */
	private resolve(schema?: JsonSchema): JsonSchema {
		if (!schema) return {};
		if (schema.$ref) return this.resolve(this.doc.components?.schemas?.[schema.$ref.split('/').pop()!]);
		if (schema.anyOf) return this.resolve(schema.anyOf.find((s) => s.type !== 'null'));
		return schema;
	}
}
