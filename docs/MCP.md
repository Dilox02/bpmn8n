# bpmn8n as an MCP server

bpmn8n speaks the [Model Context Protocol](https://modelcontextprotocol.io) over stdio. An MCP client such as Claude Code or Claude Desktop can then:

- **be Studio's LLM.** It interviews you in its own chat, writes the design and the code of the activities, and bpmn8n checks, tests and publishes them as in a web build. No LLM provider and no API key are needed.
- **read and change the running state** through bpmn8n's REST API: open user tasks, cases and their variables, messages, metrics, process mining and workflow checks.

The server is a Django management command, `manage.py mcp`. It has no extra dependencies.

## Install

### 1. Start bpmn8n

```bash
./start.sh            # Django, n8n, Postgres and the models; safe to re-run
./start.sh status     # web must be running
```

### 2. Connect a client

**Claude Code, in this folder.** The repository ships a [`.mcp.json`](../.mcp.json), so you don't need to configure anything:

```json
{
  "mcpServers": {
    "bpmn8n": {
      "command": "docker",
      "args": ["compose", "exec", "-T", "web", "python", "manage.py", "mcp"]
    }
  }
}
```

Start `claude` from the repository root and approve the project server `bpmn8n` when asked. `/mcp` shows whether it is connected.

**Claude Code, from any folder.** Register it once for your user, with the path to the compose file:

```bash
claude mcp add --scope user bpmn8n -- \
  docker compose -f /path/to/bpmn8n/docker-compose.yml exec -T web python manage.py mcp
```

**Claude Desktop.** Add the same command to `claude_desktop_config.json` (Settings → Developer → Edit Config), then restart the app:

```json
{
  "mcpServers": {
    "bpmn8n": {
      "command": "docker",
      "args": ["compose", "-f", "/path/to/bpmn8n/docker-compose.yml", "exec", "-T", "web", "python", "manage.py", "mcp"]
    }
  }
}
```

**Without Docker** (Django from `.venv`, see the README's *Python environment*). Use the virtual environment's Python as the command and run it from the repository root:

```json
{ "command": "/path/to/bpmn8n/.venv/bin/python", "args": ["/path/to/bpmn8n/manage.py", "mcp"] }
```

### 3. Check it by hand (optional)

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
              '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | docker compose exec -T web python manage.py mcp
```

Two JSON lines come back: the server's identity, then the list of tools.

## Tools

| Tool | Arguments | What it does |
|---|---|---|
| `studio_projects` | | Lists the Studio projects with their status: interview, research, building, ready or failed. |
| `studio_project` | `slug` | Returns a project's whole state: interview, research, brief, spec, the cards of its last build and its n8n workflow. |
| `studio_create` | `name`, `description` | Creates a project. The description is the interview's first answer. |
| `studio_prompt` | `slug`, `step` (`interview`, `design` or `write`), `activity` | Returns the instructions Studio gives its LLM, with the live context: existing tasks, lanes, the builder's step types, code conventions and the project so far. Follow them. |
| `studio_save` | `slug`, `messages`, `brief`, `spec` | Saves the interview transcript, the brief or the design. A spec the build cannot use is refused with the list of problems. |
| `studio_write` | `slug`, `activity`, `code` | Writes one activity's Python module into the project's draft. |
| `studio_check` | `slug` | Imports the draft, runs each activity's examples and checks the workflow. Returns `{imports, examples, problems}`. |
| `studio_publish` | `slug` | Only once the check is clean: writes `projects/<slug>/`, imports or updates the workflow in n8n and reloads the server's tasks. |
| `api` | `method`, `path`, `body` | Calls any route of the REST API (`/api/openapi.json` lists them) and returns the status and the JSON. |

## Build a process

Ask the client something like *"design an expense approval process in bpmn8n Studio"*. It follows these steps:

1. `studio_create` creates the project from the name and your description.
2. `studio_prompt step=interview` gives the interview instructions. The client asks you one question at a time in its chat, then `studio_save` stores the transcript (`messages`) and the `brief`.
3. `studio_prompt step=design` gives the shape of the spec: the assessment (who decides and why, the GDPR notes), the new activities with 2 or 3 examples each, the workflow's steps and flows. `studio_save spec=…` stores it, or refuses it with what to fix.
4. `studio_prompt step=write activity=<name>` gives the instructions for each new activity, and `studio_write` writes its module.
5. `studio_check` runs until it is clean: no import errors, every example passing, no errors and no `human` problems. Fix modules with `studio_write` and the design with `studio_save`.
6. `studio_publish` publishes the project. **Studio never activates a workflow**: publish it in the n8n editor. On a rebuild, the workflow keeps its id and webhook. If it is active, n8n saves the change as a draft for you to publish.

The project lands in `projects/<slug>/`, with `tasks/`, `examples.json`, `process.py`, `workflow.json` and `INTERVIEW.md`, exactly as a web build writes it. The Studio page at `http://localhost:8000/studio/<slug>/` shows it, with its canvas.

## Look at and act on running cases

Ask, for example, *"which user tasks are open?"* or *"show the variables of case X"*. The client uses `api`:

| Ask | Call |
|---|---|
| Open tasks, by lane | `GET /api/usertasks?status=open&lane=dpo` |
| Complete a task with its form | `POST /api/usertasks/{id}/complete` with `{"approved": true, "reason": "…"}` |
| A case's process variables | `GET /api/cases/{case_id}` |
| Deliver a message to a waiting case | `POST /api/messages/{name}` with `{"correlation_key": "…", "payload": {…}}` |
| Metrics and process mining | `GET /api/metrics`, `GET /api/processmining?process=…` |
| Check a workflow before importing it | `POST /api/workflows/check` with the n8n workflow JSON |

A message adds variables to its case, but it never changes the ones the case already has. The keys it left out are listed in the reply's `reason`.

## Good to know

- **It has a staff member's power.** Studio writes code that the server imports and runs. Connect only clients you trust, on your own machine: the server speaks stdio and opens no port.
- **One request at a time.** `studio_check` can take up to 5 minutes, and other calls wait until it ends.
- **The web Studio comes first.** While a research or a build runs from the web page, the tools refuse to change that project.
- **The API token is used for you** when `BPMN8N_API_TOKEN` is set.
- **The check enforces bpmn8n's rules.** A person must take every final decision (GDPR art. 22). Every `{{variable}}` must come from an earlier step. Branches that run at once may not both wait: n8n parks an execution on one wait at a time, so a person's answer would sit until the other branch's timer ends.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `service "web" is not running` | Run `./start.sh`. |
| The tools don't appear in Claude Code | Start `claude` from the repository root, approve the server, then check `/mcp` or `claude mcp list`. |
| `import_workflow: skipped … 403` on a rebuild | Re-run `./start.sh`: it replaces an n8n API key that cannot update workflows. |
| `Saved as a draft of the active workflow` | That's expected: open the workflow in n8n and publish it. |
| `<slug> is research/building in the web Studio` | Wait for that job to end, or follow it on the Studio page. |
