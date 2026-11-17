// Live BPMN execution preview, with one token per invocation/input item and join queue.
(async () => {
  const source = new URL(document.currentScript.src);
  const { esc, date, text, html, keyed, localURL, previewURL, ApiClient, LiveUpdates, viewComponent, installRoute, decodeExecution, executionState } =
    await import(new URL('bpmn8n-live.mjs?v=4', source));
  const backend = localURL(source.searchParams.get('bpmn8n') || 'http://localhost:8000');
  const preview = previewURL(source.searchParams.get('preview') || 'http://localhost:5679');
  const stylesheet = document.createElement('link'); stylesheet.rel = 'stylesheet'; stylesheet.href = new URL('bpmn8n-executions.css?v=3', source); document.head.append(stylesheet);
  const labels = { active: 'In esecuzione', ready: 'Nel motore (non confermato)', waiting: 'In attesa al join', buffered: 'Elaborato · attesa degli altri item', complete: 'Completato', error: 'Errore' };
  function json(value) {
    const seen = new WeakSet();
    return (JSON.stringify(value, (_, item) => {
      if (item && typeof item === 'object') { if (seen.has(item)) return '[riferimento]'; seen.add(item); }
      return item;
    }, 2) ?? 'Non disponibile').slice(0, 30000);
  }
  class ExecutionView {
    constructor(workflowId, executionId, vm) {
      this.workflowId = workflowId; this.executionId = executionId; this.vm = vm;
      this.api = new ApiClient(`${backend}/api/executions/${encodeURIComponent(workflowId)}/${encodeURIComponent(executionId)}`);
      this.native = new ApiClient('/rest');
      this.root = document.createElement('section'); this.root.className = 'bpmn8n-execution'; this.root.dataset.testId = 'live-execution'; this.root.lang = 'it'; this.root.translate = false;
      this.root.innerHTML = `<header class="execution-live-header"><div><h2>Esecuzione #${esc(executionId)}</h2><p class="execution-status" role="status">Caricamento dello stato…</p></div>
        <div class="execution-live-actions"><button data-action="refresh">Aggiorna</button><button data-action="stop">Interrompi</button><a data-action="native" href="?native=1">Vista n8n / debug</a></div></header>
        <p class="execution-connection" role="status">Connessione live…</p><p class="execution-error" role="alert"></p>
        <div class="execution-legend"><span data-state="active">In esecuzione</span><span data-state="ready">Nel motore (non confermato)</span><span data-state="waiting">Attesa al join</span><span data-state="complete">Completato</span></div>
        <p class="execution-canvas-state" role="status">Caricamento del workflow…</p><iframe credentialless class="execution-canvas" title="Workflow BPMN e token dell’esecuzione"></iframe>
        <section><h3>Token e rami aperti</h3><p class="execution-token-help">Ogni item in ingresso è distinto. Le invocazioni tracciate confermano l’attività; lo snapshot del motore e le code ai join restano separati.</p><div class="execution-tokens"></div></section>
        <section><h3>Funzioni invocate</h3><p class="execution-call-count"></p><div class="execution-calls"></div></section>
        <details class="execution-history"><summary>Ultime chiamate completate</summary><div class="execution-recent"></div></details>`;
      this.frame = this.root.querySelector('iframe');
      this.previewSupported = 'credentialless' in this.frame || !['localhost', '127.0.0.1'].includes(new URL(preview).hostname);
      if (this.previewSupported) this.frame.src = `${preview}/workflows/demo?theme=dark&live=1`;
      else { this.frame.hidden = true; text(this.root.querySelector('.execution-canvas-state'), 'La preview locale richiede un browser con isolamento credentialless. Usa la Vista n8n / debug.'); }
      this.receive = event => {
        if (event.source !== this.frame.contentWindow || event.origin !== new URL(preview).origin) return;
        let data; try { data = typeof event.data === 'string' ? JSON.parse(event.data) : event.data; } catch { return; }
        if (data?.command === 'n8nReady') { this.frameReady = true; this.sendWorkflow(); this.sendStates(); }
        if (data?.command === 'bpmn8nCanvasReady') { this.frameDrawn = true; text(this.root.querySelector('.execution-canvas-state'), ''); }
        if (data?.command === 'bpmn8nSelectNode') {
          this.selectedNode = data.nodeName;
          this.renderTokens();
        }
      };
      addEventListener('message', this.receive);
      this.canvasTimeout = setTimeout(() => {
        if (!this.frameDrawn && this.previewSupported) text(this.root.querySelector('.execution-canvas-state'), 'Il canvas non è stato caricato. Riprova con Aggiorna o apri la Vista n8n / debug.');
      }, 15000);
      this.root.querySelector('[data-action=refresh]').addEventListener('click', () => {
        if (!this.frameDrawn && this.previewSupported) this.frame.src = `${preview}/workflows/demo?theme=dark&live=1`;
        this.refresh();
      });
      this.root.querySelector('[data-action=stop]').addEventListener('click', () => this.vm.$emit('stopExecution'));
      this.root.querySelector('[data-action=native]').addEventListener('click', event => {
        event.preventDefault(); this.vm.$router.push({ path: this.vm.$route.path, query: { ...this.vm.$route.query, native: '1' } });
      });
      this.live = new LiveUpdates(`${this.api.base}/stream`, () => this.refresh(), value => text(this.root.querySelector('.execution-connection'), value));
      this.refresh();
    }
    async refresh() {
      if (this.loading) { this.pending = true; return; }
      this.loading = true;
      try {
        const [snapshot, activity] = await Promise.all([this.native.get(`/executions/${encodeURIComponent(this.executionId)}`), this.api.get('/activity')]);
        const execution = snapshot.data.data;
        if (execution.workflowId !== this.workflowId) throw Error('Questa esecuzione appartiene a un altro workflow.');
        execution.data = decodeExecution(execution.data);
        const redacted = execution.data.redactionInfo?.isRedacted;
        if (redacted) throw Error('Dati dell’esecuzione protetti: usa la vista n8n e i permessi di accesso previsti.');
        this.execution = execution; this.state = executionState(execution, activity.data);
        if (!this.workflow) {
          const original = execution.workflowData;
          this.workflow = { name: original.name, nodes: original.nodes.map(({ credentials, ...node }) => node), connections: original.connections, settings: original.settings };
          this.sendWorkflow();
        }
        text(this.root.querySelector('.execution-status'), `${execution.status} · avvio ${date(execution.startedAt || execution.createdAt)} · ${this.state.tokens.length} token aperti`);
        text(this.root.querySelector('.execution-error'), '');
        this.root.querySelector('[data-action=stop]').hidden = this.state.terminal;
        this.renderTokens(); this.renderCalls(); this.sendStates();
        if (this.state.terminal) { this.live.destroy(); text(this.root.querySelector('.execution-connection'), 'Esecuzione terminata · stato finale'); }
      } catch (error) { if (error.name !== 'AbortError') text(this.root.querySelector('.execution-error'), error.message); }
      finally { this.loading = false; if (this.pending) { this.pending = false; this.refresh(); } }
    }
    sendWorkflow() {
      if (this.frameReady && this.workflow) this.frame.contentWindow.postMessage(JSON.stringify({ command: 'openWorkflow', workflow: this.workflow }), new URL(preview).origin);
    }
    sendStates() {
      if (this.frameReady && this.state) this.frame.contentWindow.postMessage({ command: 'bpmn8nTokens', states: this.state.states }, new URL(preview).origin);
    }
    renderTokens() {
      if (!this.state) return;
      const allTokens = this.selectedNode ? this.state.tokens.filter(t => t.node === this.selectedNode) : this.state.tokens;
      const tokens = ['active', 'waiting', 'buffered', 'ready'].flatMap(state => allTokens.filter(t => t.state === state)).slice(0, 100);
      const container = this.root.querySelector('.execution-tokens');
      keyed(container, tokens, t => t.id, () => {
        const el = document.createElement('details'); el.className = 'execution-token'; el.innerHTML = '<summary></summary><p></p><pre></pre>'; return el;
      }, (el, token) => {
        el.dataset.state = token.state;
        text(el.querySelector('summary'), `${token.node} · ${labels[token.state]}${token.itemIndex == null ? '' : ` · item ${token.itemIndex}, ingresso ${token.port}`}${token.runIndex == null ? '' : ` · iterazione ${token.runIndex}`}`);
        text(el.querySelector('p'), `Token ${token.id}${token.binary?.length ? ` · allegati: ${token.binary.join(', ')}` : ''}`);
        if (el.open) text(el.querySelector('pre'), json({ input: token.input, provenienza: token.source, pairedItem: token.pairedItem }));
        el.ontoggle = () => { if (el.open) text(el.querySelector('pre'), json({ input: token.input, provenienza: token.source, pairedItem: token.pairedItem })); };
      });
      let note = this.root.querySelector('.execution-empty-tokens');
      if (!note) { note = document.createElement('p'); note.className = 'execution-empty-tokens'; container.after(note); }
      note.hidden = tokens.length > 0 && allTokens.length <= 100;
      text(note, this.selectedNode ? `Nessun token aperto su ${this.selectedNode}.` : this.state.terminal ? 'Nessun token aperto: esecuzione terminata.' : 'Nessun token aperto nello snapshot disponibile.');
      if (allTokens.length > 100) text(note, `Mostrati 100 di ${allTokens.length} token, dando priorità alle chiamate attive. Seleziona un nodo per restringere la vista.`);
      let clear = this.root.querySelector('.execution-clear-filter');
      if (!clear) { clear = document.createElement('button'); clear.className = 'execution-clear-filter'; clear.textContent = 'Mostra tutti i rami'; clear.onclick = () => { this.selectedNode = null; this.renderTokens(); }; container.before(clear); }
      clear.hidden = !this.selectedNode;
    }
    renderCalls() {
      text(this.root.querySelector('.execution-call-count'), `${this.state.active.length} chiamate attive. Ogni invocazione mantiene input e identità propri, anche sullo stesso nodo.`);
      const render = (selector, rows) => keyed(this.root.querySelector(selector), rows, c => c.id, () => {
        const el = document.createElement('details'); el.className = 'execution-call'; el.innerHTML = '<summary></summary><p></p><pre></pre>'; return el;
      }, (el, call) => {
        text(el.querySelector('summary'), `${call.node_name} → ${call.function}() · ${call.status} · #${call.id}`);
        text(el.querySelector('p'), `${call.source || call.function} · avvio ${date(call.started)}${call.ended ? ` · ${Math.round(call.duration_ms)} ms` : ''}`);
        const detail = async () => {
          if (!el.open) return;
          try { const response = await this.api.get(`/activity/${call.id}`); if (el.open) text(el.querySelector('pre'), json(response.data)); }
          catch (error) { if (error.name !== 'AbortError') text(el.querySelector('pre'), error.message); }
        };
        if (el.open) detail(); el.ontoggle = detail;
      });
      render('.execution-calls', this.state.active); render('.execution-recent', this.state.recent);
    }
    destroy() { clearTimeout(this.canvasTimeout); this.live.destroy(); this.api.destroy(); this.native.destroy(); removeEventListener('message', this.receive); this.root.remove(); }
  }
  const component = viewComponent('Bpmn8nExecutionLive', vm => new ExecutionView(vm.$route.params.workflowId, vm.$route.params.executionId, vm), ['execution']);
  // List metadata changes do not remount the canvas; only switching the execution does.
  component.watch = { '$route.params.executionId'() {
    this.localView?.destroy(); this.localView = new ExecutionView(this.$route.params.workflowId, this.$route.params.executionId, this);
    this.$el.parentNode.insertBefore(this.localView.root, this.$el);
  }, 'execution.status'() { this.localView?.refresh(); } };
  installRoute('ExecutionPreview', 'executionPreview', component, true);
})();
