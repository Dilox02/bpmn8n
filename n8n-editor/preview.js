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
  try { localStorage.setItem('N8N_THEME', light ? 'light' : 'dark'); } catch (_) { /* private mode: n8n default */ }

  const style = document.createElement('style');
  const chrome = `
    [data-test-id=canvas-background] { display: none !important; }
    [data-test-id=canvas-node-toolbar], [data-test-id=node-issues], [data-test-id=canvas-node-status-warning],
    [class*=_bolt_], [class*=canvasControls], [data-test-id=canvas-controls],
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

  let stati = {}, drawn = 0;
  // n8n does not fit the view to a workflow it receives: press its own (hidden) fit button when the nodes
  // appear or change, and when the frame changes size
  const fit = () => document.querySelector('[data-test-id=zoom-to-fit]')?.click();
  addEventListener('resize', () => setTimeout(fit, 150));
  const colora = () => {
    const nodes = document.querySelectorAll('[data-test-id=canvas-node][data-node-name]');
    if (nodes.length !== drawn) { drawn = nodes.length; setTimeout(fit, 300); }
    nodes.forEach(nodo => {
    const stato = stati[nodo.dataset.nodeName];
    if (stato) nodo.dataset.stato = stato; else delete nodo.dataset.stato;
    });
  };
  new MutationObserver(colora).observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener('message', e => {
    let m; try { m = typeof e.data === 'string' ? JSON.parse(e.data) : e.data; } catch (_) { return; }
    if (m && m.command === 'bpmn8nStati') { stati = m.stati || {}; colora(); }
  });
})();
