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
// Colours are bpmn8n's tokens (bpmn8n/static/bpmn8n/tokens.css, mounted as <base>static/bpmn8n-tokens.css and
// linked here once for every hook), each with its old value as fallback. The canvas follows the user's n8n
// theme: in dark mode it is flat and every node a tint from its name; in light mode n8n's own colours.
(() => {
  // n8n's base path: '/', or N8N_PATH behind the one-origin gateway ('/n8n/'); else from this file's own URL
  const BASE = window.BASE_PATH || new URL('../', document.currentScript?.src || location.href).pathname;
  if (!document.querySelector('link[data-bpmn8n-tokens]')) {
    const tokens = Object.assign(document.createElement('link'), { rel: 'stylesheet', href: `${BASE}static/bpmn8n-tokens.css?v=1` });
    tokens.dataset.bpmn8nTokens = '';
    document.head.prepend(tokens);  // first: n8n's and the hooks' own styles come after it
  }
  // a page that embeds the preview forces its theme (?theme=dark|light); the editor follows n8n's
  const forced = new URLSearchParams(location.search).get('theme');
  const dark = forced === 'dark' ? ':root' : forced === 'light' ? null : 'body[data-theme=dark]';
  const ICONS = `${BASE}icons/CUSTOM/nodes/icons/`;
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
            'CallActivity', 'Metrics'),
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
    .vue-flow { --bpmn-stroke: var(--b8-shape-stroke, #22242a); --bpmn-fill: var(--b8-shape-fill, #ffffff); }
    body[data-theme=dark] .vue-flow { --bpmn-stroke: var(--b8-shape-stroke, #e4e4ea); --bpmn-fill: var(--b8-shape-fill, #2b2b2b); }
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

    ${dark === null ? '' : `
    /* dark mode: Studio's look (n8n-editor/preview.js) in the editor too, a flat canvas with no dot grid */
    ${dark} ${onBpmn} { background: var(--b8-canvas, #0d0d12) !important; }
    ${dark} ${onBpmn} [data-test-id=canvas-background] { display: none !important; }

    /* tints (below): a flat colour per node, white marks on it */
    ${dark} ${onBpmn} [data-test-id=canvas-node][style*="--b8-tint"] { --bpmn-fill: var(--b8-tint); }
    :where(${dark} ${onBpmn} [data-test-id=canvas-node][style*="--b8-tint"]) ${box} {  /* :where: gateways, events stay bare */
      background: var(--b8-tint) !important;
    }
    ${dark} ${onBpmn} [style*="--b8-tint"] .n8n-node-icon, ${dark} ${onBpmn} [style*="--b8-tint"] .n8n-node-icon * {
      color: var(--b8-canvas-mark, #f4f4f8) !important;
    }
    ${dark} ${TASKS}[style*="--b8-tint"] ${name} { color: var(--b8-canvas-mark, #f4f4f8) !important; }`}
  `;
  document.head.appendChild(style);

  // every node a tint from its name (the same name, the same tint), in the editor as in Studio's preview;
  // the CSS above draws it in dark mode only, and the node card (panel.js) wears it in both
  const hue = text => {  // the name without the " 2", " 3"... n8n adds to repeated names
    let h = 2166136261;  // FNV-1a: deterministic, well spread
    for (const c of text.replace(/ \d+$/, '')) h = Math.imul(h ^ c.codePointAt(0), 16777619);
    return ((h >>> 0) * 137.508) % 360;  // golden angle: near names drift apart
  };
  const paint = () => document.querySelectorAll('[data-test-id=canvas-node][data-node-name]').forEach(node => {
    const tint = `hsl(${hue(node.dataset.nodeName)} var(--b8-tint-saturation, 45%) var(--b8-tint-lightness, 36%))`;
    if (node.style.getPropertyValue('--b8-tint') !== tint) node.style.setProperty('--b8-tint', tint);
  });
  new MutationObserver(paint).observe(document.documentElement, { childList: true, subtree: true });
})();
