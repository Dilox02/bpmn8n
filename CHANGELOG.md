# Changelog

Versions follow semver. The version lives in `bpmn8n/__init__.py`, `pyproject.toml` and
`n8n-nodes-bpmn8n/package.json` (a test keeps the three equal); `python -m bpmn8n version` prints it.

## 0.2.0

The `feat/framework-v2` branch (docs/PLAN.md section 8 lists what is done and what is partial).

- **Foundation**: one element registry (`/api/elements`) with a drift test; `ProcessGraph`; a `TokenEngine`; the
  process contract (JSON Schema, OpenAPI, `/api/contracts/<id>`, `manage.py process_contract`) and the landscape.
- **One platform**: an opt-in one-origin gateway, shared design tokens and theme, deep links, one generated
  contract with a version handshake, one configuration (`manage.py config`), the case page, shared web components,
  and the `python -m bpmn8n` CLI (`up`, `down`, `status`, `doctor`, `logs`, `export`, `import`, `deploy`, `diff`,
  `pull`, `backup`, `restore`, `schedule`, `version`). The top bar shows the health of n8n, the models, Redis and
  search; the n8n editor carries a slim bpmn8n strip.
- **BPMN coverage**: deadline and error boundaries, reminders and escalation (`run_deadlines`), signals, terminate
  end, loops, call-activity data mapping, compensation (sagas); BPMN 2.0 XML export and import with swimlanes.
- **Method**: rules as classes, naming, implicit gateways, deadlock search.
- **Simulation**: scenarios, discrete-event simulation with confidence intervals, queueing network, staffing,
  calibration, model against reality; page, API, CLI and canvas mode.
- **Testing**: scenario tests, differential tests against n8n, drift checks, `seed_events`, CI.
- **Production**: a Docker image and production override with pinned n8n, secure settings, `workflows
  diff/push/pull`, process versions and history, encrypted LLM keys, audit log, erasure by case, Prometheus and JSON
  logs, idempotent task runs, backup and restore, a `scheduler` service, and `manage.py prune` with retention.
- **People**: priorities and SLAs, suspend, reassign and delegate, four-eyes and quorum, form types, human in the loop.
- **Intelligence**: heatmap, filters, statistics, cases and variants, cohorts, queueing on the log.
- **Agents**: pattern macros, guardrails, budgets, error paths and retries; local Evaluations and the Metrics node.
- **n8n's paid features, open**: global variables, workflow history, log streaming, case search, Insights, source
  control.

## 0.1.0

The first public version: Django tasks as n8n nodes, checked workflows, Studio, process mining.
