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

### Global variables: `{{vars.max_refund}}`, shared by every workflow

Process variables belong to one case; **global variables** belong to every workflow at once: a limit, a rate, a counter, a switch. They are bpmn8n's open alternative to n8n's paid Variables (`$vars`).

- **Where they live**: in Redis when `REDIS_URL` is set (`docker-compose.yml` uses `redis://redis:6379/1`, a database n8n's queue does not use; Redis keeps them on disk with `--appendonly yes`), else in the database (`GlobalVariable`: tests, SQLite, bpmn8n inside another Django project). Both behave the same.
- **Keys** are letters, digits, `_ . -` (`acme.max_refund`); **values** are JSON; a variable may **expire** after a TTL.
- **In templates**, wherever bpmn8n fills them (task fields, texts, gateway conditions, user task title, description and context, correlation keys): `{{vars.max_refund}}`, `{{amount}} > {{vars.max_refund}}`, `{{vars.acme.policy.currency}}` (a key with dots: the longest key that exists, then the path inside its value). Only the keys a template names are read, once per template. `vars` is reserved: a case variable called `vars` is shadowed in templates.
- **The Global Variable node** (BPMN, a task with the data-store marker): *Get*, *Set* (optional TTL), *Increment* (atomic; a missing variable counts as 0; the TTL applies when the increment creates it, a counter per time window) or *Delete*. The value goes on with the item under the variable's key, so the next bpmn8n step also has it as a case variable.
- **The API** (token as the rest of `/api`): `GET /api/variables?prefix=`, `GET|PUT|DELETE /api/variables/<key>` (`PUT {"value": …, "ttl": seconds}`), `POST /api/variables/<key>/increment {"by": 1}`, `POST /api/variables/<key>/compare-and-set {"expected": …, "value": …}` (409 with the current value when it changed).
- **In Python**: `from bpmn8n.core.shared import store`: `get`, `set(key, value, ttl)`, `delete`, `list(prefix)`, `increment`, `compare_and_set`, and `with store().lock("nightly-export", ttl=30):`, a short lock that a crashed holder releases after `ttl` seconds.
- **The page** `/variables/` (staff): filter by prefix, add, edit and delete, with the last update and the expiry.

## People: inbox, task page and states

Every User or Manual Task has its own page, `/inbox/<id>/`, generated from the node's form: the case's context, the fields, the actions allowed in its current state. Only people in the lane see the task (a Django group; without a lane, anyone). Opening the node in n8n:
- while the editor follows an execution (*Execute workflow*), the output panel says *Waiting for input* with the link **Open the task page**, which leads to that execution's task;
- always, even when reopening an execution from the list, a notice links the **inbox**, where every task shows its workflow and execution number (what n8n shows as *ID#*). n8n 2.42 does not show the waiting node of an execution reopened from the list (nor its Wait node): there the direct link cannot appear.

```
open ──claim, reassign──▶ claimed ──complete──▶ completed      (open completes directly too)
  ▲  ◀──────release─────────┘  ↻ reassign, delegate: claimed by someone else
open, claimed ──suspend──▶ suspended ──resume──▶ open           n8n still waits; nobody can claim it
open, claimed, suspended ──▶ cancelled   n8n no longer waits (stopped, deleted, finished), or the quorum was reached
open, claimed, suspended ──▶ expired     its deadline passed: n8n went on through "timeout"
```

- **claim**: a person takes the task; the others see it read-only until it is released or completed;
- **complete**: in a single transaction the task closes, the form goes into the case's variables and the event log, and n8n resumes. If n8n does not answer, nothing changes and you can retry; if the execution no longer exists (n8n answers 404, or 409 "finished"), the task becomes **cancelled** instead of staying open forever;
- **suspend** / **resume**: a suspended task waits for nobody: n8n still waits, but nobody can claim or complete it, and the inbox count leaves it out; resumed, it goes back to its lane, open. Its worker (the assignee, or anyone of the lane while it is open) or staff can do it;
- **reassign** (staff) and **delegate** (the assignee) hand the task to someone else who may work it: it becomes claimed by them;
- with several items (multi-instance) the process resumes when all the tasks of the group are completed, with one item per completed task in the order of the items. **Completion** can ask for less ([BO] §2.7's join checker): *any* (the first wins) or *at least N*; once reached, the case resumes with the completed items and the batch's other tasks are cancelled ("the quorum was reached"), like an Event-Based Gateway's losers;
- if n8n retries the same node run, it gets the entire original batch, including completed tasks, with no duplicates; a new loop iteration creates a new wait;
- an administrator can cancel tasks left hanging from the admin (the *Cancel* action).

**Due date, deadline and priority.** *Due* is the task's SLA: after a time span, or until a date (an ISO date, which means the end of that day, a date-time, or a `{{variable}}` holding one, filled in from the case). It orders the inbox and marks the task *overdue* (or *due soon*, within a day). With **Deadline** on, the due time interrupts the wait (BPMN's interrupting boundary timer): the node gets two outputs, **timeout** (output 0, taken when the due time passes: n8n moves on by itself) and **done** (completed in time); its tasks become **expired** (read lazily whenever tasks are read, and by `python manage.py expire_tasks`), completing one answers 410, and the tasks of one batch share the earliest due time, since n8n waits once. Without a deadline the node keeps its single output. **Priority** (low, normal, high, urgent) orders the inbox after *overdue*: the inbox lists the most urgent first (overdue, then priority, then the earliest due, then the oldest), with tabs *All*, *Mine*, *My lanes*, *Overdue*, *Suspended* and a priority filter.

**Who works it** ([BO] §2.3.4). **Assignee**: a username or a `{{variable}}` (e.g. `{{approver}}`): the task is created already claimed by that person, if they exist and may work it; else it opens to the lane. **Not Same As** is four-eyes: whoever completed one of the named activities in the same case may not claim or complete this one (the task page says why), and is not offered for a reassignment.

**Form fields.** *Text*, *Long Text*, *Number*, *Yes/No*, *Choice* (its options, comma-separated), *Date* (`YYYY-MM-DD`) and *Email*; every field is required unless *Required* is off (an optional field left empty becomes `null`; a Yes/No is never missing: unchecked is false), and *Help* is shown under it. bpmn8n checks each value; on the task page each error appears next to its field and what was typed stays. Forms with only name, label and type work as before.

| Builder (`user_task`, `manual_task`) | Node | API (`POST /api/usertasks`) |
|---|---|---|
| `due_in=(2, "days")` · `due_at="{{due_date}}"` | Due | `due`: ISO or `{{variable}}` |
| `deadline=True` (then `flow(..., output="timeout")` / `"done"`) | Deadline | `deadline` |
| `priority="high"` | Priority | `priority` |
| `assignee="{{approver}}"` · `not_same_as=["Submit Expense"]` | Assignee · Not Same As | `assignee` · `not_same_as` |
| `completion="any"` or `2` | Completion · Quorum | `completion` |
| `form=[{"name", "label", "type", "required", "help", "options"}]` (or `[name, label, type]`) | Form Fields | `form` |

`GET /api/usertasks?priority=&overdue=` lists them; `POST /api/usertasks/<id>/suspend`, `/resume`, `/reassign {"username"}`, `/delegate {"username"}` move them (409: the state does not allow it; 410: cancelled or expired; 422: invalid data). The task page links its case (`/cases/<case id>/`) and shows its due time, priority and only the actions its state allows.

The same states apply to messages: the losers of an Event-Based Gateway become *cancelled*; a message for an execution that no longer exists goes to the next one waiting.

## Workflow check

`POST /api/workflows/check[?start_data=a,b]` (the n8n workflow as JSON) or `python manage.py check_workflow file.json [--start-data a,b] [--json]` (exits with 1 if there are errors). For a whole project folder, `python manage.py check_project projects/<name> [--json]`: every module imports, every activity passes its examples, `workflow.json` passes the check with the start data of `process.py`. The start data are the fields of the webhook body: Studio knows them from the interview and declares them, so every variable is checked.

The checker follows paths from triggers. Required fields must exist on every alternative path, including the first entry into a loop; a guaranteed parallel join combines its branches' fields. Unknown output schemas remain conservative, but do not hide missing data on a known alternative. Event gateway message outputs have unknown payloads; timeout preserves the incoming data. Disabled nodes pass data through output 0. Missing lone-template entries in a human task's optional context are warnings because rendering omits those entries; missing title variables and required task inputs remain errors. A condition counts as a person's decision only while its fields retain human provenance on every path, without a later automated overwrite.

For technical housekeeping such as looping over a file queue until `pending == 0`, a `condition_gateway` may declare `decision_scope="technical"` in its Python/spec definition (in the n8n editor: the gateway's *Decision Scope*). This exempts only that deterministic gateway from the human-review rule. It does not exempt Laya gateways or LLM agents, and must not be used for decisions about people.

The rules are classes in `bpmn8n/workflows/rules.py`, run in this order; `python manage.py rules [--md]` lists them and prints this table (a test fails when the table here is stale). The checker reads the workflow as a `ProcessGraph` and each node as an element of the registry (`bpmn8n/workflows/elements.py`, served as JSON at `GET /api/elements` and by `python manage.py elements`): a rule asks whether a node waits, decides or joins, not which n8n type it has.

| Rule | Level | Category | What it finds |
|---|---|---|---|
| `empty` | error | structure | a workflow with no step after its trigger |
| `trigger` | warning | structure | no trigger: nothing starts a case |
| `connection` | error | structure | a connection from or to a node that does not exist |
| `reachable` | error | structure | a node no path from a trigger reaches, detached cycles included: it never runs |
| `task` | error | structure | a `@task` that does not exist, or of another kind than its node |
| `output` | warning | structure | an output that leads nowhere, on a gateway or any node with several outputs, a task's `error` output included (not a conditions gateway's `default`, nor a task's `main`; Laya's `default` must lead to a person) |
| `condition` | error | data | an invalid branch condition; a warning for a branch with no condition (never taken) |
| `lane` | warning | people | a human task whose lane is no group: only superusers will see the task |
| `subprocess` | error | structure | a Call Activity whose sub-process waits (people, messages); a warning when the sub-process cannot be read or is not published |
| `input` | error | data | with the start data declared: a required input of a task that neither its node nor, on every path, an earlier step or the start data provides |
| `variable` | error | data | with the start data declared: a `{{variable}}` nothing provides on every path (`{{vars.…}}` always exists); a warning for an optional entry of a human task's context, left out when empty |
| `start-data` | info | data | without declared start data: the fields the webhook body must carry (the required inputs no step produces) |
| `human` | error | people | after an LLM agent, a path that reaches an end without a person (User or Manual Task); a warning after a gateway (Laya or conditions), not after a conditions gateway that reads only form fields filled in before it or declares `decision_scope="technical"` (housekeeping, never decisions about people); an info for an LLM agent audited on a sample (`oversight="audit"`) declared `risk="low"` |
| `wait` | error | structure | branches that run at once (a parallel or inclusive gateway, one output leading to several steps) where more than one waits (a person, a message, a timer over a minute) |
| `metrics` | warning | metrics | a Metrics node with nothing produced before it, or without a threshold; an info when missing the threshold changes nothing (no conditions gateway after it) |
| `error-path` | info | people | a step n8n retries whose last failure reaches no person: it stops the case (no `on_error`), or its error path ends without a User or Manual Task (an `error` output leading nowhere is the `output` rule's) |
| `implicit-split` | info | structure | one output leading to several steps (not a Parallel Gateway's): they all run, a split nobody drew |
| `implicit-join` | warning | structure | a step that is not a Merge, reached by branches that run at once: it runs once per arriving branch; the branches of an exclusive or event-based choice may meet anywhere |
| `endless-loop` | error | structure | a loop no flow leaves (no output out of it, none left open): it never ends |
| `loop` | warning | structure | a loop with nothing in it that chooses, waits or keeps time: it may spin without end |
| `deadlock` | error | structure | playing every combination of choices on the token engine (up to 300 cases): a case that gets stuck (tokens wait for what never comes), a Merge that never fires, a loop no case leaves; an info when there were more cases to play |
| `naming` | info | style | a task, or the process, named with a mushy verb (`handle`, `process`, `review`…) or as an event or a state (`Claim Received`, `Request is approved`); triggers and events may be past tense |

These are structural checks, not PM4Py's *soundness*, which gives false negatives on n8n workflows (a Merge is an inclusive join, parallel branches need not rejoin): tried on the four examples, it rejected two correct ones. Implicit gateways ([AW] §2.3, §3.3) are judged by dominators: the branches reaching a step that is not a Merge run at once when their nearest common dominator is a parallel split, an inclusive gateway or one output leading to several steps. Loops are judged structurally (a loop with no way out, a loop nothing in it decides); a loop through an n8n node the registry does not know (If, Switch, Loop Over Items) is not judged. The `deadlock` rule then plays the workflow on the token engine, which sees what the structure cannot: Merges that wait for each other, a loop a parallel gateway keeps feeding (see *Scenario tests*). Studio runs the check on every build and treats the "without a person" warning as an error too (what its activities print goes to stderr, so the JSON report stays readable; a build or research whose process dies, killed or with its container restarted, is ended by the pages after 3 minutes without its heartbeat); a test checks that ACME's examples pass it (with that warning where they close a case on their own).

### Scenario tests

`examples.json` tests the activities one by one; `scenarios.json`, next to it, tests the process: a case played on the token engine (`bpmn8n/engine/`, n8n's semantics on a virtual clock) without n8n, given what people answer, which messages come and what Laya decides. It is a list of scenarios:

```json
[{"name": "a theft whose police report never comes is reviewed by a person",
  "start": {"policy_number": "POL-3311", "event_type": "theft", "amount_claimed": 7000, "injured": false},
  "decisions": {"Wait for Police Report": "timeout"},
  "people": {"Review Missing Police Report": {"approved": false, "reason": "no report"}},
  "expect": {"visits": ["Review Missing Police Report"], "not": ["Issue Payout"],
             "ends": "Send Rejection Notification", "variables": {"approved": false}}}]
```

| Key | What it gives |
|---|---|
| `start` | the data that starts the case (the webhook's body) |
| `decisions` | per gateway, the branch it takes (`"yes"`), the branches of an inclusive one (`["a", "b"]`), or `"timeout"` for an event-based gateway or a user task with a deadline. A Laya gateway must have one; on a conditions gateway it is checked against what its conditions take |
| `people` | per user or manual task, the form answers (a field the form does not have fails) |
| `messages` | per receive task or event-based gateway, `{"name": message, "payload": {...}}`; the payload adds only the variables the case does not have, as delivery does |
| `tasks` | `"run"` (the default): the real `@task` functions run on the case's variables, their input bound by name and by `{{templates}}` as the n8n node binds it; `"stub"`: none runs |
| `outputs` | per node, what it returns instead of running: an LLM agent's answer (its text), a task that calls the outside world or reads data the check's database does not have |
| `workflow` | the workflow file, relative to the project folder (default `workflow.json`; ACME's are in `examples/workflows/`) |
| `expect` | `path` (every step in order), `visits` (steps it passes), `not` (steps it must not pass), `ends` (a step where a token ends, or `"error:<code>"` for an error end event), `variables` (a subset of the variables at the end), `max_steps` |

A list for a node gives one value per visit, for loops (`"people": {"Approve": [{...}, {...}]}`; per visit on an inclusive gateway: `[["a"], ["a", "b"]]`); running out of them fails the scenario, as does a missing decision, answer or message, naming the node. Conditions gateways are evaluated for real on the case's variables; Laya gateways, LLM agents and people never run. What the tasks write in the database is rolled back and their mail stays in memory, so a check leaves no trace and plays the same every time. A case that gets stuck, reaches the loop limit or ends with an error nobody expected fails too. Each scenario reports whether it passed, the first difference from what it expects, the path taken and the variables at the end. Out of scope: a user task with several items (one token is one item); a timer, a call activity and other n8n nodes pass the case on unchanged unless `outputs` says what they add; `{{vars.…}}` is not read.

`python manage.py check_project projects/<name>` runs the scenarios after the examples (a failing one fails the project; the JSON report has them under `"scenarios"`), and so do Studio's verify step and the MCP `studio_check`, where a failing scenario is a workflow error sent back for a fix (and blocks `studio_publish`). `python manage.py test_scenarios projects/<name> [--json]` runs only them.

**From a real case.** `python manage.py record_scenario <case id> projects/<name> [--name …] [--workflow …]` writes a scenario from the event log: the start data, the branches the gateways took, people's answers, the messages, what the tasks returned (as `outputs`: delete one to run that task for real) and, when the replay agrees, the exact path as the expectation. A bug seen in production becomes a regression test: record the case, change the expectation to what should have happened, fix the workflow until it passes. An LLM agent's answer and a timeout leave no event: the replay names what is missing.

**Deadlock search.** The `deadlock` rule plays the workflow on the engine over every combination of choices (every output of every gateway, every set of an inclusive gateway's branches, timeout or not), loops up to three rounds, up to 300 cases (beyond that it says so, and judges only what it saw). It reports, with the path that leads there, a case that can get stuck (tokens waiting at Merges that wait for each other), a Merge that never fires and a loop no case can leave (a parallel gateway sending a token back every round): what a structural check cannot see. The tracked projects play in a few milliseconds each.

### BPMN coverage

What BPMN 2.0's process diagrams have, and how bpmn8n has it:

| | Elements |
|---|---|
| **Nodes of bpmn8n** | Service, Script, Business Rule, Send, Receive, User and Manual Task; Call Activity; Exclusive, Inclusive, Parallel and Event-Based Gateway; intermediate Timer Event; Error End Event; an interrupting **timer boundary event** on a User or Manual Task (a deadline: the node gets a `timeout` output); multi-instance with a completion condition (all, any, at least N); a Global Variable step (a data store shared by every workflow) |
| **n8n itself, in the notation** | message start (Webhook), timer start (Schedule Trigger), none start (Execute Workflow Trigger); the join of parallel and inclusive branches (Merge, with execution order v1); multi-instance in parallel (n8n runs a node once per item: one User Task per item); loops (a flow back); text annotations (sticky notes); an error end's event sub-process (n8n's Error Trigger workflow); an **error boundary** on a task (n8n's per-node error output: `task(on_error="branch")`, see *Agentic patterns*) |
| **Can be added** (n8n allows them, bpmn8n does not have them yet) | non-interrupting timer and message **boundary events** on a User Task (a reminder or an escalation while the task stays open: a separate execution, since n8n waits on one thing at a time); explicit none and **terminate** end events (the latter stops the execution's other branches through n8n's API); **signal** events (one message that resumes every waiting case, on the existing subscriptions); conditional events (a condition checked on a timer); sequential multi-instance (Loop Over Items); **pools and lanes** drawn as bands (sticky notes, laid out by lane); **import and export of .bpmn files** (BPMN XML, to and from Camunda, bpmn.io, Signavio); compensation and transactions (sagas: each completed task records how to undo it) |
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
- **LLM agent** (`llm_agent`: n8n's *AI Agent* node with the default provider's chat model) only for open-ended work on text that no rule can do and that really saves a person time: summarising, drafting a reply, proposing a decision with its reasons. When it is not needed anywhere, the design leaves it out and explains why; its patterns (a chain, parallel readings, reflection, guardrails, audit) are under *Agentic patterns* below;
- **person** for every final decision.

The constraints, which the check enforces:

- **people take the final decisions** (GDPR art. 22): after every gateway and every LLM agent, each path to an end passes through a User or Manual Task. Studio sends workflows that break this back to the LLM;
- **data minimisation**: an LLM agent receives only the variables its prompt names, never special categories of data (health, religion…). The prompt is filled in by bpmn8n (the `compose_prompt` task), so the event log keeps what was sent to the provider and what it answered;
- **explainability and transparency**: rules before models; every agent is told that it prepares work for a person, must use only the data it receives, explain how it reached its answer and say when the data is not enough. Its answer reaches the person in the task's context, marked as written by a model.

An `llm_agent` becomes four nodes: `<name>: prompt` (the prompt filled in with the case's variables), `<name>` (the AI Agent) with `<name>: model` (the chat model) under it, and `<name>: answer` (the answer, saved in the step's `output` variable). The **bpmn8n LLM: <provider>** credential is created in n8n, or updated if the key has changed, when a workflow with an agent is built: google → *Google Gemini Chat Model*, anthropic → *Anthropic Chat Model*, the other presets → *OpenAI Chat Model* with the provider's URL.

Studio writes code that the server then runs: it is for staff only and the code must be reviewed before production. *Build again* rewrites the project's activities (it does not reuse them) and replaces the folder only if the new version passes. gunicorn's reload waits for the LLM calls in progress (`--graceful-timeout 360`).

### One look on both servers

Django (`:8000`) and n8n (`:5678`, its preview on `:5679`) are one product with one definition of each visual concept:

- **Tokens**: `bpmn8n/static/bpmn8n/tokens.css` is the only place that defines colours (light and dark), status colours, type sizes, spacing, radii and the canvas palette, all named `--b8-*`. `app.css` uses only those. n8n gets the same file as `/static/bpmn8n-tokens.css` (mounted read-only in `docker-compose.yml`), linked once by `bpmn.js`; every hook writes `var(--b8-…, <its old colour>)`, so a missing file only brings back the hooks' own colours. The pages follow taste-skill v2's *framed-sheet* style (coloured L-shaped frame, white sheet, colour only on data, IBM Plex Sans; Plex Mono only for IDs, keys and code), checked at 1440, 768 and 390 px with text at 4.5:1 or more in both themes.
- **Theme**: the sidebar's *System / Light / Dark* switch is kept in the `b8-theme` cookie and sets `data-theme` on the page; embedded canvases are drawn in it. n8n keeps its own theme setting (`bpmn.js` no longer overrides it): in dark mode its canvas is flat and each node a tint from its name, in light mode n8n's own colours.
- **Navigation**: the sidebar is the `NAV` list in `bpmn8n/web/views.py` (URL name, label, icon, staff only). An entry shows once its URL name resolves, so a new page appears by adding its route; *Editor* (staff) opens n8n.
- **Vocabulary**: `bpmn8n/web/vocab.py` names every status once, with its colour family; templates write `{{ status|status_pill }}`. Everything is in English.
- **The case page**, `/cases/<workflow id>:<execution id>/`: its status (running, waiting for a person or a message, completed, failed: inferred from its events and waits), every step with its input and output (bounded), what an LLM was sent or wrote, each decision (the question, the options, the branch taken, Laya's scores or the conditions), its tasks and messages, its variables, and the workflow read from n8n, drawn by the preview with the steps passed, the ones the case is at and the ones not reached (the page posts `{"command": "bpmn8nStates", "states": {"<node>": "waiting" | "active" | "done"}}`). Without n8n the page says so and shows the event log. Staff see every case; a person sees the cases that have a task in their lanes.
- **Deep links**: the case page opens its execution in n8n and Studio's project opens its workflow; in n8n, the node card links **Open the case** while an execution is open, and a User Task's node links its task page.

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

## Agentic patterns

When an LLM has earned its place, the builder offers the patterns of [AE] (Cimino, *Agentic Engineering*, 2026) as vetted templates: each is a step of `Workflow` (and of Studio's spec) that expands to the steps above, so the check judges it like any other workflow. None decides: like `llm_agent`, each needs a `user_task` after it where a person reviews the answer (the `human` rule), and each caps its LLM calls (J4: at most 5 links, branches or rounds).

```python
w.prompt_chain("Draft Reply", ["List the claims in {{complaint}}", "Draft a reply to these claims: {{previous}}"],
               output="reply")                      # "Draft Reply 1", "Draft Reply 2"; {{reply_1}}, then {{reply}}
w.parallel_llm("Read Claim", {"medical": "The medical facts in {{claim}}", "legal": "The legal issues in {{claim}}"},
               output="readings")                   # a parallel gateway, two agents, a Merge: {{readings.medical}}
w.reflection("Draft Notice", "Draft the notice for {{case_summary}}", critique="Plain words, every deadline stated",
             output="notice", max_rounds=2)         # producer, critic (PASS or REVISE), back until it passes
w.guardrailed_agent("Summarise Message", "Summarise {{message}}", output="summary", on_block="support",
                    output_checks={"pii": True, "length": 1500})
w.user_task("Approve Reply", "Approve the reply", "support", [("final", "Reply", "string")])
w.flow("Complaint Received", "Draft Reply", "Approve Reply")
```

- **Prompt chaining** (`prompt_chain`, [AE] 20–28): agents in sequence, `{{previous}}` in a prompt is the answer before it. For work in steps that build on each other.
- **Parallelisation** (`parallel_llm`, [AE] 34–35): independent agents at once, joined by a Merge that combines their answers into one item: `{{output}}` holds them by key, each is also `{{<output>_<key>}}`.
- **Reflection** (`reflection`, [AE] 36–39): a producer drafts, a critic judges the draft against the criteria and starts its answer with PASS or REVISE; a technical condition gateway sends the draft back with the critique until it passes or `max_rounds` drafts were made (`read_critique` counts `{{reflection_round}}` in the case, so the loop always ends). The person sees the last draft whether it passed or not (`{{passed}}`, `{{<output>_critique}}`).
- **Guardrails** (`guardrail`, `guardrailed_agent`, [AE] guardrails and safety, J3): the `check_guardrails` task checks a text, its `{{variables}}` filled: `pii` (emails, phone numbers, IBANs by mod 97, Italian fiscal codes, card numbers by Luhn: regular expressions, no model), `length`, `blocklist` (whole words), `schema` (a JSON object with the given keys; a code fence around it is fine). It returns `passed`, `findings` (check, what, where: the characters) and `redacted`, the text with its personal data replaced by placeholders such as `[EMAIL]`, whatever the checks; the event log keeps it like every task. `guardrail(name, text, checks, on_block=None)` adds a gateway: `passed` goes on, `blocked` goes to a user task in the lane `on_block`, or wherever it is flowed (`flow(name, step, output="blocked")`). `guardrailed_agent` checks the prompt, gives the LLM the **redacted** prompt, checks the answer (`output_checks`, by default the same checks), and sends a blocked text to a person instead of going on. With an external provider, a text that may hold personal data passes a guardrail before any agent reads it; it generalises the hospital project's pseudonymisation, which also replaces the patient id with a keyed pseudonym.

**Error paths** (C3, J7, [AE] 61–62). `task(..., retry=2, retry_wait_ms=2000)` sets n8n's *Retry On Fail* (Max Tries = retry + 1, at most 5; waits up to 5 s), for a system that may be briefly down. After the last try, `on_error=None` stops the case, `"continue"` goes on with an `error` field, and `"branch"` gives the task a second output, `error` (an error boundary event): `w.flow("Book Slot", "Book By Phone", output="error")` sends the case to a person who takes it over. The check knows that output: left unconnected it is the `output` rule's warning, and the data on it is what the task received, not what it would have produced. The `error-path` rule notes (info) a retried step whose last failure reaches no person.

**Oversight** (H10, [AE] 63–66). `llm_agent(..., oversight="review")`, the default, is human-in-the-loop: a person reviews every answer. `oversight="audit", audit_rate=0.1, risk="low"` is human-on-the-loop: the `draw_audit_sample` task decides from the case id alone (the same case always gets the same answer; about 10% of the cases) whether a person audits it, the event log records which, and the step's `audit` output goes to that person while its first output, `continue`, goes on. The `human` rule accepts an audited agent only when the step is declared `risk="low"`, and says so in an info; otherwise it stays an error, because a decision with legal or similar effects on a person needs a person every time (GDPR art. 22). Declare it only for drafts that commit no one (an internal summary, a suggested tag).

**Cost.** Every LLM agent is retried up to 3 times, 5 s apart (a provider's 429 or 503 is often gone in seconds), so a failing answer can cost three calls. A prompt chain costs one call per prompt, a parallel step one per branch, a reflection two per round (up to `2 × max_rounds`), each times three in the worst case: `max_rounds` 2 or 3 is usually enough, and every bound is checked when the workflow is built.

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

### Insights

**http://localhost:8000/insights/** (signed-in users) is the workbench on the event log, after Disco: pick a process and a window, narrow the cases with filters, and read what happened. `bpmn8n/analytics/` computes it with numpy, without PM4Py; the same figures are at `/api/analytics/*`.

- **Filters** (shown as chips, each with the cases it kept; applied left to right; the whole view is the URL, so a link shares it):

  | Filter | Keeps the cases that |
  |---|---|
  | timeframe | started, ended, ran (intersecting) or ran only (contained) in a window |
  | variants | follow the top N sequences of activities, or the most frequent ones covering X % |
  | performance | took between two durations, or whose time from activity A's end to B's start is in bounds |
  | endpoints | start and/or end with given activities |
  | attribute | have (or have not) an event with an activity, resource, status or process |
  | follower | have A followed by B, directly or eventually, optionally within a time (or not) |

  In the API a stack is JSON in the query string, e.g. `filters=[{"type":"attribute","key":"resource","values":["anna"]},{"type":"variants","top":3}]`; a stack that cannot be read is a 422 saying why.
- **Statistics**: cases (open and closed), events, durations (mean, median, p90, max of the closed cases), events and cases in progress over time, the case-duration histogram; per activity its executions, cases, durations, the mean wait before it (its start minus the end of the step before, in the same case) and errors; per resource its busy time and utilisation; variants with coverage; **handoffs**, where a case changes hands and how long it waits there; per gateway the share of each branch and Laya's confidence.
- **Compare**: add filters to *cohort B* and the page sets the same figures, activities and branch shares side by side with the filtered cases (A), with B − A: specialists against residents, this month against last.
- **Bottlenecks: queueing on what really happened.** Each lane is a station: its people (the active members of the lane's Django group, else the people seen) serve its tasks first come, first served. From the log: arrivals per hour λ and their variability ca², mean work E[S] and its cs², utilisation ρ = λE[S]/c, the observed wait against the G/G/c prediction (`bpmn8n.simulation.queueing`), the wait with one person less and one more, the people a **target wait** needs, and Little's law (the time-average work in progress from the events against λW: a large gap means work still open at the window's edges, or a log that is not steady). Activities are ranked by utilisation and share of the cycle time.
  - What the log cannot separate, it says: a User Task's event spans its creation to its completion. With a claim time on the task, waiting and work separate exactly; without one, work is estimated from each person's previous completion (exact when people take the next task as soon as they finish one, otherwise an upper bound on work); a task completed through the API names no person, so only its response time is known and utilisation is an upper bound. System steps are delays (software scales with its workers). Rates are per calendar hour: there are no working hours yet.
- **Heatmap on the canvas**: on a workflow with bpmn8n nodes the editor shows a *Heat* switch (Off · Frequency · Duration). It reads `/api/analytics/overlay?workflow_id=<id>` with the browser's n8n session (bpmn8n checks that session may open the workflow, as for evaluations) and rings each node from cold to hot with a badge, how many times it ran or its mean duration, and a legend; nodes no case reached are dimmed.
- **A demo without n8n**: `python manage.py seed_events [--process "Claim Handling"] [--cases 300] [--seed 0] [--workflow <id>] [--replace]` writes a synthetic log: Poisson arrivals, a Laya gateway whose unsure decisions go to a person, a loop, errors, two lanes of people with real queues (the lanes become groups of users who cannot sign in), and the last cases still open. With `--workflow` set to a real workflow id whose node names match, the heatmap shows on its canvas.
- Limits: the cases in scope are read in one pass (about 1 GB per 5 million events, as mining): narrow by process and window beyond that. The window counts the cases that *started* in it.

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
python manage.py test bpmn8n --parallel 1             # 384 tests: API, states, check, Studio (real build), mining, every project's examples and scenarios
python manage.py check_project projects/<name>        # one project: imports, examples, scenarios, workflow
python manage.py test_scenarios projects/<name>       # only its scenarios (scenarios.json) on the token engine
python manage.py record_scenario <case id> projects/<name>  # a real case becomes a scenario (regression test)
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

## Production

`./start.sh` is the development stack: `web` installs its packages at boot and reloads on every edit. Production runs
the **web image** instead, through an override of the same compose file:

```bash
docker build -t bpmn8n-web .                       # or let compose build it (--build)
SECRET_KEY=… BPMN8N_API_TOKEN=… ALLOWED_HOSTS=bpmn8n.example.org \
  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

- **The image** (`Dockerfile`): wheels built in a first stage, then a slim runtime with no compiler, the code baked in,
  static files collected at build time, gunicorn without `--reload`, a non-root user (only `projects/` is writable: Metrics
  samples and Studio builds) and a `HEALTHCHECK` on `/api/health`. It runs `migrate` before gunicorn; with several replicas,
  run the migrations once instead. `requirements.local.txt`, when present, is installed too. CI builds it on every push.
- **`docker-compose.prod.yml`**: `web` from the image (`BPMN8N_WEB_IMAGE`, default `bpmn8n-web:latest`), no source mount,
  `DEBUG=0`, JSON logs, and `restart: unless-stopped` on every service. Compose refuses to start without `SECRET_KEY`,
  `BPMN8N_API_TOKEN` and `ALLOWED_HOSTS`; `web` is added to the hosts because n8n's nodes call `http://web:8000`.
  A TLS proxy (Caddy, nginx, Traefik) publishes `127.0.0.1:8000`. Evaluation samples stay in the container: mount a
  volume on a project's `run/` folder to keep them across deployments.
- **Settings with `DEBUG=0`** (`bpmn8n/settings.py`, `production()`): the server refuses to start with the development
  secret key, without an API token, or with `ALLOWED_HOSTS` empty or `*`. Secure cookies; HSTS for a year with subdomains
  (`HSTS_SECONDS`, `HSTS_INCLUDE_SUBDOMAINS=0`, `HSTS_PRELOAD=1`); `CSRF_TRUSTED_ORIGINS` (e.g. `https://bpmn8n.example.org`);
  `SECURE_PROXY_SSL_HEADER=X-Forwarded-Proto` when the proxy sets it to `https`; `SECURE_SSL_REDIRECT=1` to redirect at
  Django rather than at the proxy (`/api/` is exempt: n8n calls it over HTTP inside the network). whitenoise serves the
  hashed, compressed static files from gunicorn. `DEBUG` stays on when unset, so the tests and `runserver` need no
  variables; the image sets `DEBUG=0`. `manage.py check --deploy` is clean but for two opt-ins: W008 (the redirect, left
  to the proxy) and W021 (HSTS preload).
- **Health**: `GET /api/health` needs no token and answers only statuses and latencies (no URLs, no errors): `database`,
  `redis` (with `REDIS_URL`), `n8n` (`/healthz`), `laya` and `extractor` (the models server's `/health`), `search`
  (SearXNG's `/healthz`), each `up`, `down` or `off`, within 2 s, in parallel. Overall `ok`, `degraded` or `down`; HTTP 503
  only when the database is down, since restarting `web` cannot fix the others. `python manage.py doctor` prints the same
  as a table with the errors, plus hints from the configuration (no `N8N_API_KEY`, no n8n credential id, no LLM provider,
  an open API with `DEBUG` on, no search), and exits with 1 when the database, Redis or n8n is down.
- **Prometheus**: `GET /api/metrics/prometheus` (text format, the API token like the rest of the API) exposes, from the
  database so every worker counts: `bpmn8n_endpoint_calls_total`, `bpmn8n_endpoint_errors_total` and
  `bpmn8n_endpoint_duration_seconds` (sum and count) per endpoint, `bpmn8n_cases_started_total` per process,
  `bpmn8n_user_tasks_waiting` per lane and status, `bpmn8n_message_subscriptions_open` per message,
  `bpmn8n_studio_projects` per status. Prometheus sends the token as a bearer credential (the scrape job below).
- **JSON logs**: `LOG_FORMAT=json` (the prod override's default) writes the `bpmn8n` logger as one JSON object per line
  (time, level, logger, message, exception, and any `extra=` fields), with the standard library only.
- **The n8n version is pinned** (`docker.n8n.io/n8nio/n8n:2.42.3` for `n8n`, `n8n-worker` and `n8n-preview`): the editor
  hooks match n8n's class names and read its stores. To upgrade, change both tags in `docker-compose.yml`, restart, then
  run `node --test n8n-editor/test/*.test.mjs` and the editor's e2e tests (`pytest e2e/test_bpmn_elements.py`) and look at
  a workflow, an execution and the Evaluations tab; keep the old tag if anything falls back to n8n's own look.

The Prometheus scrape job:

```yaml
scrape_configs:
  - job_name: bpmn8n
    metrics_path: /api/metrics/prometheus
    authorization:
      credentials_file: /etc/prometheus/bpmn8n_token   # the value of BPMN8N_API_TOKEN
    static_configs:
      - targets: ["web:8000"]
```

### Workflows in git: diff, push, pull

The open alternative to n8n's paid source control and environments: **the project folder in git is the source of
truth**, and each n8n instance (development, staging, production, each with its own `N8N_URL` and `N8N_API_KEY`) is a
deployment target.

```bash
python manage.py workflows diff [projects/<name> ...] [--json]   # drift between git and n8n; exits with 1 on drift
python manage.py workflows push [projects/<name> ...] [--json]   # create or update in n8n; never activates
python manage.py workflows pull [projects/<name> ...] [--json]   # n8n's version back into workflow.json
```

Without folders it takes every `projects/<name>/` with a `workflow.json`. The workflow in n8n is the Studio project's
(by its stored id) or, otherwise, the one with the same name; two with the same name is an error.

- **diff** compares node by node, ignoring node ids, positions, `webhookId`, versions, dates and credential ids
  (credentials compare by type and name), and lists nodes added and removed, each changed field with its value in git and
  in n8n, connections added and removed, and changed settings. `workflows diff` in CI fails the job when someone edited a
  workflow in n8n without pulling it (exit 1; exit 2 when n8n cannot be reached).
- **push** checks everything before the first write: every credential a node uses must exist in that n8n (the same id,
  or the same type and name, so one `workflow.json` deploys to several instances); every Call Activity's sub-process must
  exist there or be pushed together. Sub-processes go first and the Call Activity is pointed at their id in that instance
  (found by id, or by the name the editor's *From list* mode stores). An active workflow's change is saved as a draft to
  publish in the editor, as n8n 2 does.
- **pull** writes n8n's name, nodes, connections and settings into `workflow.json` in the file's own key order, so
  `git diff` shows only what the editor changed (positions included); an unchanged workflow leaves the file untouched.
  Settings n8n adds on its own show up as drift until the first pull.

## Licences

- **PM4Py is AGPL-3.0.** For internal use nothing changes. If bpmn8n is offered **to third parties over a network** (SaaS), the AGPL requires publishing the code of the whole service, or buying PM4Py's commercial licence. Mining is isolated in `bpmn8n/mining/` (the workflow check does not use it), so it stays replaceable.
- **n8n** is under the *Sustainable Use License* (fair-code): internal use is free; the Enterprise features (`.ee.` code) require an n8n licence. bpmn8n does not unlock them: it offers its own open alternatives.

| n8n paid feature | In bpmn8n (open) |
|---|---|
| Variables (`$vars`) | **global variables** on Redis (or the database): `{{vars.<key>}}`, the Global Variable node, `/api/variables`, `/variables/` (see *Global variables*); per-case process variables: `{{book}}` |
| Workflow history beyond 24 hours, workflow diffs | **versions + diff + restore**: every version bpmn8n sees is kept (`WorkflowVersion`), each case records the version it runs (`Case.process_version`), `/workflows/` and `/workflows/<id>/` show what changed and restore an old version, `/api/history/<workflow>` |
| Log streaming | **Redis stream + webhooks + SSE**: every finished step goes to the Redis stream `bpmn8n:events` and to `EVENT_WEBHOOKS`; `GET /api/events/stream` serves it live |
| Advanced execution filters, custom execution data | **case search**: `GET /api/cases?process=&version=&since=&until=&status=&var.<path>=` and the page `/cases/` |
| Insights | **bpmn8n Insights** (`/insights/`), the event log in Postgres, `/api/metrics`, process mining |
| Source control and environments | `manage.py workflows diff/push/pull`: the project folder in git is the source, n8n the target |

**Workflow history.** A version is n8n's `versionId`; with n8n 2's publishing, cases run the published version (`activeVersionId`), and that is the one a case records. When a case starts, bpmn8n asks n8n for the workflow's current version at most once every 30 seconds per workflow (never without `N8N_API_KEY`) and keeps it if it has not seen it. n8n's public API shows only the current version, so run `python manage.py snapshot_workflows` (the workflows bpmn8n has seen; `--all`: every workflow in n8n) from cron to keep every version. A version's page lists, against the one before, the steps added, removed and renamed (matched by node id), every setting changed with its value before and after, and the flows added and removed (a move on the canvas is not a change). *Restore this version* (staff) saves it back to n8n as a new version and never activates it: an active workflow keeps running its published version until someone publishes the restored one.

**Log streaming.** What is sent is what happened (process, case, activity, endpoint, status, times, resource, error), never the step's input and output, which stay in the database. A step is announced once its transaction commits; a webhook gets a JSON POST from a background thread with a 3-second timeout, and a sink that fails is logged, never the step's problem. The Redis stream keeps about `EVENT_STREAM_MAXLEN` events (100 000). `GET /api/events/stream` sends one server-sent event per step with an `id` to send back as `Last-Event-ID`: it reads the Redis stream, or without Redis polls the database (two seconds behind, so late writes are not missed); each response lasts 55 seconds and the client reconnects where it stopped.

**Case search.** `status` is *waiting* (a person or a message is awaited), *error* (its last step failed), *active* (a step ran in the last minute or runs now) or *closed*. `var.<path>` takes a path in dots and `__gt`, `__gte`, `__lt`, `__lte` or `__contains` (text, ignoring case): `var.amount__gt=1000&var.customer.email__contains=@acme.com`. A value is JSON when it parses (`1000`, `true`, `"00123"` quoted to stay text), else text. Results come newest first, `limit` (up to 200) and `offset`, with `matched`: the values of the variables the query names. `/cases/` (staff) is the same search as a form; each case links to its page.

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
confirmed active calls from engine stack entries and inputs waiting at a join. **n8n view / debug**
keeps the original execution inspector available.

Local preview iframes use `credentialless` isolation and `localhost` to prevent the anonymous preview
instance from clearing the main editor's cookies (cookies are shared across ports). The supplied
Chrome/Chromium setup is verified; other browsers need credentialless support or a separate HTTPS
preview origin. The preview CSP explicitly allows the two local editor origins.

Checks: `python manage.py test bpmn8n.tests`, `npm --prefix n8n-nodes-bpmn8n test`, and
`node --test n8n-editor/test/*.test.mjs`. Browser checks exercise live SSE updates, retained focus,
selection/open details/SVG identity, both local session hostnames, the real execution canvas, and a
controlled fork fixture with two active branches followed by one waiting join input.
