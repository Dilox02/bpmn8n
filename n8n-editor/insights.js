// Heatmap of the event log on n8n's canvas ([OC] Fig. 22, Disco's map §4.1.4): on a canvas with bpmn8n
// nodes, a small switch (Off / Frequency / Duration) reads bpmn8n's /api/analytics/overlay for this workflow
// and rings each node hot to cold, with a badge (how many times it ran, or its mean duration) and a legend.
// One hue per measure, light to strong: frequency blue, duration red. *Capacity* reads /api/simulation/overlay
// instead: each step of a lane coloured by the lane's utilisation ρ in closed form (red from 85 %), with the
// expected wait, and the bottleneck in the legend; steps nobody waits for are dimmed. Loaded in the editor through
// EXTERNAL_FRONTEND_HOOKS_URLS (docker-compose.yml: n8n), with ?bpmn8n=<bpmn8n's public URL>. The read uses
// the browser's n8n session (bpmn8n checks it may open the workflow), as the evaluations view does.
// ponytail: finds nodes by n8n's data-test-id / data-node-name and the workflow in the URL (/workflow/<id>);
// an n8n upgrade that renames them only hides the switch. The pure functions below are tested with
// `node --test n8n-editor/test/insights.test.mjs`.
(() => {
  const MODES = ['off', 'frequency', 'duration', 'capacity'];
  const HUES = { frequency: 212, duration: 8 };  // one sequential hue per measure
  const NEAR = 0.85;  // utilisation from which a lane is red, as on bpmn8n's simulation page

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
  const overlayURL = (base, id, mode) => `${String(base).replace(/\/$/, '')}/api/${mode === 'capacity' ? 'simulation' : 'analytics'}` +
    `/overlay?workflow_id=${encodeURIComponent(id)}`;
  /** A lane's utilisation as a colour: a cool ramp up to NEAR, red from there (null: nobody waits there). */
  const capacityColour = rho => {
    if (rho == null || !Number.isFinite(rho)) return null;
    if (rho >= NEAR) return 'hsl(0 85% 58%)';
    const t = Math.max(0, rho) / NEAR;
    return `hsl(190 ${Math.round(25 + 50 * t)}% ${Math.round(70 - 18 * t)}%)`;
  };
  const percent = rho => `${Math.round(rho * 100)} %`;
  const capacityBadge = node => (node?.utilisation == null ? '' : percent(node.utilisation));
  const capacityDescribe = (name, node) => `${name}: ${node.resource} \u00b7 ${node.servers ?? 'unlimited'} ` +
    `${node.servers === 1 ? 'person' : 'people'}, busy ${percent(node.utilisation)}` +
    (node.stable === false ? ' \u00b7 cannot keep up' : ` \u00b7 waits ${duration(node.wait)}`) +
    ` \u00b7 ${Number(node.visits ?? 0).toFixed(2).replace(/\.?0+$/, '')} visits per case`;
  const capacityLegend = overlay => (overlay?.bottleneck ? `bottleneck ${overlay.bottleneck}` +
    `${overlay.stable === false ? ', cannot keep up' : ''}` : overlay?.note || 'no lane: nobody waits');
  /** bpmn8n on the same loopback host as the editor, so the n8n session cookie goes along (cookies ignore ports). */
  const sameHost = (configured, here) => {
    const url = new URL(configured), location = new URL(here);
    const loopback = new Set(['localhost', '127.0.0.1']);
    if (loopback.has(url.hostname) && loopback.has(location.hostname)) url.hostname = location.hostname;
    return url.href.replace(/\/$/, '');
  };
  // Replay ([DI] 4.1.6): the latest cases of the workflow as dots moving along its nodes on a compressed clock.
  const DWELL = 0.4;  // animation seconds a dot stays on a node at least, so a 10 ms step is still seen
  const replayURL = (base, id, { since, limit = 50 } = {}) => `${String(base).replace(/\/$/, '')}/api/analytics/replay` +
    `?workflow_id=${encodeURIComponent(id)}&limit=${limit}${since ? `&since=${encodeURIComponent(since)}` : ''}`;
  /** Real time to animation time: the whole window fits in `length` seconds (at speed 1); all cases keep their order. */
  const compress = (cases, length = 40) => {
    const starts = cases.flatMap(c => c.steps.map(s => s.start)), ends = cases.flatMap(c => c.steps.map(s => s.end));
    if (!starts.length) return { length: 0, cases: [] };
    const t0 = Math.min(...starts), span = Math.max(...ends) - t0, k = span > 0 ? length / span : 0;
    return { length, cases: cases.map(c => ({ id: c.case_id, steps: c.steps.map(s => {
      const start = (s.start - t0) * k;
      return { node: s.node, start, end: Math.max((s.end - t0) * k, start + DWELL) };
    }) })) };
  };
  /** Where a case's dot is at animation time t: on a node while it runs, between two while it waits, null before
   * it starts or after it ends (or when a node is not on the canvas). `pos`: node name to {x, y}. */
  const positionAt = (c, t, pos) => {
    let i = -1;
    c.steps.forEach((s, n) => { if (s.start <= t) i = n; });
    const here = c.steps[i], from = here && pos[here.node];
    if (!from) return null;
    if (t <= here.end) return { x: from.x, y: from.y, node: here.node, moving: false };
    const next = c.steps[i + 1], to = next && pos[next.node];
    if (!to) return null;
    const f = Math.min(1, Math.max(0, (t - here.end) / Math.max(next.start - here.end, 1e-9)));
    return { x: from.x + (to.x - from.x) * f, y: from.y + (to.y - from.y) * f, node: next.node, moving: true };
  };
  /** The animation time after dt milliseconds at `speed`, wrapping to the start past the end of the longest case. */
  const advance = (t, dt, speed, length) => { const n = t + dt / 1000 * speed; return length > 0 && n > length + 1 ? 0 : n; };
  /** The next moment something starts or ends after t (the step-by-step mode for reduced motion); null at the end. */
  const nextBoundary = (replay, t) => {
    const times = replay.cases.flatMap(c => c.steps.flatMap(s => [s.start, s.end])).filter(x => x > t + 1e-9);
    return times.length ? Math.min(...times) : null;
  };
  const Heat = { MODES, NEAR, scale, measure, heat, colour, count, duration, badge, describe, legend, workflowId, overlayURL, sameHost,
    capacityColour, capacityBadge, capacityDescribe, capacityLegend, replayURL, compress, positionAt, advance, nextBoundary, DWELL };
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
    .bpmn8n-replay { position: fixed; inset: 0; z-index: 19; pointer-events: none; }
    .bpmn8n-replay .dot { position: absolute; left: 0; top: 0; width: 10px; height: 10px; border-radius: 50%; background: hsl(212 90% 62%);
      box-shadow: 0 0 0 2px rgba(22, 22, 28, .8); pointer-events: auto; }
    .bpmn8n-replay .dot[hidden] { display: none; }
    .bpmn8n-heat .replay { display: inline-flex; align-items: center; gap: 6px; }
    .bpmn8n-heat select { font: inherit; color: inherit; background: transparent; border: 1px solid rgba(255, 255, 255, .2); border-radius: 6px; }
    [data-bpmn8n-heat] [data-canvas-node-render-type] { transition: box-shadow .2s, opacity .2s; }
    [data-bpmn8n-heat=hot] [data-canvas-node-render-type] {
      box-shadow: 0 0 0 3px var(--bpmn8n-heat), 0 0 16px 3px var(--bpmn8n-heat) !important; }
    [data-bpmn8n-heat=none] [data-canvas-node-render-type] { opacity: .4; }
    .bpmn8n-heat-badge { position: absolute; top: -12px; right: -10px; z-index: 10; min-width: 22px; height: 20px; padding: 0 6px;
      border-radius: 10px; background: var(--bpmn8n-heat); color: #111116; font: 600 11px/20px system-ui, sans-serif;
      text-align: center; white-space: nowrap; pointer-events: none; }
  `;
  document.head.append(style);

  let mode = 'off', data = null, overlay = null, loadedFor = null, panel = null, queued = false;
  const source = id => `${id}:${mode === 'capacity' ? 'simulation' : 'analytics'}`;  // capacity reads another endpoint
  try { mode = MODES.includes(localStorage.getItem('bpmn8n-heat')) ? localStorage.getItem('bpmn8n-heat') : 'off'; } catch { /* private mode */ }

  const say = text => { if (panel) panel.querySelector('.state').textContent = text; };
  async function fetchOverlay(id) {
    const key = source(id);
    loadedFor = key; data = null; overlay = null; say(mode === 'capacity' ? 'Reading the closed forms\u2026' : 'Reading the event log\u2026');
    try {
      const browserId = (() => { try { return localStorage.getItem('n8n-browserId') || ''; } catch { return ''; } })();
      const response = await fetch(overlayURL(BPMN8N, id, mode), { credentials: 'include', headers: browserId ? { 'browser-id': browserId } : {} });
      if (loadedFor !== key) return;
      if (response.status === 401 || response.status === 403) throw Error('Sign in to n8n again to read bpmn8n.');
      if (!response.ok) throw Error((await response.json().catch(() => ({}))).detail || `bpmn8n did not answer (HTTP ${response.status}).`);
      overlay = await response.json();
      data = overlay.nodes || {};
      say(Object.keys(data).length ? '' : mode === 'capacity' ? overlay.note || 'Nothing to size here.' : 'No case of this workflow in the event log yet.');
    } catch (error) {
      if (loadedFor === key) say(error.message === 'Failed to fetch' ? `bpmn8n is unreachable at ${BPMN8N}.` : error.message);
    }
    schedule();
  }

  function ensurePanel() {
    if (panel) return panel;
    panel = document.createElement('div');
    panel.className = 'bpmn8n-heat'; panel.setAttribute('role', 'group'); panel.setAttribute('aria-label', 'Heatmap of the event log and capacity');
    panel.innerHTML = `<span>Heat</span>${MODES.map(m => `<button type="button" data-mode="${m}">${m[0].toUpperCase() + m.slice(1)}</button>`).join('')}
      <button type="button" data-replay="toggle" aria-pressed="false">Replay</button>
      <span class="replay" hidden><button type="button" data-replay="play">Pause</button><button type="button" data-replay="step">Step</button>
        <label>Speed <select data-replay="speed"><option>0.5</option><option selected>1</option><option>2</option><option>4</option></select></label></span>
      <span class="legend"></span><span class="state" role="status"></span>`;
    panel.querySelector('[data-replay=speed]').addEventListener('change', event => { if (rp) rp.speed = Number(event.target.value); });
    panel.addEventListener('click', event => {
      const control = event.target.closest('button')?.dataset.replay;
      if (control) return replayControl(control);
      const next = event.target.closest('button')?.dataset.mode;
      if (!next) return;
      mode = next;
      try { localStorage.setItem('bpmn8n-heat', mode); } catch { /* private mode */ }
      schedule();
    });
    document.body.append(panel);
    return panel;
  }

  // Replay: dots on a fixed layer over the canvas, at the nodes' screen centres, read again at every frame.
  let rp = null;  // { replay, t, playing, speed, last, raf, layer, dots }
  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  function drawReplay() {
    if (!rp) return;
    const pos = {};
    document.querySelectorAll('[data-test-id=canvas-node][data-node-name]').forEach(el => {
      const b = el.getBoundingClientRect();
      pos[el.dataset.nodeName] = { x: b.left + b.width / 2, y: b.top + b.height / 2 };
    });
    rp.replay.cases.forEach((c, i) => {
      const dot = rp.dots[i] || (rp.dots[i] = rp.layer.appendChild(Object.assign(document.createElement('i'), { className: 'dot' })));
      const at = positionAt(c, rp.t, pos);
      dot.hidden = !at;
      if (at) { dot.style.transform = `translate(${at.x - 5}px, ${at.y - 5}px)`; dot.title = `${c.id} \u00b7 ${at.node}`; }
    });
  }
  function frame(now) {
    if (!rp?.playing) return;
    rp.t = advance(rp.t, now - rp.last, rp.speed, rp.replay.length);
    rp.last = now;
    drawReplay();
    rp.raf = requestAnimationFrame(frame);
  }
  function setPlaying(on) {
    if (!rp) return;
    rp.playing = on && !reduced();  // reduced motion: only the Step button moves the dots
    panel.querySelector('[data-replay=play]').textContent = rp.playing ? 'Pause' : 'Play';
    cancelAnimationFrame(rp.raf);
    if (rp.playing) { rp.last = performance.now(); rp.raf = requestAnimationFrame(frame); }
  }
  async function replayControl(control) {
    if (control === 'toggle') {
      if (rp) { cancelAnimationFrame(rp.raf); rp.layer.remove(); rp = null; panel.querySelector('.replay').hidden = true; panel.querySelector('[data-replay=toggle]').setAttribute('aria-pressed', 'false'); say(''); return; }
      const id = workflowId(location.pathname);
      say('Reading the cases\u2026');
      try {
        const browserId = (() => { try { return localStorage.getItem('n8n-browserId') || ''; } catch { return ''; } })();
        const response = await fetch(replayURL(BPMN8N, id), { credentials: 'include', headers: browserId ? { 'browser-id': browserId } : {} });
        if (!response.ok) throw Error((await response.json().catch(() => ({}))).detail || `bpmn8n did not answer (HTTP ${response.status}).`);
        const replay = compress((await response.json()).cases);
        if (!replay.cases.length) return say('No case of this workflow in the event log yet.');
        const layer = Object.assign(document.createElement('div'), { className: 'bpmn8n-replay' });
        document.body.append(layer);
        rp = { replay, t: 0, speed: Number(panel.querySelector('[data-replay=speed]').value), layer, dots: [] };
        panel.querySelector('.replay').hidden = false;
        panel.querySelector('[data-replay=toggle]').setAttribute('aria-pressed', 'true');
        say(`${replay.cases.length} cases${reduced() ? ', step by step (reduced motion)' : ''}`);
        drawReplay(); setPlaying(true);
      } catch (error) { say(error.message === 'Failed to fetch' ? `bpmn8n is unreachable at ${BPMN8N}.` : error.message); }
    } else if (control === 'play' && rp) setPlaying(!rp.playing);
    else if (control === 'step' && rp) {
      setPlaying(false);
      rp.t = nextBoundary(rp.replay, rp.t) ?? 0;
      drawReplay();
    }
  }

  function paint() {
    queued = false;
    drawReplay();
    const flow = document.querySelector('.vue-flow');
    const id = workflowId(location.pathname);
    const ours = flow && id && flow.querySelector('[data-test-id=canvas-node][data-node-type^="CUSTOM.bpmn8n"]');
    if (!ours) { if (panel) panel.hidden = true; mode !== 'off' && clear(); return; }
    ensurePanel().hidden = false;
    const box = flow.getBoundingClientRect();
    Object.assign(panel.style, { top: `${Math.round(box.top + 12)}px`, left: `${Math.round(box.left + 12)}px` });
    panel.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
    const legendBox = panel.querySelector('.legend');
    if (mode === 'off') { clear(); legendBox.textContent = ''; delete legendBox.dataset.said; say(''); return; }
    if (loadedFor !== source(id)) { clear(); fetchOverlay(id); return; }
    if (!data) return;
    if (mode === 'capacity') return paintCapacity();
    const ends = legend(data, mode), said = `${mode}|${ends?.low}|${ends?.high}`;
    if (legendBox.dataset.said !== said) {
      legendBox.innerHTML = ends
        ? `${ends.low}<span class="ramp" style="background: linear-gradient(90deg, ${colour(0, mode)}, ${colour(1, mode)})"></span>${ends.high}` +
          ` \u00b7 ${mode === 'duration' ? 'mean duration' : 'times run'}` : '';
      legendBox.dataset.said = said;
    }
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

  function paintCapacity() {
    const legendBox = panel.querySelector('.legend'), said = `${capacityLegend(overlay)}|${overlay.page}`;
    if (legendBox.dataset.said !== said) {  // rewritten only when it changes: the observer would repaint forever
      const ramp = `linear-gradient(90deg, ${capacityColour(0)}, ${capacityColour(NEAR - 0.01)} 85%, ${capacityColour(NEAR)} 85%)`;
      legendBox.innerHTML = `0 %<span class="ramp" style="background: ${ramp}"></span>100 % \u00b7 utilisation (closed form) \u00b7 `;
      legendBox.append(capacityLegend(overlay));
      if (overlay.page) {
        const a = Object.assign(document.createElement('a'), { href: BPMN8N + overlay.page, target: '_blank', rel: 'noopener', textContent: ' \u00b7 what if' });
        a.style.color = 'inherit';
        legendBox.append(a);
      }
      legendBox.dataset.said = said;
    }
    document.querySelectorAll('[data-test-id=canvas-node][data-node-name]').forEach(el => {
      const name = el.dataset.nodeName, node = data[name], tint = capacityColour(node?.utilisation);
      let tag = el.querySelector(':scope > .bpmn8n-heat-badge');
      if (!tint) { el.dataset.bpmn8nHeat = 'none'; tag?.remove(); el.removeAttribute('title'); return; }
      if (el.style.getPropertyValue('--bpmn8n-heat') !== tint) el.style.setProperty('--bpmn8n-heat', tint);
      el.dataset.bpmn8nHeat = 'hot';
      if (!tag) { tag = document.createElement('span'); tag.className = 'bpmn8n-heat-badge'; el.append(tag); }
      const text = capacityBadge(node);
      if (tag.textContent !== text) tag.textContent = text;
      el.title = capacityDescribe(name, node);
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
