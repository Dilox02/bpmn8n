# bpmn8n — framework plan

What to add, what to refactor and in which order, to make bpmn8n the most complete and best-engineered
framework for **building, testing, simulating, deploying and monitoring** BPMN workflows on n8n.
Every item is traced to the code it changes and to the document it comes from.

Written 2026-11-18 from a read of the whole repository (commit `589cc45`, branch `feat/subprocess-start`) and of the nine
documents in `bpmn materials/`. English, like the rest of the repository.

**Contents**
1. [Where the framework stands](#1-where-the-framework-stands) · 2. [What the nine documents give us](#2-what-the-nine-documents-give-us) ·
3. [BPMN 2.0 coverage matrix](#3-bpmn-20-coverage-matrix) · 4. [The plan: eleven workstreams](#4-the-plan-eleven-workstreams) ·
5. [Phasing](#5-phasing) · 6. [Risks, non-goals, decisions](#6-risks-non-goals-open-decisions) · 7. [Appendix: sources and method](#7-appendix-sources-and-method)

## Summary: ten moves

| # | Move | Why it comes first |
|---|---|---|
| 1 | **An element is a class, a workflow is a graph** (element registry + one `ProcessGraph`) | Today one BPMN element is spread over 8 files and the workflow graph is re-derived 3 times. Everything below (engine, simulation, XML, lint) derives from these two. |
| 2 | **One platform, two servers**: Django is the shell, n8n is the canvas; one origin, one identity, one token set, one vocabulary, one contract, one CLI | The two servers share no design system, no login, no config, no contract; four workarounds exist only to bridge them (§1.4). |
| 3 | **Test the process, not only the tasks**: dry-run engine, scenario tests, recorded model calls, CI | `examples.json` tests functions; nothing tests "this case takes this path" without a running n8n. No CI exists. |
| 4 | **Simulation and capacity**: analytic queueing first, event simulation second, calibrated from your own event log, shown on the canvas | Absent today. The sources give the full recipe (BPMN simulation course + queueing notes), and the event log is the unfair advantage. |
| 5 | **Production path**: images, versioned deployments (git → n8n with diff and drift detection), process version stored per case, backups, observability | `web` runs `pip install` at boot under `gunicorn --reload`; n8n is unpinned; no version is stored per case. |
| 6 | **Human work**: SLAs, priority, escalation (boundary timers), reassign, four-eyes, quorum, richer forms | Bonita's user experience is the benchmark; bpmn8n has claim/complete only. |
| 7 | **BPMN coverage**: boundary events, signals, terminate, multi-instance, loops, compensation, lane bands, BPMN XML in/out | The poster and Allweyer define what "complete" means (§3). |
| 8 | **Process intelligence**: map overlays, filter stack, statistics, cohort comparison, import wizard | Disco is the benchmark; PM4Py already does the maths. |
| 9 | **Agentic patterns as vetted templates**: reflection, guardrails, task-as-tool, HITL vs HOTL, evaluation gates | Cimino's 2026 course is the catalogue; bpmn8n has one generic `llm_agent`. |
| 10 | **Method as lint**: Sharp's naming, levels and swimlane rules checked and taught to Studio | Already half there (`MUSHY_VERBS`); the rest is cheap and shapes every model Studio writes. |

**Suggested first eight PRs** (all green on today's tests, each useful alone):
1. CI (GitHub Actions): ruff, `manage.py test`, `npm test`, `node --test`. (F10)
2. Drift test: every Python node parameter exists in the TS node and vice versa. (F6)
3. `ProcessGraph` extracted from `check.py`; `check`, `mining/model.py` and `layout()` use it. (A2)
4. Element registry with the traits `waits / decides / human / transparent`; replaces the type-sets in `check.py`. (A1)
5. `vocab` + `tokens.css` shared by Django and the n8n hooks; Italian leftovers removed. (B3, B10)
6. `process_version` recorded on `Case` and `Event`. (G4)
7. `Dockerfile` for `web`, pinned n8n tag, no `--reload` in the prod profile. (G1)
8. Analytic token-count and M/M/c calculator behind `manage.py simulate --analytic`. (E2, E5)

---

## 1. Where the framework stands

### 1.1 Shape and numbers

| Area | Lines | Notes |
|---|---:|---|
| Python framework (`bpmn8n/`, no tests/migrations) | 6 112 | api 945 · studio 972 · workflows 829 · models 629 · core 598 · clients 594 · mining 497 · management 490 · web 255 |
| Python tests | 1 959 | 146 test functions; e2e 1 009 lines / 33 functions on the live stack |
| n8n nodes (TypeScript) | 1 125 | 15 nodes on 4 base classes (`TaskNode`, `GatewayNode`, `WaitingNode`, `HumanTaskNode`) plus plain `INodeType`; 14 node tests |
| n8n editor hooks (JS/CSS) | 1 032 | `bpmn.js` canvas look, `panel.js` node card, `preview.js`, `evaluations.js`, `executions.js`, `live.mjs`; 10 tests |
| Templates + CSS (Django) | 779 | "framed-sheet" design, IBM Plex, light/dark by OS |
| Orchestration | 208 | `start.sh` (Bash: build, models, compose, n8n login, credential, API key, export/import) |

Six projects are tracked in the repository (`acme`, `hospital`, `hospital_appointment_booking_uc_01`,
`motor_insurance_claim`, `personal_data_breach_response`, `erasmus_learning_agreement`).

### 1.2 What is already strong (keep and build on)

- **Event log as the spine**: every step is an `Event` row in Postgres with input/output (`core/observability.py`); mining, metrics, live views and training sets all read it.
- **Wait state machine** (`models/waiting.py`): template-method base `WaitingActivity` with transactional transitions; `UserTask` and `MessageSubscription` are two small subclasses. This is the right shape to extend (signals, deadlines, reassign).
- **Case variables** with a sandboxed condition evaluator (`core/variables.py`, no `eval`), merged under a row lock.
- **The workflow check** does real data-flow analysis (dominators, join semantics, provenance of human decisions) and encodes lessons from live failures.
- **`@task` → OpenAPI → n8n node** with typed schemas, example-based tests and `check_project`.
- **Studio** is gated: a project reaches the server only after import, examples and workflow check pass; the MCP server exposes the same steps.
- **Honesty**: the guide lists known limits and falsified hypotheses (H11). Keep this habit; the plan adds to it.

### 1.3 DRY and OOP debt (evidence)

| Knowledge | Where it lives today | Cost |
|---|---|---|
| **What a BPMN element is** (type string, kind, outputs, traits, symbol, summary) | `workflows/builder.py`, `workflows/check.py`, `mining/model.py`, `api/gateways.py`, `n8n-editor/bpmn.js`, `n8n-editor/panel.js`, `tools/build_icons.py`, `nodes/Bpmn8n<Kind>.node.ts`, `package.json`, plus the GUIDE, `mcp.py` and `prompts.py` text | *exclusive gateway* is named in 8 code files, *timer* in 6. Adding an element is 8+ edits and no test notices a missed one. |
| **The graph of a workflow** (successors, predecessors, reachability, dominators) | `check.py` (`outs`, `into`, `after`, `dominators`), `mining/model.py` (`outputs`), `builder.layout()` (`after`, `forward`) | n8n's semantics (AND split, XOR join, Merge as OR join) are re-derived three times and kept in sync by hand. A fourth consumer (simulation) would make it four. |
| **Node parameters** (names and shapes) | TS `properties`, builder keyword arguments, `check.py` `params.get(...)`, `SPEC_EXAMPLE` in `prompts.py`, MCP tool text | No test compares TS ↔ Python. A renamed parameter fails only when a case reaches the node. |
| **The n8n ↔ Django protocol** | `Bpmn8nClient.post` (6 `X-Bpmn8n-*` literals, hand-built bodies in `inputRequest`) vs `Trace.of` + `RunIn` (7 literals) | The request body is mirrored by hand; OpenAPI exists but only the task *catalogue* is read from it. |
| **Vocabulary** (statuses, durations, token states) | Python models, templates, `duration.ts` `UNIT_MS` vs `check.waits()`, JS (`attesa/attivo/fatto`, `tinta`, `colora`), Italian strings in `api/executions.py` | Two languages in one repository; the same words defined in five places. |
| **Design tokens** | `app.css` `:root` vs hard-coded hex in `bpmn.js` (`#0d0d12`, `#22242a`…), `preview.js` token colours, `evaluations.css`, `executions.css`, `panel.js` | At least three palettes for one product. |
| **Configuration** | `conf.py` DEFAULTS · `settings.py` · compose `environment` · `.env` written by `start.sh` · TS `process.env` · `.mcp.json` | Three URLs for n8n (`N8N_URL`, `N8N_PUBLIC_URL`, `N8N_PREVIEW_URL`) plus `BPMN8N_PUBLIC_URL`; `HumanTaskNode.ts` hard-codes `localhost:8000` as default. |
| **A workflow's definition** | `process.py` spec → `workflow.json` → n8n database → `workflows/*.json` export | Four copies, no declared source of truth, no drift detector. |
| **Rules documentation** | rules in `check.py`, a table in the GUIDE, text in the Studio prompt | Edited separately. |

OOP verdict. **Good**: `WaitingActivity` (template method), `Task`, `ModelClient`/`LLM` hierarchy, `WorkflowCheck`
(one method per rule), `ChangeStream`, `Trace`/`Span`. **Procedural hot spots**: `Workflow` (one method per step plus a
`STEPS` tuple; `layout()` is 80 lines doing five jobs); `check.py` (type-sets `HUMAN`, `WAITING`, `TRANSPARENT`,
`DECIDING` and `if node["type"] in …` ladders instead of polymorphism); `ProjectBuilder` (319 lines: LLM loop, file
writes, n8n import and a `SIGHUP` to gunicorn); `mining/model.py` (type switches); `web/views.py` (dict rows built by hand).

### 1.4 The two web servers today

| Server | Process | Serves |
|---|---|---|
| **Django** (gunicorn, `:8000`) | `web` | `/inbox`, `/inbox/<id>`, `/studio`, `/activities`, `/docs`, `/admin`, `/api` (+ Swagger), `/login` |
| **n8n** (`:5678`, queue mode; workers; `:5679` preview) | `n8n`, `n8n-worker`, `n8n-preview` | the editor with four injected hooks (`bpmn.js`, `panel.js`, `evaluations.js`, `executions.js`; the preview has `preview.js`), the BPMN nodes, webhooks |

| Axis | Django | n8n | Inconsistency and the workaround that exists today |
|---|---|---|---|
| **Origin** | `localhost:8000` | `localhost:5678`, `:5679` | CORS middleware `openapi_cors` (a 28-line special case), `N8N_CONTENT_SECURITY_POLICY frame-ancestors` lists in compose, `credentialless` iframes, and a rule that both must use the same *hostname* because cookies ignore ports (GUIDE, "Live evaluations") |
| **Identity** | Django users and groups (lanes) | n8n owner account | `evaluation_read_auth` forwards the browser's `n8n-auth` cookie to n8n's internal `/rest` to prove who you are; `start.sh` stores `N8N_EMAIL/PASSWORD` in `.env` |
| **Design** | framed-sheet, IBM Plex, light/dark by OS | forced dark `#0d0d12` + a flat hue per node name; `bpmn.js` overwrites the user's `N8N_THEME` on every load; evaluations/executions have their own CSS | Three visual languages |
| **Navigation** | sidebar | n8n's sidebar | No link from a Django case to its canvas or back, except the task link in the node panel |
| **Contract** | OpenAPI (generated) | hand-written TS | The body of every task call is built by hand in TS |
| **Config / ops** | env + DB `Settings` | compose env, `EXTERNAL_FRONTEND_HOOKS_URLS` with `?v=N` cache busters bumped by hand | `start.sh` is the only operations surface; health = three different URLs |
| **Language** | English | English | Italian leftovers (`api/executions.py`: "ulteriori livelli", "caratteri"; `preview.js`: `stati/attesa/attivo/fatto`) |

### 1.5 Testing and production gaps

| Gap | Evidence |
|---|---|
| No CI | no `.github/`; e2e needs the whole stack and a Mac models server |
| No process-level test | `examples.json` tests a task; `check` is structural; the only execution tests need n8n |
| No deterministic model calls | Laya, GLiNER and LLM answers are live in every e2e |
| No workflow version per case | the guide's own known limit: conformance uses the *current* version |
| `web` is not an image | `python:3.12-slim` + `pip install` at boot, `gunicorn --reload`, `DEBUG` defaults to `1`, `ALLOWED_HOSTS=*`, API open without a token |
| n8n unpinned | `docker.n8n.io/n8nio/n8n` with no tag, while `bpmn.js` matches hashed class names and `panel.js` reads Pinia stores |
| Promotion = whole instance | `./start.sh export/import` moves *all* workflows and unpublishes them (the script re-publishes by hand) |
| Secrets | LLM provider keys live in the database in clear text |
| No retention or erasure | `Case.variables` and `Event.data` keep inputs and outputs forever |
| Mining in memory | ~1 GB per 5 M events (documented limit) |
| No simulation, no SLA, no boundary events, no compensation | confirmed by search: none of these words occur in the code |

---

## 2. What the nine documents give us

Tags used in the plan: **[AW]** Allweyer · **[BO]** Bonita · **[DI]** Disco · **[AE]** Cimino, Agentic Engineering ·
**[BS]** Cimino, BPMN Modeling and Simulation · **[QT]** Stea, Queueing Theory · **[WM]** Cimino/Sharp, Workflow Modeling ·
**[OC]** Cimino et al., oral-cancer paper · **[PO]** BPMN poster. Page numbers are PDF pages; `A…`/`B…` are the statement
numbers printed in [WM] and [BS].

### [AW] Allweyer, *BPMN 2.0* (2009, 156 pp)
| Feature taken | Where | Plan |
|---|---|---|
| Implicit splits and joins, "not okay" patterns, different instances at a parallel join | §2.3, §3.3, §4.1–4.3 | D5, D6 |
| Gateway semantics: inclusive merge, complex gateway | §3.4–3.5 | A3 (semantics), complex = non-goal |
| Collaborations, message flows, private vs public process, multi-instance participants, message content | §5 | C12, C18, A4 |
| Start, end, intermediate events; event-based decisions | §6 | C4, C5, C10 (instantiating event-based gateway = non-goal) |
| Sub-process, loops and multi-instance, ad-hoc, task types, call activity, global tasks | §7 | C6, C7, C8 |
| Interrupting and non-interrupting events, errors, escalation, event sub-processes | §8 | C1, C2, C3 |
| Transactions, compensation | §9 | C9 |
| Data objects, data stores, data to called activities | §10 | C8, C14 |
| Choreographies, conversations | §11–12 | C18 (documentation view only) |
| Business model vs executable model; governance, risk, compliance | §1.1, §1.4 | G3, K7 |

### [BO] Bonita Open Solution 5.5 User Guide (2011, 140 pp)
| Feature taken | Where | Plan |
|---|---|---|
| Actor selectors and filters: group, list, dynamic, *same as previous*, random candidates | §2.3.1–2.3.4 | H3 |
| Typed data variables, expression editor | §2.4 | A4 |
| Call activity with data mapping parent ↔ sub-process; event sub-process | §2.5 | C8 |
| Loop; multi-instantiation with *instantiator* and *join checker* | §2.6–2.7 | C6, C7 |
| Timers: start, intermediate, **boundary (deadline)** | §2.8, §2.2.10 | C1, C2 |
| Messages, errors, signals, links; transactions | §2.9–2.13 | C3, C4, C9, C11 |
| Connectors; **"disconnect connectors to test run a process"** | §2.14 | F3, F4, K9 |
| Simulation parameters on tasks, events and transitions | §2.2.5.5, §2.2.6.5, §2.2.8.3, §2.19 | E1 |
| Export/import/duplicate process; categories | §2.15, §2.17 | G3 |
| Forms: widgets, mandatory/read-only, conditional display, validators, multi-page | §3.2 | H5 |
| Task priority; assign, unassign, suspend, resume a step | §2.2.5.2, §4.3.1 | H1, H2 |
| Case labels, case overview, workload dashboard, start a case | §4.3.2–4.3.5 | H6, H7, H9 |
| Administrator sees the process graphically inside a case | §4.4.2.1 | H6, B12 |

### [DI] Disco User Guide (2015, 155 pp)
| Feature taken | Where | Plan |
|---|---|---|
| Minimum log = case id, activity, timestamp (+ resource, attributes); extraction checklist | §1.2–1.3 | I7 |
| Import wizard: column roles, timestamp patterns, combined columns, several timestamps, troubleshooting; MXML/XES | §3.1–3.3 | I7 |
| Map with level-of-detail sliders (activities, paths), search, frequency and performance metrics, animation | §4.1 | I1, I2, I4 |
| Statistics: global, activity, resource, attribute | §4.2 | I5 |
| Cases view: variants, individual cases, search | §4.3 | I6 |
| Filters: timeframe, variation, performance, endpoints, attribute, follower; filter stack; copy as scenario | §5, §6.2.1 | I3 |
| Export: process map, event log (XES/MXML/CSV), **anonymisation**, charts, projects | §6.3, §7 | I8, I11 |

### [AE] Cimino, *Agentic Engineering* (2026, 88 slides; mostly figures)
| Pattern | Slides | Plan |
|---|---|---|
| Levels of agent complexity; five hypotheses on the future of agents | 11–19 | context for J1 |
| Prompt chaining · Router · Parallel · Reflection (self, producer + critic) | 20–28 · 30–33 · 34–35 · 36–39 | J1, J4 |
| Tool use · Planning · Multi-agent collaboration | 40–42 · 43–45 · 46–48 | J1, J2 |
| Memory management · Learning and adaptation · Goal setting and monitoring | ≈49–60 | J5, J6, J8 |
| **Exception handling and recovery** | 61–62 | J7, G8 |
| **Human-in-the-loop vs human-on-the-loop** | 63–66 | H10 |
| Inter-agent communication: **MCP vs A2A**, agent cards | 67–69 | A4, J11 |
| Reasoning (CoT, ToT, ReAct) | 70–72 | J12 |
| Guardrails and safety · Evaluation and monitoring · Prioritization · Exploration and discovery | ≈75–86 | J3, J9, J10, J13 |

### [BS] Cimino, *BPMN Modeling and Simulation* (lectures, 96 pp A4×2)
| Feature taken | Where | Plan |
|---|---|---|
| Semi-formal numbered notation of a process (rules I–IX) | p.17, 52, 77 | D11 |
| **Scenario analysis by token counting**: 100 tokens, branch percentages, Σ ending tokens = 100 (library, hospital, pizza, job application) | p.18–21, 53–60 | E2 |
| Simulation set-up: task duration and cost, branch percentages, arrivals (peak N), lane instances | p.23–28 | E1 |
| Outputs: queue time per task, cost per object and per instance, resource usage over time, completion per scenario | p.26–28, 62–64 | E3 |
| **Simulation vs emulation**: the tool cannot run parallel paths in one scenario, so a redundant "emulation" model is needed; resources get allocated unevenly with few tokens; inter-arrival time faked with a timer in a 1-instance lane and then subtracted | p.65–68, 73–75 | E12 (native parallel simulation) |
| What-if staffing under constraints (max per lane 20, total ≤ 70/75, duration < 200 min), sensitivity per lane | p.45–51, 90–96 | E6 |
| **Process simulation spec**: `PSS` arrivals, `RES` resources, `WS` work schedule, `TSK` tasks, `GTW` gateways; outputs `GI`, `CH` | p.83–85 (B47–B49) | E1, E3 |
| **Stochastic runs → confidence intervals**, normality check (Q-Q plot) | p.89 (B50–B57) | E4 |
| Toolchain: editor → `.bpmn` → Signavio → BIMP | p.80–82 (B42–B46) | C13 |
| Level 0–3 decomposition; expanded sub-process cannot connect outside; interruption pattern | p.2–3 | D4, C1 |
| Pools, lanes, message flows, data objects, claims-process modelling exercise | p.10–13 | C12, C14 |

### [QT] Stea, *Notes on Queueing Theory* (107 pp)
| Feature taken | Where | Plan |
|---|---|---|
| Birth-death analysis; M/M/1 indices and distributions | §3.2–3.3 | E5 |
| **M/M/c** (Erlang C), delay centres M/M/∞ | §3.4 | E5 |
| Finite capacity M/M/1/K (blocking), finite populations, bulk arrivals | §3.6–3.8 | E5 (K), later |
| Non-exponential service (M/G/1, insensitivity) | §3.9 | E5 |
| **Open networks**, Burke, **feedback loops**, traffic equations | §4.1–4.3 | E5 |
| Closed networks, Buzen's convolution; classed networks; processor sharing | §4.4–4.5, §5 | non-goal until WIP-limited processes exist |

### [WM] Cimino, Workflow Modeling methodology (after Sharp, 2009; 32 pp)
| Rule | Where | Plan |
|---|---|---|
| Process names: verb + noun, singular; "noun is verbed"; **no result, no process**; avoid mushy verbs | A06–A16, A22–A29 | D2 (the verb list is already `MUSHY_VERBS`) |
| Trigger: action, temporal or condition event; trigger ≠ precondition | A17–A21 | D2, D10 |
| 5 ± 2 sub-processes; same token moves through the process | A30–A32 | D4 |
| Scope: WHAT (triggers, results, sub-processes, cases), WHO, HOW (mechanisms, measures) | A33–A35 | D10 |
| Discovery: nouns → verbs → result-trigger chains → process boundaries (1:1 clusters = a process; 1:M/M:1 = separate) | A36–A48 | D10 |
| Swimlanes: actor order, **collaborative ≠ concurrent**, handoff = where delay, errors, expense arise | A49–A52, A63–A64 | C12, D8 |
| Decision computed *before* the diamond; label branches with statistics | A70–A71 | D3 |
| Levels: **handoff / service / task**; stop when the cause is understood | A72–A96 | D4 |
| Show every actor that holds the work; committees, systems, batch jobs, holding areas as actors | A97–B31 | C12, D8 |
| Scheduled steps, time scale on the diagram, "cloud" for unknown parts | B32–B34 | D9 |
| Cleaning over-complex diagrams | B35–B41 | D4 |

### [OC] Cimino et al., *Explainable screening of oral cancer*, Smart Health 35 (2025)
| Feature taken | Where | Plan |
|---|---|---|
| A **BPMN protocol validated experimentally** with human experts; deep-learning output explained by **case-based reasoning** (similar past cases) | abstract, §3.4, Fig. 5 | H11 |
| **Heatmap of execution counts** on the BPMN (hot = frequent) | §4.4, Fig. 22 | I1 |
| **Gateway percentages per user group** (specialised vs resident doctors) | Table 7 | I9, D3 |
| A **model-drift** gateway that triggers retraining | Fig. 5, Table 7 j) | I12, J6 |

### [PO] BPMN poster
The element inventory behind §3: activities, markers, task types, events, gateways, data, pools, conversations, choreographies.

---

## 3. BPMN 2.0 coverage matrix

✅ have · 🟡 partial · ❌ missing and feasible · ⛔ not possible in n8n (emulate or document only).

| Element | Now | n8n feasibility | Plan |
|---|:--:|---|---|
| **Start**: none (sub-process start) · message (webhook, form) · timer (schedule) | ✅ | native | — |
| Start: conditional · signal · multiple | ❌ | poller / subscription | C4, C10 |
| Start: error, escalation, compensation (event sub-process) | 🟡 | error = n8n Error Trigger workflow; others separate workflow | C2, C3 |
| **Intermediate catch**: timer · message (receive task, event-based gateway) | ✅ | native | — |
| Intermediate catch/throw: signal · escalation · link · conditional · compensation | ❌ | Django subscriptions and pollers | C4, C2, C9, C10, C11 |
| Intermediate throw message | 🟡 | a Send Task does it | — |
| **Boundary**: timer (interrupting and not) · message · escalation · signal · conditional | ❌ | interrupting = `timeout` output of a wait; non-interrupting = child execution | C1, C2 |
| Boundary: error | 🟡 | n8n "continue using error output" | C3 |
| **End**: none (implicit) · error | ✅ | native | — |
| End: terminate · signal · escalation · message · cancel · compensation | ❌ | terminate through n8n API stop; others as events | C5, C9 |
| **Tasks**: service, script, business rule, send, receive, user, manual | ✅ | native | — |
| Markers: **multi-instance parallel** | 🟡 | one wait per item; no completion condition | C6 |
| Markers: **sequential multi-instance**, **loop** | ❌ | Loop Over Items / flow back with a guard | C6, C7 |
| Markers: compensation · ad-hoc | ❌ | ledger of completed tasks · not natural | C9 · non-goal |
| **Sub-process**: call activity (collapsed) | 🟡 | works; no data mapping, may not wait | C8 |
| Sub-process: expanded embedded, transaction, ad-hoc | ⛔ | n8n canvas has no nesting; use call activity | C9 (transaction as a group), documented |
| **Gateways**: exclusive · parallel · inclusive · event-based | ✅ | native + Merge v1 | — |
| Gateways: instantiating event-based · complex · parallel event-based | ❌ / ⛔ | no general rule | non-goal |
| **Data**: data object, data store, data association | 🟡 (objects and associations: data lens; no stores) | derive from variables read/written | C14 |
| **Pools / lanes**: lane as a property | ✅ | `lane` on human tasks only | C12 |
| Pools / lanes as bands; message flow between pools | 🟡 (bands and one pool: yes; message flows: no) | sticky bands + derived dotted flows | C12 |
| Collaboration of several workflows (message exchange) | ❌ | derived from `messages` + call activities | C18 |
| Choreography · conversation diagrams | ⛔ | not executable | C18 (view only) |
| **Artifacts**: text annotation | ✅ | sticky note | — |
| Artifacts: group | ❌ | sticky group | non-goal |
| **Flows**: sequence, default, conditional | ✅ | native | — |

---

## 4. The plan: eleven workstreams

Priority: **P0** protects or unblocks everything · **P1** high value · **P2** valuable · **P3** defer until a user needs it.
Size: **S** ≤ 2 days · **M** ≤ 1 week · **L** 1–3 weeks · **XL** more. *Now*: ✅ ❌ 🟡 as in §3.

### WS-A · Foundation: elements, graph, engine, contract

The leverage point. Build the model once; every other workstream becomes a consumer of it.

```
 Element registry ──▶ builder · checker · layout · icons · palette · panel card · docs · Studio prompt · MCP text
        │
 ProcessGraph (n8n JSON → typed nodes/edges) ──▶ check · Petri/BPMN (mining) · layout · XML export · landscape
        │
 TokenEngine (clock + resolver strategies) ──▶ dry-run tests · simulation · replay/conformance · token animation
```

| ID | Feature | Source | Now | P | Size |
|---|---|---|:--:|:--:|:--:|
| **A1** | **Element registry**: one `Element` class per BPMN element with `kind`, `n8n_type`, `category`, `symbol`, `summary`, a pydantic `params` model, traits (`waits`, `decides`, `human`, `transparent`, `default_last`, `timeout_first`) and hooks `outputs()`, `produces()`, `consumes()`, `defaults_for_simulation()`. `Workflow.from_spec`, `check.py`, `mining/model.py`, the palette list, icons, GUIDE tables and the Studio prompt all read it. A manifest `elements.json` is emitted for `bpmn.js` and `panel.js`. | code analysis §1.3 · [PO] | ❌ | P0 | L |
| **A2** | **`ProcessGraph`**: parse n8n JSON once into typed nodes and edges with `successors`, `predecessors`, `after()`, `dominators`, topological order, sub-nodes (agent models), disabled-node pass-through, stickies stripped. Pure Python, no Django. | §1.3 | ❌ | P0 | M |
| **A3** | **`TokenEngine`**: executes a `ProcessGraph` with n8n's semantics (AND split, XOR join, Merge = OR join, default flow, event-based race, wait). Two strategies make it reusable: a `Clock` (virtual or wall) and a `Resolver` (real call · stub · sampled). Used by dry-run tests (F3), simulation (E3), replay and the canvas token animation. *Lazy path*: start from the semantics `WorkflowModel` already encodes; prove equality with n8n by differential tests (F5), do not port n8n. | [AW] §2.3, 3.2–3.4 · [BS] p.65–68 · [BO] §2.18 | ❌ | P1 | L |
| **A4** | **Process contract**: derive from a workflow what the outside world sees: typed start data (today only names in `SPEC["start_data"]`), awaited and sent messages, results, lanes, deadlines. Emits JSON Schema + OpenAPI for the start webhook, a typed client, an MCP tool and an A2A-style agent card. This is BPMN's *public process* next to the executable *private* one. | [AW] §5.4, 5.7 · [BO] §2.4 · [AE] 67–69 | 🟡 | P1 | M |
| **A5** | **OOP clean-ups, no behaviour change**: `Rule` classes replace `WorkflowCheck` methods (D1); `ProjectBuilder` split into `Designer`, `Publisher`, `N8nDeployer`, `ServerReloader`; `Task.as_row()` replaces dict-building in `views.activities`; `layout()` split into `Levels`, `Rows`, `Placement`. | §1.3 | 🟡 | P2 | M |

### WS-B · One platform, two servers

**Principle: Django is the shell, n8n is the canvas.** Make the two feel like one product along six axes, each with
one implementation shared by both sides (DRY) and a single owner object per axis (OOP).

```
                    browser
                       │ one origin  (app.localhost)
                ┌──────▼───────┐   session bridge / forward-auth
                │   gateway    │────────────────────────────────┐
                └─┬────┬────┬──┘                                │ preview.localhost
      /  /api  /inbox  │    │ /n8n/  /webhook/                  ▼
        Django ────────┘    ▼                            n8n-preview (read-only)
   (shell, API, UX)     n8n main ── Redis ── workers
        │  └────────────┬──────┘
   Postgres        models server  (ModelBackend: Mac · CPU container · remote)
```

| ID | Axis / feature | Detail | Now | P | Size |
|---|---|---|:--:|:--:|:--:|
| **B1** | **One origin** | A gateway (Caddy/nginx/Traefik) serves Django at `/`, n8n at `/n8n/` (`N8N_PATH`, `N8N_EDITOR_BASE_URL`, `WEBHOOK_URL`) and production webhooks at `/webhook/`. It removes `openapi_cors`, the CSP lists and the same-hostname rule. The **preview stays on its own host** (`preview.localhost`): the anonymous preview instance clears the editor's cookies when they share an origin. *Spike first*: n8n under a sub-path with custom hooks (`bpmn.js` uses absolute `/static/…` and `/icons/CUSTOM/…`), and `*.localhost` on Safari. | ❌ | P1 | L |
| **B2** | **One identity** | Step 1 (now): one `N8nSession` auth class replaces the cookie-forwarding code in `evaluation_read_auth`, used by every browser-facing read. Step 2 (spike): Django session as the only login through the gateway; n8n Community Edition has no SSO, so test what n8n's external hooks allow and keep a provisioning fallback (same credentials created by the CLI). Lanes stay Django groups; n8n editor access = staff only. | 🟡 | P1 | M + spike |
| **B3** | **One token set** | `tokens.css` + `tokens.json` (colour, status, type scale, spacing, radius, light and dark) are the only definition. `app.css`, `bpmn.js`, `preview.js`, `evaluations.css`, `executions.css`, `panel.js` consume `var(--b8-*)`; n8n hooks load it from the same static route. | ❌ | P0 | M |
| **B4** | **One theme switch** | A cookie both sides read. Stop overwriting `N8N_THEME`; the flat dark canvas becomes the canvas palette of the token set. | ❌ | P1 | S |
| **B5** | **One chrome** | A slim bpmn8n strip injected into the n8n editor (Inbox with count, Studio, Activities, Docs); Django's sidebar gains Editor and Executions. Done as `n8n-editor/strip.js` (Inbox with its waiting count, Cases, Insights, Simulation, Docs; the count shows on a workflow's page) and a top bar with status dots. | ✅ | P2 | M |
| **B6** | **Deep links both ways** | node panel → task page ✅; add node panel → case page, → activity source; case page → canvas with live tokens; Studio project → workflow in the editor. | 🟡 | P1 | S |
| **B7** | **One contract** | `bpmn8n/api/contract.py`: the `X-Bpmn8n-*` header names (one list, `core/headers.py`), the request shapes the nodes send (`RunIn`, `GatewayIn`, `ConditionsIn`, `UserTaskIn`, `SubscriptionIn`, with `RunOut`) and a version (API version + digest). `manage.py contract --ts` generates `nodes/shared/contract.ts` from the pydantic schemas (no npm dependency), replacing the hand-built bodies; a test fails when it is stale. The nodes send `X-Bpmn8n-Contract`; a mismatch gets an `X-Bpmn8n-Warning` the node shows as an output hint. Not yet: the status vocabulary (B10's `vocab.json`). | ✅ | P1 | M |
| **B8** | **One config** | `bpmn8n/config.py` (a pydantic model; no pydantic-settings) is the only reader of the environment (a test says so); `manage.py config [--env\|--json\|--md]` prints it with secrets masked and generates the guide's table. One `PUBLIC_URL`; the three public addresses derive from it unless set explicitly, and the CLI hands them to compose. | ✅ | P1 | M |
| **B9** | **One operations surface** | `python -m bpmn8n` (console script `bpmn8n`) replaces the logic in `start.sh`: `up`, `down`, `status`, `doctor`, `logs`, `export`, `import`, `deploy`, `diff`. Internally `Service` objects (`Postgres`, `Redis`, `Models`, `Web`, `N8n`, `Preview`, `Search`) with `ensure()`, `start()`, `stop()`, `health()`, and n8n's bootstrap in Python. `start.sh` is a bootstrap under 30 lines. Aggregated `/api/health`. Also `pull`, `backup`/`restore`, `schedule` (with a `scheduler` compose service) and `version`; the top bar shows status dots (`/status/` for staff). | ✅ | P1 | L |
| **B10** | **One vocabulary** | `vocab.json`: task statuses, token states, element labels; a template tag, a JS module and the MCP text read it. English only; remove the Italian leftovers (`api/executions.py`, `preview.js`: `stati/attesa/attivo/fatto`, `tinta`, `colora`). | ❌ | P0 | S |
| **B11** | **Shared components** | Three vanilla web components written once: `<b8-pill>`, `<b8-timeline>`, `<b8-process-map>`; used by Django templates and by the evaluations/executions hooks, which today rebuild their own DOM. | ✅ | P2 | M |
| **B12** | **One case page** | `/cases/<id>`: timeline, variables, decisions with their explanation, the canvas with the case's tokens. Replaces the inbox `trail`, and parts of the executions and evaluations views. | ❌ | P1 | L |

### WS-C · BPMN coverage

Everything here must be expressible in n8n; what cannot is listed in §3 and stays documentation.

| ID | Feature | How in n8n | Source | Now | P | Size |
|---|---|---|---|:--:|:--:|:--:|
| **C1** | **Boundary timer on User/Receive Task, interrupting** (a deadline) | `due_in` / `due_at` parameter → `until` of the wait → the existing `timeout` output | [AW] §8.1 · [BO] §2.8.3, 2.2.10 · [BS] p.2 | ❌ | P0 | M |
| **C2** | **Non-interrupting timer / escalation** (reminder while the task stays open) | `Deadline` table + poller → webhook of a *child execution* (n8n parks one wait per execution; `one_wait_at_a_time` already says "start the other wait as its own process") | [AW] §8.2, 8.4 · [BO] §2.2.10 | ✅ | P1 | L |
| **C3** | **Error boundary** on tasks | builder `on_error="branch"` → n8n *continue using error output* + a named output; checker follows it | [AW] §8.3 · [BO] §2.11.3 | 🟡 | P1 | M |
| **C4** | **Signal events** (throw, catch, start) | `SignalSubscription` = broadcast variant of `MessageSubscription`, no correlation key, delivers to every waiter | [AW] §6.4 · [BO] §2.12 | ✅ | P2 | M |
| **C5** | **Terminate end event** | stop sibling branches with n8n's execution-stop API; explicit *none* end for diagram fidelity (P3) | [AW] §6.3 | ✅ | P2 | M |
| **C6** | **Multi-instance**: completion condition (`all`, `any`, `quorum n`, expression), **sequential** (Loop Over Items), **data-driven** instances (`foreach: "{{items}}"`) | `UserTask.resume_payload` group semantics + builder macro | [AW] §7.2 · [BO] §2.7 (instantiator, join checker) · [WM] B12–B13 | 🟡 | P1 | L |
| **C7** | **Standard loop** with a mandatory `max_iterations` guard | builder macro `loop(until=…, max=…)` over a flow back; lint D6 requires an exit | [AW] §7.2 · [BO] §2.6 | ✅ `loop()` | P1 | S |
| **C8** | **Call Activity data mapping** (`inputs`, `outputs`) and a **versioned** reference; study sub-processes that wait | map variables in and out (also data minimisation); the child has its own case id | [AW] §7.5, 10.5 · [BO] §2.5.1.1 | ✅ mapping · versioned reference and waiting sub-processes open | P1 | M + spike |
| **C9** | **Transaction and compensation (saga)**: `@task(compensate="refund_payment")`, a compensation throw runs them in reverse over the case's completed tasks, `cancel` end | ledger = the `Event` table; transaction drawn as a group | [AW] §9 · [BO] §2.13 · [PO] | ✅ | P2 | L |
| **C10** | **Conditional events** | Django poller over case variables | [AW] §6 | ❌ | P3 | M |
| **C11** | **Link events** | only for XML round trip and long edges | [AW] §6.4 · [BO] §2.9 | ❌ | P3 | S |
| **C12** | **Swimlane layout**: rows = lanes (customer, core actors, supporting actors, other processes, holding areas, systems), pool bands for external parties, handoffs highlighted; a *handoff view* that collapses consecutive boxes in one lane | `layout()` mode + sticky bands drawn by `bpmn.js` | [WM] A49–A52, A76–A80, B01–B31 · [AW] §5 · [BS] p.10–11 · [BO] §2.2.3–2.2.4 | ✅ lanes layout, bands, `handoffs` rule (a handoff-collapsing view and message flows between pools are not built) | P1 | L |
| **C13** | **BPMN XML export with diagram interchange** (`bpmndi` shapes from the layout), then **import** (`.bpmn` → spec). Opens in bpmn.io, Camunda Modeler, Signavio, BIMP. | `ProcessGraph` + layout → XML writer; import maps tasks, gateways, timer/message/error events and lanes | [AW] §1.4 · [BS] p.80–82 (B42–B46) | ✅ export and import (`bpmn_xml.py`, `bpmn_import.py`, `manage.py export_bpmn` / `import_bpmn`, `GET /api/workflows/{id}.bpmn`) | P1 export / P2 import | L |
| **C14** | **Data lens**: show which variables each step reads and writes as data objects, toggled off by default (data flow on top of work flow hides the work flow) | derived from `data_before` in the checker | [AW] §10 · [BS] p.11 · [WM] A68–A69 | ✅ `manage.py data_lens`, `?data=1` (data objects and associations; no data stores) | P2 | M |
| **C18** | **Process landscape**: all workflows as nodes; edges = call activities and message exchanges; "who waits for a message nobody sends". A derived conversation diagram, not an executable one. | `ProcessContract` (A4) over every workflow | [AW] §11–12, 5.4 · [WM] A30, A72 | ✅ | P1 | M |

### WS-D · Modeling quality and method

The checker already enforces *correctness*. This adds *clarity*, from the method documents, and makes Studio follow it.

| ID | Feature | Source | Now | P | Size |
|---|---|---|:--:|:--:|:--:|
| **D1** | **Rules as classes** (`id`, `level`, `category`, `doc`, `check(graph)`), a registry, tests per rule with a golden fixture, `manage.py rules --md` generating the GUIDE table. Studio's "treat warning X as error" becomes a policy list, not code. | §1.3 | 🟡 | P1 | M |
| **D2** | **Naming**: process and step names are *verb + noun, singular*; no mushy verb (today only for `@task`); not an event or a state ("claim arrives", "sorted"); trigger named as an event | [WM] A06–A16, A22–A29, A59–A62, A17–A21 | 🟡 | P1 | S |
| **D3** | **Decisions**: a deciding step before each diamond, every branch labelled, branch percentages shown when known (from mining or simulation) | [WM] A70–A71 · [OC] Table 7 | ❌ | P2 | S |
| **D4** | **Size and levels**: more than 9 top-level steps → suggest a Call Activity (5 ± 2); a *level view* with handoff / service / task presets; stop-modelling hint | [WM] A30–A31, A72–A96, B35–B41 · [BS] p.2–3 | ❌ | P2 | M |
| **D5** | **Implicit gateways**: a node with several outputs but no explicit parallel gateway (implicit AND) and a non-Merge node with several inputs (implicit XOR; runs once per arrival) | [AW] §2.3, 3.3, 4.2–4.3 | ❌ | P1 | S |
| **D6** | **Deadlock and livelock** by bounded state-space exploration on the engine (A3): a join that cannot complete, a loop with no exit, a wait nobody can end. Replaces the "structural only" stance without PM4Py's false negatives. | [AW] §3.3 · [WM] p.8 | ✅ rule `deadlock` | P1 | M |
| **D7** | **Separation of duties**: warn when the approver may equal the requester; error when a policy says so | [BO] §2.3.4.1 | 🟡 rule `separation-of-duties` (info) | P2 | S |
| **D8** | **Handoff analysis**: lane changes per path, "yo-yo" detection (static); resource switches and waiting at handoffs (from the log) | [WM] A64, A76–A80, B11 | ❌ | P2 | M |
| **D9** | **Time on the diagram**: scheduled steps and deadlines labelled (what the SLA is) | [WM] B32–B33 | ❌ | P3 | S |
| **D10** | **Studio follows the method**: interview structured as WHAT/WHO/HOW; asks for the *countable result* and the customer; discovery mode (nouns → verbs → chains → process boundaries, producing a *portfolio* of processes); level views generated from the same spec; a **critic pass** that runs the lint rules and sends findings back (a *reflection* workflow, J4) | [WM] A33–A48, A08–A12 · [AE] 36–39 | 🟡 | P1 | L |
| **D11** | **Semi-formal text view**: render a workflow as the numbered notation (`4.a.1`, `if … → 14`) and accept it back; reviewable by non-technical people and diff-friendly in git. The "protocol" of [OC]. | [BS] p.17–22, 52 · [OC] §3.4 | ❌ | P2 | M |

### WS-E · Simulation and capacity

The biggest absent feature, and the one the sources describe best. Pure Python with `numpy`; **no PM4Py import** so the
AGPL boundary stays where it is (`bpmn8n/mining/`).

**Design.** `bpmn8n/simulation/` = `SimSpec` (data) + `analytic` (closed forms) + `des` (event simulation on the
`TokenEngine`) + `calibrate` (from the log) + `optimise`. The analytic part answers instantly and serves as the oracle
that tests the simulator (E13).

| ID | Feature | Source | P | Size |
|---|---|---|:--:|:--:|
| **E1** | **`SimSpec`**: next to the workflow as `projects/<p>/simulation.json` (like `examples.json`, `evaluations.json`). Arrivals (peak *N* at t=0 · fixed · Poisson λ · schedule); per step: duration distribution (fixed, exponential, normal, triangular, lognormal, empirical) and cost; resources: lane → capacity, cost per hour, **work calendar**; gateway: branch probabilities, or conditions evaluated on sampled data; timers (real durations); message and human waits as latency distributions. Element defaults come from the registry (A1), so a Studio design is simulable on day one. | [BS] p.83–85 (PSS, RES, WS, TSK, GTW), 23–28 · [BO] §2.2.5.5, 2.2.6.5, 2.2.8.3 | P1 | M |
| **E2** | **Scenario analyser** (analytic): enumerate paths, probability and expected tokens per path for *N* start tokens, Σ ending = *N* checked. Loops handled exactly through the absorbing Markov chain `(I − Q)⁻¹`, not by hand-expanded S4–S7. Output is the table students draw by hand. | [BS] p.18–21, 53–60 | P1 | M |
| **E3** | **Event simulator** on the `TokenEngine` with a virtual clock: queues per resource (FIFO, priority), calendars, **native parallel gateways**, timers, human latency, seeds, warm-up. Outputs exactly the course's set: completed instances, total cost, total time; process cycle and waiting times; resource utilisation over time; per-instance cost; per-task cost and queue time; completion per scenario (first and last token out, tokens, mean). | [BS] p.26–28, 62–64, 85 | P1 | L |
| **E4** | **Replications and confidence intervals** by default (never a single run): *t* interval at 90/95/99 %, normality check, fixed seeds, **common random numbers** for paired comparison of alternatives | [BS] p.89 (B50–B57) | P1 | M |
| **E5** | **Analytic queueing**: open Jackson network from the routing probabilities (traffic equations, feedback included); per lane M/M/c (Erlang C), M/G/1 (Pollaczek–Khinchine) with a squared coefficient of variation, M/M/1/K for blocking, Little's law; stability warning when ρ ≥ 1. Instant "what if I add a person" without a simulation. | [QT] §3.3–3.4, 3.6, 3.9, 4.1–4.3 · [BS] p.45–51 | P1 | M |
| **E6** | **Staffing optimiser**: minimise headcount subject to per-lane maximum, total cap and a duration/SLA target; greedy descent reducing the least sensitive lane, plus a **sensitivity table** (Δ duration per person removed). Analytic pre-screen cuts the number of simulations. | [BS] p.45–51, 90–96 | P2 | M |
| **E7** | **Calibration from the event log**: arrival rate and seasonality, a fitted distribution per activity (exponential/lognormal/gamma by AIC, else empirical), branch probabilities from decision mining (already built), resource counts from `resource`, human claim and work times from `UserTask` timestamps. *"Simulate from reality"*. | [DI] §1.2, 4.2 · [BS] p.83 · [OC] Table 7 | P1 | L |
| **E8** | **Simulation vs reality**: per activity predicted vs observed utilisation, queue and duration; a fit score; a drift warning. The simulator is checked against the thing it models. | [DI] §4.1.4 · [OC] Fig. 22 | P2 | M |
| **E9** ✅ | **Cost model including agents**: human cost by lane hour; LLM cost from the usage the call already records; per-case cost distribution; **automation what-if** (replace a step by an agent: cost, time, review rate, accuracy). | [BS] p.23–28, 61 · [AE] | P2 | M |
| **E10** ✅ | **Functional + performance hybrid**: run the real `@task` functions (or their stubs) over generated cases so the simulation also yields *outcomes* (which branch, which variables), not only times; the generated cases double as test data (F11). | [BO] §2.14.2 · [BS] p.17–22 | P2 | M |
| **E11** | **Interfaces**: `manage.py simulate`, `POST /api/simulation/*` (async job, SSE through `ChangeStream`), MCP tool `simulate`, a Studio "Capacity" card; **UI**: click a node on the canvas to edit its duration, cost and lane capacity; sliders per lane with the instant analytic estimate; charts reusing the evaluations view; compare runs. | [BO] §2.2.5.5 | P1 | L |
| **E12** | **No emulation workaround**: parallel paths and inter-arrival times are native. Document it as the contrast with a simulator that needs a redundant "emulation" model and a timer faked in a 1-instance lane. | [BS] p.65–68, 73–75 | — | — |
| **E13** | **Validation tests**: DES against M/M/1 and M/M/c closed forms, token conservation, Little's law residual below ε, analytic = DES on the course's worked examples (library 60/20/10/6/2/1/1; emergency-centre scenarios). | [BS] p.18–21, 55–60 · [QT] | P1 | S |

### WS-F · Testing

Today the pyramid has a wide base (tasks) and a wide top (e2e); the middle is missing.

```
                      ▲  e2e on the real stack (33)             ← exists
                     ╱ ╲ differential: engine vs n8n  (F5)
                    ╱   ╲ scenario tests on the engine (F3)    ← the missing middle
                   ╱     ╲ rule tests · property tests (F2, F7)
                  ╱       ╲ contract / drift tests  (F6)
                 ╱─────────╲ task examples · unit (146)         ← exists
```

| ID | Feature | Source | Now | P | Size |
|---|---|---|:--:|:--:|:--:|
| **F1** | Schema-driven fuzz of `@task`s: for any input the Schema accepts, the task returns its output Schema or a typed error | code analysis | ❌ | P2 | M |
| **F2** | One golden-fixture test per lint rule (D1); mutation testing of the checker as a later hardening | — | 🟡 | P2 | M |
| **F3** | **Scenario tests**: `scenarios.json` per project — start data, stubs (gateway decisions, human answers, arriving messages, time jumps), and expectations (*path contains / excludes / order*, final variables, end kind, virtual duration, no stuck token). Run by `check_project`, CI, MCP and Studio; a failing scenario is replayed on the canvas. | [BS] p.17–22 (scenarios) · [BO] §2.14.2 (run with connectors disconnected) · [AE] evaluation | 🟡 run by `check_project`, `test_scenarios`, Studio, MCP; not yet: virtual duration, canvas replay | **P0** | L |
| **F4** | **Record/replay** ("cassettes") of Laya, GLiNER and LLM calls so scenarios are deterministic and offline; golden decision sets stay in `e2e/datasets` | [AE] evaluation and monitoring | 🟡 `record_scenario`: a case's decisions, answers, messages and task outputs; not yet Laya/GLiNER/LLM cassettes | P1 | M |
| **F5** | **Differential tests**: the same workflow and stubs through the engine and through real n8n must give the same trace. This is what makes A3 safe. | risk §6 | ✅ `e2e/test_differential.py` (ACME) | P1 | M |
| **F6** | **Contract and drift tests**: Python params ↔ TS node properties (both directions); OpenAPI snapshot; header contract; a **canary** that renders a reference workflow in the pinned n8n and the next one and compares screenshots, since `bpmn.js` depends on class names | §1.3 | ❌ | **P0** | M |
| **F7** | **Property tests** (Hypothesis): layout (no overlap, no flow behind a step), spec → build → check never crashes, engine token conservation, XML export → import round trip | — | ❌ | P2 | M |
| **F8** | Load and soak: scripted cases with p95 budgets, nightly; extends `tools/bench.py` and `e2e/test_mining_and_load.py` | — | 🟡 | P2 | M |
| **F9** | **Evaluation gates**: `bpmn8n eval --fail-under`; an LLM-as-judge metric type; dataset versions; results stored per commit. Builds on Evaluations and the Metrics node. | [AE] evaluation and monitoring | 🟡 `eval_gate` in CI; no LLM judge, dataset versions | P1 | M |
| **F10** | **CI** (GitHub Actions): ruff · Django tests · `npm test` · `node --test` · contract/drift · scenarios · image build; nightly e2e on compose with a **fake models server** so no Mac or GPU is needed; coverage floor | — | ❌ | **P0** | M |
| **F11** | **Synthetic history generator** from a `SimSpec`: fills the event log for demos, dashboards and mining tests | [BS] p.83–85 | ❌ | P2 | S |
| **F12** | `bpmn8n test` as the one command; results as Studio cards; failing path highlighted on the canvas | — | ❌ | P1 | S |

### WS-G · Production

| ID | Feature | Detail | Now | P | Size |
|---|---|---|:--:|:--:|:--:|
| **G1** | **Images** | `Dockerfile` for `web` (multi-stage, non-root, no `pip` at boot, no `--reload`); n8n pinned by tag and digest; health checks; a `prod` compose profile. Static files by the gateway or whitenoise, not `staticfiles_urlpatterns`. | ❌ | **P0** | M |
| **G2** | **Secure defaults** | `DEBUG` off unless set; token mandatory outside dev; `ALLOWED_HOSTS`, CSRF trusted origins, secure cookies, HSTS; a startup self-check that refuses to run an open API in prod. | 🟡 | P0 | S |
| **G3** | **Environments and promotion** | The **project folder in git is the source of truth**; n8n is a deployment target. `bpmn8n deploy --env <name>` is idempotent: credentials, workflow upsert by stable id, sub-processes first, publish gate, unique webhook paths. `bpmn8n diff` shows drift between git and n8n; `bpmn8n pull` brings editor changes back into the project. Replaces the whole-instance `export/import`. | ❌ | P1 | L |
| **G4** | **Process version per case** | `process_version` (content hash + optional label) on `Case` and `Event`; conformance, mining and simulation filter by version; policy for cases in flight; a human-readable change summary from the diff. Closes the documented known limit. | ❌ | **P0** | M |
| **G5** | **Studio publish gate** | scenarios pass + lint clean + (optionally) a simulation sanity run + a human review. Studio writes code the server runs: open a **branch and pull request** instead of writing into the live tree. | 🟡 | P1 | M |
| **G6** | **Security and privacy** | API tokens per client with scopes (n8n credential, MCP, evaluation reads); rotation; rate limits; **encrypt LLM keys at rest**; immutable **audit log** of human actions (who claimed, completed, reassigned); **retention and erasure** (purge or pseudonymise by case); anonymised export | 🟡 | P1 | L |
| **G7** | **Observability** | JSON logs with case id; Prometheus `/metrics` from the existing Event aggregates; OpenTelemetry export from `Span`; Grafana dashboards; alerts for SLA breaches, **stuck cases** (no event, no wait), error rate, model latency | 🟡 | P1 | L |
| **G8** | **Reliability** | `pg_dump` backup + restore drill (`bpmn8n backup`); Redis persistence; rolling restart with drain; **`Idempotency-Key`** on task endpoints (the `X-Bpmn8n-Run/Item` identity already travels) so a retried `sendTask` does not send twice; retry policy on `@task` (J7) | ❌ | P1 | M |
| **G9** | **Model backends** | `ModelBackend` interface (Mac MLX/Vision · CPU container · remote HTTP such as vLLM); health, fallback, circuit breaker; documented capacity (Laya's single GPU is the limit near 50 cases/s) | 🟡 | P2 | M |
| **G10** | **Housekeeping** | partition or archive `Event`; mine from SQL aggregates instead of a DataFrame in memory beyond the documented 1 GB / 5 M events; retention for `run/` evaluation folders. Done: `manage.py prune` (task runs deleted, events archived to a gzipped CSV, waiting cases kept) run daily by the scheduler with `PRUNE_DAYS`; not yet partitioning, mining from SQL aggregates or `run/` retention | 🟡 | P2 | L |
| **G11** | **Release discipline** | semver, changelog, the TS package versioned with Python through the B7 handshake, the Python package published. Done: one version (0.2.0) in three places kept equal by a test, `CHANGELOG.md`, `python -m bpmn8n version`; not yet the handshake or publishing | 🟡 | P2 | S |

### WS-H · People and forms

Bonita 5.5 is the benchmark for the work people do inside a process.

| ID | Feature | Source | Now | P | Size |
|---|---|---|:--:|:--:|:--:|
| **H1** | **SLA, due date, priority** on human tasks (`due_in`, `priority`); inbox sorts by urgency, overdue badge, colours from the token set | [BO] §2.2.5.2, 4.3.1.5 · [AE] prioritization | ❌ | **P0** | M |
| **H2** | **Reassign, delegate, unassign, suspend, resume**: new transitions in `WaitingActivity.TRANSITIONS`, same conditional-update mechanism | [BO] §4.3.1 | ❌ | P1 | M |
| **H3** | **Actor selectors**: static group, user from a case variable (`{{approver}}`), filters *same as task X* / **not same as task X (four-eyes)** / random / least busy; OIDC groups later. Enforced in `UserTask.can_work`. | [BO] §2.3 · [WM] B12–B13 | ❌ | P1 | M |
| **H4** | **Quorum and voting** (C6): committee approvals, first-wins, *n of m*; remaining tasks close like an event-based gateway's losers | [BO] §2.7 · [AW] §7.2 · [WM] B12–B13 | ❌ | P1 | M |
| **H5** | **Richer forms**: choice, date, long text, file/image, list of rows; mandatory, read-only, conditional visibility, validators (regex, length, expression); multi-page. One JSON-Schema-style definition renders the Django page, the MCP prompt and the node UI. | [BO] §3.2.1–3.2.2 | 🟡 | P1 | L |
| **H6** | **Case page for people and admins**: overview, labels, the process drawn with the current position | [BO] §4.3.2, 4.4.2.1 | ❌ | P1 | (B12) |
| **H7** | **Workload dashboard**: open, claimed, overdue by lane, mean wait, per person | [BO] §4.3.3 | ❌ | P2 | M |
| **H8** | **Notifications**: assigned, due soon, overdue, escalated; email (backend exists) and webhook; per-user digest | [BO] | ❌ | P2 | M |
| **H9** | **Start a case from the UI** (form generated from `start_data`) and public intake forms (n8n Form Trigger is already recognised by `bpmn.js`) | [BO] §4.3.5 | ❌ | P1 | M |
| **H10** | **Oversight modes**: *human-in-the-loop* (a person approves before effect, today's rule) and *human-on-the-loop* (policy by a person, execution automatic, **sampled audit** and a kill switch), chosen per step with the reason recorded; `people_decide` takes the mode as a parameter | [AE] 63–66 | 🟡 | P1 | M |
| **H11** | **Similar cases panel**: on a task page, the nearest past cases and their outcomes through the sentence embeddings already on the models server | [OC] §3.4 | ❌ | P2 | M |
| **H12** | Task page: bulk actions, saved filters, keyboard use, accessibility pass | [BO] §4.3 | 🟡 | P3 | M |

### WS-I · Process intelligence

PM4Py does the maths; the gap is the **workbench** around it. One filter language serves mining, calibration (E7) and datasets.

| ID | Feature | Source | Now | P | Size |
|---|---|---|:--:|:--:|:--:|
| **I1** | **Map overlay on the designed canvas**: frequency (absolute, case, max repetitions) and performance (total, mean, median, max, min) on nodes and edges, heat colours | [DI] §4.1.1, 4.1.4 · [OC] Fig. 22 | ❌ | P1 | M |
| **I2** | Level-of-detail sliders (activities %, paths %) and search, on the *discovered* model | [DI] §4.1.2–4.1.3 | 🟡 | P2 | M |
| **I3** | **`LogFilter` stack** (chainable classes): timeframe (contained / intersecting / started / ended), variants (top-N, coverage), performance (case duration, between two activities), endpoints, attribute (include/exclude), follower (eventually / directly follows with time bounds); API `filters=`; UI stack; *save as named dataset* | [DI] §5, 6.2.1 | ❌ | P1 | L |
| **I4** | **Animation**: replay cases over the map with the live token view that `live.mjs` already draws | [DI] §4.1.6 | ✅ | P2 | M |
| **I5** | **Statistics**: global, events and active cases over time, case-duration histogram, mean waiting time, per resource, per attribute | [DI] §4.2 | ❌ | P1 | M |
| **I6** | **Cases view**: variants with coverage, one case as a timeline with *why this path* (gateway scores and conditions already in `Event.data`), search | [DI] §4.3 · [OC] | 🟡 | P1 | M |
| **I7** | **Import wizard**: column roles, timestamp patterns, several timestamps (start/complete), combined columns, XES/MXML/Excel, a **data readiness report** (missing timestamps, ties, coverage) | [DI] §1.2–1.3, 3.1–3.3 | 🟡 | P2 | M |
| **I8** | **Export** XES/MXML/CSV with **anonymisation**; charts and tables as CSV | [DI] §7 | 🟡 | P2 | S |
| **I9** | **Cohort comparison**: gateway percentages and durations between two filters (specialist vs resident; this month vs last; variant A vs B), side by side | [OC] Table 7, §4.4 | ❌ | P2 | M |
| **I10** | **Bottleneck ranking** joined with queueing: utilisation ρ per lane from the log plus an analytic waiting-time estimate | [QT] §3.4 · [WM] A64 | ❌ | P2 | S |
| **I11** | Snapshot a mining analysis (filters + results) for sharing | [DI] §6.3 | ✅ | P3 | M |
| **I12** | Drift as a first-class step ("model drift?") with an action | [OC] Fig. 5, Table 7 j) | 🟡 | P3 | S |

### WS-J · Agentic patterns

bpmn8n has one generic `llm_agent`. The catalogue in [AE] becomes **vetted templates**: macros in the builder that expand
to steps and flows, each carrying the lint rules that keep it safe.

| ID | Feature | Source | P | Size |
|---|---|---|:--:|:--:|
| **J1** | **Pattern macros** `pattern(name, …)`: `prompt_chain`, `parallel_llm` (fan-out + merge), `reflection` (producer + critic, `max_rounds`), `planner_executor`, `tool_agent`, `hitl_review`, `guardrailed_agent`. Router already exists as the Laya gateway. A gallery in Studio. | [AE] 20–48 | P1 | L |
| **J2** ✅ | **Task-as-tool**: expose chosen `@task`s to the AI Agent as tools (OpenAPI → tool schema), so the agent calls typed, logged, governed functions rather than free n8n tools | [AE] 40–42, 67–69 | P1 | M |
| **J3** | **Guardrail step**: input and output checks (PII through GLiNER, schema/regex, policy rule, a classifier) with `pass / block / escalate` outputs, logged; generalises the hospital project's pseudonymisation | [AE] guardrails | P1 | M |
| **J4** | **Reflection with a budget**: stop criterion, maximum rounds, cost cap per case; Studio's own build loop uses it (D10) | [AE] 36–39 | P2 | S |
| **J5** | **Memory**: short-term = case variables ✅; long-term store with retention and PII policy | [AE] memory | P3 | L |
| **J6** ✅ | **Learning loop**: capture a person's override of Laya or of an agent draft as a labelled example (`overridden`, `final`), feed the decisions dataset ✅, evaluate, and propose a threshold or prompt change | [AE] learning · [OC] drift | P2 | M |
| **J7** | **Exception handling and recovery** as a policy on `@task` / agent: classify (transient, permanent, validation), retry with backoff and jitter, fallback task, circuit breaker, escalate to a person, compensation (C9) | [AE] 61–62 | P1 | M |
| **J8** ✅ | **Goals and monitoring**: process goals (KPI + target) in `evaluations.json`, evaluated continuously from the log, alerts through G7 | [AE] goal setting, evaluation | P2 | M |
| **J9** (partial) | **Evaluation** = F9 applied to agents and gateways: judge metric, golden sets, regression gate | [AE] evaluation | P1 | (F9) |
| **J10** | **Prioritisation**: a priority score and queue policy for human tasks and agent calls | [AE] prioritization · [BO] §4.3.1.5 | P3 | S |
| **J11** | **A2A / MCP for processes**: each published process as an MCP tool and agent card (start, status, complete a task), from the A4 contract. *Defer until a consumer exists.* | [AE] 67–69 · [AW] §5.4 | P3 | M |
| **J12** | **Reasoning transparency**: structured `rationale` and `evidence` fields stored in the event and shown in the task page | [AE] 70–72 · [OC] | P2 | S |
| **J13** | **Suggest improvements** from mining: bottlenecks and automation candidates | [AE] exploration | P3 | M |

### WS-K · UX and what sets bpmn8n apart from other simulators and BPM suites

What the sources say the alternatives do and do not do:

| Capability | Bonita 5.5 [BO] | Disco [DI] | Course simulators (Visual Paradigm, BIMP) [BS] | bpmn8n today | bpmn8n target |
|---|---|---|---|---|---|
| Model BPMN | own Studio | no | yes | n8n canvas, BPMN look | + lanes, XML (C12, C13) |
| Execute | engine | no | no | n8n | same |
| Human tasks and forms | forms builder, actors, priority | no | no | inbox, basic form | H1–H5 |
| Discover from a log | no | yes | no | PM4Py report | I1–I10 |
| Conformance to the design | no | not its purpose | no | alignments | + per version (G4) |
| Simulation | separate reference guide (§2.19) | no | yes, **with limits**: no parallel paths in one scenario, uneven resource allocation with few tokens, arrival time faked by a timer (p.65–68, 73) | **none** | E1–E13 |
| Parameters from the real log | n/a | n/a | typed in by hand | n/a | **E7, E8** |
| Static verification | — | — | — | structural check | + deadlock search (D6) |
| Agents / LLMs | no (2011) | no | no | `llm_agent`, Studio | J1–J13 |

What this adds up to. Each is a **differentiator** that comes for free once §WS-A exists:

| ID | Differentiator | Built from |
|---|---|---|
| **K1** | **One canvas, many layers**: Design · Data · Frequency · Performance · Simulation · Live tokens · Failing test path, switched with one control | C14, I1, E8, B11, F3 |
| **K2** | **Close the loop**: mine → calibrate → simulate → compare with reality → change the design → check → deploy, in one tool | E7, E8, G3 |
| **K3** | **Functional and performance simulation together**: real task code over generated cases gives outcomes, not only times | E10 |
| **K4** | **Exact scenario tables** (the token counts), with loops solved in closed form | E2 |
| **K5** | **Interactive what-if**: a slider per lane with an instant analytic answer; the simulation confirms in the background | E5, E6 |
| **K6** | **Cost per case including model calls** | E9 |
| **K7** | **Process handbook**: one click produces level 1/2/3 diagrams, lanes and responsibilities, decisions, KPIs, simulation summary and compliance findings, from the spec | D4, D11, C12 |
| **K8** | **Time-travel**: a scrubber over a case, a simulation or a test with a virtual clock | A3 |
| **K9** | **Vetted task library** `bpmn8n.lib` (HTTP request, email, database query, file, PDF/OCR, notify): fewer `# TODO`s in Studio output; the connectors of [BO] §2.14 as Python tasks, with a mock mode for tests | [BO] §2.14 |
| **K10** | **Everything has a CLI, API and MCP twin** (Studio already does) | B9 |
| **K11** | **Accessible and bilingual UI** (English first; strings in one catalogue) | B10 |

---

## 5. Phasing

```
Phase 0  Safety net ─▶ Phase 1  Skeleton ─▶ Phase 2  One platform ─┐
   F10 F6 A2 B10        A1 D1 B3 B8 B9        B1 B2 B4–B7 B11 B12  │
   G2 (quick)           G1 G4 F12             C18                   ├─▶ Phase 5  People + coverage
                                  │                                  │      H1–H5 H9 H10  C1–C9  C12 C13
                                  ▼                                  │
                           Phase 3  Test the process ─▶ Phase 4  Simulation ─▶ Phase 6  Intelligence + agents
                             A3 F3 F4 F5 D5 D6 D2         E2 E5 → E1 E3 E4 → E7 E8 E11      I1–I9   J1–J4 J7 J9
                                                                     G3 G5 G6 G7 G8  (production, in parallel from Phase 1)
```

| Phase | Goal | Contents | Exit test |
|---|---|---|---|
| **0 Safety net** | Nothing regresses silently | F10 CI · F6 drift tests · A2 `ProcessGraph` (pure refactor) · B10 vocabulary · G2 secure defaults | CI green on every PR; the drift test fails when a TS parameter is renamed |
| **1 Skeleton** | One definition per concept | A1 registry · D1 rule classes · B3 tokens · B8 config · B9 CLI · G1 images · G4 process version · F12 | Adding a *signal* element touches one class and one TS file; `bpmn8n up` works from a clean checkout |
| **2 One platform** | Two servers that feel like one | B1 origin · B2 identity · B4–B7 · B11 · B12 · C18 | One login, one URL, one palette; a case links canvas ↔ page both ways |
| **3 Test the process** | A workflow is tested like code | A3 engine · F3 scenarios · F4 cassettes · F5 differential · D2 D5 D6 lint | `check_project` runs scenarios; a deliberately broken join is caught before n8n sees it |
| **4 Simulation** | Capacity and what-if | E2 E5 (analytic, first) → E1 E3 E4 → E7 E8 → E6 E11 · F11 E13 | The library and emergency-centre examples reproduce the slide numbers; DES agrees with M/M/c; a calibrated model predicts a real process's mean cycle time within a stated error |
| **5 People and coverage** | Work as people do it | H1–H5 H9 H10 · C1–C9 C12 C13 | An approval with a deadline escalates, is reassigned, needs two different people |
| **6 Intelligence and agents** | Learn from runs; safe agents | I1–I9 · J1–J4 J7 J9 · D10 D11 | Heatmap on the canvas; a *reflection* template passes its own evaluation gate |

Production work (G3, G5–G8) runs alongside from Phase 1 because it does not wait on the others.

---

## 6. Risks, non-goals, open decisions

**Risks**
| Risk | Mitigation |
|---|---|
| **n8n coupling**: `bpmn.js` matches hashed class names, `panel.js` reads Pinia stores, tests use the internal `/rest` API | Pin the version (G1); the canary in F6 renders a reference workflow on the pinned and the next release; keep each hook defensive (it already degrades to n8n's own look) |
| **Engine drift**: A3 re-implements n8n's semantics | Differential tests (F5); semantics live in one place (A3), consumed by dry-run, simulation and mining |
| **AGPL**: PM4Py is AGPL | `simulation/` imports no PM4Py; `scipy` only where already installed; the boundary stays `bpmn8n/mining/` |
| **n8n licence** (Sustainable Use): no enterprise features | Nothing here unlocks `.ee.` code; SSO and RBAC are done on bpmn8n's side |
| **Scope**: engine, simulator and mining workbench are three products | Phase order: analytic simulation first (days), event simulation second; each phase ships alone |
| **LLM nondeterminism** in tests | Cassettes (F4); judge metrics with fixed seeds and tolerances |
| **macOS-only pieces** (Vision OCR, MLX Laya) in production | `ModelBackend` (G9); fake server in CI |

**Non-goals** (YAGNI; revisit only when a use case appears)
complex and parallel event-based gateways · executable choreographies · ad-hoc sub-process · link and conditional events (C10, C11) ·
group artifact · multi-tenancy · closed queueing networks / Buzen (until a process has a fixed number of cases in flight) ·
form theming à la Bonita §3.4–3.6 · a native modeller replacing the n8n canvas · a JavaScript port of the engine (the browser reads results; it does not compute them).

**Open decisions**
| # | Decision | Recommendation |
|---|---|---|
| 1 | One origin through a gateway, or keep two origins | One origin, preview on its own host; spike first (B1) |
| 2 | Identity: accept two logins for now? | Yes for Phase 2 step 1; run the SSO spike before promising one login (B2) |
| 3 | TS node properties: generate from Python, or keep hand-written plus a drift test | Drift test now (F6); generate only if drift keeps happening |
| 4 | Where simulation parameters live | `projects/<p>/simulation.json`, next to `examples.json` |
| 5 | `start.sh` → Python CLI | Yes (B9); keep `start.sh` as a bootstrap |
| 6 | Source of truth for a workflow | The project folder in git (G3) |
| 7 | Simulation library | Own small engine (heap + numpy); no SimPy dependency unless the engine outgrows 400 lines |

---

## 7. Appendix: sources and method

| Tag | Document | Pages | How it was read |
|---|---|---:|---|
| [AW] | Allweyer, *BPMN 2.0: Introduction to the Standard for Business Process Modeling* (2009) | 156 | table of contents and sampled chapters (gateways, events, exceptions) — **not read in full** |
| [BO] | Bonita Open Solution 5.5 User & Reference Guide (2011) | 140 | table of contents, plus the sections on actors, multi-instance, timers, forms, user experience |
| [DI] | Disco User Guide (2015) | 155 | table of contents and the import, map, statistics and filter chapters |
| [AE] | Cimino, *Agentic Engineering* (2026) | 88 | slide images and titles (the text layer is empty) |
| [BS] | Cimino, *BPMN Modeling and Simulation* | 96 | text, read in depth; garbled pages recovered by OCR |
| [QT] | Stea, *Notes on Queueing Theory* | 107 | table of contents and chapters 2–4 |
| [WM] | Cimino, workflow modeling methodology (after Sharp) | 32 | pages 1–5 as text; pages 6–32 recovered by OCR (Apple Vision) and read |
| [OC] | Cimino et al., *Explainable screening of oral cancer…*, Smart Health 35 (2025) | 23 | the BPMN protocol, experiment and Table 7 |
| [PO] | BPMN 2.0 poster | 1 | element inventory |

[BO] and [DI] are old products: their *concepts* are taken, not their interfaces. Items marked *spike* depend on a
behaviour of n8n 2.42 that was not verified while writing this plan; each has a fallback.

---

## 8. Implementation status (2026-11-19, branch `feat/framework-v2`)

Built in phases by engineers in parallel (one worktree each), merged, then verified after each phase; after N4: 671
Django tests green on SQLite and on Postgres inside the web container, 36 node tests, 34 editor tests, ruff clean,
every tracked project's `check_project` ok (examples + scenarios + workflow check), all 44 end-to-end tests green on
the running stack (the engine vs n8n differential included).

Security checks: every API route needs the token and every page a login (tests enumerate the routes, so a new one
cannot forget), forms need CSRF, no open redirect, no path traversal, case data escaped; `check --deploy` clean with
production settings; ruff's bandit rules reviewed (no real finding); pip-audit finds no known vulnerability; npm's three
remaining advisories are in n8n-workflow's build-time types (not shipped); no secret from the local `.env` files is in
the git history (the n8n credential *id* in the projects' workflows is a reference, not a secret). Open by design: the
OpenAPI document is public (the editor reads it) and carries the tasks' examples, so examples must stay synthetic.

| Workstream | Done | Partial | Not yet |
|---|---|---|---|
| A Foundation | A1 registry + `/api/elements` + drift test, A2 `ProcessGraph`, A3 `TokenEngine` | A5 (rules are classes; `ProjectBuilder` not split), A4 process contract (start data typed from the tasks, messages awaited and sent, lanes, deadlines, results, calls; JSON Schema + OpenAPI + summary, `/api/contracts/<id>`, `manage.py process_contract`; no typed client, MCP tool or agent card) | |
| B One platform | B1 one origin (opt-in Caddy gateway, profile `one-origin`, on with `PUBLIC_URL`; spike: `docs/spikes/one-origin.md`), B3 tokens, B4 theme, B6 deep links, B7 one contract (generated `contract.ts`, version handshake), B8 one config, B10 vocabulary, B12 case page, B5 (Django nav, the strip in n8n, status dots), B9 (`bpmn8n` CLI incl. `pull`, `backup`/`restore`, `schedule`, `version`; `/api/health`, `doctor`), B11 `<b8-pill>`, `<b8-timeline>`, `<b8-process-map>` (case page, status pills, landscape, evaluations' statuses) | B2 (spike: n8n Community Edition takes no identity from a proxy short of a hook into its internals, not adopted; two logins, one account: `manage.py n8n_sync_owner`) | |
| C BPMN coverage | C1 deadline boundary (`timeout`), C3 error boundary (`on_error`), C2 non-interrupting reminder/escalation (`Reminder` row + `run_deadlines`), C4 signals (`broadcast` subscriptions, `POST /api/signals/<name>`), C5 terminate end (n8n stop + bookkeeping), C7 `loop()` with its loop maximum, C8 call-activity data mapping, C9 compensation (`@task(compensate=)`, `/api/cases/<id>/compensate`, `cancel_end`), C18 landscape (`/landscape/`: calls and messages from the contracts and the subscriptions, findings), C13 BPMN XML export with diagram interchange and import (`export_bpmn`/`import_bpmn`, `GET /api/workflows/{id}.bpmn`), C14 data lens (`data_lens`, `?data=1`) | C6 (quorum; no sequential multi-instance), C12 (lanes layout, bands, `handoffs` rule; no handoff-collapsing view, no message flows between pools) | |
| D Method | D1 rule classes, D2 naming, D5 implicit gateways, D6 deadlock search | D7 (four-eyes enforced on tasks; rule `separation-of-duties` notes it, no policy makes it an error), D10 (Studio knows the patterns) | D3, D4, D8, D9, D11 |
| E Simulation | E1 spec, E2 scenarios, E3 DES, E4 confidence intervals, E5 queueing network, E6 staffing, E7 calibration, E8 model vs reality, E9 cost per case split into people and models (LLM usage and prices), E10 functional mode (`--functional`, real tasks, capped), E11 page + API + CLI + canvas mode, E12, E13 | | |
| F Testing | F3 scenario tests, F5 differential vs n8n (ACME, identical traces), F6 drift, F10 CI, F11 `seed_events` | F2, F4 (`record_scenario`; no model cassettes), F7 (token conservation), F9 (`eval_gate` in CI; no LLM judge or dataset versions), F12 | F1, F8 |
| G Production | G1 image + prod override + pinned n8n, G2 secure settings, G3 `workflows diff/push/pull`, G4 process version + history, G6 LLM keys encrypted at rest + `rotate_keys`, append-only audit log of human actions, erasure by case (`erase_case`), G7 Prometheus + JSON logs, G8 idempotent task runs (`TaskRun`: a retried run executes once) | G5 (scenarios gate Studio; no PR flow), G6 (no per-client scoped tokens, rate limits or anonymised export), G8 (backups exist, `backup`/`restore`; no drill or drain), G10 (`prune` archives events and deletes task runs; no partitioning or SQL-side mining), G11 (one version, `CHANGELOG.md`; the Python package is not published) | G9 |
| H People | H1 SLA/priority, H2 suspend/resume/reassign/delegate, H3 assignee + four-eyes, H4 quorum, H5 form types, H6 case page, H7 workload dashboard (`/workload/`), H8 e-mail notifications (assigned, due soon, overdue, escalated; opt-out; `send_digests`), H9 start a case from the UI (`/start/`), H10 HITL/HOTL, H11 similar cases panel (embeddings, TF-IDF fallback) | H8 (no webhook channel), H9 (no public intake forms; POST webhooks only), H12 | |
| I Intelligence | I1 heatmap, I2 process map with activity and path sliders (frequency or performance, server-drawn SVG), I3 filter stack, I4 replay on the canvas (`/api/analytics/replay`, compressed clock, step mode for reduced motion), I5 statistics, I6 cases/variants, I7 import wizard `/insights/import/` (CSV, XES, column roles, timestamp detection, readiness report; `import_eventlog --map`), I8 CSV/XES export with HMAC pseudonyms (`/api/analytics/export`, audited), I9 cohorts, I10 queueing on the log, I11 snapshots (`Snapshot`, listed and reopened by URL) | I2 (no activity search box), I7 (no MXML or Excel, no combined columns), I8 (no MXML; charts and tables not as CSV) | I12 |
| J Agents | J1 pattern macros, J2 task-as-tool (HTTP Request Tool sub-nodes, rule `agent-tool`), J3 guardrails, J4 budgets, J6 learning loop (`overridden`/`final`, override rate, `suggest_thresholds`), J7 error paths and retries, J8 goals (`check_goals`, `/api/goals`, `bpmn8n_goal_ok`) | J9 (goals and `eval_gate --goals`; no golden sets) | J5, J10–J13 |
| n8n's paid features | Variables → global variables on Redis; workflow history → versions, diff, restore; log streaming → Redis stream, webhooks, SSE; advanced execution search → `/api/cases`, `/cases/`; Insights → `/insights/`; source control and environments → `workflows diff/push/pull` | | SSO, project RBAC, external secrets |

Bugs found and fixed on the way: the hospital process no longer built (its user task contexts were text); the
data-breach process skipped the Garante notification for a high risk without risk to people; process mining crashed on
a gateway that read a list; the check credited form answers to a user task's timeout path; editing a technical
condition gateway in n8n dropped its decision scope; a chat model counted as a second start in conformance.

## 9. Next phases (what §8 leaves, in order)

Each phase ships alone, keeps every suite green (Django on SQLite and on Postgres in the web container, node and
editor tests, ruff, every project's `check_project`, the e2e suite on the running stack) and ends with the test
in its last column.

| Phase | Items | Why in this order | Exit test |
|---|---|---|---|
| **N1 Hardening** | G8 idempotency keys on task runs (the `X-Bpmn8n-Run/Item` identity already travels), G6 LLM keys encrypted at rest + audit log of human actions + erasure by case, D7 four-eyes lint rule, C7 `loop()` macro, F9 evaluation gates in CI, F5 engine vs n8n differential test | Safety before reach: retries must not send twice, secrets and personal data need a lifecycle, and F5 is what keeps the engine (scenarios, simulation) honest | a task retried by n8n runs its effect once; `manage.py erase_case` leaves no personal data in Case, Event or UserTask; F5 replays the ACME processes in both engines with identical traces |
| **N2 One platform** | B1/B2 spikes then the gateway (one origin, preview on its own host), B7 TS types from OpenAPI, B8 one config, B9 `bpmn8n` CLI replacing `start.sh`'s logic, B11 shared web components, A4 process contract + C18 landscape | The spikes decide whether one login is possible on n8n Community Edition; the contract feeds the landscape, typed clients and MCP | one URL serves Django and the editor; `bpmn8n up` from a clean checkout; the landscape names a message nobody sends |
| **N3 BPMN coverage** | C13 BPMN XML export (with diagram interchange) then import, C12 swimlane layout, C2 non-interrupting timers, C4 signals, C5 terminate, C8 Call Activity data mapping, C9 compensation (sagas), C14 data lens | Export first: it opens every workflow in bpmn.io, Camunda Modeler, Signavio and BIMP at once; the rest are nodes on the engine and checker that now exist | ACME exported and re-imported gives the same spec; a saga undoes its completed tasks in reverse order in a scenario test |
| **N4 People, intelligence, agents** | H7 workload dashboard, H8 notifications, H9 start a case from the UI, H11 similar-case panel; I2 detail sliders, I4 animation, I7 import wizard, I8 anonymised export; J2 task-as-tool, J6 learning loop from human overrides, J8 goals and alerts; E9 LLM cost in the simulation, E10 functional + performance simulation | They build on what exists (case page, insights, simulation, agents) and need N1's audit and erasure | an override of a Laya decision lands in the decisions dataset and moves its evaluation; a simulated case costs its LLM calls |

**Status:** N1–N4 are built (rows above). What is left, by workstream: D3, D4, D8, D9, D11 (method); F1, F8
(testing); G9; H12; I12; J5, J10–J13; the partial items of each row; and what n8n Community Edition cannot give
(one login, SSO, project RBAC, external secrets). The N4 exit test holds: a person's override of a Laya decision is
recorded on the decision (`overridden`, `final`), counted in the override rate and read by `suggest_thresholds` and
the goals; a simulated case costs its LLM calls (`cost_models_per_case`).
