// Theme of the n8n-preview service (docker-compose.yml): loaded in the editor's <head> through
// EXTERNAL_FRONTEND_HOOKS_URLS, after bpmn.js (the BPMN notation, as in the editor). A canvas without the
// read-only stripes, minimap, node toolbar, issue or trigger marks. ?theme=light (bpmn8n Studio's page in
// light mode) keeps n8n's own colours on the page's well colour; otherwise a dark canvas, every node
// flat in the tint bpmn.js gives it from its name. The embedding page can post
// {command: 'bpmn8nStati', stati: {nodeName: 'attesa' | 'attivo' | 'fatto'}} to dim, pulse or light up nodes.
// ponytail: n8n's class names are hashed per build, so the CSS matches data-test-id and
// [class*=...]; an n8n upgrade that renames them only brings back n8n's own look.
(() => {
  const light = new URLSearchParams(location.search).get('theme') === 'light';
  const live = new URLSearchParams(location.search).has('live');
  try { localStorage.setItem('N8N_THEME', light ? 'light' : 'dark'); } catch (_) { /* private mode: n8n default */ }

  const style = document.createElement('style');
  const chrome = `
    [data-test-id=canvas-background] { display: none !important; }
    [data-test-id=canvas-node-toolbar], [data-test-id=node-issues], [data-test-id=canvas-node-status-warning],
    [class*=_bolt_], ${live ? '' : '[class*=canvasControls], [data-test-id=canvas-controls],'}
    [data-test-id=canvas-minimap], .vue-flow__minimap, [class*=minimap] { display: none !important; }`;
  style.textContent = light ? `body, .vue-flow { background: #f4f6f8 !important; }${chrome}` : `
    body, .vue-flow { background: #0d0d12 !important; }${chrome}
    [data-canvas-node-render-type] {  /* bpmn.js tints them; read-only: no border, no selection */
      border: 0 !important;
      box-shadow: none !important;
      transition: opacity .35s, filter .35s;
    }
    [data-stato=attesa] [data-canvas-node-render-type] { opacity: .3; filter: grayscale(1); }
    [data-stato=attivo] [data-canvas-node-render-type] { animation: bpmn8n-attivo .45s ease-in-out infinite alternate; }
    @keyframes bpmn8n-attivo { to { filter: brightness(1.45); } }
  `;
  document.head.appendChild(style);
  if (live) {
    const tokens = document.createElement('style');
    tokens.textContent = `
      [data-token-state=active] { --token-color: #f5bd69; }
      [data-token-state=ready] { --token-color: #87bafa; }
      [data-token-state=waiting] { --token-color: #d2b2fa; }
      [data-token-state=buffered] { --token-color: #85d8b0; }
      [data-token-state=complete] { --token-color: #85d8b0; }
      [data-token-state=error] { --token-color: #ff9c91; }
      [data-token-state] [data-canvas-node-render-type] { border-color: var(--token-color) !important; }
      :is([data-token-state=active],[data-token-state=ready],[data-token-state=waiting],[data-token-state=error]) [data-canvas-node-render-type] {
        outline: 2px solid var(--token-color); outline-offset: 3px;
      }
      .bpmn8n-token-badges { position: absolute; top: -12px; right: -8px; z-index: 10; display: flex; gap: 4px; }
      .bpmn8n-token-badge { min-width: 22px; height: 22px; padding: 0 5px; border-radius: 12px;
        background: var(--badge-color); color: #15151a; font: 600 12px/22px sans-serif; text-align: center; }
      [data-test-id=canvas-node][data-token-state] { cursor: pointer; }
      @media (prefers-reduced-motion: reduce) { [data-stato=attivo] [data-canvas-node-render-type] { animation: none; } }
    `;
    document.head.appendChild(tokens);
  }

  let stati = {}, tokenStates = {}, drawn = 0;
  // n8n does not fit the view to a workflow it receives: press its own (hidden) fit button when the nodes
  // appear or change, and when the frame changes size
  const fit = () => document.querySelector('[data-test-id=zoom-to-fit]')?.click();
  addEventListener('resize', () => setTimeout(fit, 150));
  const colora = () => {
    const nodes = document.querySelectorAll('[data-test-id=canvas-node][data-node-name]');
    if (nodes.length !== drawn) {
      drawn = nodes.length; setTimeout(fit, 300);
      if (live && drawn) parent.postMessage({ command: 'bpmn8nCanvasReady', count: drawn }, '*');
    }
    nodes.forEach(nodo => {
    const stato = stati[nodo.dataset.nodeName];
    if (stato) nodo.dataset.stato = stato; else delete nodo.dataset.stato;
    const token = tokenStates[nodo.dataset.nodeName];
    if (token) nodo.dataset.tokenState = token.state; else delete nodo.dataset.tokenState;
    let badges = nodo.querySelector('.bpmn8n-token-badges');
    if (token?.count) {
      if (!badges) { badges = document.createElement('span'); badges.className = 'bpmn8n-token-badges'; nodo.append(badges); }
      const counts = token.counts || { [token.state]: token.count };
      const colors = { active: '#f5bd69', ready: '#87bafa', waiting: '#d2b2fa', buffered: '#85d8b0' };
      for (const state of Object.keys(colors)) {
        let badge = badges.querySelector(`[data-token-count="${state}"]`);
        if (counts[state]) {
          if (!badge) { badge = document.createElement('span'); badge.className = 'bpmn8n-token-badge'; badge.dataset.tokenCount = state; badge.style.setProperty('--badge-color', colors[state]); badges.append(badge); }
          if (badge.textContent !== String(counts[state])) badge.textContent = String(counts[state]);
          badge.title = `${counts[state]} token · ${state}`;
        } else badge?.remove();
      }
    } else badges?.remove();
    });
  };
  new MutationObserver(colora).observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener('message', e => {
    if (e.source !== parent) return;
    let m; try { m = typeof e.data === 'string' ? JSON.parse(e.data) : e.data; } catch (_) { return; }
    if (m && m.command === 'bpmn8nStati') { stati = m.stati || {}; colora(); }
    if (m && m.command === 'bpmn8nTokens') { tokenStates = m.states || {}; colora(); }
  });
  if (live) document.addEventListener('click', event => {
    const node = event.target.closest('[data-test-id=canvas-node][data-node-name]');
    if (node) parent.postMessage({ command: 'bpmn8nSelectNode', nodeName: node.dataset.nodeName }, '*');
  });
})();
