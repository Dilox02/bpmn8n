"""n8n workflows made of bpmn8n's BPMN nodes, as the n8n editor saves them: the JSON to import,
publish through n8n's API or show in its canvas. Pure Python, no Django."""
import json
import re
import uuid
from collections import defaultdict
from datetime import date, datetime

from .elements import AGENT, MERGE, NODE, SET, TASK_KINDS, element, node_type, types
from .graph import ProcessGraph

__all__ = ["AGENT", "BRANCHING", "DEFAULT_LAST", "MERGE", "NODE", "SET", "TASK_KINDS", "TIMEOUT_FIRST", "SpecError",
           "Workflow", "node_type", "output_names"]

DEFAULT_LAST = types(default_last=True)  # their last output: "default"
(TIMEOUT_FIRST,) = types(timeout_first=True)  # its first output: "timeout"
BRANCHING = types(chooses=True) | types(splits=True)  # their outputs are the configured branches


def output_names(node):
    """A node's outputs in order: a gateway's branches (with "default" or "timeout"), a human task's
    "timeout" and "done" when it has a deadline, else ["main"] (the element registry says which)."""
    return element(node).outputs(node.get("parameters"))


class SpecError(ValueError):
    """A workflow spec the builder cannot build."""


FIELD_TYPES = ("string", "text", "number", "boolean", "choice", "date", "email")  # a human task's form fields
PRIORITIES = ("low", "normal", "high", "urgent")
UNITS = ("seconds", "minutes", "hours", "days", "weeks")  # duration.ts UNIT_MS


def form_field(field):
    """A form field as the User Task node saves it: [name, label, type], or a dict with also required
    (default true), help and a choice's options (the node keeps them as comma-separated text)."""
    if not isinstance(field, dict):
        name, label, kind = field
        field = {"name": name, "label": label, "type": kind}
    if (kind := field.get("type", "string")) not in FIELD_TYPES:
        raise SpecError(f"form field {field['name']!r}: type {kind!r} is not one of {', '.join(FIELD_TYPES)}")
    options = field.get("options") or []
    if kind == "choice" and not options:
        raise SpecError(f"form field {field['name']!r}: a choice needs its options")
    return {"name": field["name"], "label": field.get("label", ""), "type": kind,
            **({"options": ", ".join(options) if isinstance(options, (list, tuple)) else options} if options else {}),
            **({"required": False} if field.get("required", True) is False else {}),
            **({"help": field["help"]} if field.get("help") else {})}


def people_parameters(step, due_in, due_at, deadline, priority, assignee, not_same_as, completion):
    """A human task node's SLA, priority, actor rules and completion, as n8n saves them (defaults left out)."""
    parameters = {}
    if due_in is not None and due_at is not None:
        raise SpecError(f"{step}: due_in or due_at, not both")
    if due_in is not None:
        amount, unit = due_in
        if unit not in UNITS or isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            raise SpecError(f"{step}: due_in is (amount > 0, unit), unit one of {', '.join(UNITS)}; not {due_in!r}")
        parameters |= {"due": "for", "dueAmount": amount, "dueUnit": unit}
    elif due_at is not None:
        text = str(due_at).strip()
        if not (re.fullmatch(r"\{\{\s*[\w.]+\s*\}\}", text) or iso_moment(text)):
            raise SpecError(f"{step}: due_at is one {{{{variable}}}} or an ISO date, not {due_at!r}")
        parameters |= {"due": "until", "dueUntil": text}  # bpmn8n fills the variable in from the case
    if deadline:
        if "due" not in parameters:
            raise SpecError(f"{step}: a deadline needs its time: due_in or due_at")
        parameters["deadline"] = True
    if priority not in PRIORITIES:
        raise SpecError(f"{step}: priority is one of {', '.join(PRIORITIES)}, not {priority!r}")
    if priority != "normal":
        parameters["priority"] = priority
    if assignee:
        parameters["assignee"] = assignee
    if not_same_as:
        if isinstance(not_same_as, str) or not all(isinstance(a, str) and a for a in not_same_as):
            raise SpecError(f"{step}: not_same_as is a list of activity (step) names")
        parameters["notSameAs"] = list(not_same_as)
    if completion == "any":
        parameters["completion"] = "any"
    elif isinstance(completion, int) and not isinstance(completion, bool) and completion >= 1:
        parameters |= {"completion": "atLeast", "quorum": completion}
    elif completion != "all":
        raise SpecError(f"{step}: completion is all, any or a number of tasks (at least 1), not {completion!r}")
    return parameters


def iso_moment(text):
    """Whether `text` is an ISO date or date-time."""
    for parse in (date.fromisoformat, datetime.fromisoformat):
        try:
            parse(text)
            return True
        except ValueError:
            pass
    return False


class Workflow:
    """Builds an n8n workflow. Nodes are laid out left to right in the order they are added
    (time runs left to right); `row` moves alternative branches up or down, layout() computes it all."""

    STEPS = ("start", "subprocess_start", "task", "gateway", "condition_gateway", "parallel_gateway", "receive_task", "event_gateway",
             "error_end", "call_activity", "timer", "manual_task", "user_task", "merge", "llm_agent", "metrics")  # a spec's steps
    # what every LLM agent is told, whatever its instructions: it prepares, a person decides (GDPR art. 22)
    AGENT_RULES = ("You prepare work for a person, who reviews it and takes the decision: never present your answer "
                   "as a final decision. Use only the data you are given. Explain briefly how you reached your "
                   "answer, so the person can check it, and say plainly when the data is not enough.")

    def __init__(self, name, credential=None, chat_model=None):
        """chat_model: the n8n chat model node of the LLM agents, {"type", "typeVersion", "parameters",
        "credentials"?} (admin → Settings: LLMProvider.n8n_chat_model)."""
        self.name, self.credential, self.chat_model = name, credential, chat_model
        self.nodes, self.connections = [], {}
        self.steps = {}  # a step made of several nodes: name -> (first node, last node)
        self._column = -1

    def add(self, name, type_, parameters, row=0, bpmn=True, version=1, same_column=False, at=None, **extra):
        """at=(column, row) places the node explicitly (a layout computed elsewhere)."""
        if at is not None:
            self._column, row = at
        else:
            self._column += 0 if same_column else 1
        node = {"id": str(uuid.uuid4()), "name": name, "type": type_, "typeVersion": version,
                "position": [self._column * 280, row * 200], "parameters": parameters, **extra}
        if bpmn and self.credential:
            node["credentials"] = {"bpmn8nApi": self.credential}
        self.nodes.append(node)
        return self

    def column(self, n):
        """The next node goes in column `n` (to lay an alternative branch beside another)."""
        self._column = n - 1
        return self

    def start(self, name, path, respond="onReceived"):
        """Message start event: a webhook. respond="onReceived" answers at once and lets the
        process run on; "lastNode" answers with all the items of the last node."""
        return self.add(name, "n8n-nodes-base.webhook",
                        {"httpMethod": "POST", "path": path, "responseMode": respond, "options": {},
                         **({"responseData": "allEntries"} if respond == "lastNode" else {})},
                        bpmn=False, version=2, webhookId=str(uuid.uuid4()))

    def subprocess_start(self, name):
        """The start of a reusable sub-process: n8n's "When Executed by Another Workflow" trigger, which
        receives the items a Call Activity passes and hands them on unchanged."""
        return self.add(name, "n8n-nodes-base.executeWorkflowTrigger", {"inputSource": "passthrough"},
                        bpmn=False, version=1.1)

    def task(self, name, kind, endpoint, row=0, same_column=False, output="result", text=None, at=None, extra=None,
             fields=None, **inputs):
        """kind: serviceTask | scriptTask | businessRuleTask | sendTask; endpoint: @task function name;
        output: result | merge | key (what the node passes on); text: extract the input from this
        text with GLiNER (then `inputs` are overrides). `fields`: the inputs as a dict, for names
        that clash with these parameters; `extra`: more node keys (e.g. notes)."""
        parameters = {"task": f"/api/tasks/{endpoint}", "output": output,
                      "input": {"mappingMode": "defineBelow", "value": {**(fields or {}), **inputs},
                                "matchingColumns": [], "schema": []}}
        if text is not None:
            parameters |= {"inputFrom": "text", "text": text, "includeExtraction": True}
        return self.add(name, node_type(kind), parameters, row, same_column=same_column,
                        at=at, **(extra or {}))

    def metrics(self, name, project, metrics, label=""):
        """Record local evaluations without changing the input. project: folder under projects/;
        metrics: [{name, value (number or n8n expression), unit?, direction: higher|lower?, threshold?}].
        An optional threshold is inclusive. Saved under the project's run/<workflow>/<execution>/metrics/.
        Use n8n expressions (= followed by {{ $json.field }}) for computed values, never Python code."""
        if not metrics or len({m["name"] for m in metrics}) != len(metrics):
            raise SpecError("metrics must contain at least one metric with unique names")
        return self.add(name, node_type("metrics"), {"project": project, "label": label, "metrics": {"values": [
            {**m, "checkThreshold": m.get("threshold") is not None} for m in metrics]}})

    def gateway(self, name, kind, question, branches, state="", min_confidence=0.7):
        """Laya decides. kind: exclusive | inclusive. branches: {name: description}; outputs
        follow their order, then `default`."""
        return self.add(name, node_type(f"{kind}Gateway"), {
            "decider": "laya", "question": question, "state": state, "minConfidence": min_confidence,
            "branches": {"values": [{"name": n, "description": d} for n, d in branches.items()]},
        })

    def condition_gateway(self, name, kind, conditions, decision_scope=None):
        """Data decides. conditions: {branch name: condition on the process variables}, e.g.
        "{{amount}} > 1000" (an n8n expression evaluating to a boolean also works).
        decision_scope="technical" is only for machine/data housekeeping, never decisions about people."""
        if decision_scope not in (None, "technical"):
            raise SpecError("a condition gateway's decision_scope must be 'technical' or omitted")
        return self.add(name, node_type(f"{kind}Gateway"), {
            "decider": "conditions",
            **({"decisionScope": decision_scope} if decision_scope else {}),
            "branches": {"values": [{"name": n, "condition": c} for n, c in conditions.items()]},
        })

    def parallel_gateway(self, name, branches, row=0):
        return self.add(name, node_type("parallelGateway"),
                        {"branches": {"values": [{"name": n} for n in branches]}}, row, bpmn=False)

    def receive_task(self, name, message, correlation_key, row=0, same_column=False):
        return self.add(name, node_type("receiveTask"),
                        {"message": message, "correlationKey": correlation_key}, row,
                        same_column=same_column, webhookId=str(uuid.uuid4()))

    def event_gateway(self, name, messages, correlation_key, timeout, row=0):
        """Event-based gateway: output 0 is "timeout", then one per message. timeout: (amount, unit)."""
        return self.add(name, node_type("eventBasedGateway"), {
            "branches": {"values": [{"name": m} for m in messages]}, "correlationKey": correlation_key,
            "mode": "for", "amount": timeout[0], "unit": timeout[1]}, row, webhookId=str(uuid.uuid4()))

    def error_end(self, name, code, message="", row=0, same_column=False):
        return self.add(name, node_type("errorEndEvent"), {"code": code, "message": message}, row,
                        same_column=same_column)

    def call_activity(self, name, workflow_id, row=0):
        return self.add(name, node_type("callActivity"),
                        {"workflowId": {"__rl": True, "mode": "id", "value": workflow_id}}, row, bpmn=False)

    def timer(self, name, amount=None, unit="hours", until=None, row=0, same_column=False):
        """Waits for amount unit (seconds | minutes | hours | days | weeks), or until a moment: until is a
        {{variable}} holding an ISO date or date-time that an earlier step produced (a deadline)."""
        if until is not None:
            variable = re.fullmatch(r"\{\{\s*([\w.]+)\s*\}\}", str(until).strip())
            if not variable:
                raise SpecError(f"{name}: until must be one {{{{variable}}}} with a date, not {until!r}")
            parameters = {"mode": "until", "until": f"={{{{ $json.{variable.group(1)} }}}}"}
        elif amount is None:
            raise SpecError(f"{name}: missing amount (and unit), or until: a timer waits for a time or until a date")
        else:
            parameters = {"mode": "for", "amount": amount, "unit": unit}
        return self.add(name, node_type("timerEvent"), parameters, row, bpmn=False, same_column=same_column)

    def manual_task(self, name, title, lane, context="", row=0, same_column=False, **people):
        """A person does it and confirms it; `people`: as user_task's (due_in, deadline, priority...)."""
        return self.user_task(name, title, lane, form=[], context=context, row=row, same_column=same_column,
                              kind="manualTask", **people)

    def user_task(self, name, title, lane, form, context="", description="", row=0, same_column=False,
                  kind="userTask", due_in=None, due_at=None, deadline=False, priority="normal", assignee="",
                  not_same_as=(), completion="all"):
        """form: [name, label, type] with type string | number | boolean, or {"name", "label", "type", "required",
        "help", "options"} with type also text | choice (its options) | date | email; fields are required
        unless "required": false. context: what the person sees, {label: "{{variable}}"} (a dict, or its JSON);
        empty shows all the process variables.
        The SLA: due_in=(amount, unit) or due_at="{{variable}}" (or an ISO date) orders the inbox and marks the
        task overdue; with deadline=True it interrupts the wait: the node gets two outputs, "timeout" (taken
        when the due time passes, the tasks expire) and "done". priority: low | normal | high | urgent.
        assignee: a username or "{{variable}}" (created claimed by that person); not_same_as: activities whose
        completer may not work this one (four-eyes); completion: all | any | N, how many of the tasks of a
        multi-instance batch resume the case (the others are cancelled)."""
        if not all(isinstance(f, (list, tuple)) and len(f) == 3 or isinstance(f, dict) and f.get("name")
                   for f in form):  # a dict as the whole form would unpack its keys
            raise ValueError("form: a list of [name, label, type] or {name, label, type, ...}")
        if isinstance(context, dict):
            context = json.dumps(context, ensure_ascii=False)
        elif context:  # the node parses it as JSON: plain text would fail when a case reaches the step
            try:
                if not isinstance(json.loads(context), dict):
                    raise ValueError
            except ValueError:
                raise ValueError(f"{name}: context must be an object {{label: \"{{{{variable}}}}\"}}, not {context[:60]!r}") from None
        return self.add(name, node_type(kind), {
            "title": title, "lane": lane, "description": description, "context": context,
            "form": {"fields": [form_field(f) for f in form]},
            **people_parameters(name, due_in, due_at, deadline, priority, assignee, not_same_as, completion),
        }, row, same_column=same_column, webhookId=str(uuid.uuid4()))

    def merge(self, name, inputs=2, row=0):
        """Join (n8n's Merge): waits for the inputs that can still deliver, then runs once."""
        return self.add(name, MERGE, {"numberInputs": inputs}, row, bpmn=False, version=3)

    def llm_agent(self, name, prompt, output, instructions=""):
        """An LLM (n8n's AI Agent, with the chat model of admin → Settings) drafts text that needs judgement:
        a summary, a reply, a proposal with its reasons. prompt: the task, with the {{variables}} it needs and
        only those (it sees nothing else: data minimisation); output: the variable its answer becomes;
        instructions: its role and constraints. It never decides: a person reviews its answer (a user_task
        after it). Four nodes: the prompt (filled in bpmn8n, so the event log keeps what the LLM was sent),
        the agent with its chat model, and the answer as `output`."""
        first, last = f"{name}: prompt", f"{name}: answer"
        self.task(first, "scriptTask", "compose_prompt", fields={"prompt": prompt})
        self.add(name, AGENT, {"promptType": "define", "text": "={{ $json.prompt }}",
                               "options": {"systemMessage": f"{instructions}\n\n{self.AGENT_RULES}".strip()}},
                 bpmn=False, version=3.1,  # an external provider's 429/503 ("high demand") is often gone in seconds
                 retryOnFail=True, maxTries=3, waitBetweenTries=5000,
                 onError="continueRegularOutput")  # then the error goes on as its answer: a person sees it
        if self.chat_model:
            self.add(f"{name}: model", self.chat_model["type"], self.chat_model["parameters"], bpmn=False,
                     version=self.chat_model["typeVersion"], same_column=True,
                     **({"credentials": self.chat_model["credentials"]} if self.chat_model.get("credentials") else {}))
            self.connections[f"{name}: model"] = {"ai_languageModel": [[{"node": name, "type": "ai_languageModel",
                                                                         "index": 0}]]}
        self.add(last, SET, {"mode": "manual", "assignments": {"assignments": [
            {"id": str(uuid.uuid4()), "name": output, "type": "string", "value":
             "={{ $json.output ?? '(The LLM did not answer: ' + ($json.error?.message ?? $json.error ?? 'no reply') + ')' }}"}]},
            "includeOtherFields": False, "options": {}}, bpmn=False, version=3.4)
        self.flow(first, name, last)
        self.steps[name] = (first, last)  # after its own flow: other steps' flows reach its first and leave its last
        return self

    def node(self, name):
        return next(n for n in self.nodes if n["name"] == name)

    def flow(self, *names, output=0):
        """Sequence flow through `names`; `output` is the output the first hop leaves from: its index
        or its name (a gateway's branch, "default", "timeout"). A hop that exists already is not added
        again (two flows through the same steps would run them twice)."""
        if isinstance(output, str):
            labels = output_names(self.node(names[0]))
            if output not in labels:
                raise ValueError(f"{names[0]!r} has no output {output!r}, only {labels}")
            output = labels.index(output)
        for i, (src, dst) in enumerate(zip(names, names[1:])):
            src, dst = self.steps.get(src, (src, src))[1], self.steps.get(dst, (dst, dst))[0]
            outputs = self.connections.setdefault(src, {"main": []})["main"]
            port = output if i == 0 else 0
            outputs.extend([] for _ in range(port + 1 - len(outputs)))
            if not any(c["node"] == dst for c in outputs[port]):
                outputs[port].append({"node": dst, "type": "main", "index": self._next_input(dst)})
        return self

    def _next_input(self, name):
        """A Merge takes each incoming flow on its own input; any other node on its only one."""
        if self.node(name)["type"] != MERGE:
            return 0
        return sum(c["node"] == name for outs in self.connections.values() for port in outs.get("main", []) for c in port)

    def layout(self):
        """Levels left to right: a step's column is its longest path from the trigger (a join comes after
        all its branches). The longest path runs straight along the top row, except a detour (a branch
        that joins its sibling later goes below, so the direct flow never passes behind it); every other branch takes the
        first row below that is free until it joins the others, so branches never overlap, and a step that
        branches join goes in the highest of their rows (the one its longest branch kept free). Flows then run right and down, as n8n draws them
        (out on the right, in on the left); only loops go back. A chat model goes under its agent."""
        graph = ProcessGraph(self.as_json())
        models = graph.parent  # chat model -> its agent
        after = {name: [n for port in graph.outs[name] for n in port] for name in graph.nodes}
        # every step nothing leads to starts a row, a trigger or not (graph.starts would leave orphans out)
        starts = [n for n in graph.nodes if not graph.into[n] and n not in graph.subnodes]
        order, seen = [], set()

        def visit(name):  # depth first; the reverse of the finishing order is a topological order
            seen.add(name)
            for n in after.get(name, []):
                if n not in seen:
                    visit(n)
            order.append(name)

        for name in starts:
            visit(name)
        order.reverse()
        rank, depth = {n: i for i, n in enumerate(order)}, dict.fromkeys(order, 0)
        forward = {n: [c for c in dict.fromkeys(after.get(n, [])) if rank[c] > rank[n]] for n in order}  # no loops
        for name in order:
            for n in forward[name]:
                depth[n] = max(depth[n], depth[name] + 1)
        tail, downstream = {}, {}  # columns from a step to the end of its longest path; the steps after it
        for name in reversed(order):
            tail[name] = max((tail[c] + depth[c] - depth[name] for c in forward[name]), default=0)
            downstream[name] = set(forward[name]).union(*(downstream[c] for c in forward[name]))

        joins = {n for n in order if sum(n in forward[m] for m in order) > 1}  # steps that branches join

        def own_columns(child):
            """The columns a branch keeps its row for: from its first step up to the step it joins, so that
            its flow into that step passes no other step either."""
            if child in joins:
                return range(depth[child], depth[child] + 1)
            last = child
            while forward[last] and (nxt := straight_first(last)[0]) not in joins:
                last = nxt
            joined = straight_first(last)[0] if forward[last] else None
            return range(depth[child], depth[joined] if joined else depth[last] + 1)

        def straight_first(name):
            """Its next steps, the one that stays on its row first: not a detour (a branch that joins a
            sibling later, whose flow would else pass behind it), then the longest."""
            nexts = forward[name]
            return sorted(nexts, key=lambda c: (any(o in downstream[c] for o in nexts if o != c), -tail[c]))
        agents = {agent: model for model, agent in models.items()}
        busy, offered, row = set(), defaultdict(list, {n: [i] for i, n in enumerate(starts)}), {}  # (column, row)
        for name in sorted(order, key=depth.get):
            row[name] = min(offered[name])
            busy.add((depth[name], row[name]))
            if name in agents:  # its chat model, in the first free row under it
                below = row[name] + 1
                while (depth[name], below) in busy:
                    below += 1
                busy.add((depth[name], below))
                row[agents[name]], depth[agents[name]] = below, depth[name]
            for i, child in enumerate(straight_first(name)):
                lane, columns = row[name], own_columns(child)
                if i and offered[child] and min(offered[child]) <= lane:  # placed higher already: no lane to keep
                    continue
                if i:  # a side branch
                    lane += 1
                    while any((c, lane) in busy for c in columns):
                        lane += 1
                    busy.update((c, lane) for c in columns)
                offered[child].append(lane)
        for node in self.nodes:  # ponytail: unconnected nodes (no flow in or out) all go at the origin
            node["position"] = [depth.get(node["name"], 0) * 280, row.get(node["name"], 0) * 200]
        return self

    @classmethod
    def from_spec(cls, spec, credential=None, chat_model=None):
        """A workflow from a JSON spec (what bpmn8n Studio's LLM writes), laid out automatically:
        {"name": ..., "steps": [{"type": <one of STEPS>, "name": ..., <that method's arguments>}],
         "flows": [{"from": <step>, "branch": <output name, for gateways>, "to": [<step>, ...]}]}"""
        workflow, names = cls(spec.get("name") or "Untitled process", credential, chat_model), set()
        steps, flows = spec.get("steps", []), spec.get("flows", [])
        if not (isinstance(steps, list) and all(isinstance(s, dict) for s in steps)
                and isinstance(flows, list) and all(isinstance(f, dict) for f in flows)):
            raise SpecError('"steps" and "flows" must be lists of objects')
        for step in steps:
            args = dict(step)
            kind, name = args.pop("type", None), args.get("name")
            if kind not in cls.STEPS:
                raise SpecError(f"step {name!r}: unknown type {kind!r}, use one of {', '.join(cls.STEPS)}")
            if name in names:
                raise SpecError(f"two steps are named {name!r}: names must be unique")
            names.add(name)
            try:
                getattr(workflow, kind)(**args)
            except (TypeError, ValueError, AttributeError, KeyError, IndexError) as e:  # an argument of the wrong shape
                raise SpecError(f"step {name!r} ({kind}): {type(e).__name__}: {e}") from e
        for flow in flows:
            to = flow.get("to", [])
            path = [flow.get("from"), *(to if isinstance(to, list) else [to])]
            if unknown := [n for n in path if n not in names]:
                raise SpecError(f"flow {flow}: no step named {unknown}")
            try:
                workflow.flow(*path, output=flow.get("branch") or 0)  # "branch": null on a plain flow
            except (TypeError, ValueError) as e:
                raise SpecError(f"flow {flow}: {e}") from e
        return workflow.layout()

    def as_json(self):
        # v1 = what n8n's editor uses: a join runs once no branch can still deliver (inclusive join);
        # the legacy v0 forced every branch upstream of a Merge to run, even those a gateway skipped
        return {"name": self.name, "nodes": self.nodes, "connections": self.connections,
                "settings": {"executionOrder": "v1"}}
