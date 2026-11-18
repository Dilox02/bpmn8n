# bpmn8n

A framework for building BPMN workflows on n8n, checked before they run.
- **n8n** is the canvas and the engine, with 14 custom BPMN nodes drawn in the BPMN notation (tasks, gateways, events).
- **Django** exposes the tasks as endpoints, discovered automatically through **OpenAPI/Swagger**.
- **Studio**: describe a process, an LLM researches its domain on the web, interviews you and writes the activities and the workflow into a project folder, checked before they are loaded; a kanban shows the progress live.
- **Workflow check**: finds before execution what n8n would only reveal when a case gets there.
- **[GLiNER2.5](https://github.com/fastino-ai/GLiNER2)** fills in task inputs by reading free text.
- **[Laya](https://github.com/NandhaKishorM/laya)** (a "System 1" model) decides gateways and yes/no fields.
- **OCR** reads photographed or scanned documents (a referral, a receipt) with macOS's Vision, on the server.
- **People** work User and Manual Tasks on a page generated from the form: opening the node in n8n shows the link.
- Every step ends up in an **event log** for process mining.

```
 n8n (queue mode)                    Django · gunicorn × N                models server (host)
 ┌─────────────────────────┐        ┌──────────────────────────────┐     ┌──────────────────────┐
 │ main: editor + webhook  │        │ /api/tasks/<t>[/run]         │────▶│ /v1/extract  GLiNER  │
 │   │  Redis queue        │──POST─▶│ /api/gateways/*              │────▶│ /v1/systemone Laya   │
 │   ▼                     │◀─GET───│ /api/openapi.json            │────▶│ /v1/ocr      Vision  │
 │ worker × N: BPMN nodes  │◀resume─│ /api/usertasks · messages    │     │ micro-batching, MLX  │
 └─────────────────────────┘        │ /api/workflows/check         │     └──────────────────────┘
       ▲ link "Open the task page"  │ /inbox/<id> · /studio · /docs│────▶ LLM (Gemini, Claude,
       └────────────────────────────│ /api/eventlog.csv · mining   │      OpenAI, Ollama…)
             │                      └──────────────────────────────┘
             └────────── Postgres (n8n + bpmn8n) ─────────────┘
```

## Getting started

```bash
./start.sh            # start everything (safe to re-run: it rebuilds and reloads the nodes)
./start.sh status     # what is running
./start.sh logs       # live logs
./start.sh stop       # stop everything (data is kept)
./start.sh export     # n8n workflows -> workflows/*.json (to version with git)
./start.sh import     # workflows/*.json -> n8n (published workflows stay published)
```

The script:
- builds the nodes into `n8n-nodes-bpmn8n/dist`;
- on an **Apple Silicon Mac** starts the native **models server** (`tools/models_server.py`): Laya on the GPU (about 20 ms per decision), GLiNER2.5 on the CPU (about 20 ms), OCR with Vision (about 0.4 s per page). Elsewhere it uses CPU containers (`--profile cpu-models`), without OCR;
- starts Postgres, Redis, Django, n8n (main + worker, with the BPMN canvas), its read-only preview and SearXNG (web search for Studio's research), and applies the migrations;
- creates in n8n the **bpmn8n (web:8000)** credential and an API key (read and create workflows, never activate them; list, create and update credentials, for the LLM agents); writes the key, the credential id and the model addresses to `.env`, so manual `docker compose …` commands use the same configuration.

Then:
- **http://localhost:8000/docs/**: this guide;
- **http://localhost:8000/activities/**: the activities registered live, by node type, each with its decorator and code, its inputs (the node's fields) and its result;
- **http://localhost:8000/admin/** → *Settings*: the LLM providers for Studio; *Groups*: the lanes (roles);
- **http://localhost:8000/studio/**: design a process with the LLM.

More capacity: `WEB_WORKERS=8 ./start.sh` for Django, `docker compose up -d --scale n8n-worker=3` for n8n.
On n8n's first start, create the owner account at http://localhost:5678, then `N8N_EMAIL=… N8N_PASSWORD=… ./start.sh` (they are kept in `.env`, which git ignores).
The admin user: `docker compose exec web python manage.py createsuperuser`.
Requirements: Docker, Node.js and, on a Mac, [uv](https://docs.astral.sh/uv/).

## How the code is organised

```
bpmn8n/                      the framework (a Django app, installable as a library)
  api/          HTTP: tasks (@task and the TASKS registry), gateways, usertasks, messages, workflows,
                observability, mining
  core/         process variables, the case of a request, measurements and event log
  models/       cases (Case, Event) · waiting (UserTask, MessageSubscription and their states)
                config (Settings, LLMProvider) · studio (Project, Card)
  clients/      to the outside: http (retries), laya, extraction (GLiNER + Laya), llm, n8n, web (search, pages)
  workflows/    builder (Workflow, also from a JSON spec) · check (the workflow check)
  studio/       domain research, interview, prompts and project builds (jobs: their own process, cards)
  mining/       process mining with PM4Py (isolated: it is AGPL)
  web/          pages: inbox, task page, activities, docs, studio (style in static/bpmn8n/app.css)
  tasks.py      bpmn8n's own activities: compose_prompt (for the LLM agents), read_document_text (OCR)
  templates/ · migrations/ · management/commands/ · tests/
projects/                    one app per folder, discovered automatically
  acme/         the example processes: tasks.py (the activities), processes.py (the workflows),
                examples.json (the activities' tests)
  <name>/       what Studio creates: tasks/<activity>.py, one module per activity, and examples.json
n8n-nodes-bpmn8n/            the 14 BPMN nodes (TypeScript); nodes/icons/bpmn/: every BPMN 2.0 symbol
n8n-editor/                  n8n editor hooks: bpmn.js (the BPMN notation on the canvas), preview.js
searxng/                     the web search's settings, for Studio's research
tools/                       models server, benchmark, icons
e2e/                         tests on the running stack (Playwright)
examples/workflows/          ACME's workflows, to import into n8n
```

## One task = one function

```python
# projects/<name>/tasks.py or projects/<name>/tasks/<activity>.py (every module of a tasks/ package),
# or the same in any app of INSTALLED_APPS: discovered automatically
from typing import Literal
from ninja import Schema
from pydantic import Field
from bpmn8n.api import task

class Expense(Schema):
    employee: str = Field(description="Email or name of the employee who paid")  # also GLiNER's question
    category: Literal["meal", "travel", "hotel", "other"] = Field(description="Kind of expense")  # a menu in n8n
    amount: float = Field(description="Total amount paid, with currency")
    attendees: int = Field(1, description="Number of people the expense was for")

class PolicyCheck(Schema):
    within_policy: bool = Field(description="Is the expense within the per-person limit?")

@task("businessRuleTask")
def check_expense_policy(request, data: Expense) -> PolicyCheck:   # the result schema is optional
    """Per-person limit by category."""                             # the node's description
    ...
```

The **Business Rule Task** node lists it in its menu at once, without a rebuild. Every task has two endpoints:
- `POST /api/tasks/<name>` with the fields;
- `POST /api/tasks/<name>/run`, the one the nodes use: it binds the fields to the process variables (below), extracts from free text if needed, validates and runs **in a single pass**, and saves the result in the case.

Rules:
- verb + object names, in the singular (`@task` warns about *mushy verbs*: `manage_`, `handle_`…); two tasks with the same name are an error at startup;
- the input is **one** `Schema` argument; the result schema (`-> PolicyCheck`) goes into OpenAPI, tells the workflow check what the task produces and is enforced on every call: a result that breaks it fails at that task (500 with the reason), not three steps later as a missing variable;
- the project's `examples.json` says what each task must do, `{"check_expense_policy": [{"input": {...}, "output": {the fields expected}}]}`: `python manage.py check_project projects/<name>` runs them (with the import of every module and the workflow check), and so does `manage.py test bpmn8n` for every project;
- **tasks keep no state in memory**: with several workers each request may land in a different process. State goes in the database.

## The BPMN nodes in n8n

The icons are the official symbols of the BPMN 2.0 notation (the same as the poster), from [bpmn-font](https://github.com/bpmn-io/bpmn-font) (SIL OFL 1.1), in a light and a dark version; `nodes/icons/bpmn/` has every other symbol of the notation too (events of every kind, gateways, markers, data, sub-processes), from `tools/build_icons.py`. Search for **"BPMN"** in n8n's "+" panel.

**The canvas speaks BPMN.** `n8n-editor/bpmn.js`, loaded in n8n's editor and in the preview through `EXTERNAL_FRONTEND_HOOKS_URLS`, draws the nodes as [bpmn.io](https://bpmn.io) does: tasks as rounded rectangles with their type marker top left and their name inside (a Call Activity with a thick border and the [+] marker), gateways as diamonds, events as circles (start thin, intermediate double, end thick), sequence flows dark. On a canvas with bpmn8n nodes, n8n's own nodes join the notation: a Webhook is a message start event, a schedule a timer start event, a Merge the gateway that joins the branches, any other node a task with its icon as marker. The nodes keep n8n's grid, so editing works as before: the handles sit on the shape's outline and show on hover. It is CSS on n8n's `data-*` attributes: an n8n upgrade that renames them only brings back n8n's own look. The builder lays columns by the **longest** path, so a join comes after all its branches.

| Node | BPMN | What it does |
|---|---|---|
| Service / Script / Business Rule / Send Task | automated tasks | runs the `@task` chosen from the menu |
| User Task / Manual Task | human tasks | one task per item in the lane; the process **suspends** and resumes when they are all completed. Opened, the node links the task page (below) |
| Receive Task | waiting for a message | resumes when a system calls `POST /api/messages/<name>` with the *correlation key* |
| Call Activity | sub-process | runs another workflow (trigger "When Executed by Another Workflow") and continues with its result |
| Exclusive / Inclusive Gateway | XOR / OR | **Decide With**: *Laya* (reads text) or *Conditions* (`{{amount}} > 1000`); no branch → `default` |
| Parallel Gateway | AND | every item goes on along all branches |
| Event-Based Gateway | race between events | the first of several messages wins, or `timeout` (output 0); the losers are cancelled |
| Timer Event | intermediate timer | waits for an interval ("2 weeks") or until a date |
| Error End Event | error end | closes the case with a named error, records it in the event log and fires n8n's Error Trigger |

Receive Task and Event-Based Gateway wait for one message per execution: with several items the node stops with a clear error instead of losing some (split them upstream with *Loop Over Items*).

**Task input** ("Input From"):
- **Fields**: each field from the Python schema: a value, a `{{variable}}`, or **empty** to take the variable with the same name;
- **Text (GLiNER Extraction)**: a free text (an email, a ticket) the fields are extracted from. Fields filled in by hand take precedence; "Include Extraction Details" adds what was extracted and with what confidence.

**Task output**: *Task Result Only*, *Input + Task Result* or *Input + Result Under a Key*.

### Process variables: `{{book}}` instead of `{{ $('Request Received').first().json.body.book }}`

Each case (one execution of the workflow) has its own **variables**: the data that started it (the webhook body) plus every task result, completed form and received message. They live in Django (the `Case` table, `GET /api/cases/<id>`), so they survive waits of days and different workers.

| Where | Write | What happens |
|---|---|---|
| a task field | *(empty)* | takes the variable with the same name (`book` → `book`) |
| a task field | `{{reader}}` | takes another variable, with its type (a number stays a number) |
| text (subject, title, description) | `{{book}} is ready, due on {{due_date}}` | substitution in the text |
| nested data | `{{customer.email}}`, `{{hosts.0}}` | dots for keys and indexes |
| a gateway condition | `{{amount}} > 1000 and not {{vip}}` | only comparisons, `and/or/not`, literals, walked by bpmn8n's own evaluator (no `eval`); a number that arrives as text (`"100"` from a form) compares as a number with a number, `==` included |
| correlation key | `{{request_id}}` | |

n8n expressions (a field in *Expression* mode, e.g. `{{ $json.x }}`) keep working: n8n evaluates them first, bpmn8n receives the value.

Native n8n elements used as BPMN:
- **join** (parallel and inclusive) = the **Merge** node: with execution order v1, the editor's default, it waits only for the active branches;
- **boundary error event** on a node = *Settings → On Error → Continue (using error output)*.

## People: inbox, task page and states

Every User or Manual Task has its own page, `/inbox/<id>/`, generated from the node's form: the case's context, the fields, the actions allowed in its current state. Only people in the lane see the task (a Django group; without a lane, anyone). Opening the node in n8n:
- while the editor follows an execution (*Execute workflow*), the output panel says *Waiting for input* with the link **Open the task page**, which leads to that execution's task;
- always, even when reopening an execution from the list, a notice links the **inbox**, where every task shows its workflow and execution number (what n8n shows as *ID#*). n8n 2.42 does not show the waiting node of an execution reopened from the list (nor its Wait node): there the direct link cannot appear.

```
open ──claim──▶ claimed ──complete──▶ completed
  ▲  ◀─release──┘    │
  └──────────────────┴─────────────▶ cancelled   (n8n no longer waits: execution stopped, deleted or finished)
```

- **claim**: a person takes the task; the others see it read-only until it is released or completed;
- **complete**: in a single transaction the task closes, the form goes into the case's variables and the event log, and n8n resumes. If n8n does not answer, nothing changes and you can retry; if the execution no longer exists (n8n answers 404, or 409 "finished"), the task becomes **cancelled** instead of staying open forever;
- with several items (multi-instance) the process resumes when all the tasks of the group are completed, with one item per task in the order of the items;
- if n8n retries the same node run, it gets the entire original batch, including completed tasks, with no duplicates; a new loop iteration creates a new wait;
- an administrator can cancel tasks left hanging from the admin (the *Cancel* action).

The same states apply to messages: the losers of an Event-Based Gateway become *cancelled*; a message for an execution that no longer exists goes to the next one waiting.

## Workflow check

`POST /api/workflows/check[?start_data=a,b]` (the n8n workflow as JSON) or `python manage.py check_workflow file.json [--start-data a,b] [--json]` (exits with 1 if there are errors). For a whole project folder, `python manage.py check_project projects/<name> [--json]`: every module imports, every activity passes its examples, `workflow.json` passes the check with the start data of `process.py`. The start data are the fields of the webhook body: Studio knows them from the interview and declares them, so every variable is checked.

The checker follows paths from triggers. Required fields must exist on every alternative path, including the first entry into a loop; a guaranteed parallel join combines its branches' fields. Unknown output schemas remain conservative, but do not hide missing data on a known alternative. Event gateway message outputs have unknown payloads; timeout preserves the incoming data. Disabled nodes pass data through output 0. Missing lone-template entries in a human task's optional context are warnings because rendering omits those entries; missing title variables and required task inputs remain errors. A condition counts as a person's decision only while its fields retain human provenance on every path, without a later automated overwrite.

For technical housekeeping such as looping over a file queue until `pending == 0`, a `condition_gateway` may declare `decision_scope="technical"` in its Python/spec definition. This exempts only that deterministic gateway from the human-review rule. It does not exempt Laya gateways or LLM agents, and must not be used for decisions about people.

| Level | What it finds |
|---|---|
| error | a workflow with no step after its trigger; a connection to a missing node; a `@task` that does not exist or is of another kind than the node; a node unreachable from a trigger, including detached cycles; an invalid condition; a Call Activity whose sub-process waits (people, messages). With the start data declared (`start_data`): a required input or a required `{{variable}}` not supplied on every alternative path |
| error | after an LLM agent, a path that reaches an end without passing through a person (User or Manual Task) |
| warning | a gateway output that leads nowhere (for Laya also `default`: a person decides there); a lane without a group; no trigger; a sub-process that cannot be read; after a gateway (Laya or conditions), a path that reaches an end without a person (not for a conditions gateway that reads only fields of forms filled in before it: it applies a person's decision) |
| info | without declared start data: the fields the webhook body must carry (the required inputs that no earlier step produces) |

These are structural checks, not PM4Py's *soundness*, which gives false negatives on n8n workflows (a Merge is an inclusive join, parallel branches need not rejoin): tried on the four examples, it rejected two correct ones. Studio runs the check on every build and treats the "without a person" warning as an error too (what its activities print goes to stderr, so the JSON report stays readable; a build or research whose process dies, killed or with its container restarted, is ended by the pages after 3 minutes without its heartbeat); a test checks that ACME's examples pass it (with that warning where they close a case on their own).

### BPMN coverage

What BPMN 2.0's process diagrams have, and how bpmn8n has it:

| | Elements |
|---|---|
| **Nodes of bpmn8n** | Service, Script, Business Rule, Send, Receive, User and Manual Task; Call Activity; Exclusive, Inclusive, Parallel and Event-Based Gateway; intermediate Timer Event; Error End Event |
| **n8n itself, in the notation** | message start (Webhook), timer start (Schedule Trigger), none start (Execute Workflow Trigger); the join of parallel and inclusive branches (Merge, with execution order v1); multi-instance in parallel (n8n runs a node once per item: one User Task per item); loops (a flow back); text annotations (sticky notes); an error end's event sub-process (n8n's Error Trigger workflow) |
| **Can be added** (n8n allows them, bpmn8n does not have them yet) | timer and message **boundary events** on a User Task (a reminder or an escalation: a second output, like the Event-Based Gateway's `timeout`); **error boundary** on a task (n8n's per-node error output); explicit none and **terminate** end events (the latter stops the execution's other branches through n8n's API); **signal** events (one message that resumes every waiting case, on the existing subscriptions); conditional events (a condition checked on a timer); sequential multi-instance (Loop Over Items); **pools and lanes** drawn as bands (sticky notes, laid out by lane); **import and export of .bpmn files** (BPMN XML, to and from Camunda, bpmn.io, Signavio); compensation and transactions (sagas: each completed task records how to undo it) |
| **Not possible in n8n** (emulated at best) | containers: an expanded sub-process, an event sub-process inside the process, a transaction's box, pools as real containers (n8n's canvas has no nesting: a Call Activity or bands instead); an event drawn attached to an activity's border (it would be an output of the node); message flows between pools on the same canvas; complex gateways and parallel event-based gateways (no general rule to express them); choreography and conversation diagrams (not executable) |

## Studio: assisted design

1. **Domain research**, before the first question (`manage.py research_project`, in its own process, its steps as cards): the LLM plans web searches on the process's domain (the laws and standards that apply, the usual steps, roles and documents, the customary deadlines and amounts, what goes wrong), reads the pages found (2 per search), looks for what is still missing and searches again (2 rounds of up to 3 searches), then writes **domain notes** with their numbered sources. The notes join the project's local context: the interview, the design and the code read them, and `INTERVIEW.md` keeps them. During the interview the LLM can ask for more research (`{"research": "..."}`) when the person mentions a rule or a practice it needs to know, up to 3 researches per project.
   - The searches go through **SearXNG** (`SEARCH_URL`, the `searxng` service: several search engines, JSON, no API key; empty = no research, the interview starts at once). A query names the domain, never the company, its customers or a person; only public addresses are read (never the server's own network); a page is data the LLM summarises, never instructions. Whatever fails (no search engine, unreadable pages), the interview goes on with what is known.
2. **Interview** (`/studio/`, for staff): name the process and describe it. The LLM asks one question at a time (trigger and data, steps, roles, decisions and criteria, the data of each step, exceptions) knowing the tasks and lanes that already exist and what the research found (the person's answers win over it), then writes the *brief*.
3. **Build**, in a separate process (`manage.py build_project`), one kanban card per phase, updated via **SSE** with the cards to do, in progress and done:
   - *Design the process*: first a **critical assessment** of who does each step that needs judgement (software, rule, Laya gateway, LLM agent or person, with the reason), then the new activities (name, kind, inputs, outputs), the start data and the workflow as a spec for the builder. See *Who decides* below;
   - the design also gives each new activity 2–3 **examples** (input → the outputs that follow from the brief's rules): they are its tests, written to `examples.json` by the build, so the code cannot quietly change them;
   - *Write <activity>*: one module per activity (`tasks/<activity>.py`, with its own imports: no fragment can shadow another's), in a fenced block that must parse and define the function;
   - *Test the activities and check the workflow*: in a fresh process (`manage.py check_project`) every module is imported, every example is run and the workflow is checked; only what fails goes back to the LLM, with the traceback or the wrong result: the failing module, or the workflow (up to 4 rounds);
   - *Publish*: the project is written to `projects/_<name>/` and becomes `projects/<name>/` only if it passes, so a broken project never reaches the server;
   - *Import into n8n*: creates the workflow (inactive) and links the editor;
   - *Load the tasks*: reloads gunicorn, and the new activities appear in the nodes' menus.

   Under the kanban, the project page **embeds the workflow's canvas**: the `n8n-preview` service (read-only, no login) draws it as n8n draws it, in the BPMN notation and the page's light or dark theme. `./start.sh` starts it; its address is `N8N_PREVIEW_URL` (default `http://localhost:5679`) and Django's origin is allowed in its `frame-ancestors`.
4. **The project** extends the framework and can be edited freely:

```
projects/<name>/tasks/<activity>.py   one activity: its Schemas + @task function (TODOs where a real system is needed)
projects/<name>/examples.json  the activities' tests (python manage.py check_project projects/<name>)
projects/<name>/process.py     the workflow as a spec for Workflow.from_spec
projects/<name>/workflow.json  the same workflow for n8n (⋯ → Import from File)
projects/<name>/INTERVIEW.md   the brief, who decides and why, the domain research with its sources, the interview
```

### Who decides: software, rule, model, LLM or person

For each step, the design picks the simplest solution that is reliable, explainable and lawful, and writes it in the assessment (`spec.assessment`, shown on the project page and in `INTERVIEW.md`):

- **software** (a task) when the rule is known, or to read fields from a text (GLiNER, on the server);
- **condition_gateway** when an explicit rule on the data decides: the first choice, it is fully explainable;
- **Laya gateway** to route a free text among a few known branches: it runs on the server, gives a confidence and below the threshold hands over to a person;
- **LLM agent** (`llm_agent`: n8n's *AI Agent* node with the default provider's chat model) only for open-ended work on text that no rule can do and that really saves a person time: summarising, drafting a reply, proposing a decision with its reasons. When it is not needed anywhere, the design leaves it out and explains why;
- **person** for every final decision.

The constraints, which the check enforces:

- **people take the final decisions** (GDPR art. 22): after every gateway and every LLM agent, each path to an end passes through a User or Manual Task. Studio sends workflows that break this back to the LLM;
- **data minimisation**: an LLM agent receives only the variables its prompt names, never special categories of data (health, religion…). The prompt is filled in by bpmn8n (the `compose_prompt` task), so the event log keeps what was sent to the provider and what it answered;
- **explainability and transparency**: rules before models; every agent is told that it prepares work for a person, must use only the data it receives, explain how it reached its answer and say when the data is not enough. Its answer reaches the person in the task's context, marked as written by a model.

An `llm_agent` becomes four nodes: `<name>: prompt` (the prompt filled in with the case's variables), `<name>` (the AI Agent) with `<name>: model` (the chat model) under it, and `<name>: answer` (the answer, saved in the step's `output` variable). The **bpmn8n LLM: <provider>** credential is created in n8n, or updated if the key has changed, when a workflow with an agent is built: google → *Google Gemini Chat Model*, anthropic → *Anthropic Chat Model*, the other presets → *OpenAI Chat Model* with the provider's URL.

The pages follow taste-skill v2's *framed-sheet* style (coloured L-shaped frame, white sheet, colour only on data, IBM Plex Sans; Plex Mono only for IDs, keys and code), checked with its inspector at 1440, 768 and 390 px.

Studio writes code that the server then runs: it is for staff only and the code must be reviewed before production. *Build again* rewrites the project's activities (it does not reuse them) and replaces the folder only if the new version passes. gunicorn's reload waits for the LLM calls in progress (`--graceful-timeout 360`).

### Settings and LLM providers

*Admin → Settings* is a single record with the LLM providers and the default one. The **preset** fills in the URL and model left empty; the key is pasted there and stays in the database, never in the code. Local models need no key; the **timeout** (seconds per reply, 120) should be raised for a large model on the Mac. With Django outside Docker the URL is `http://localhost:<port>/v1`. Reasoning models (`<think>…</think>`, also when only the closing tag is in the reply, as Qwen and DeepSeek in LM Studio write it) and the imperfect JSON replies of small models are handled, and code comes in a ```` ```python ```` block, not inside a JSON string; with a local model give LM Studio a context of at least 16k tokens (the design prompt carries the catalog of tasks and the workflow steps) and raise the provider's timeout; a used-up quota (Gemini's free tier: 20 requests a day per model) fails at once with its message in the card.

| Preset | Protocol | Default model |
|---|---|---|
| google | OpenAI-compatible (`generativelanguage.googleapis.com/v1beta/openai`) | `gemini-3.5-flash` (the `gemini-flash-latest` alias hung for minutes) |
| anthropic | Messages API with the official SDK (`anthropic`), server-side fallback | `claude-opus-5-5` |
| openai, mistral, groq, openrouter | OpenAI-compatible | to be set |
| lmstudio | OpenAI-compatible, a local model served by LM Studio (`host.docker.internal:1234`) | to be set (e.g. `qwen/qwen3.5-9b`) |
| ollama | OpenAI-compatible, a local model (`host.docker.internal:11434`) | to be set |
| custom | any OpenAI-compatible server (vLLM, LM Studio…) | to be set |

## Extraction from text: GLiNER + Laya

For each field of the `Schema`, the model that is reliable for that kind of field is used:
- **values written in the text** (`str`, `int`, `float`: names, codes, amounts, numbers) → **GLiNER2.5 small**. Amounts like "1.299,50 €" or "$1,299.50" become numbers;
- **judgements** (`bool`, `Literal`: yes/no, a category) → **Laya**. GLiNER answered "yes" with confidence 0.99 even to "Hi", while Laya gave P = 0.05.

The two calls run in parallel. Below `Min Confidence` a field is "not found": its default applies, or the request comes back with the list of missing fields (422). The models server groups concurrent extractions into a single pass of the model (**micro-batching**): 207 extractions/s against 46 one at a time.

### Reading documents: OCR

`read_document_text` (a Service Task, in every catalog, Studio's included) reads the text of a photographed or scanned document: `image` is a JPEG, PNG or HEIC, base64 or a data URL (e.g. a field of the webhook body). It returns `document_text`, the lines top to bottom, and `ocr_confidence` (0 to 1: low for a blurred or dark photo, a good condition for a gateway that hands the document to a person). The next task extracts the fields from it with **Input From: Text** and `{{document_text}}`, so OCR + GLiNER turn a photo into a task's input:

```
Webhook {"referral_image": "data:image/heic;base64,…"}
  → Read Document Text  (image: {{referral_image}})        → document_text, ocr_confidence
  → Check Referral      (Input From: Text, {{document_text}}) → referral_code, specialty, priority…
```

It runs on the Mac with Apple's Vision framework (`POST /v1/ocr` of the models server, on the Neural Engine, nothing to download): the image never leaves the server, which matters for documents that carry health data. The event log does not keep the image (`Field(exclude=True)`), only the text read.

## Creating custom nodes

**Level 1 (the normal case)**: a `@task` function, as above. No rebuild.

**Level 2, a new kind of BPMN node**:
1. write `n8n-nodes-bpmn8n/nodes/Bpmn8n<Kind>.node.ts` starting from a base class:
   - `TaskNode` calls an endpoint;
   - `GatewayNode` decides with Laya or with conditions;
   - `WaitingNode` suspends and resumes, with multiple outputs, timeouts and multi-instance;
   - for everything else, `INodeType` + `bpmnDescription()`;
2. add the icon: the bpmn-font glyph in `GLYPHS` in `tools/build_icons.py`;
3. add the node to `package.json` → `n8n.nodes` and re-run `./start.sh`.

## Local evaluations in n8n

The **Evaluations** tab now runs bpmn8n's local implementation. Add a **Metrics** node after the step
to measure, choose the folder under `projects/` containing `workflow.json`, and add named numeric
values (including n8n expressions), units and optional inclusive thresholds. Higher/lower specifies
the comparison direction. The node preserves JSON, binary attachments and item pairing. An optional
run label distinguishes datasets and test fixtures. No subscription or cloud service is required.

Samples are atomic JSON files in `projects/<project>/run/<workflow ID>/<execution ID>/metrics/`.
The sample identity includes node ID, loop iteration and item index: retries replace the same sample;
iterations and concurrent nodes have distinct files. Only configured measurements and run identity
are written, not the input item or credentials. Back up `run/` with the project. No retention cleanup
runs automatically. The browser shows the latest 50 measured executions, each metric's last/mean/min/max,
threshold pass counts, a comparison of means with another run, raw samples and JSON export. Threshold
results describe collected samples; n8n's **Executions** tab remains the source of execution status.

Put a Metrics node where a decision is taken, not after every step. A metric is worth recording when
it answers a question whose answer changes what happens next: whether a loop iteration is healthy enough
to continue, whether the result may be used. Give it the threshold that decides, and follow the node
with a technical condition gateway that acts on the same values (the node itself never stops a run).
Counts and progress are already in n8n's execution data. A value that a step did not measure this time
(`null`) is left out of the sample rather than recorded as 0. The workflow check warns about a Metrics
node with no threshold or placed before anything was produced, and notes thresholds no gateway acts on.

Agents can add a `metrics` step through the workflow builder/Studio/MCP:

```python
workflow.metrics("Quality", "my_project", [
    {"name": "Accuracy", "value": "={{ $json.accuracy }}", "unit": "ratio", "threshold": .9},
    {"name": "Latency", "value": "={{ $json.latency_ms }}", "unit": "ms", "direction": "lower", "threshold": 500},
], label="experiment-1")
```

`POST /api/evaluations/samples` uses the existing BPMN API credential. Reads at
`GET /api/evaluations/<workflow ID>/runs` and `.../runs/<project>/<execution ID>` accept the backend
API token or the n8n session cookie, verified against that workflow's n8n access permissions. Evaluation
reads follow the same open-API policy as other endpoints when no API token is configured for development.
Browser reads need the same hostname as the n8n cookie (the supplied Compose setup uses `localhost` on both
ports); cross-host deployments need a same-host reverse proxy. CORS permits only `N8N_PUBLIC_URL`.
The editor hook replaces the Evaluation route component; it does not change n8n's licence flags or
enable its commercial evaluation backend. As with the other editor hooks, verify router integration
when upgrading n8n. If assets remain cached after editing a hook, restart the n8n editor container.

## Simulation and capacity

How long will a case take, where will it wait, how many people does a lane need? bpmn8n answers on the
workflow itself, from the same n8n JSON that runs: queueing theory in closed form at once, an event
simulation to confirm, both from a spec you write or one read from the event log. Pure Python and numpy
(`bpmn8n/engine/`, `bpmn8n/simulation/`); no PM4Py there.

**The token engine** (`bpmn8n/engine/`) plays a case on a virtual clock with n8n's semantics: one token per
item; an output connected to several nodes forks the token (AND split); a node that is not a Merge runs once
per token that reaches it (XOR join); a Merge fires once every input has a token or can no longer get one
(loops included), and in n8n's default *append* mode passes on every item it joined, so the steps after a
parallel join run once per branch (*combine* passes one); gateways take what a `Resolver` decides
(exclusive one output, inclusive any subset or `default`, event-based `timeout` or one message); an Error
End Event stops the case; a node visited more than `visit_limit` times per case stops it as *loop limit*.
`Resources` make tokens wait for a lane's people (FIFO, optional priority, working hours). Every case keeps
its trace. The simulator uses it with sampled times; a `Stub` resolver plays a case with given answers.

**The spec**: `projects/<p>/simulation.json`, next to `workflow.json`, in the sections of the course's process
simulation specification ([BS] p.83–85). Everything is optional: without it a user or manual task is done by
its lane (one person, about 10 minutes), a timer waits for its parameters, an event-based gateway's message
comes before its timeout, automated steps take no time and wait for nobody, branches are equally likely.

```jsonc
{"unit": "minutes", "workflow": "workflow.json",
 "arrivals":  {"type": "poisson", "rate": 0.1, "count": 500},             // PSS: peak (count) · interval (every) · poisson (rate) · list (times)
 "resources": {"managers": {"capacity": 2, "cost_per_hour": 60, "schedule": "office"}},   // RES
 "schedules": {"office": {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "09:00", "end": "17:00"}},  // WS
 "tasks":     {"Approve Expense": {"duration": {"type": "lognormal", "mean": 12, "sd": 8},
                                   "resource": "managers", "cost": 0.5}},  // TSK, any step by name
 "gateways":  {"Assess Justification": {"business": 0.6, "personal": 0.1, "default": 0.3}},  // GTW
 "replications": 10, "seed": 1, "horizon": null, "warmup": 0, "visit_limit": 100}
```

Durations: a number (fixed), or `exponential` (mean), `normal`, `lognormal`, `gamma` (mean, sd),
`triangular` (min, mode, max), `uniform` (min, max), `empirical` (values). An exclusive or event-based
gateway's probabilities sum to 1, what they leave goes to `default` / `timeout`; an inclusive gateway gives
each branch its own probability, and takes `default` when none applies. `projects/acme/simulation.json`
simulates the expense approval.

**Commands and API**

```bash
python manage.py simulate projects/acme --analytic          # closed forms and the scenario table, at once
python manage.py simulate projects/acme --replications 20   # the event simulation, with confidence intervals
python manage.py simulate projects/<p> --calibrate --process "<n8n workflow name>" [--since 2026-01-01]
python manage.py simulate projects/acme --staffing 20 --metric mean   # the fewest managers for a 20-minute mean
```

`POST /api/simulation/analytic`, `/run` (with `compare`: a second spec on common random numbers),
`/staffing`, and `GET /api/simulation/calibrate?process=&since=&until=&project=`. A workflow comes inline
(`{"workflow": …}`) or as a project (`{"project": "acme"}`), with an optional `spec`. Runs are synchronous,
capped at 200 000 simulated cases per request; bigger ones go through the command.

**What the outputs mean**
- **Scenario table** (`--analytic`): the paths a case can take (the outputs of its choices, in order), with
  their probability and tokens out of N, as students draw them ([BS] p.18–21): for ACME's library lending
  with available 60 %, hold 25 %, decline 25 %, a week without answer 50 %, the rows are 60, 20, 10, 6, 2, 1,
  0.6… and sum to 100. Loops are unrolled until a path's probability falls below a threshold; the rest is
  one "unrolled further" row. A case through a parallel split ends once: its row combines the choices of
  every branch and lists every end it reached. Expected visits per step come exactly from the absorbing
  chain (I − Q)⁻¹.
- **Queueing network** (`--analytic`): per lane λ = arrival rate × visits of its steps, ρ, Wq and W as a
  G/G/c station (M/M/c is exact); the bottleneck; *unstable* when work arrives faster than people do it.
- **Simulation**: completed cases, total cost, simulated time; cycle and waiting times (mean, p50, p90,
  max); utilisation per resource and over time; cost per case; per step visits, queue time, work time and
  cost; per scenario the first and last case out, how many and their mean cycle time ([BS] p.26–28,
  62–64, 85). Every headline metric is the mean of the replications with its Student t interval at 90, 95
  and 99 % ([BS] p.89): read a difference only when the intervals say so.
- **Staffing**: the fewest people per resource for a target (mean or p90 cycle time, or the total time of
  the simulated cases), at most 20 each by default and optionally a total cap; greedy descent as in [BS]
  p.45–51, 90–96, screened by the closed forms and confirmed by replications, with the sensitivity of one
  person less per resource.
- **Calibration**: arrivals, a fitted distribution per activity (exponential, lognormal or gamma by AIC;
  the samples themselves when fewer than 10), branch shares from the gateways' decisions, people per lane
  from who completed the tasks. The log has no claim time: a human task's time includes the wait for a free
  person, which the simulation then adds again on a busy lane.

**Approximations** (each marked in the code): every station sees the process's arrival variability (no
propagation between stations); Allen–Cunneen for G/G/c; the analytic cycle time takes the longest of the
parallel branches' means (an underestimate when they vary); the closed forms ignore work schedules; work
started before closing goes on after it; parallel branches overlap in time (n8n runs one node of an execution
at a time, but people work in parallel and automated steps take milliseconds).

**Unlike the course's simulators** ([BS] p.65–68, 73–75), which cannot run parallel paths of one scenario
and need a redundant "emulation" model, allocate few resources unevenly, and fake inter-arrival times with a
timer in a one-instance lane to subtract afterwards: here parallel branches, joins, loops and arrivals are
native, and the analytic tables, the simulation and the running workflow read the same JSON.

## Observability and process mining

- Every measured call is an `Event` row in Postgres. The **metrics** (`/api/metrics`: calls, errors, latencies) are SQL aggregates, so they cover every worker. `X-Bpmn8n-Profile: 1` adds cProfile and the memory peak.
- **Event log** (`/api/eventlog.csv`, streamed): process = workflow, `case_id` = `<workflow id>:<execution id>`, activity = node, resource = who did it. It imports into PM4Py, Disco or ProM; for external or historical logs there is `manage.py import_eventlog file.csv`.
- Every event also keeps **what it read and produced** (`Event.data`): the input and result of tasks, forms and messages; for gateways the state they decided on, the options, the branches taken and Laya's confidence.

### Process mining pipeline (`bpmn8n/mining/`, with [PM4Py](https://pm4py.fit.fraunhofer.de))

It reinvents nothing: PM4Py (Fraunhofer FIT) does the mining, scikit-learn the decision rules.

| Phase | What | How |
|---|---|---|
| Log | events → XES DataFrame | `EventLog` (`log.py`); a case is *closed* if it has waited for nothing for 1 minute |
| Discovery | variants, directly-follows with **branch probabilities**, the real BPMN model | `pm4py.discover_dfg`, `discover_bpmn_inductive` (*inductive miner*) |
| Conformance | do the cases follow the workflow **designed** in n8n? | the workflow (read via the n8n API) becomes BPMN and then a Petri net (`WorkflowModel`, `model.py`); PM4Py's optimal **alignments**: fitness, deviations "skipped X" / "unexpected X" |
| Performance | lead time p50/p90/p95, slowest transitions (bottlenecks), times and resources per activity | `discover_performance_dfg` |
| Decision mining | per gateway: who decides, branches, Laya's certainty, outcome per branch, **rules** that explain the branch from the data | scikit-learn decision tree |
| Concept drift | when the process changed | Bose et al. (CAiSE 2011), in PM4Py; from 800 cases |
| Training | a dataset for a neural network + its card | `TrainingData` (`training.py`) |

Endpoints:
- `GET /api/processmining?since=&process=`: the report per process.
- `GET /api/processmining/discovered.bpmn?process=`: the real process in BPMN 2.0, opens in bpmn.io and Camunda Modeler.
- `GET /api/datasets/card` and `GET /api/datasets/{prefixes|decisions}?split=train|val|test` (streamed JSON Lines).
- All of it in a folder, with no HTTP time limits: `docker compose exec web python manage.py mine --out mining/`.

**Data to train a neural network**:
- `prefixes`: for every step of a case, the sequence up to there (activity, resource, elapsed time) and the data known up to that moment, with three targets: **next activity**, **remaining time**, **outcome**. It is the format of *predictive process monitoring* (LSTM, transformer).
- `decisions`: every gateway decision with the question, the state, the options, the branch taken, Laya's scores and the **case outcome**. Useful to distil Laya or as a reward for a reasoning model.
- Only cases that are **closed and conform** to their model are used, closed at the same instant the analysis evaluates them.
- The splits are **temporal**: the oldest cases go to train, the most recent to test. A train case still open when validation starts is dropped, because a random split shows the model the future (Weytjens & De Weerdt, BPM 2021).
- The **card** says for each target how many examples there are per label and split. It warns about labels that are rare in train, labels that appear only in validation or test, and process changes, saying from which date it is worth training.

## bpmn8n inside another Django project

bpmn8n is also a library (`pyproject.toml`). An existing project installs it, adds it to `INSTALLED_APPS` and mounts its API:

```python
# settings.py: set only the values that differ from bpmn8n/conf.py's defaults (LAYA_URL, N8N_URL, …)
INSTALLED_APPS += ["bpmn8n"]
# urls.py: the n8n nodes call <baseUrl>/api/...; the project's routes come first
from bpmn8n.api import api, discover
discover()  # registers the @tasks of the apps' tasks modules (a broken one is logged and left out)
urlpatterns += [path("api/", api.urls)]
```

The project gets:
- the cases' event log (`Event`, `Case`), with `measure()` and `record()` (`bpmn8n.core.observability`) to record its own steps there;
- process variables and process mining (`bpmn8n.mining`);
- its own `@task`s as n8n nodes;
- the workflow builder and check (`bpmn8n.workflows.Workflow`, `bpmn8n.workflows.check`).

**Showing a workflow in n8n's canvas from another page**:
- an iframe on `<n8n>/workflows/demo`, or on the **n8n-preview** service (port 5679: read-only, with no login nor database, in the BPMN notation, with a dark theme and tasks coloured by name);
- on the message `{"command": "n8nReady"}`, send it `{"command": "openWorkflow", "workflow": …}` via `postMessage`;
- the page's origin must be allowed in `N8N_CONTENT_SECURITY_POLICY` (`frame-ancestors`, in `docker-compose.yml`);
- with the main n8n you need to be logged in in the same browser.

## Many active workflows together

What the framework guarantees with hundreds of active workflows:
- **Unique cases**: a case is `<workflow id>:<execution id>`. n8n's execution ids repeat across workflows and restart after a database migration (it really happened).
- **Separate processes**: mining groups by workflow, so two processes that start with the same activity stay distinct.
- **Messages never delivered "at random"**: if several workflows wait for the same message with the same correlation key, delivery answers **409** with the list of candidates, and the sender specifies `"process"`. A race already won or an expired timeout give 404.
- **Webhooks**: n8n refuses to activate two workflows with the same path, so use a unique path per process.
- **Capacity**: add Django and n8n workers. Laya's decisions go through a single GPU and are the limit beyond about 50 cases/s (below).

## Verified hypotheses

The hypotheses were verified on the real stack (n8n 2.42, Apple M5) with the tests in `e2e/`. The samples are small, so these are indications to confirm on a company's data. The detailed reports (`reports/`) are not in the repository: they are regenerated with the stack running, `pytest e2e/test_laya_eval.py e2e/test_extraction_eval.py e2e/test_mining_and_load.py`.

| # | Hypothesis | Outcome |
|---|---|---|
| H1 | A `@task` appears in the n8n node with the right fields, without a rebuild | Yes |
| H2 | Laya routes tickets (5 languages) and expense notes zero-shot; branches as *intents* > keywords; inclusive with short yes/no questions | Yes, 87% / 90%; F1 0.77 |
| H2d | Below 0.7 confidence it pays to let a person decide | Yes, above 0.7: 100% correct, about 35% to a person |
| H3 | Human-in-the-loop: lanes, inbox, resume | Yes |
| H4 | The event log allows correct process mining | Yes, with PM4Py: the 230 closed cases of the 4 processes have fitness 1.0 against the n8n workflows (alignments); a constructed deviation is found and named ("skipped Is Book Available?") |
| H5/H8 | It withstands bursts of cases | No, then yes: 6 causes found and fixed (below) |
| H6 | Slow Laya = wrong backend | Yes, CPU in Docker 167 ms → Apple GPU (MLX) **14 ms** |
| H7 | Everything can be configured from the n8n editor | Yes, Playwright tests on the editor |
| H9 | GLiNER2.5 small reads values from text | Yes, amounts 0.96, emails 1.00, numbers; Italian 0.89; p50 26 ms |
| H10 | For yes/no and choices Laya beats GLiNER | Yes, 0.90 against 0.61 |
| H11 | Laya verifying GLiNER's values reduces errors | No, falsified: no fewer errors, some right values lost. It stays optional ("Verify Values with Laya"), off |
| H12 | Event log and metrics on Postgres → Django scales across processes | Yes, 1124 → **2388 requests/s** (4 workers), 0 errors |
| H13 | n8n's queue mode raises throughput | Yes, 15 → **53 cases/s** (200 cases in 3.8 s); more workers do not help: the limit becomes Laya's GPU |
| — | n8n's Merge can act as an inclusive join | Yes, with order v1 (v0 forced the skipped branches too) |
| — | PM4Py's soundness checks n8n workflows | No: false negatives on 2 correct examples out of 4: the check uses structural rules |

**Robustness: what broke, and how it was fixed** (each case has its test):
- Laya under load answers 503: now it retries, honouring `Retry-After`.
- The Docker proxy leaves some connections hanging: a 10 s timeout per attempt, with retries.
- Django's `runserver` dropped connections: replaced by gunicorn.
- Keep-alive race ("socket hang up"): keep-alive disabled on the server side.
- An expired wait resumed on the wrong output, because n8n reruns it "disabled": `timeout` is always output 0.
- State in memory in a task, with several workers: it goes in the database.
- Duplicate case ids after the migration: now they are `workflow:execution`.
- A task whose process had been stopped stayed open forever: now it becomes *cancelled*.
- The form's variables entered the case after n8n resumed: now in the same transaction.
- With several items a User Task lost every item but the first: now one task per item.

**Guidelines**
1. Describe an exclusive gateway's branches as *the customer's situation*, not as keywords.
2. Inclusive: short, simple yes/no questions.
3. `Min Confidence` 0.7 with the `default` output connected to a User Task (the check reminds you).
4. Clear field descriptions: they are the questions for GLiNER and Laya.
5. Models on the GPU, Django and n8n with several workers; to go further, more replicas of the models server.

## Use cases (`projects/acme/`, `examples/workflows/`)

- **UC1 Ticket triage**: Register → Exclusive (Laya) → refund, bug, sales; Laya unsure → human triage.
- **UC2 Expense notes**: Policy (business rule) → Exclusive → **Approve Expense** (managers) → Record → Notify.
- **UC3 Incidents**: Normalize → Inclusive (isolate host / reset password / DPO) → actions; no clear action → **Review Alert**.
- **UC4 Library lending** (lesson 2): available → Fetch (manual) → Check Out (user) → Parallel; on loan → reply → **Event-Based Gateway** (hold-book / decline-hold / 1 week) → hold → **Timer** 2 weeks → the check again.

`python -m projects.acme.processes` rewrites `examples/workflows/`.

## Tests

```bash
python manage.py test bpmn8n                          # 111 tests: API, states, check, Studio (real build), mining, every project's examples
python manage.py check_project projects/<name>        # one project: imports, examples, workflow
cd n8n-nodes-bpmn8n && npm test                       # 12 tests: schema → fields, input, output, waits, errors
.venv-models/bin/python -m unittest discover tools    # micro-batcher
pip install -r requirements-dev.txt && playwright install chromium
pytest e2e                                            # 37 E2E tests on the ./start.sh stack (UI included)
python tools/bench.py                                 # throughput of an endpoint
```

The datasets in `e2e/datasets/` and some E2E tickets are in several languages on purpose (`"lang"`: en, it, es…): they measure Laya and GLiNER on multilingual text.

## Known limits

- The gateways' timeout has the precision of n8n's *wait tracker*, about 1 minute: fine for hours or days.
- Laya's decisions go through one GPU at a time: `laya-mlx` does not batch different texts together (upstream `laya` does).
- OCR needs macOS (Vision): the CPU containers have none, and `read_document_text` fails there with the models server's message. It reads images, not PDFs (one page at a time). An image in the webhook body stays in the case's start data, like the rest of it.
- Compensation events and transactions are missing (see *BPMN coverage*).
- Studio's research reads the web: its notes can be wrong or out of date (the sources are listed to check them), and a page could try to steer the LLM; the notes are summaries, the person's answers win, and the code is tested and must be reviewed.
- The E2E tests use n8n's internal REST API (`/rest/...`), which may change between versions.
- Conformance uses the **current** version of the workflow: cases run before a change may show up as deviating. An Event-Based Gateway's timeout writes no event either: in the model it is a silent path.
- Mining keeps the log in memory (about 1 GB per 5 million events); beyond that, analyse by time windows (`since`).
- A task becomes *cancelled* when someone tries to complete it and n8n says the execution no longer exists, not at the moment it is stopped.
- Studio writes code tested on its examples, not against the real systems: the `# TODO`s must be completed and the code reviewed. The examples come from the same LLM as the design: a wrong expected value fails the build rather than passing it, and the card says which.
- An n8n API key without the credential scopes (created before the LLM agents) is replaced by `./start.sh`.

## Licences

- **PM4Py is AGPL-3.0.** For internal use nothing changes. If bpmn8n is offered **to third parties over a network** (SaaS), the AGPL requires publishing the code of the whole service, or buying PM4Py's commercial licence. Mining is isolated in `bpmn8n/mining/` (the workflow check does not use it), so it stays replaceable.
- **n8n** is under the *Sustainable Use License* (fair-code): internal use is free; the Enterprise features (`.ee.` code) require an n8n licence. bpmn8n does not unlock them: it offers its own open alternatives.

| n8n paid feature | In bpmn8n (open) |
|---|---|
| Variables (`$vars`) | per-case process variables: `{{book}}` |
| Log streaming, audit log, Insights | event log in Postgres, `/api/metrics`, process mining |
| Advanced execution filters | `/api/cases/<id>`, event log filterable by process and date |
| Source control, workflow diffs, named versions | `./start.sh export` writes `workflows/*.json` to version with git; `./start.sh import` imports them again |

Free and legal: registering the *Community Edition* (n8n → Settings → Usage and plan) unlocks folders, debugging in the editor, search in execution data and 24 hours of workflow history in n8n.

### Live evaluations and execution tokens

The local Evaluations view uses **SSE** (`/api/evaluations/<workflow>/stream`): events carry only
change notifications, then the client fetches conditional JSON responses. `ETag`/`If-None-Match`
return 304 for unchanged blocks. SSE reconnects with `Last-Event-ID`, sends heartbeat comments,
closes while the page is hidden and is disposed on navigation. Streams rotate after 55 seconds;
the server checks small indexed revision queries once per second. No browser polling timer or
refresh checkbox is used by this view. n8n's incumbent execution sidebar retains its own controls.

`EvaluationStore` maintains `projects/.evaluations.sqlite3` in WAL mode. Existing JSON samples are
imported once; normal reads use the index. Run lists contain at most 50 summaries. Sample pages are
bounded to 200 records and chart series to 200 averaged time buckets (default 150). Exports stream
all samples. Source sample JSON files remain in each project's `run/` directory. Editing those files
by hand does not update a live index; remove the rebuildable index only with the services stopped
if a full import is needed. Back up both the index and the run folders.

A project's optional `evaluations.json` provides its title, purpose, process stages, metric descriptions
and progress labels. Thresholds and measurements still come from Metrics nodes: the text does not
replace their configuration. The contrastive project explains accepted groups, rejected anchors and
remaining work, and shows failed final thresholds above the charts.

The execution preview keeps its canvas mounted while state changes arrive. It reads n8n's saved
execution stack and join buffers, alongside live framework function invocations. Every input item,
input port and loop iteration remains distinct. The shared node HTTP client sends node/run/item
identity, allowing calls on the same node to be correlated without merging parallel tokens. Older
executions without that identity retain their invocation history, but cannot promise exact item-level
correlation. Completed executions show their final snapshot; previous intermediate states cannot be
reconstructed if n8n did not persist them.

Framework spans persist a `running` Event before invoking a function, save its input, then update the
same Event at completion or failure. Active spans are excluded from aggregate timing and mining logs.
Invocation bodies are loaded only when expanded, with bounded previews. The canvas distinguishes
confirmed active calls from engine stack entries and inputs waiting at a join. **Vista n8n / debug**
keeps the original execution inspector available.

Local preview iframes use `credentialless` isolation and `localhost` to prevent the anonymous preview
instance from clearing the main editor's cookies (cookies are shared across ports). The supplied
Chrome/Chromium setup is verified; other browsers need credentialless support or a separate HTTPS
preview origin. The preview CSP explicitly allows the two local editor origins.

Checks: `python manage.py test bpmn8n.tests`, `npm --prefix n8n-nodes-bpmn8n test`, and
`node --test n8n-editor/test/*.test.mjs`. Browser checks exercise live SSE updates, retained focus,
selection/open details/SVG identity, both local session hostnames, the real execution canvas, and a
controlled fork fixture with two active branches followed by one waiting join input.
