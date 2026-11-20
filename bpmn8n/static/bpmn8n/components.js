// Shared web components, written once for Django's pages and n8n's editor hooks:
//   <b8-pill tone="positive|negative|warning|accent|neutral">completed</b8-pill>
//   <b8-timeline items='[{"label", "meta", "at", "status"}]'></b8-timeline>, or around an <ol> of <li data-status>
//   <b8-process-map> around a server-drawn SVG (web/landscape.py): pointing at a workflow lights its neighbours
// A classic script, not a module, so a hook on another origin loads it with a plain <script src>; it brings its
// styles, written only with tokens.css's --b8-* properties, which both servers load. Node's tests require() the
// pure parts (n8n-editor/test/components.test.mjs).
(() => {
  const TONES = ['positive', 'negative', 'warning', 'accent', 'neutral'];
  const STATUSES = ['complete', 'error', 'running'];  // an event's status (Event.status): the dot's colour
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const tone = value => TONES.includes(value) ? value : 'neutral';

  // a timeline's entries from its JSON attribute (text or already parsed): anything else is no entry
  function timelineItems(value) {
    let items = value;
    if (typeof value === 'string') { try { items = JSON.parse(value); } catch { return []; } }
    return (Array.isArray(items) ? items : []).filter(i => i && typeof i === 'object' && i.label != null)
      .map(i => ({ label: String(i.label), meta: String(i.meta ?? ''), at: String(i.at ?? ''), status: STATUSES.includes(i.status) ? i.status : 'complete' }));
  }
  const timelineHTML = items => `<ol>${items.map(i => `<li data-status="${i.status}"><div class="b8-entry"><span class="b8-label">${esc(i.label)}</span>` +
    `${i.at ? `<time>${esc(i.at)}</time>` : ''}${i.meta ? `<span class="b8-meta">${esc(i.meta)}</span>` : ''}</div></li>`).join('')}</ol>`;

  // what pointing at one workflow of a map lights: itself, the edges that touch it and the workflows at their ends
  function related(edges, id) {
    const nodes = new Set(id == null ? [] : [id]), lit = new Set();
    edges.forEach((e, i) => { if (id != null && (e.source === id || e.target === id)) { lit.add(i); nodes.add(e.source); nodes.add(e.target); } });
    return { nodes, edges: lit };
  }

  const CSS = `
b8-pill { display: inline-flex; align-items: center; gap: 6px; flex: none; height: 22px; padding: 0 8px; border-radius: var(--b8-radius-pill); font-size: var(--b8-fs-small); font-weight: 500; line-height: 1; white-space: nowrap; font-variant-numeric: tabular-nums; background: var(--b8-pill-neutral-bg); color: var(--b8-pill-neutral-ink); }
b8-pill[tone=positive] { background: var(--b8-pill-positive-bg); color: var(--b8-pill-positive-ink); }
b8-pill[tone=negative] { background: var(--b8-pill-negative-bg); color: var(--b8-pill-negative-ink); }
b8-pill[tone=warning] { background: var(--b8-pill-warning-bg); color: var(--b8-pill-warning-ink); }
b8-pill[tone=accent] { background: var(--b8-pill-accent-bg); color: var(--b8-pill-accent-ink); }
b8-timeline { display: block; }
b8-timeline > ol { display: grid; margin: 0; padding: 0; list-style: none; }
b8-timeline > ol > li { position: relative; padding-left: 20px; }
b8-timeline > ol > li::before { content: ""; position: absolute; left: 0; top: 17px; width: 8px; height: 8px; border-radius: 50%; background: var(--b8-control); }
b8-timeline > ol > li:not(:last-child)::after { content: ""; position: absolute; left: 3.5px; top: 31px; bottom: -9px; width: 1px; background: var(--b8-border); }
b8-timeline > ol > li[data-status=error]::before { background: var(--b8-negative); }
b8-timeline > ol > li[data-status=running]::before { background: var(--b8-warning); }
b8-timeline .b8-entry { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 16px; padding: 10px 0; }
b8-timeline .b8-label { font-weight: 500; overflow-wrap: anywhere; }
b8-timeline .b8-meta { grid-column: 1 / -1; color: var(--b8-muted); font-size: var(--b8-fs-small); }
b8-timeline .b8-entry time { color: var(--b8-muted); font-size: var(--b8-fs-small); white-space: nowrap; }
b8-process-map { display: block; overflow-x: auto; }
b8-process-map svg { display: block; max-width: none; font-family: var(--b8-font); }
b8-process-map .node rect { fill: var(--b8-surface); stroke: var(--b8-control); stroke-width: 1; }
b8-process-map .node .name { fill: var(--b8-text); font-size: 13px; font-weight: 500; }
b8-process-map .node .meta { fill: var(--b8-muted); font-size: 11px; }
b8-process-map .node.warn rect { stroke: var(--b8-warning); stroke-width: 1.5; }
b8-process-map .node.outside rect, b8-process-map .node.missing rect { fill: var(--b8-raised); stroke-dasharray: 4 3; }
b8-process-map .node:focus-visible { outline: none; }
b8-process-map .node:focus-visible rect { stroke: var(--b8-accent); stroke-width: 2; }
b8-process-map .edge path { fill: none; stroke: var(--b8-muted); stroke-width: 1.25; }
b8-process-map .edge.message path { stroke-dasharray: 5 4; }
b8-process-map .edge.unsent path { stroke: var(--b8-warning); }
b8-process-map .edge text { fill: var(--b8-muted); font-size: 11px; paint-order: stroke; stroke: var(--b8-raised); stroke-width: 4px; stroke-linejoin: round; }
b8-process-map marker path { fill: var(--b8-muted); }
b8-process-map[data-focus] .node:not(.b8-related), b8-process-map[data-focus] .edge:not(.b8-related) { opacity: .3; }
b8-process-map .edge.b8-related path { stroke: var(--b8-accent); }
@media (prefers-reduced-motion: no-preference) { b8-process-map .node, b8-process-map .edge { transition: opacity var(--b8-t-hover) var(--b8-ease-out); } }
`;

  const api = { tone, esc, timelineItems, timelineHTML, related, CSS };
  if (typeof module === 'object' && module.exports) module.exports = api;  // node's tests
  const { customElements, HTMLElement, document } = globalThis;
  if (!customElements || customElements.get('b8-pill')) return;  // node, or loaded twice
  const style = document.createElement('style');
  style.textContent = CSS;
  document.head.append(style);

  // the tone attribute and the stylesheet are the whole pill: one element so the words keep one look everywhere
  customElements.define('b8-pill', class extends HTMLElement {});

  customElements.define('b8-timeline', class extends HTMLElement {
    static observedAttributes = ['items'];
    connectedCallback() { this.render(); }
    attributeChangedCallback() { if (this.isConnected) this.render(); }
    render() { if (this.hasAttribute('items')) this.innerHTML = timelineHTML(timelineItems(this.getAttribute('items'))); }
  });

  customElements.define('b8-process-map', class extends HTMLElement {
    connectedCallback() {
      if (this.listening) return;
      this.listening = true;
      const show = id => {  // the drawing is read when pointed at: connected, its children may not be parsed yet
        const edges = [...this.querySelectorAll('.edge[data-source]')].map(el => ({ source: el.dataset.source, target: el.dataset.target, el }));
        const lit = related(edges, id);
        this.toggleAttribute('data-focus', id != null);
        this.querySelectorAll('.node[data-id]').forEach(el => el.classList.toggle('b8-related', lit.nodes.has(el.dataset.id)));
        edges.forEach((e, i) => e.el.classList.toggle('b8-related', lit.edges.has(i)));
      };
      const at = e => e.target.closest?.('.node[data-id]')?.dataset.id ?? null;
      this.addEventListener('pointerover', e => show(at(e)));
      this.addEventListener('pointerleave', () => show(null));
      this.addEventListener('focusin', e => show(at(e)));
      this.addEventListener('focusout', () => show(null));
    }
  });
})();
