// Heatmap of the event log on n8n's canvas ([OC] Fig. 22, Disco's map §4.1.4): on a canvas with bpmn8n
// nodes, a small switch (Off / Frequency / Duration) reads bpmn8n's /api/analytics/overlay for this workflow
// and rings each node hot to cold, with a badge (how many times it ran, or its mean duration) and a legend.
// One hue per measure, light to strong: frequency blue, duration red. Loaded in the editor through
// EXTERNAL_FRONTEND_HOOKS_URLS (docker-compose.yml: n8n), with ?bpmn8n=<bpmn8n's public URL>. The read uses
// the browser's n8n session (bpmn8n checks it may open the workflow), as the evaluations view does.
// ponytail: finds nodes by n8n's data-test-id / data-node-name and the workflow in the URL (/workflow/<id>);
// an n8n upgrade that renames them only hides the switch. The pure functions below are tested with
// `node --test n8n-editor/test/insights.test.mjs`.
(() => {
  const MODES = ['off', 'frequency', 'duration'];
  const HUES = { frequency: 212, duration: 8 };  // one sequential hue per measure

  /** value / max, clamped to 0 to 1 (0 when there is nothing to compare with). */
  const scale = (value, max) => (!(max > 0) || !(value > 0) ? 0 : Math.min(1, value / max));
  /** What a mode measures on a node of the overlay. */
  const measure = (node, mode) => (mode === 'duration' ? node?.mean : node?.frequency) ?? 0;
  /** A node's heat in 0 to 1 against the hottest node of the workflow. */
  const heat = (nodes, name, mode) => scale(measure(nodes[name], mode), Math.max(0, ...Object.values(nodes).map(n => measure(n, mode))));
  /** Cold to hot: paler and greyer to deeper and more saturated, readable on the dark and the light canvas. */
  const colour = (h, mode) => {
    const t = Math.min(1, Math.max(0, h));
    return `hsl(${HUES[mode] ?? HUES.frequency} ${Math.round(25 + 65 * t)}% ${Math.round(70 - 22 * t)}%)`;
  };
  /** 950, 1.2k, 12k, 1.3M */
  const count = n => {
    if (!(n >= 1000)) return String(Math.round(n || 0));
    const [d, s] = n >= 1e6 ? [1e6, 'M'] : [1e3, 'k'];
    return `${(n / d).toFixed(n / d < 10 ? 1 : 0).replace(/\.0$/, '')}${s}`;
  };
  /** 350 ms, 42 s, 12 min, 3.4 h, 2.1 d */
  const duration = seconds => {
    if (seconds == null || !Number.isFinite(seconds)) return '\u2014';
    if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
    if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`;
    if (seconds < 3600) return `${seconds < 600 ? (seconds / 60).toFixed(1) : Math.round(seconds / 60)} min`;
    if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} h`;
    return `${(seconds / 86400).toFixed(1)} d`;
  };
  const badge = (node, mode) => (mode === 'duration' ? duration(node.mean) : count(node.frequency));
  const describe = (name, node) => `${name}: ran ${node.frequency} times in ${node.cases} cases \u00b7 mean ${duration(node.mean)}, ` +
    `median ${duration(node.median)}${node.errors ? ` \u00b7 ${node.errors} error${node.errors > 1 ? 's' : ''}` : ''}`;
  /** The legend's two ends: the coldest and the hottest node's value. */
  const legend = (nodes, mode) => {
    const values = Object.values(nodes).map(n => measure(n, mode));
    if (!values.length) return null;
    const fmt = v => (mode === 'duration' ? duration(v) : count(v));
    return { low: fmt(Math.min(...values)), high: fmt(Math.max(...values)) };
  };
  /** The workflow open in the editor, from its URL; null for a new, unsaved one. */
  const workflowId = pathname => {
    const id = (String(pathname).match(/\/workflow\/([^/?#]+)/) || [])[1];
    return id && id !== 'new' ? decodeURIComponent(id) : null;
  };
  const overlayURL = (base, id) => `${String(base).replace(/\/$/, '')}/api/analytics/overlay?workflow_id=${encodeURIComponent(id)}`;
  /** bpmn8n on the same loopback host as the editor, so the n8n session cookie goes along (cookies ignore ports). */
  const sameHost = (configured, here) => {
    const url = new URL(configured), location = new URL(here);
    const loopback = new Set(['localhost', '127.0.0.1']);
    if (loopback.has(url.hostname) && loopback.has(location.hostname)) url.hostname = location.hostname;
    return url.href.replace(/\/$/, '');
  };
  const Heat = { MODES, scale, measure, heat, colour, count, duration, badge, describe, legend, workflowId, overlayURL, sameHost };
  if (typeof module === 'object' && module.exports) { module.exports = Heat; return; }  // node --test

  const BPMN8N = sameHost(new URL(document.currentScript?.src || location.href).searchParams.get('bpmn8n') || 'http://localhost:8000', location.href);
  const style = document.createElement('style');
  style.textContent = `
    .bpmn8n-heat { position: fixed; z-index: 20; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; max-width: 360px;
      padding: 6px 8px; border-radius: 8px; font: 12px/16px system-ui, sans-serif; color: #e8e8ee;
      background: rgba(22, 22, 28, .92); border: 1px solid rgba(255, 255, 255, .12); }
    .bpmn8n-heat button { padding: 3px 8px; border: 1px solid transparent; border-radius: 6px; background: none; color: inherit; font: inherit; cursor: pointer; }
    .bpmn8n-heat button:hover { background: rgba(255, 255, 255, .08); }
    .bpmn8n-heat button[aria-pressed=true] { background: rgba(255, 255, 255, .16); border-color: rgba(255, 255, 255, .2); }
    .bpmn8n-heat .ramp { display: inline-block; width: 72px; height: 8px; border-radius: 4px; vertical-align: middle; margin: 0 6px; }
    .bpmn8n-heat .legend, .bpmn8n-heat .state { flex-basis: 100%; opacity: .8; }
    .bpmn8n-heat .legend:empty, .bpmn8n-heat .state:empty { display: none; }
    [data-bpmn8n-heat] [data-canvas-node-render-type] { transition: box-shadow .2s, opacity .2s; }
    [data-bpmn8n-heat=hot] [data-canvas-node-render-type] {
      box-shadow: 0 0 0 3px var(--bpmn8n-heat), 0 0 16px 3px var(--bpmn8n-heat) !important; }
    [data-bpmn8n-heat=none] [data-canvas-node-render-type] { opacity: .4; }
    .bpmn8n-heat-badge { position: absolute; top: -12px; right: -10px; z-index: 10; min-width: 22px; height: 20px; padding: 0 6px;
      border-radius: 10px; background: var(--bpmn8n-heat); color: #111116; font: 600 11px/20px system-ui, sans-serif;
      text-align: center; white-space: nowrap; pointer-events: none; }
  `;
  document.head.append(style);

  let mode = 'off', data = null, loadedFor = null, panel = null, queued = false;
  try { mode = MODES.includes(localStorage.getItem('bpmn8n-heat')) ? localStorage.getItem('bpmn8n-heat') : 'off'; } catch { /* private mode */ }

  const say = text => { if (panel) panel.querySelector('.state').textContent = text; };
  async function load(id) {
    loadedFor = id; data = null; say('Reading the event log\u2026');
    try {
      const browserId = (() => { try { return localStorage.getItem('n8n-browserId') || ''; } catch { return ''; } })();
      const response = await fetch(overlayURL(BPMN8N, id), { credentials: 'include', headers: browserId ? { 'browser-id': browserId } : {} });
      if (loadedFor !== id) return;
      if (response.status === 401 || response.status === 403) throw Error('Sign in to n8n again to read the event log.');
      if (!response.ok) throw Error(`bpmn8n did not answer (HTTP ${response.status}).`);
      data = (await response.json()).nodes || {};
      say(Object.keys(data).length ? '' : 'No case of this workflow in the event log yet.');
    } catch (error) {
      if (loadedFor === id) say(error.message === 'Failed to fetch' ? `bpmn8n is unreachable at ${BPMN8N}.` : error.message);
    }
    schedule();
  }

  function ensurePanel() {
    if (panel) return panel;
    panel = document.createElement('div');
    panel.className = 'bpmn8n-heat'; panel.setAttribute('role', 'group'); panel.setAttribute('aria-label', 'Heatmap of the event log');
    panel.innerHTML = `<span>Heat</span>${MODES.map(m => `<button type="button" data-mode="${m}">${m[0].toUpperCase() + m.slice(1)}</button>`).join('')}
      <span class="legend"></span><span class="state" role="status"></span>`;
    panel.addEventListener('click', event => {
      const next = event.target.closest('button')?.dataset.mode;
      if (!next) return;
      mode = next;
      try { localStorage.setItem('bpmn8n-heat', mode); } catch { /* private mode */ }
      schedule();
    });
    document.body.append(panel);
    return panel;
  }

  function paint() {
    queued = false;
    const flow = document.querySelector('.vue-flow');
    const id = workflowId(location.pathname);
    const ours = flow && id && flow.querySelector('[data-test-id=canvas-node][data-node-type^="CUSTOM.bpmn8n"]');
    if (!ours) { if (panel) panel.hidden = true; mode !== 'off' && clear(); return; }
    ensurePanel().hidden = false;
    const box = flow.getBoundingClientRect();
    Object.assign(panel.style, { top: `${Math.round(box.top + 12)}px`, left: `${Math.round(box.left + 12)}px` });
    panel.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
    if (mode === 'off') { clear(); panel.querySelector('.legend').textContent = ''; say(''); return; }
    if (loadedFor !== id) { clear(); load(id); return; }
    if (!data) return;
    const ends = legend(data, mode);
    panel.querySelector('.legend').innerHTML = ends
      ? `${ends.low}<span class="ramp" style="background: linear-gradient(90deg, ${colour(0, mode)}, ${colour(1, mode)})"></span>${ends.high}` +
        ` \u00b7 ${mode === 'duration' ? 'mean duration' : 'times run'}` : '';
    document.querySelectorAll('[data-test-id=canvas-node][data-node-name]').forEach(el => {
      const name = el.dataset.nodeName, node = data[name];
      let tag = el.querySelector(':scope > .bpmn8n-heat-badge');
      if (!node) { el.dataset.bpmn8nHeat = 'none'; tag?.remove(); return; }
      const tint = colour(heat(data, name, mode), mode);
      if (el.style.getPropertyValue('--bpmn8n-heat') !== tint) el.style.setProperty('--bpmn8n-heat', tint);
      el.dataset.bpmn8nHeat = 'hot';
      if (!tag) { tag = document.createElement('span'); tag.className = 'bpmn8n-heat-badge'; el.append(tag); }
      const text = badge(node, mode);
      if (tag.textContent !== text) tag.textContent = text;
      el.title = describe(name, node);
    });
  }

  function clear() {
    document.querySelectorAll('[data-bpmn8n-heat]').forEach(el => {
      delete el.dataset.bpmn8nHeat; el.style.removeProperty('--bpmn8n-heat'); el.removeAttribute('title');
      el.querySelector(':scope > .bpmn8n-heat-badge')?.remove();
    });
  }

  function schedule() { if (!queued) { queued = true; requestAnimationFrame(paint); } }
  new MutationObserver(mutations => {
    if (mutations.some(m => [...m.addedNodes, ...m.removedNodes].some(n => n.nodeType === 1 && !n.classList?.contains('bpmn8n-heat-badge')))) schedule();
  }).observe(document.documentElement, { childList: true, subtree: true });
  addEventListener('resize', schedule);
  schedule();
})();
