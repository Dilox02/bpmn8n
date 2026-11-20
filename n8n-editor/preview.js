// Theme of the n8n-preview service (docker-compose.yml): loaded in the editor's <head> through
// EXTERNAL_FRONTEND_HOOKS_URLS, after bpmn.js (the BPMN notation and bpmn8n's tokens, as in the editor). A
// canvas without the read-only stripes, minimap, node toolbar, issue or trigger marks. ?theme=light (an
// embedding page in light mode) keeps n8n's own colours on the page's well colour; otherwise a dark canvas,
// every node flat in the tint bpmn.js gives it from its name. The embedding page can post
// {command: 'bpmn8nStates', states: {nodeName: 'waiting' | 'active' | 'done'}} to dim the nodes a case has
// not reached, pulse the ones where it is now and light up the ones it passed (bpmn8n/web/vocab.py).
// ponytail: n8n's class names are hashed per build, so the CSS matches data-test-id and
// [class*=...]; an n8n upgrade that renames them only brings back n8n's own look.
(() => {
  const light = new URLSearchParams(location.search).get('theme') === 'light';
  const live = new URLSearchParams(location.search).has('live');
  try { localStorage.setItem('N8N_THEME', light ? 'light' : 'dark'); } catch (_) { /* private mode: n8n default */ }

  const style = document.createElement('style');
  style.textContent = `
    [data-test-id=canvas-background] { display: none !important; }
    [data-test-id=canvas-node-toolbar], [data-test-id=node-issues], [data-test-id=canvas-node-status-warning],
    [class*=_bolt_], ${live ? '' : '[class*=canvasControls], [data-test-id=canvas-controls],'}
    [data-test-id=canvas-minimap], .vue-flow__minimap, [class*=minimap] { display: none !important; }
    [data-canvas-node-render-type] { transition: opacity .35s, filter .35s; }
    [data-state=waiting] [data-canvas-node-render-type] { opacity: .3; filter: grayscale(1); }
    [data-state=active] [data-canvas-node-render-type] {  /* the legend's orange, in either theme */
      outline: 4px solid var(--b8-accent, #ff6900) !important; outline-offset: 4px;
      animation: bpmn8n-active .6s ease-in-out infinite alternate;
    }
    @keyframes bpmn8n-active { to { outline-color: transparent; } }
    @media (prefers-reduced-motion: reduce) { [data-state=active] [data-canvas-node-render-type] { animation: none; } }
    ${light ? `body, .vue-flow { background: var(--b8-canvas, #f4f6f8) !important; }` : `
    body, .vue-flow { background: var(--b8-canvas, #0d0d12) !important; }
    [data-canvas-node-render-type] {  /* bpmn.js tints them; read-only: no border, no selection */
      border: 0 !important;
      box-shadow: none !important;
    }`}
  `;
  document.head.appendChild(style);
  const TOKENS = {  // a live token's state -> its colour (n8n-editor/live.mjs names the states)
    active: 'var(--b8-token-active, #f5bd69)', ready: 'var(--b8-token-ready, #87bafa)',
    waiting: 'var(--b8-token-waiting, #d2b2fa)', buffered: 'var(--b8-token-done, #85d8b0)',
  };
  if (live) {
    const tokens = document.createElement('style');
    tokens.textContent = `
      ${Object.entries(TOKENS).map(([state, color]) => `[data-token-state=${state}] { --token-color: ${color}; }`).join('\n')}
      [data-token-state=complete] { --token-color: var(--b8-token-done, #85d8b0); }
      [data-token-state=error] { --token-color: var(--b8-token-error, #ff9c91); }
      [data-token-state] [data-canvas-node-render-type] { border-color: var(--token-color) !important; }
      :is([data-token-state=active],[data-token-state=ready],[data-token-state=waiting],[data-token-state=error]) [data-canvas-node-render-type] {
        outline: 2px solid var(--token-color); outline-offset: 3px;
      }
      .bpmn8n-token-badges { position: absolute; top: -12px; right: -8px; z-index: 10; display: flex; gap: 4px; }
      .bpmn8n-token-badge { min-width: 22px; height: 22px; padding: 0 5px; border-radius: 12px;
        background: var(--badge-color); color: var(--b8-token-ink, #15151a); font: 600 12px/22px var(--b8-font, sans-serif); text-align: center; }
      [data-test-id=canvas-node][data-token-state] { cursor: pointer; }
    `;
    document.head.appendChild(tokens);
  }

  let states = {}, tokenStates = {}, drawn = 0;
  // n8n does not fit the view to a workflow it receives: press its own (hidden) fit button when the nodes
  // appear or change, and when the frame changes size
  const fit = () => document.querySelector('[data-test-id=zoom-to-fit]')?.click();
  addEventListener('resize', () => setTimeout(fit, 150));
  const paint = () => {
    const nodes = document.querySelectorAll('[data-test-id=canvas-node][data-node-name]');
    if (nodes.length !== drawn) {
      drawn = nodes.length; setTimeout(fit, 300);
      if (live && drawn) parent.postMessage({ command: 'bpmn8nCanvasReady', count: drawn }, '*');
    }
    nodes.forEach(node => {
      const state = states[node.dataset.nodeName];
      if (state) node.dataset.state = state; else delete node.dataset.state;
      const token = tokenStates[node.dataset.nodeName];
      if (token) node.dataset.tokenState = token.state; else delete node.dataset.tokenState;
      let badges = node.querySelector('.bpmn8n-token-badges');
      if (token?.count) {
        if (!badges) { badges = document.createElement('span'); badges.className = 'bpmn8n-token-badges'; node.append(badges); }
        const counts = token.counts || { [token.state]: token.count };
        for (const [state, color] of Object.entries(TOKENS)) {
          let badge = badges.querySelector(`[data-token-count="${state}"]`);
          if (counts[state]) {
            if (!badge) { badge = document.createElement('span'); badge.className = 'bpmn8n-token-badge'; badge.dataset.tokenCount = state; badge.style.setProperty('--badge-color', color); badges.append(badge); }
            if (badge.textContent !== String(counts[state])) badge.textContent = String(counts[state]);
            badge.title = `${counts[state]} token · ${state}`;
          } else badge?.remove();
        }
      } else badges?.remove();
    });
  };
  new MutationObserver(paint).observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener('message', e => {
    if (e.source !== parent) return;
    let m; try { m = typeof e.data === 'string' ? JSON.parse(e.data) : e.data; } catch (_) { return; }
    if (m && m.command === 'bpmn8nStates') { states = m.states || {}; paint(); }
    if (m && m.command === 'bpmn8nTokens') { tokenStates = m.states || {}; paint(); }
  });
  if (live) document.addEventListener('click', event => {
    const node = event.target.closest('[data-test-id=canvas-node][data-node-name]');
    if (node) parent.postMessage({ command: 'bpmn8nSelectNode', nodeName: node.dataset.nodeName }, '*');
  });
})();
