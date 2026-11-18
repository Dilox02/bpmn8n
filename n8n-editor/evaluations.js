// Local evaluations: indexed data, conditional HTTP reads and stable DOM blocks.
(async () => {
  const source = new URL(document.currentScript.src);
  const { esc, number, measurement, date, metricKey, runKey, text, html, keyed, localURL, sessionURL, ApiClient, LiveUpdates, viewComponent, installRoute } =
    await import(new URL('bpmn8n-live.mjs?v=5', source));
  const backend = localURL(source.searchParams.get('bpmn8n') || 'http://localhost:8000');
  const css = document.createElement('link'); css.rel = 'stylesheet'; css.href = new URL('bpmn8n-evaluations.css?v=4', source); document.head.append(css);
  const statuses = { passed: 'Thresholds met', failed: 'Thresholds not met', unscored: 'Measurements only' };

  class MetricChart {
    constructor() {
      this.root = document.createElement('figure'); this.root.className = 'evaluation-chart';
      this.root.innerHTML = `<figcaption></figcaption><p class="metric-explanation"></p><svg viewBox="0 0 480 190" role="img">
        <g class="chart-grid"></g><path class="chart-line" fill="none"/><path class="chart-threshold" fill="none"/>
        <circle class="chart-last" r="4"/><g class="chart-labels"></g></svg><p></p>`;
    }
    update(metric) {
      if (this.revision === JSON.stringify(metric)) return;
      this.revision = JSON.stringify(metric);
      text(this.root.querySelector('figcaption'), metric.name); text(this.root.querySelector('.metric-explanation'), metric.explanation || metric.node_name);
      this.root.querySelector('svg').setAttribute('aria-label', `${metric.name}: last ${number(metric.last)} ${metric.unit}, ${metric.count} samples`);
      const values = metric.series.map(p => p.value);
      const low = Math.min(0, ...metric.series.map(p => p.minimum), metric.threshold ?? Infinity);
      const high = Math.max(...metric.series.map(p => p.maximum), metric.threshold ?? -Infinity, low + 1);
      const y = v => 150 - (v - low) / (high - low) * 125;
      const x = i => values.length < 2 ? 250 : 52 + i / (values.length - 1) * 410;
      this.root.querySelector('.chart-line').setAttribute('d', values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(' '));
      this.root.querySelector('.chart-last').setAttribute('cx', x(values.length - 1));
      this.root.querySelector('.chart-last').setAttribute('cy', y(values.at(-1)));
      this.root.querySelector('.chart-threshold').setAttribute('d', metric.threshold == null ? '' : `M52,${y(metric.threshold)}H462`);
      html(this.root.querySelector('.chart-grid'), [0, .5, 1].map(t => `<path d="M52,${150-t*125}H462"/>`).join(''));
      html(this.root.querySelector('.chart-labels'), [0, .5, 1].map(t => `<text x="44" y="${154-t*125}" text-anchor="end">${esc(measurement(low+t*(high-low), metric.unit))}</text>`).join('') +
        `<text x="52" y="178">${esc(date(metric.series[0]?.at))}</text><text x="462" y="178" text-anchor="end">${esc(date(metric.series.at(-1)?.at))}</text>`);
      text(this.root.querySelector('p'), `Last ${measurement(metric.last, metric.unit)} · ${metric.count} samples${metric.series.length < metric.count ? ` · ${metric.series.length} intervals (mean)` : ''}${metric.threshold == null ? '' : ` · threshold ${metric.direction === 'higher' ? '≥' : '≤'} ${measurement(metric.threshold, metric.unit)}`}`);
    }
  }

  class EvaluationsView {
    constructor(workflowId) {
      this.workflowId = workflowId; this.runs = []; this.offset = 0;
      this.api = new ApiClient(`${backend}/api/evaluations/${encodeURIComponent(workflowId)}`);
      this.root = document.createElement('section'); this.root.className = 'bpmn8n-evaluations'; this.root.dataset.testId = 'local-evaluations';
      this.root.lang = 'en'; this.root.translate = false;
      this.root.innerHTML = `<header class="evaluation-header"><div><h1>Workflow evaluations</h1><p class="evaluation-description"></p></div>
        <div class="evaluation-actions"><span class="evaluation-connection" role="status">Connecting…</span><button data-action="charts">Charts</button><button data-action="refresh">Refresh</button></div></header><ol class="evaluation-stages"></ol>
        <p class="evaluation-message" role="status" aria-live="polite">Loading…</p>
        <div class="evaluation-empty" hidden><h2>The first measurement starts in the workflow</h2><p>Add a Metrics node, choose the project and define the metrics with n8n expressions.</p></div>
        <details class="evaluation-history"><summary>Execution history</summary><div class="evaluation-table-scroll"><table class="evaluation-runs"><caption>Executions with metrics · last 50</caption><thead><tr><th>Execution</th><th>Last measured</th><th>Project</th><th>Samples</th><th>Evaluation</th></tr></thead><tbody></tbody></table></div></details>
        <section class="evaluation-detail" hidden><header class="evaluation-detail-header"><div><h2></h2><p class="evaluation-label"></p></div><div class="evaluation-actions"><a data-action="execution">Open in Executions</a><a data-action="export">Export JSON</a></div></header>
        <section class="evaluation-overview" aria-label="Progress of the dataset"></section><label class="evaluation-compare">Compare with <select data-action="compare"></select></label>
        <h3>How it is going</h3><p class="evaluation-chart-help">Each chart follows one metric over time. The dashed line is its threshold, when it has one.</p><div class="evaluation-charts"></div>
        <details class="evaluation-all-metrics"><summary>Every metric and threshold</summary><div class="evaluation-table-scroll"><table class="evaluation-metrics"><caption>Metrics by node</caption><thead><tr><th>Metric / node</th><th>Last</th><th>Mean</th><th>Min – max</th><th>Samples</th><th>Threshold</th><th>Outcomes</th><th>Δ mean</th></tr></thead><tbody></tbody></table></div></details>
        <details class="evaluation-storage"><summary>Where the data is stored</summary><p class="evaluation-path"></p></details><details class="evaluation-samples"><summary></summary><div class="evaluation-table-scroll"><table><thead><tr><th>When</th><th>Node</th><th>Iteration:item</th><th>Measurements</th></tr></thead><tbody></tbody></table></div><div class="evaluation-actions"><button data-action="newer">Newer</button><span class="evaluation-page"></span><button data-action="older">Older</button></div></details></section>`;
      this.root.addEventListener('click', event => {
        const target = event.target.closest('button'); if (!target) return;
        if (target.dataset.run) this.select(target.dataset.run);
        if (target.dataset.action === 'refresh') this.refresh();
        if (target.dataset.action === 'charts') this.root.querySelector('.evaluation-charts').scrollIntoView({ block: 'start' });
        if (target.dataset.action === 'older') { this.offset += 100; this.load(); }
        if (target.dataset.action === 'newer') { this.offset = Math.max(0, this.offset - 100); this.load(); }
      });
      this.root.querySelector('[data-action=compare]').addEventListener('change', async event => {
        this.baselineKey = event.target.value; await this.loadBaseline(); this.renderDetail();
      });
      this.live = new LiveUpdates(`${this.api.base}/stream`, () => this.refresh(), value => text(this.root.querySelector('.evaluation-connection'), value));
      this.refresh();
    }
    message(value, error = false) { const el = this.root.querySelector('.evaluation-message'); text(el, value); el.dataset.error = String(error); }
    async refresh() {
      if (this.loading) { this.pending = true; return; } this.loading = true;
      const button = this.root.querySelector('[data-action=refresh]'); button.disabled = true;
      try {
        const [result, context] = await Promise.all([this.api.get('/runs'), this.api.get('/context')]);
        if (context.changed) { this.context = context.data; this.renderContext(); }
        if (result.changed) { this.runs = result.data.runs; this.renderRuns(); }
        if (!this.selectedKey && this.runs.length) {
          const preferred = this.runs.find(r => r.label === this.context?.preferred_run_label) || this.runs[0];
          this.selectedKey = runKey(preferred); this.renderRuns();
        }
        await this.load(); await this.loadBaseline();
        this.message(this.runs.length ? '' : 'No measured run yet: connect a Metrics node to start.');
      } catch (error) { if (error.name !== 'AbortError') this.message(error.message, true); }
      finally { this.loading = false; button.disabled = false; if (this.pending) { this.pending = false; this.refresh(); } }
    }
    async select(key) { this.selectedKey = key; if (this.baselineKey === key) { this.baselineKey = ''; this.baseline = null; } this.offset = 0; await this.load(); this.renderRuns(); }
    async load() {
      if (!this.selectedKey) return;
      const selection = this.selectedKey, offset = this.offset;
      try {
        const result = await this.api.get(`/runs/${selection.split('/').map(encodeURIComponent).join('/')}?limit=100&offset=${offset}`);
        if (selection !== this.selectedKey || offset !== this.offset) return;
        if (result.changed || !this.selected || runKey(this.selected) !== selection || this.selected.offset !== offset) {
          this.selected = result.data; this.renderDetail();
        }
      } catch (error) { if (error.name !== 'AbortError') this.message(error.message, true); }
    }
    async loadBaseline() {
      if (!this.baselineKey) { this.baseline = null; return; }
      try {
        const key = this.baselineKey;
        const result = await this.api.get(`/runs/${key.split('/').map(encodeURIComponent).join('/')}?limit=1`);
        if (key === this.baselineKey) { this.baseline = result.data; if (result.changed) this.renderDetail(); }
      } catch (error) { if (error.name !== 'AbortError') this.message(error.message, true); }
    }
    renderRuns() {
      this.root.querySelector('.evaluation-empty').hidden = !!this.runs.length;
      keyed(this.root.querySelector('.evaluation-runs tbody'), this.runs, runKey, () => { const el = document.createElement('tr'); el.innerHTML = '<td><button></button><small></small></td><td></td><td></td><td></td><td><span class="evaluation-status"></span></td>'; return el; }, (el, run) => {
        el.dataset.selected = String(this.selectedKey === runKey(run));
        const cells = el.children; const button = cells[0].querySelector('button'); button.dataset.run = runKey(run);
        text(button, `#${run.execution_id}`); text(cells[0].querySelector('small'), run.label);
        text(cells[1], date(run.updated_at)); text(cells[2], run.project); text(cells[3], number(run.sample_count));
        const status = cells[4].querySelector('span'); status.dataset.status = run.status; text(status, statuses[run.status]);
      });
      const select = this.root.querySelector('[data-action=compare]');
      keyed(select, [{ value: '', label: 'No comparison' }, ...this.runs.filter(r => runKey(r) !== this.selectedKey).map(r => ({ value: runKey(r), label: `#${r.execution_id} · ${r.label || date(r.updated_at)}` }))], o => o.value,
        () => document.createElement('option'), (el, option) => { el.value = option.value; text(el, option.label); });
      select.value = this.baselineKey || '';
    }
    renderDetail() {
      const run = this.selected; if (!run) return;
      const detail = this.root.querySelector('.evaluation-detail'); detail.hidden = false;
      text(detail.querySelector('h2'), `Results of run #${run.execution_id}`); text(detail.querySelector('.evaluation-label'), run.label);
      text(detail.querySelector('.evaluation-path'), run.path);
      detail.querySelector('[data-action=execution]').href = `/workflow/${encodeURIComponent(this.workflowId)}/executions/${encodeURIComponent(run.execution_id)}`;
      detail.querySelector('[data-action=export]').href = sessionURL(`${this.api.base}/runs/${this.selectedKey.split('/').map(encodeURIComponent).join('/')}/export`);
      this.renderOverview();
      const comparison = new Map((this.baseline?.metrics || []).map(m => [metricKey(m), m]));
      keyed(detail.querySelector('.evaluation-metrics tbody'), run.metrics, metricKey, () => document.createElement('tr'), (el, m) => {
        const base = comparison.get(metricKey(m)), delta = base ? m.mean-base.mean : null;
        html(el, `<td><strong>${esc(m.name)}</strong><small>${esc(m.node_name)}</small></td><td>${number(m.last)} ${esc(m.unit)}</td><td>${number(m.mean)}</td><td>${number(m.minimum)} – ${number(m.maximum)}</td><td>${m.count}</td><td>${m.threshold == null ? '—' : `${m.direction === 'higher' ? '≥' : '≤'} ${number(m.threshold)}`}</td><td>${m.checked_count ? `${m.passed_count} / ${m.checked_count} met` : 'Measurement only'}</td><td>${delta == null ? '—' : `${delta > 0 ? '+' : ''}${number(delta)}`}</td>`);
      });
      const chartMetrics = run.metrics.filter(m => m.count > 1);
      keyed(detail.querySelector('.evaluation-charts'), chartMetrics.length ? chartMetrics : run.metrics, metricKey,
        () => { const chart = new MetricChart(); chart.root.chart = chart; return chart.root; }, (el, metric) => el.chart.update({ ...metric, explanation: this.context?.metrics?.[metric.name] }));
      text(detail.querySelector('.evaluation-samples summary'), `Recorded samples (${run.sample_count})`);
      keyed(detail.querySelector('.evaluation-samples tbody'), run.samples, s => `${s.node_id}/${s.sample_id}`, () => document.createElement('tr'), (el, sample) => {
        html(el, `<td>${esc(date(sample.recorded_at))}</td><td>${esc(sample.node_name)}</td><td>${esc(sample.sample_id)}</td><td>${sample.metrics.map(m => `${esc(m.name)}: ${number(m.value)} ${esc(m.unit)}`).join('<br>')}</td>`);
      });
      detail.querySelector('[data-action=newer]').disabled = !run.offset; detail.querySelector('[data-action=older]').disabled = !run.has_more;
      text(detail.querySelector('.evaluation-page'), `${run.offset+1}–${Math.min(run.offset+run.limit, run.sample_count)} of ${run.sample_count}`);
    }
    renderContext() {
      text(this.root.querySelector('h1'), this.context.title);
      text(this.root.querySelector('.evaluation-description'), this.context.description);
      keyed(this.root.querySelector('.evaluation-stages'), this.context.stages || [], value => value,
        () => document.createElement('li'), (el, value) => text(el, value));
      if (this.selected) this.renderOverview();
    }
    renderOverview() {
      const run = this.selected, config = this.context?.progress;
      const overview = this.root.querySelector('.evaluation-overview');
      if (!config) {
        html(overview, `<p>${esc(statuses[run.status])}. The thresholds refer to the samples collected. Open Executions for the engine's state.</p>`);
        return;
      }
      const get = role => (run.metrics.find(m => m.node_name === config.node && m.name === config[role]) || run.metrics.find(m => m.name === config[role]))?.last ?? 0;
      const accepted = get('accepted'), rejected = get('rejected'), pending = get('pending'), total = accepted+rejected+pending;
      const final = run.metrics.find(m => m.name === config.quality);
      const passed = final && final.threshold != null && (final.direction === 'higher' ? final.last >= final.threshold : final.last <= final.threshold);
      const failures = run.metrics.filter(m => m.checked_count && m.name !== config.quality && m.threshold != null && (m.direction === 'higher' ? m.last < m.threshold : m.last > m.threshold));
      const outcome = final ? passed ? 'Final checks passed' : 'The final checks did not promote the dataset'
        : pending ? 'Partial data: processing still to complete' : 'Processing finished: waiting for the final checks';
      html(overview, `<h3>${esc(outcome)}</h3><p>${number(accepted+rejected)} of ${number(total)} ${esc(config.completed_label || 'items resolved')}. ${pending ? 'The final quality is known after the export.' : 'Rejected items stay out of the export.'}</p>
        <progress max="${Math.max(total, 1)}" value="${accepted+rejected}" aria-label="Items resolved"></progress>
        <dl><div><dt>${esc(config.accepted_label || 'Accepted')}</dt><dd>${number(accepted)}</dd></div><div><dt>${esc(config.rejected_label || 'Rejected')}</dt><dd>${number(rejected)}</dd></div><div><dt>${esc(config.pending_label || 'To process')}</dt><dd>${number(pending)}</dd></div></dl>
        ${failures.length ? `<p><strong>Thresholds not met</strong></p><ul>${failures.map(m => `<li>${esc(m.name)}: ${esc(measurement(m.last, m.unit))} · target ${m.direction === 'higher' ? '≥' : '≤'} ${esc(measurement(m.threshold, m.unit))}</li>`).join('')}</ul>` : ''}
        <p class="evaluation-note">${esc(config.note || 'The state of active calls is in Executions.')}</p>`);
    }
    destroy() { this.live.destroy(); this.api.destroy(); this.root.remove(); }
  }
  installRoute('Evaluation', 'default', viewComponent('Bpmn8nLocalEvaluations', vm => new EvaluationsView(vm.workflowId || vm.$route.params.workflowId), ['workflowId']));
})();
