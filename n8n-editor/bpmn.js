// BPMN 2.0 notation on n8n's canvas, as bpmn.io draws it: loaded in the editor's <head> through
// EXTERNAL_FRONTEND_HOOKS_URLS (docker-compose.yml: n8n and n8n-preview). bpmn8n's tasks are rounded
// rectangles with their type marker top left and their name inside (a call activity: a thick border and
// the [+] marker), gateways diamonds, events circles. On a canvas with bpmn8n nodes, n8n's own nodes join
// the notation: a trigger is a start event (a webhook a message start event), a Merge the gateway that
// joins the branches, any other node a task with its icon as marker. Glyphs: bpmn-font
// (tools/build_icons.py). The nodes keep n8n's 96 px square, so the layout and the flows stay n8n's; the
// handles move onto the shape's outline, where the flows then start and end.
// ponytail: n8n's class names are hashed per build, so the CSS matches data-* attributes and
// [class*=...]; an n8n upgrade that renames them only brings back n8n's own look.
(() => {
  // the editor (no ?theme, which only Studio's preview gets) in n8n's dark theme, as in Studio's preview:
  // light marks and names on the dark canvas. ponytail: overrides the user's n8n theme on every load
  if (!new URLSearchParams(location.search).has('theme')) {
    try { localStorage.setItem('N8N_THEME', 'dark'); } catch (_) { /* private mode: n8n's default */ }
  }
  const ICONS = '/icons/CUSTOM/nodes/icons/';
  const SIDE = 96, TASK = 120, DIAMOND = 64, CIRCLE = 44;  // px: BPMN's 100x80 task, 50 gateway, 36 event, scaled
  const type = t => `[data-test-id=canvas-node][data-node-type="${t}"]`;
  const bpmn = (...kinds) => kinds.map(k => type(`CUSTOM.bpmn8n${k}`));
  const onBpmn = '.vue-flow:has([data-node-type^="CUSTOM.bpmn8n"])';  // a bpmn8n workflow's canvas
  const any = list => `:is(${list.join(', ')})`;

  const trigger = ':has([data-test-id=canvas-trigger-node])';
  const START = {  // n8n trigger -> BPMN start event (any other trigger: none)
    'n8n-nodes-base.webhook': 'start-event-message', 'n8n-nodes-base.formTrigger': 'start-event-message',
    'n8n-nodes-base.scheduleTrigger': 'start-event-timer', 'n8n-nodes-base.executeWorkflowTrigger': 'start-event-none',
  };
  const MERGE = `${onBpmn} ${type('n8n-nodes-base.merge')}`;
  const TASKS = any([
    ...bpmn('ServiceTask', 'ScriptTask', 'BusinessRuleTask', 'SendTask', 'ReceiveTask', 'UserTask', 'ManualTask',
            'CallActivity'),
    // n8n's own nodes, but not its AI nodes' shapes (an agent with its sub-nodes, the sub-nodes' circles)
    `${onBpmn} [data-test-id=canvas-node]:not([data-node-type^="CUSTOM.bpmn8n"], [data-node-type="n8n-nodes-base.merge"], ` +
    `[data-node-type="n8n-nodes-base.stickyNote"])${':not(' + trigger + ')'}:not(:has([class*=_configurable_], [class*=_configuration_]))`,
  ]);
  const GATEWAYS = any([...bpmn('ExclusiveGateway', 'InclusiveGateway', 'ParallelGateway', 'EventBasedGateway'), MERGE]);
  const STARTS = `${onBpmn} [data-test-id=canvas-node]${trigger}`;
  const EVENTS = any([...bpmn('TimerEvent', 'ErrorEndEvent'), STARTS]);

  const box = '[data-canvas-node-render-type]', icon = '.n8n-node-icon [class*=_nodeIconWrapper_]';
  const label = '[class*=_description_]', name = `${label} [class*=_label_]`, subtitle = `${label} [class*=_subtitle_]`;
  const size = px => `width: ${px}px !important; height: ${px}px !important; font-size: ${px}px !important; line-height: ${px}px !important;`;

  // a gateway's handles on its diamond: n8n puts the i-th of n at top = 100 / (n + 1) * (i + 1) %, and Chrome
  // writes it back to the style attribute rounded (33.3333%), so the rule matches its first digits
  const onDiamond = side => {
    const rules = [];
    for (let n = 1; n <= 8; n++) {
      for (let i = 0; i < n; i++) {
        const top = (100 / (n + 1)) * (i + 1);
        const match = Number.isInteger(top) ? `${top}%` : `${Math.trunc(top * 10) / 10}`;
        const inset = (SIDE - DIAMOND) / 2 + Math.min(Math.abs(top / 100 * SIDE - SIDE / 2), DIAMOND / 2);
        rules.push(`${GATEWAYS} .vue-flow__handle-${side}[style*="top: ${match}"] { ${side}: ${inset}px !important; }`);
      }
    }
    return rules.join('\n');
  };

  const style = document.createElement('style');
  style.textContent = `
    .vue-flow { --bpmn-stroke: #22242a; --bpmn-fill: #ffffff; }
    body[data-theme=dark] .vue-flow { --bpmn-stroke: #e4e4ea; --bpmn-fill: #2b2b2b; }
    ${onBpmn} { --canvas-edge--color: var(--bpmn-stroke); }  /* sequence flows: n8n still colours runs and hovers */
    ${onBpmn} [data-test-id=canvas-node] ${subtitle} { display: none; }  /* BPMN labels a shape with its name only */
    ${onBpmn} .vue-flow__node:not(:hover) [class*=_dot_] { opacity: 0; }  /* n8n's handles, shown to connect */
    ${any([STARTS, MERGE])} .vue-flow__handle [class*=_label_] { display: none; }  /* POST, Input 1... */

    /* tasks */
    ${TASKS} ${box} {
      --canvas-node--width: ${TASK}px !important; border: var(--bpmn-border, 2px) solid var(--bpmn-stroke) !important;
      border-radius: 12px !important; background: var(--bpmn-fill) !important;
    }
    ${TASKS} .n8n-node-icon { position: absolute; top: 6px; left: 8px; }
    ${TASKS} ${icon} { ${size(20)} }
    ${TASKS} ${label} {
      inset: 0 !important; width: auto !important; min-width: 0 !important; margin: 0 !important; padding: 24px 8px 8px;
      justify-content: center; align-items: center; pointer-events: none;
    }
    ${TASKS} ${name} {
      width: 100% !important; font-size: 12px !important; line-height: 15px !important; color: var(--bpmn-stroke);
      display: -webkit-box !important; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
    }
    ${type('CUSTOM.bpmn8nCallActivity')} { --bpmn-border: 5px; }  /* a variable: the tasks' rule is more specific */
    ${type('CUSTOM.bpmn8nCallActivity')} .n8n-node-icon { display: none; }
    ${type('CUSTOM.bpmn8nCallActivity')} ${box}::before {
      content: ''; position: absolute; bottom: 4px; left: calc(50% - 7px); width: 14px; height: 14px;
      background: url(${ICONS}bpmn/activity-marker/sub-process-marker.svg) center / contain no-repeat;
    }
    body[data-theme=dark] ${type('CUSTOM.bpmn8nCallActivity')} ${box}::before { filter: invert(1); }

    /* gateways and events: the glyph is the shape, on a transparent node */
    ${any([GATEWAYS, EVENTS])} ${box} {
      background: transparent !important; border-color: transparent !important; border-radius: 50% !important;
    }
    ${GATEWAYS} ${box} { --canvas-node--height: ${SIDE}px !important; }  /* square, whatever its outputs */
    ${GATEWAYS} ${icon} { ${size(DIAMOND)} }
    ${EVENTS} ${icon} { ${size(CIRCLE)} }
    ${GATEWAYS} ${label} { top: ${(SIDE + DIAMOND) / 2}px !important; }
    ${EVENTS} ${label} { top: ${(SIDE + CIRCLE) / 2}px !important; }
    ${EVENTS} .vue-flow__handle-left { left: ${(SIDE - CIRCLE) / 2}px !important; }
    ${EVENTS} .vue-flow__handle-right { right: ${(SIDE - CIRCLE) / 2}px !important; }
    ${onDiamond('left')}
    ${onDiamond('right')}

    /* n8n's own nodes drawn with a bpmn-font glyph instead of their icon */
    ${any([MERGE, STARTS])} ${icon} > * { visibility: hidden; }
    ${any([MERGE, STARTS])} ${icon} { background: url(${ICONS}bpmn/events/start-event-none.svg) center / contain no-repeat; }
    ${MERGE} ${icon} { background-image: url(${ICONS}bpmn/gateways/gateway-or.svg); }
    ${Object.entries(START).map(([t, glyph]) =>
      `${onBpmn} ${type(t)} ${icon} { background-image: url(${ICONS}bpmn/events/${glyph}.svg); }`).join('\n')}
    body[data-theme=dark] ${any([MERGE, STARTS])} ${icon} { filter: invert(1); }

    /* Studio's look (n8n-editor/preview.js) in the editor too: a flat dark canvas, no dot grid */
    ${onBpmn} { background: #0d0d12 !important; }
    ${onBpmn} [data-test-id=canvas-background] { display: none !important; }

    /* tints (below): a flat colour per node, white marks on it */
    ${onBpmn} [data-test-id=canvas-node][style*="--tinta"] { --bpmn-fill: var(--tinta); }
    :where(${onBpmn} [data-test-id=canvas-node][style*="--tinta"]) ${box} {  /* :where: gateways, events stay bare */
      background: var(--tinta) !important;
    }
    ${onBpmn} [style*="--tinta"] .n8n-node-icon, ${onBpmn} [style*="--tinta"] .n8n-node-icon * { color: #f4f4f8 !important; }
    ${TASKS}[style*="--tinta"] ${name} { color: #f4f4f8 !important; }
  `;
  document.head.appendChild(style);

  // every node a flat tint from its name (the same name, the same tint), in the editor as in Studio's
  // preview; ?theme=light (Studio's page in light mode) keeps n8n's own colours
  if (new URLSearchParams(location.search).get('theme') === 'light') return;
  const hue = text => {  // the name without the " 2", " 3"... n8n adds to repeated names
    let h = 2166136261;  // FNV-1a: deterministic, well spread
    for (const c of text.replace(/ \d+$/, '')) h = Math.imul(h ^ c.codePointAt(0), 16777619);
    return ((h >>> 0) * 137.508) % 360;  // golden angle: near names drift apart
  };
  const paint = () => document.querySelectorAll('[data-test-id=canvas-node][data-node-name]').forEach(node => {
    const tint = `hsl(${hue(node.dataset.nodeName)} 45% 36%)`;  // dark enough for white marks, clear of #0d0d12
    if (node.style.getPropertyValue('--tinta') !== tint) node.style.setProperty('--tinta', tint);
  });
  new MutationObserver(paint).observe(document.documentElement, { childList: true, subtree: true });
})();
