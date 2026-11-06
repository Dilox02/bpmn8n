// bpmn8n's card on top of a node's panel in the n8n editor (loaded through EXTERNAL_FRONTEND_HOOKS_URLS,
// docker-compose.yml: n8n): what the node is in BPMN terms and what it really runs, by node type. A task:
// its Python function with path:line (read from bpmn8n's OpenAPI, x-bpmn-source), what it does, its inputs
// with where each value comes from, its outputs and examples; a gateway: who decides (Laya or conditions)
// and each branch; a user or manual task: who works it and the form they get; an LLM agent: its provider
// and the only variables it is sent. The node's parameters stay below, to edit as before. The hook's URL
// carries ?bpmn8n=<bpmn8n's public URL>. It also hides n8n's survey and feature-request prompts.
// ponytail: reads the open node from n8n's Pinia stores ("ndv/<workflow>", "workflowDocuments/<workflow>")
// and places the card by data-test-id; an n8n upgrade that renames them only takes the card away.
(() => {
  const BPMN8N = (new URL(document.currentScript?.src || location.href).searchParams.get('bpmn8n') ||
                  'http://localhost:8000').replace(/\/$/, '');
  const style = document.createElement('style');
  style.textContent = `
    [data-test-id=nps-survey-modal], [data-test-id=node-feature-request] { display: none !important; }
    .bpmn8n-card { margin: 0 0 16px; padding: 12px 14px; border-radius: 8px; font-size: 12px; line-height: 1.5;
      background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.10); border-left: 4px solid var(--tint, #888); }
    .bpmn8n-card h4 { margin: 10px 0 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; opacity: .65; }
    .bpmn8n-card .kind { font-weight: 600; font-size: 13px; }
    .bpmn8n-card .agent { margin-left: 6px; padding: 1px 6px; border-radius: 4px; background: var(--tint, #555); color: #fff; font-size: 11px; }
    .bpmn8n-card p { margin: 2px 0; white-space: pre-wrap; }
    .bpmn8n-card code { font-family: ui-monospace, Menlo, monospace; font-size: 11.5px; overflow-wrap: anywhere; }
    .bpmn8n-card details > summary { cursor: pointer; margin: 10px 0 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; opacity: .65; }
    .bpmn8n-card a { color: inherit; text-decoration: underline dotted; }
    .bpmn8n-card table { width: 100%; border-collapse: collapse; }
    .bpmn8n-card td { padding: 2px 6px 2px 0; vertical-align: top; border-top: 1px solid rgba(255,255,255,.06); }
    .bpmn8n-card td:first-child code { overflow-wrap: normal; white-space: nowrap; }  /* field names whole */
    .bpmn8n-card .muted { opacity: .55; }
    .bpmn8n-card .warn { color: #f0b35a; }
  `;
  document.head.appendChild(style);

  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
  const vars = text => [...new Set(String(text ?? '').match(/\{\{\s*[\w.]+\s*\}\}/g) || [])].map(v => v.replace(/\s/g, ''));
  const rows = list => `<table>${list.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</table>`;
  const KINDS = {
    ServiceTask: ['Service task', 'Software calls a service: a Python function on bpmn8n.'],
    ScriptTask: ['Script task', 'Software transforms data: a Python function on bpmn8n.'],
    BusinessRuleTask: ['Business rule task', 'Software applies a policy: a Python function on bpmn8n.'],
    SendTask: ['Send task', 'Software notifies someone: a Python function on bpmn8n.'],
    ReceiveTask: ['Receive task', 'The case waits here for a message.'],
    UserTask: ['User task', 'A person fills in a form in the bpmn8n inbox; the case waits.'],
    ManualTask: ['Manual task', 'A person does it and confirms it in the bpmn8n inbox; the case waits.'],
    ExclusiveGateway: ['Exclusive gateway', 'The case takes one branch.'],
    InclusiveGateway: ['Inclusive gateway', 'The case takes every branch that applies.'],
    ParallelGateway: ['Parallel gateway', 'The case takes every branch at once.'],
    EventBasedGateway: ['Event-based gateway', 'The first message (or the timeout) decides the branch.'],
    TimerEvent: ['Timer event', 'The case waits for a time.'],
    ErrorEndEvent: ['Error end event', 'The case ends with an error.'],
    CallActivity: ['Call activity', 'Runs another workflow as a sub-process.'],
  };

  // n8n's state: the open node, and any node of the workflow by name
  const stores = () => document.querySelector('#app')?.__vue_app__?.config.globalProperties.$pinia?._s;
  const store = prefix => [...(stores()?.entries() || [])].filter(([k]) => k.startsWith(prefix)).map(([, s]) => s);
  const openNode = () => store('ndv/').map(s => s.activeNode).find(Boolean);
  const nodeNamed = name => store('workflowDocuments/').map(s => s.getNodeByName?.(name)).find(Boolean);

  let spec = null, specAt = 0;
  const openapi = async () => {  // refreshed when a panel opens: a new @task shows at once
    if (!spec || Date.now() - specAt > 5000) {
      try { spec = await (await fetch(`${BPMN8N}/api/openapi.json`)).json(); specAt = Date.now(); } catch (_) { spec = spec || {}; }
    }
    return spec;
  };
  const resolve = (schema = {}) => schema.$ref ? resolve(spec.components?.schemas?.[schema.$ref.split('/').pop()])
    : schema.anyOf ? resolve(schema.anyOf.find(s => s.type !== 'null')) : schema;
  const typeOf = s => (s = resolve(s), s.enum ? s.enum.join(' | ') : s.type || 'any');

  const fold = (title, html) => `<details><summary>${esc(title)}</summary>${html}</details>`;  // closed: the parameters stay in view
  const agentOf = notes => (String(notes || '').match(/^(Agent [0-9A-Z]\b[^:.]*)/) || [])[1];
  const header = (node, kind, line) => {
    const agent = agentOf(node.notes);
    return `<div><span class="kind">${esc(kind)}</span>${agent ? `<span class="agent">${esc(agent)}</span>` : ''}</div>
      <p class="muted">${esc(line)}</p>${node.notes ? fold('Notes', `<p>${esc(node.notes)}</p>`) : ''}`;
  };

  async function task(node, [kind, line]) {
    const path = node.parameters.task;
    const op = (await openapi()).paths?.[path]?.post;
    if (!op) return header(node, kind, line) + `<p class="warn">${path ? `${esc(path)}: not on bpmn8n (${esc(BPMN8N)})` : 'Choose a task below.'}</p>`;
    const name = path.split('/').pop(), source = op['x-bpmn-source'] || '';
    const body = resolve(op.requestBody?.content?.['application/json']?.schema);
    const out = resolve(op.responses?.['200']?.content?.['application/json']?.schema);
    const given = node.parameters.input?.value || {}, fromText = node.parameters.inputFrom === 'text';
    const inputs = Object.entries(body.properties || {}).map(([id, s]) => {
      const value = given[id];
      const from = value !== undefined && value !== null && value !== '' ? `<code>${esc(value)}</code>`
        : fromText ? `<span class="muted">extracted from ${esc(node.parameters.text || 'the text')}, else {{${esc(id)}}}</span>`
        : `<span class="muted">← <code>{{${esc(id)}}}</code> (same name)</span>`;
      return [`<code>${esc(id)}</code>${(body.required || []).includes(id) ? '' : ' <span class="muted">optional</span>'}`,
              `<span class="muted">${esc(typeOf(s))}</span>`, from];
    });
    const outputs = Object.entries(out.properties || {}).map(([id, s]) =>
      [`<code>${esc(id)}</code>`, `<span class="muted">${esc(typeOf(s))}</span>`, esc(resolve(s).description || '')]);
    const examples = op['x-bpmn-examples'] || [];
    return header(node, kind, line) + `
      <h4>Implementation</h4>
      <p><code>${esc(name)}()</code> · <a href="${esc(BPMN8N)}/activities/#${esc(name)}" target="_blank"><code>${esc(source)}</code></a></p>
      ${op.description ? `<p>${esc(op.description)}</p>` : ''}
      <h4>Input</h4>${inputs.length ? rows(inputs) : '<p class="muted">none</p>'}
      <h4>Output</h4>${outputs.length ? rows(outputs) : '<p class="muted">not declared</p>'}
      ${examples.length ? fold(`Examples, its tests (${examples.length})`, rows(examples.map(e =>
        [`<code>${esc(JSON.stringify(e.input))}</code>`, '→', `<code>${esc(JSON.stringify(e.output))}</code>`])))
        : '<h4>Examples</h4><p class="muted">none in examples.json: untested</p>'}`;
  }

  function gateway(node, [kind, line]) {
    const p = node.parameters, branches = p.branches?.values || [];
    if (p.decider === 'conditions') {
      return header(node, kind, line) + `<h4>Decided by conditions on the process variables</h4>` +
        rows(branches.map(b => [esc(b.name), `<code>${esc(b.condition)}</code>`])) +
        `<p class="muted">None holds → default output.</p>`;
    }
    return header(node, kind, line) + `
      <h4>Decided by Laya</h4><p>The System One model on this server reads the state and picks the branch that answers the question.</p>
      <p>Question: <b>${esc(p.question)}</b></p>
      <p>State it reads: ${p.state ? `<code>${esc(p.state)}</code>` : '<span class="warn">all the process variables</span>'}</p>
      <h4>Branches</h4>${rows(branches.map(b => [esc(b.name), esc(b.description)]))}
      <p class="muted">Below ${esc(p.minConfidence ?? 0.7)} confidence → default output, for a person to decide.</p>`;
  }

  function human(node, [kind, line]) {
    const p = node.parameters, fields = p.form?.fields || [];
    let context = p.context;
    try { context = typeof context === 'string' ? JSON.parse(context || '{}') : context || {}; } catch (_) { context = {}; }
    const shown = Object.keys(context);
    return header(node, kind, line) + `
      <h4>Who</h4><p>Lane <b>${esc(p.lane || 'anyone')}</b> · <a href="${esc(BPMN8N)}/inbox/" target="_blank">inbox</a></p>
      <h4>What they see</h4><p><b>${esc(p.title)}</b></p>${p.description ? `<p>${esc(p.description)}</p>` : ''}
      ${shown.length ? rows(shown.map(k => [esc(k), `<code>${esc(context[k])}</code>`])) : '<p class="muted">all the process variables</p>'}
      <h4>Form</h4>${fields.length ? rows(fields.map(f => [esc(f.label || f.name), `<code>${esc(f.name)}</code>`,
        `<span class="muted">${esc({ string: 'text', number: 'number', boolean: 'yes/no' }[f.type] || f.type)}</span>`]))
        : '<p class="muted">no fields: the person confirms</p>'}`;
  }

  function agent(node) {
    const prompt = nodeNamed(`${node.name}: prompt`)?.parameters?.input?.value?.prompt ?? node.parameters.text;
    const model = nodeNamed(`${node.name}: model`);
    const local = /localhost|127\.0\.0\.1|host\.docker\.internal|ollama|lmstudio/i.test(JSON.stringify(model?.parameters || {}));
    const sent = vars(prompt);
    return header(node, 'LLM agent', 'Drafts or proposes; a person decides after it.') + `
      <h4>Model</h4><p>${esc(model?.type?.split('.').pop() || 'not connected')} ${esc(model?.parameters?.modelName || model?.parameters?.model || '')}
      ${model ? (local ? '<span class="muted">· on this network</span>' : '<span class="warn">· external provider: what it is sent leaves the company</span>') : ''}</p>
      <h4>The only variables it is sent</h4><p>${sent.length ? sent.map(v => `<code>${esc(v)}</code>`).join(' ') : '<span class="muted">none</span>'}</p>
      ${prompt ? fold('Prompt', `<p class="muted">${esc(prompt)}</p>`) : ''}`;
  }

  function start(node) {
    const path = node.parameters.path || '';
    return header(node, 'Start event', 'A case starts when its data is POSTed here; the JSON body is its first process data.') +
      `<h4>Webhook</h4><p><code>POST ${esc(location.origin)}/webhook/${esc(path)}</code></p>`;
  }

  async function card(node) {
    const kind = Object.keys(KINDS).find(k => node.type === `CUSTOM.bpmn8n${k}`);
    if (kind && 'task' in node.parameters) return task(node, KINDS[kind]);
    if (kind && /Gateway$/.test(kind) && node.parameters.branches) return gateway(node, KINDS[kind]);
    if (kind === 'UserTask' || kind === 'ManualTask') return human(node, KINDS[kind]);
    if (kind) return header(node, ...KINDS[kind]);
    if (node.type === '@n8n/n8n-nodes-langchain.agent') return agent(node);
    if (node.type === 'n8n-nodes-base.webhook') return start(node);
    return null;
  }

  let shown = '';
  async function render() {
    const params = document.querySelector('[data-test-id=ndv] [data-test-id=node-parameters]');
    const node = params && openNode();
    if (!node) return;
    const key = JSON.stringify([node.name, node.parameters, node.notes]);
    if (key === shown && params.querySelector('.bpmn8n-card')) return;
    shown = key;
    const html = await card(node);
    params.querySelector('.bpmn8n-card')?.remove();
    if (!html) return;
    const el = document.createElement('div');
    el.className = 'bpmn8n-card';
    el.style.setProperty('--tint', document.querySelector(
      `[data-test-id=canvas-node][data-node-name="${CSS.escape(node.name)}"]`)?.style.getPropertyValue('--tinta') || '#888');
    el.innerHTML = html;
    params.prepend(el);
  }
  // ponytail: polls while a panel is open (parameters change without DOM events we can rely on)
  setInterval(() => document.querySelector('[data-test-id=ndv]') ? render().catch(() => {}) : (shown = ''), 700);
})();
