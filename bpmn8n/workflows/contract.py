"""The process contract: what the outside world sees of a workflow (BPMN's *public* process, next to the
executable *private* one), derived from the workflow and the task registry, never written by hand.

- the start: the trigger (a webhook's method and path, a form, a schedule, a Call Activity) and the start data,
  the fields the steps read that no step before them produces, typed from the @task input schemas where a task
  binds them (by name, or a field that is one `{{variable}}`), merged with the names a project declares
  (`SPEC["start_data"]`); a field no task types is marked `"x-bpmn8n-type": "unknown"`;
- the messages it waits for (receive tasks, event-based gateways): name, correlation key template, the payload
  fields the steps after it read that nothing else provides (a message only adds variables: models/waiting.py);
- the messages it sends: its send tasks (the `message` a node sets names what it sends) and HTTP requests to
  bpmn8n's `/api/messages/<name>`;
- the lanes, the deadlines (a human task's due time, an event-based gateway's timeout), the results (what the
  last steps produce) and the sub-processes it calls.

`Contract(workflow).start_schema()` is the JSON Schema of the start body, `.openapi()` an OpenAPI fragment for the
start webhook and the message deliveries, `.summary()` the same in words. Needs the task registry (api.tasks)."""
import json
import re
from functools import cached_property
from importlib import import_module
from pathlib import Path
from urllib.parse import quote

from ..api.tasks import TASKS
from ..core.variables import TEMPLATE
from .check import WorkflowCheck, declared_start_data

SCHEMA = "https://json-schema.org/draft/2020-12/schema"
HTTP_REQUEST = "n8n-nodes-base.httpRequest"
EXECUTE_WORKFLOW = "n8n-nodes-base.executeWorkflow"  # n8n's own way to call a sub-workflow
MESSAGES_API = re.compile(r"/api/messages/([^/?#{}\s\"']+)")  # an HTTP request that delivers a bpmn8n message
STARTS = {"start": "webhook", "formStart": "form", "timerStart": "schedule", "subprocessStart": "call"}
UNKNOWN = {"x-bpmn8n-type": "unknown"}


def field_schema(model, name):
    """The JSON Schema of one field of a pydantic model, its nested models inlined (no $ref to resolve)."""
    full = model.model_json_schema()
    defs = full.get("$defs", {})

    def inline(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(defs.get(value["$ref"].rsplit("/", 1)[-1], {}))
            return {k: inline(v) for k, v in value.items() if k != "title"}
        return [inline(v) for v in value] if isinstance(value, list) else value

    return inline(full["properties"][name])


def identifier(text):
    """"Police Report Uploaded" -> "Police_Report_Uploaded": an OpenAPI operationId."""
    return re.sub(r"\W+", "_", text).strip("_")


def target(params):
    """The workflow id a Call Activity (or n8n's Execute Workflow) names: a resource locator or a plain id."""
    value = params.get("workflowId", "")
    return str(value.get("value", "")) if isinstance(value, dict) else str(value)


class Contract:
    """The public process of one workflow. `start_data`: the field names a project declares, merged in."""

    def __init__(self, workflow, start_data=None, workflow_id=""):
        self.workflow_id = workflow_id or str(workflow.get("id") or "")
        self.check = WorkflowCheck(workflow, fetch=lambda _: None)  # start data unknown: derived below
        self.graph = self.check.graph
        self.name = self.graph.name
        self.declared = list(start_data or [])

    def params(self, name):
        return self.graph.nodes[name].get("parameters") or {}

    # --- what each step reads and what comes before it ------------------------------------

    def task_of(self, name):
        return TASKS.get(str(self.params(name).get("task", "")).rsplit("/", 1)[-1]) if self.graph.element(name).task else None

    def reads(self, name):
        """{variable: (its JSON Schema or None, required)} that the step reads: every {{template}} in its
        parameters, and a task's inputs bound by name (empty) or to one {{variable}}, typed from the task."""
        params = self.params(name)
        found = dict.fromkeys(WorkflowCheck.references(params), (None, False))
        if task := self.task_of(name):
            given = (params.get("input") or {}).get("value") or {}
            for spec in task.inputs:
                value = given.get(spec.name)
                lone = TEMPLATE.fullmatch(value.strip()) if isinstance(value, str) else None
                if value in ("", None) and params.get("inputFrom") != "text":
                    found[spec.name] = (field_schema(task.schema, spec.name), spec.required)
                elif lone and "." not in lone[1]:
                    found[lone[1]] = (field_schema(task.schema, spec.name), spec.required)
        return found

    @cached_property
    def produced(self):
        """step -> what it surely produces (its task's output, a form, a Set); None when unknown."""
        return {name: self.check.produced_by(name) for name in self.graph.flow_nodes}

    def produced_before(self, name):
        """(what any step before it produces, the steps before it whose output is unknown). ponytail: on some
        path, not on every path as the check computes: the contract describes, the check judges. A loop's way back
        is left out: what a later round produces is not there on the first."""
        before, queue = set(), [name]
        while queue:
            current = queue.pop()
            for n in self.graph.into[current] - before - {name}:
                if (n, current) not in self.graph.back_edges:
                    before.add(n)
                    queue.append(n)
        return (set().union(*(self.produced[n] or set() for n in before)),
                sorted(n for n in before if self.produced[n] is None and not self.graph.element(n).trigger))

    def ahead(self, starts):
        """The steps these lead to, along forward flows (a loop's way back left out)."""
        seen, queue = set(starts), list(starts)
        while queue:
            for n in self.graph.forward(queue.pop()):
                if n not in seen:
                    seen.add(n)
                    queue.append(n)
        return seen

    @cached_property
    def waits(self):
        """[(wait step, message, the steps its output leads to)] for every message a receive task or an
        event-based gateway waits for."""
        found = []
        for name in self.graph.flow_nodes:
            kind, params = self.graph.element(name).kind, self.params(name)
            if kind == "receiveTask":
                found.append((name, str(params.get("message", "")), self.graph.outs[name][0] if self.graph.outs[name] else []))
            elif kind == "eventBasedGateway":
                for port, label in enumerate(self.graph.outputs(name)):
                    if label != "timeout":
                        found.append((name, label, self.graph.outs[name][port] if port < len(self.graph.outs[name]) else []))
        return found

    @cached_property
    def free(self):
        """The steps a case reaches without a message: forward from the starts, past a wait only by its timeout."""
        message_outs = {(wait, n) for wait, _, targets in self.waits for n in targets}
        seen, queue = set(self.graph.starts), list(self.graph.starts)
        while queue:
            current = queue.pop()
            for n in self.graph.forward(current):
                if n not in seen and (current, n) not in message_outs:
                    seen.add(n)
                    queue.append(n)
        return seen

    # --- the contract -----------------------------------------------------------------------

    @cached_property
    def starts(self):
        """How a case starts: [{activity, kind (webhook | form | schedule | call | trigger), method, path}]."""
        found = []
        for name in self.graph.triggers:
            kind, params = STARTS.get(self.graph.element(name).kind, "trigger"), self.params(name)
            found.append({"activity": name, "kind": kind, "path": str(params.get("path", "")) if kind in ("webhook", "form") else "",
                          "method": str(params.get("httpMethod", "GET")).upper() if kind == "webhook" else
                          "POST" if kind == "form" else ""})
        return found

    @cached_property
    def fields(self):
        """The start data: name -> {schema (None: unknown), required, declared, read_by, unless}. A step reached
        without a message needs what no step before it produces; when every step that reads it comes after a step
        whose output is unknown (a task without an output Schema), `unless` names those steps: the field may be
        theirs, so it is not required. The names the project declares are added."""
        found, unknowns = {}, {}
        for name in self.graph.order:
            if name not in self.free:
                continue
            before, unknown = self.produced_before(name)
            for var, (schema, required) in self.reads(name).items():
                if var in before:
                    continue
                field = found.setdefault(var, {"schema": None, "required": False, "declared": False, "read_by": []})
                field["schema"] = field["schema"] or schema
                field["required"] |= required and not unknown
                field["read_by"].append(name)
                unknowns.setdefault(var, []).append(set(unknown))
        for var in self.declared:
            found.setdefault(var, {"schema": None, "required": False, "declared": False, "read_by": []})["declared"] = True
        return {var: {**field, "unless": sorted(set.intersection(*unknowns.get(var, [set()])))}
                for var, field in sorted(found.items())}

    @cached_property
    def receives(self):
        """[{message, correlation_key, activity, kind, timeout, reads}]: `reads` are the payload fields the steps only
        that message leads to read and nothing else provides (not the start data, no step's result)."""
        produced = set().union(*(p or set() for p in self.produced.values()))
        found = []
        for wait, message, targets in self.waits:
            params, kind = self.params(wait), self.graph.element(wait).kind
            reads = {var for n in self.ahead(targets) - self.free for var in self.reads(n)}
            found.append({"message": message, "correlation_key": str(params.get("correlationKey", "")), "activity": wait,
                          "kind": kind, "timeout": self.timeout(params) if kind == "eventBasedGateway" else "",
                          "reads": sorted(reads - set(self.fields) - produced)})
        return found

    @staticmethod
    def timeout(params):
        if params.get("mode") == "until":
            return f"until {params.get('until', '')}"
        return f"{params.get('amount', '')} {params.get('unit', '')}".strip()

    @cached_property
    def sends(self):
        """[{activity, task, message}]: every send task (`message`: the one it names, if any) and every HTTP request
        to bpmn8n's /api/messages/<name>."""
        found = []
        for name in self.graph.flow_nodes:
            node, params = self.graph.nodes[name], self.params(name)
            if self.graph.element(name).kind == "sendTask":
                named = ((params.get("input") or {}).get("value") or {}).get("message", "")
                literal = isinstance(named, str) and named.strip() and not TEMPLATE.search(named)
                found.append({"activity": name, "task": str(params.get("task", "")).rsplit("/", 1)[-1],
                              "message": named.strip() if literal else ""})
            elif node["type"] == HTTP_REQUEST and (hit := MESSAGES_API.search(str(params.get("url", "")))):
                found.append({"activity": name, "task": "", "message": hit[1]})
        return found

    @cached_property
    def lanes(self):
        """lane -> the human tasks in it ("" for a task any lane may work)."""
        found = {}
        for name in self.graph.of(human=True):
            found.setdefault(str(self.params(name).get("lane", "")), []).append(name)
        return dict(sorted(found.items()))

    @cached_property
    def deadlines(self):
        """[{activity, due, interrupting}]: a human task's due time (a deadline interrupts its wait) and an
        event-based gateway's timeout."""
        found = []
        for name in self.graph.flow_nodes:
            element, params = self.graph.element(name), self.params(name)
            if element.human and params.get("due", "none") != "none":
                due = f"{params.get('dueAmount', '')} {params.get('dueUnit', '')}" if params["due"] == "for" else \
                    f"until {params.get('dueUntil', '')}"
                found.append({"activity": name, "due": due, "interrupting": bool(params.get("deadline"))})
            elif element.kind == "eventBasedGateway":
                found.append({"activity": name, "due": self.timeout(params), "interrupting": True})
        return found

    @cached_property
    def results(self):
        """[{activity, fields (None: unknown), error}] for every last step: what a case ends with (what a Call
        Activity gets back from a sub-process), or the error it ends with."""
        return [{"activity": name, "fields": None if self.produced[name] is None else sorted(self.produced[name]),
                 "error": str(self.params(name).get("code", "")) if self.graph.element(name).kind == "errorEndEvent" else ""}
                for name in self.graph.ends()]

    @cached_property
    def calls(self):
        """[{activity, workflow_id, name}]: the sub-processes it calls."""
        return [{"activity": name, "workflow_id": target(self.params(name)),
                 "name": str((self.params(name).get("workflowId") or {}).get("cachedResultName", ""))
                 if isinstance(self.params(name).get("workflowId"), dict) else ""}
                for name in self.graph.flow_nodes
                if self.graph.element(name).kind == "callActivity" or self.graph.nodes[name]["type"] == EXECUTE_WORKFLOW]

    def as_dict(self):
        return {"workflow_id": self.workflow_id, "name": self.name, "starts": self.starts,
                "start_data": {k: {**v, "schema": v["schema"] or UNKNOWN} for k, v in self.fields.items()},
                "receives": self.receives, "sends": self.sends, "lanes": self.lanes, "deadlines": self.deadlines,
                "results": self.results, "calls": self.calls}

    # --- what it emits ------------------------------------------------------------------------

    def start_schema(self):
        """The JSON Schema of the start body (a webhook's JSON)."""
        properties = {}
        for var, field in self.fields.items():
            read = f"read by {', '.join(field['read_by'])}" if field["read_by"] else "declared by the project"
            if field["unless"]:
                read += f", unless {', '.join(field['unless'])} produces it (its task declares no output)"
            schema = field["schema"] or UNKNOWN
            properties[var] = {**schema, "description": ". ".join(filter(None, (schema.get("description"), read)))}
        return {"$schema": SCHEMA, "title": f"{self.name}: start data", "type": "object", "properties": properties,
                "required": [var for var, field in self.fields.items() if field["required"]]}

    def openapi(self, n8n_url="", bpmn8n_url=""):
        """An OpenAPI 3.1 fragment: the start webhook (on n8n) and each message delivery (on bpmn8n, with the token)."""
        paths, body = {}, self.start_schema()
        body = {k: v for k, v in body.items() if k != "$schema"}
        for start in self.starts:
            if start["path"]:
                paths[f"/webhook/{start['path']}"] = {start["method"].lower(): {
                    "operationId": "start_" + identifier(start["path"]),
                    "summary": f"Start a case of {self.name} ({start['activity']})", "servers": [{"url": n8n_url or "/"}],
                    "requestBody": {"required": True, "content": {"application/json": {"schema": body}}},
                    "responses": {"200": {"description": "the case started (n8n answers as its webhook node says)"}}}}
        for message in self.receives:
            name = message["message"]
            paths[f"/api/messages/{quote(name, safe='')}"] = {"post": {
                "operationId": "deliver_" + identifier(name),
                "summary": f"Deliver “{name}” to the case of {self.name} waiting at {message['activity']}",
                "servers": [{"url": bpmn8n_url or "/"}], "security": [{"token": []}],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["correlation_key"], "properties": {
                        "correlation_key": {"type": "string", "description": f"the case's {message['correlation_key']}"},
                        "payload": {"type": "object", "description": "becomes process variables (never replaces one)",
                                    "properties": {var: dict(UNKNOWN) for var in message["reads"]}},
                        "process": {"type": "string", "const": self.name,
                                    "description": "needed when several workflows wait for this message"}}}}}},
                "responses": {"200": {"description": "delivered: the case goes on"},
                              "404": {"description": "no case is waiting with this key (messages are not queued)"},
                              "409": {"description": "several workflows wait: say which with process"}}}}
        return {"openapi": "3.1.0", "info": {"title": f"{self.name}: process contract", "version": "1"}, "paths": paths,
                "components": {"securitySchemes": {"token": {"type": "http", "scheme": "bearer"}}}}

    def summary(self):
        """The contract in words, one line per fact."""
        lines = [self.name or "(unnamed workflow)"]
        for s in self.starts:
            how = f"{s['method']} /webhook/{s['path']}" if s["kind"] == "webhook" else s["kind"]
            lines.append(f"Starts: {how} ({s['activity']})")
        if not self.starts:
            lines.append("Starts: no trigger (a fragment)")
        unknown = sum(f["schema"] is None for f in self.fields.values())
        lines.append(f"Start data: {len(self.fields)} fields" + (f", {unknown} of unknown type" if unknown else ""))
        for var, f in self.fields.items():
            kind = (f["schema"] or {}).get("type") or ("enum" if "enum" in (f["schema"] or {}) else "unknown")
            lines.append(f"  {var}: {kind if isinstance(kind, str) else '|'.join(kind)}{', required' if f['required'] else ''}"
                         f"{', declared' if f['declared'] else ''}{' · read by ' + ', '.join(f['read_by']) if f['read_by'] else ''}"
                         f"{' · unless ' + ', '.join(f['unless']) + ' produces it' if f['unless'] else ''}")
        for m in self.receives:
            lines.append(f"Waits for “{m['message']}” at {m['activity']}, key {m['correlation_key'] or '(none)'}"
                         f"{', timeout ' + m['timeout'] if m['timeout'] else ''}; payload read: {', '.join(m['reads']) or 'nothing'}")
        for s in self.sends:
            lines.append(f"Sends: {s['activity']}" + (f" ({s['task']})" if s["task"] else "") +
                         (f", message “{s['message']}”" if s["message"] else ""))
        for lane, steps in self.lanes.items():
            lines.append(f"Lane {lane or '(any)'}: {', '.join(steps)}")
        for d in self.deadlines:
            lines.append(f"Deadline: {d['activity']} {d['due']}{' (interrupts)' if d['interrupting'] else ''}")
        for c in self.calls:
            lines.append(f"Calls: {c['name'] or c['workflow_id']} ({c['activity']})")
        for r in self.results:
            what = f"error {r['error']}" if r["error"] else "unknown" if r["fields"] is None else ", ".join(r["fields"]) or "nothing"
            lines.append(f"Ends at {r['activity']}: {what}")
        return "\n".join(lines)


def project_workflows(folder):
    """[(key, workflow, declared start data)] of a project folder: its workflow.json, else every process of its
    processes.py (PROCESSES: name -> a builder function, as ACME's), key "<project>/<process>"."""
    folder = Path(folder)
    if (path := folder / "workflow.json").exists():
        return [(folder.name, json.loads(path.read_text()), declared_start_data(folder))]
    try:
        processes = import_module(f"{folder.parent.name}.{folder.name}.processes").PROCESSES
    except (ImportError, AttributeError):
        return []
    return [(f"{folder.name}/{key}", build().as_json(), None) for key, build in processes.items()]


def local_workflows(base):
    """{key: (workflow, declared start data)} of every project under base/projects/. A project that does not load is
    left out, as the server leaves out its tasks (discover)."""
    found = {}
    for folder in sorted((Path(base) / "projects").iterdir()):
        if folder.is_dir() and re.fullmatch(r"[A-Za-z0-9_]+", folder.name):
            try:
                found |= {key: (workflow, declared) for key, workflow, declared in project_workflows(folder)}
            except Exception:  # whatever a broken project does wrong: ProjectCheck reports it
                continue
    return found
