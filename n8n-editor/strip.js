// bpmn8n's strip on top of the n8n editor: Inbox (with how many tasks wait), Cases, Insights, Simulation and Docs,
// the same places as the sidebar of bpmn8n's own pages, so one click leads from a workflow to the people and the
// numbers around it. Loaded through EXTERNAL_FRONTEND_HOOKS_URLS (docker-compose.yml: n8n) with
// ?bpmn8n=<bpmn8n's public URL>, which may carry a path prefix (one origin); n8n's own base path is read from the
// workflow's URL (/workflow/<id>, under N8N_PATH too). The waiting count is read with the browser's n8n session
// (bpmn8n checks it may open the workflow, as for the evaluations), so it shows on a workflow's page only.
// ponytail: the strip is fixed above #app, which it shifts down; an n8n upgrade that renames #app only lets the
// strip cover the editor's own top 24 px. The pure functions are tested with `node --test n8n-editor/test/strip.test.mjs`.
(() => {
  const HEIGHT = 24;  // px
  const LINKS = [['Inbox', 'inbox/'], ['Cases', 'cases/'], ['Insights', 'insights/'], ['Simulation', 'simulation/'],
    ['Variables', 'variables/'], ['Docs', 'docs/']];  // Variables: bpmn8n's, on Redis (n8n's are licensed: hidden below)
  // n8n's entries this stack does not use. Most of their modules are off (N8N_DISABLED_MODULES in docker-compose.yml);
  // these stay because they follow the licence, not a module: help, n8n's AI assistant, its licensed variables,
  // the settings that only show an upgrade page on the community edition (and the v2 migration report), and the
  // 'production checklist' popover. ponytail: n8n's own ids; an upgrade that renames one only shows it again.
  const HIDDEN = ['[data-test-id="main-sidebar-help"]', '[data-test-id^="ask-assistant"]',
    '[data-test-id^="tab-"][data-test-id$="Variables"]',
    ...['roles', 'external-secrets', 'source-control', 'sso', 'security', 'ldap', 'workersview', 'log-streaming',
      'migration-report'].map(s => `#settings-${s}`),
    '[id^="reka-popover-content"]:has([data-test-id="suggested-action-item"])'];

  /** The strip's links under bpmn8n's public URL (any path prefix kept). */
  const links = base => LINKS.map(([label, path]) => ({ label, href: `${String(base).replace(/\/+$/, '')}/${path}` }));
  /** The workflow open in the editor: /workflow/<id>, with or without a base path before it. */
  const workflowId = pathname => /\/workflow\/([^/?#]+)/.exec(pathname)?.[1] ?? null;
  /** Where the waiting count is read for a workflow; null off a workflow page. */
  const waitingUrl = (base, id) => id && id !== 'new' ? `${String(base).replace(/\/+$/, '')}/api/executions/${encodeURIComponent(id)}/waiting` : null;
  /** The count beside Inbox: nothing for none (or unknown), 99+ past 99. */
  const badge = n => (n > 0 ? (n > 99 ? '99+' : String(n)) : '');
  /** localhost and 127.0.0.1 are one machine: bpmn8n is asked on the host the editor was opened on. */
  const sameHost = (configured, here) => {
    const url = new URL(configured), page = new URL(here), loopback = ['localhost', '127.0.0.1'];
    if (loopback.includes(url.hostname) && loopback.includes(page.hostname)) url.hostname = page.hostname;
    return url.href.replace(/\/$/, '');
  };
  const Strip = { HEIGHT, HIDDEN, links, workflowId, waitingUrl, badge, sameHost };
  if (typeof module === 'object' && module.exports) { module.exports = Strip; return; }  // node --test

  const BPMN8N = sameHost(new URL(document.currentScript?.src || location.href).searchParams.get('bpmn8n') || 'http://localhost:8000', location.href);
  const hide = document.createElement('style');  // n8n's unused entries: hidden on its own and inside bpmn8n's pages
  hide.textContent = `${HIDDEN.join(',\n    ')} { display: none !important; }`;
  document.head.appendChild(hide);
  if (window.top !== window) return;  // inside a bpmn8n page (or an embedded canvas): bpmn8n's own sidebar is there
  const style = document.createElement('style');
  style.textContent = `
    :root { --bpmn8n-strip: ${HEIGHT}px; }
    #app { margin-top: var(--bpmn8n-strip); height: calc(100vh - var(--bpmn8n-strip)) !important; }
    .bpmn8n-strip { position: fixed; z-index: 2000; top: 0; left: 0; right: 0; height: var(--bpmn8n-strip); display: flex; align-items: center;
      gap: 2px; padding: 0 12px; box-sizing: border-box; font: 12px/1 var(--b8-sans, system-ui, sans-serif); color: var(--b8-muted, #b4b4b4);
      background: var(--b8-surface, #111); border-bottom: 1px solid var(--b8-border, #2e2e2e); }
    .bpmn8n-strip b { margin-right: 10px; font-weight: 600; color: var(--b8-text, #fff); }
    .bpmn8n-strip a { display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px; border-radius: var(--b8-radius-control, 6px);
      color: inherit; text-decoration: none; }
    .bpmn8n-strip a:hover { color: var(--b8-text, #fff); background: var(--b8-active, #262626); }
    .bpmn8n-strip a:focus-visible { outline: 2px solid var(--b8-accent, currentColor); outline-offset: 1px; }
    .bpmn8n-strip .count { padding: 1px 6px; border-radius: 8px; font-size: 11px; color: var(--b8-primary-ink, #000); background: var(--b8-accent, #5fb3c8); }
    .bpmn8n-strip .count[hidden] { display: none; }
  `;
  document.head.appendChild(style);
  if (!document.querySelector('link[data-bpmn8n-tokens]')) {  // normally bpmn.js links them first
    const tokens = Object.assign(document.createElement('link'), { rel: 'stylesheet', href: new URL('../static/bpmn8n-tokens.css?v=1', document.currentScript?.src || location.href).href });
    tokens.dataset.bpmn8nTokens = '';
    document.head.prepend(tokens);
  }
  const strip = document.createElement('nav');
  strip.className = 'bpmn8n-strip';
  strip.setAttribute('aria-label', 'bpmn8n');
  strip.innerHTML = `<b>bpmn8n</b>${links(BPMN8N).map(l => `<a href="${l.href}" target="_blank" rel="noopener">${l.label}${l.label === 'Inbox' ? '<span class="count" hidden></span>' : ''}</a>`).join('')}`;
  // the hooks run in <head>, before <body> exists: appending at once threw, and left the space with no strip
  if (document.body) document.body.appendChild(strip);
  else addEventListener('DOMContentLoaded', () => document.body.appendChild(strip));

  let last = null;
  async function refresh() {
    const url = waitingUrl(BPMN8N, workflowId(location.pathname));
    const count = strip.querySelector('.count');
    if (!url || document.hidden) { if (!url) { count.hidden = true; last = null; } return; }
    try {
      const r = await fetch(url, { credentials: 'include' });
      const waiting = r.ok ? (await r.json()).waiting : 0;
      if (waiting !== last) { last = waiting; count.textContent = badge(waiting); count.hidden = !badge(waiting); count.setAttribute('aria-label', `${waiting} waiting`); }
    } catch { /* bpmn8n unreachable: the links stay, the count goes */ count.hidden = true; last = null; }
  }
  refresh();
  setInterval(refresh, 30000);
  addEventListener('popstate', refresh);
  document.addEventListener('visibilitychange', refresh);
  let path = location.pathname;  // n8n's router changes the URL without a reload
  setInterval(() => { if (location.pathname !== path) { path = location.pathname; refresh(); } }, 1000);
})();
