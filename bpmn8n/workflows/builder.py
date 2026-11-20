"""n8n workflows made of bpmn8n's BPMN nodes, as the n8n editor saves them: the JSON to import,
publish through n8n's API or show in its canvas. Pure Python, no Django."""
import json
import re
import uuid
from collections import defaultdict
from datetime import date, datetime

from .elements import AGENT, MERGE, NODE, SET, STICKY, TASK_KINDS, node_type, types
from .graph import ERROR_OUTPUT, ProcessGraph
from .graph import outputs as output_names

__all__ = ["AGENT", "BRANCHING", "DEFAULT_LAST", "GUARDRAIL_CHECKS", "MERGE", "NODE", "SET", "TASK_KINDS",
           "TIMEOUT_FIRST", "SpecError", "Workflow", "node_type", "output_names"]

DEFAULT_LAST = types(default_last=True)  # their last output: "default"
(TIMEOUT_FIRST,) = types(timeout_first=True)  # its first output: "timeout"
BRANCHING = types(chooses=True) | types(splits=True)  # their outputs are the configured branches
GUARDRAIL_CHECKS = ("pii", "length", "blocklist", "schema")  # what the check_guardrails task can check
ON_ERROR = {"branch": ERROR_OUTPUT, "continue": "continueRegularOutput"}  # task(on_error=...) -> n8n's On Error
PREVIOUS = re.compile(r"\{\{\s*previous\s*\}\}")  # in a prompt_chain's prompt: the answer before
MAX_LLM_STEPS = 5  # J4: the most links of a prompt_chain, branches of a parallel_llm, rounds of a reflection
ROUNDS_LEFT = "rounds_left"  # what the loop step's count_round task tells its gateway: the budget, no decision
BAND = "Lane: "  # the name of a swimlane's sticky note: layout("lanes") draws them and redraws them


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


def people_parameters(step, due_in, due_at, deadline, priority, assignee, not_same_as, completion,
                      remind_after=None, escalate_to=""):
    """A human task node's SLA, priority, actor rules and completion, as n8n saves them (defaults left out)."""
    parameters = {}
    if remind_after is not None:
        amount, unit = remind_after
        if unit not in UNITS or isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            raise SpecError(f"{step}: remind_after is (amount > 0, unit), unit one of {', '.join(UNITS)}; "
                            f"not {remind_after!r}")
        parameters |= {"remindAmount": amount, "remindUnit": unit}
    if escalate_to:
        if remind_after is None or not isinstance(escalate_to, str):
            raise SpecError(f"{step}: escalate_to (a lane, or \"/path\" of an escalation workflow's webhook) "
                            "needs remind_after, the time it happens")
        parameters["escalateTo"] = escalate_to
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
             "error_end", "call_activity", "timer", "manual_task", "user_task", "merge", "llm_agent", "metrics",
             "global_variable", "guardrail", "prompt_chain", "parallel_llm", "reflection",
             "guardrailed_agent", "loop", "signal_catch", "signal_throw", "terminate_end", "compensate",
             "cancel_end")  # a spec's steps
    # the @tasks (bpmn8n/tasks.py) that steps place on their own: llm_agent's prompt and audit, the patterns',
    # the loop's count
    STEP_TASKS = ("compose_prompt", "draw_audit_sample", "check_guardrails", "read_critique", "count_round",
                  "throw_signal", "compensate_case", "record_llm_call")
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
        node = {"id": self.stable_id(name), "name": name, "type": type_, "typeVersion": version,
                "position": [self._column * 280, row * 200], "parameters": parameters, **extra}
        if bpmn and self.credential:
            node["credentials"] = {"bpmn8nApi": self.credential}
        self.nodes.append(node)
        return self

    def stable_id(self, *parts):
        """A node's id (or a part's: its webhook, an assignment) from the workflow's name and the node's: the
        same workflow built again has the same ids, so a regenerated workflow.json diffs only where it changed."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([self.name, *parts])))

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
                        bpmn=False, version=2, webhookId=self.stable_id(name, "webhook"))

    def subprocess_start(self, name):
        """The start of a reusable sub-process: n8n's "When Executed by Another Workflow" trigger, which
        receives the items a Call Activity passes and hands them on unchanged."""
        return self.add(name, "n8n-nodes-base.executeWorkflowTrigger", {"inputSource": "passthrough"},
                        bpmn=False, version=1.1)

    def task(self, name, kind, endpoint, row=0, same_column=False, output="result", text=None, at=None, extra=None,
             fields=None, on_error=None, retry=0, retry_wait_ms=1000, **inputs):
        """kind: serviceTask | scriptTask | businessRuleTask | sendTask; endpoint: @task function name;
        output: result | merge | key (what the node passes on); text: extract the input from this
        text with GLiNER (then `inputs` are overrides). `fields`: the inputs as a dict, for names
        that clash with these parameters; `extra`: more node keys (e.g. notes). When it fails: retry=1..4
        tries again, retry_wait_ms apart (at most 5000), for a system that may be briefly down; then
        on_error=None stops the case, "branch" takes the task's "error" output (an error boundary: flow it
        to a person, flow(name, person, output="error")), "continue" goes on with an `error` field."""
        parameters = {"task": f"/api/tasks/{endpoint}", "output": output,
                      "input": {"mappingMode": "defineBelow", "value": {**(fields or {}), **inputs},
                                "matchingColumns": [], "schema": []}}
        if text is not None:
            parameters |= {"inputFrom": "text", "text": text, "includeExtraction": True}
        if on_error not in (None, *ON_ERROR):
            raise SpecError(f"{name}: on_error must be 'branch', 'continue' or omitted, not {on_error!r}")
        if retry and not (1 <= retry <= 4 and 0 <= retry_wait_ms <= 5000):  # n8n's limits: 5 tries, 5 s apart
            raise SpecError(f"{name}: retry is 1 to 4 (n8n tries at most 5 times), retry_wait_ms 0 to 5000")
        settings = ({"onError": ON_ERROR[on_error]} if on_error else {}) | (
            {"retryOnFail": True, "maxTries": retry + 1, "waitBetweenTries": retry_wait_ms} if retry else {})
        return self.add(name, node_type(kind), parameters, row, same_column=same_column,
                        at=at, **settings, **(extra or {}))

    def metrics(self, name, project, metrics, label=""):
        """Record local evaluations without changing the input. project: folder under projects/;
        metrics: [{name, value (number or n8n expression), unit?, direction: higher|lower?, threshold?}].
        An optional threshold is inclusive. Saved under the project's run/<workflow>/<execution>/metrics/.
        Use n8n expressions (= followed by {{ $json.field }}) for computed values, never Python code."""
        if not metrics or len({m["name"] for m in metrics}) != len(metrics):
            raise SpecError("metrics must contain at least one metric with unique names")
        return self.add(name, node_type("metrics"), {"project": project, "label": label, "metrics": {"values": [
            {**m, "checkThreshold": m.get("threshold") is not None} for m in metrics]}})

    def global_variable(self, name, key, operation="get", value=None, by=1, ttl=0):
        """A variable every workflow shares (bpmn8n's store, {{vars.<key>}} in any field): get, set, increment
        or delete `key`; the value goes on with the item under the key. value: any JSON (set); by: what
        increment adds; ttl: seconds it lives, 0 for ever (set, and increment when it creates the variable)."""
        if operation not in ("get", "set", "increment", "delete"):
            raise SpecError(f"global_variable {name!r}: operation is get, set, increment or delete")
        parameters = {"operation": operation, "key": key, "output": "merge"}
        if operation == "set":
            parameters |= {"value": json.dumps(value), "ttl": ttl}
        elif operation == "increment":
            parameters |= {"by": by, "ttl": ttl}
        return self.add(name, node_type("globalVariable"), parameters)

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
                        same_column=same_column, webhookId=self.stable_id(name, "webhook"))

    def event_gateway(self, name, messages, correlation_key, timeout, row=0):
        """Event-based gateway: output 0 is "timeout", then one per message. timeout: (amount, unit)."""
        return self.add(name, node_type("eventBasedGateway"), {
            "branches": {"values": [{"name": m} for m in messages]}, "correlationKey": correlation_key,
            "mode": "for", "amount": timeout[0], "unit": timeout[1]}, row, webhookId=self.stable_id(name, "webhook"))

    def error_end(self, name, code, message="", row=0, same_column=False):
        return self.add(name, node_type("errorEndEvent"), {"code": code, "message": message}, row,
                        same_column=same_column)

    def call_activity(self, name, workflow_id, row=0, inputs=None, outputs=None):
        """Runs another workflow as a sub-process. Without a mapping the items go in and its result comes back as
        they are. inputs: {child variable: "{{parent variable}}"}, the only fields the sub-process gets;
        outputs: the variables of its result that come back (merged into the item): the only ones, and what the
        workflow check takes the step to produce."""
        parameters = {"workflowId": {"__rl": True, "mode": "id", "value": workflow_id}}
        if inputs is not None:
            if not isinstance(inputs, dict) or not inputs or not all(isinstance(k, str) and k for k in inputs):
                raise SpecError(f"{name}: inputs is {{child variable: \"{{{{parent variable}}}}\"}}, at least one")
            parameters["inputs"] = json.dumps(inputs, ensure_ascii=False)
        if outputs is not None:
            if isinstance(outputs, str) or not outputs or not all(isinstance(o, str) and o for o in outputs):
                raise SpecError(f"{name}: outputs is a list of the sub-process's variables that come back")
            parameters["outputs"] = list(outputs)
        return self.add(name, node_type("callActivity"), parameters, row, bpmn=False)

    def signal_catch(self, name, signal, row=0, same_column=False):
        """Waits for the signal `signal`: POST /api/signals/<signal> resumes every case waiting for it (a message
        goes to one, by its correlation key); its payload adds variables the case does not have."""
        if not isinstance(signal, str) or not signal.strip():
            raise SpecError(f"{name}: signal is the name of the signal to wait for")
        return self.add(name, node_type("signalCatch"), {"signal": signal.strip()}, row, same_column=same_column,
                        webhookId=self.stable_id(name, "webhook"))

    def signal_throw(self, name, signal, payload=None, row=0, same_column=False):
        """Throws the signal `signal` (with `payload`, {{variables}} allowed): every case waiting at a signal_catch
        for it resumes. It is a task: it never waits, and nobody waiting is no error."""
        if not isinstance(signal, str) or not signal.strip():
            raise SpecError(f"{name}: signal is the name of the signal to throw")
        return self.task(name, "sendTask", "throw_signal", row, same_column,
                         fields={"name": signal.strip(), "payload": payload or {}})

    def terminate_end(self, name, reason="", row=0, same_column=False):
        """Ends the whole case here and stops its other branches (and what they wait for): n8n's execution is
        stopped, bpmn8n cancels the waiting tasks and answers 409 to the case's later calls."""
        return self.add(name, node_type("terminateEndEvent"), {"reason": reason}, row, same_column=same_column)

    def compensate(self, name, row=0, same_column=False, on_error=None):
        """Undoes the case's completed tasks that declared an undo (@task(compensate="...")), newest first: a saga.
        Fails (on_error as a task's) when an undo does; running it again undoes only what is left."""
        return self.task(name, "serviceTask", "compensate_case", row, same_column, on_error=on_error)

    def cancel_end(self, name, code, message="", row=0):
        """A cancel end event: compensates, then ends with the error `code` (as error_end)."""
        undo = f"{name}: undo"
        self.compensate(undo, row)
        self.error_end(name, code, message, row)
        self.flow(undo, name)
        self.steps[name] = (undo, name)
        return self

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
                  not_same_as=(), completion="all", remind_after=None, escalate_to=""):
        """form: [name, label, type] with type string | number | boolean, or {"name", "label", "type", "required",
        "help", "options"} with type also text | choice (its options) | date | email; fields are required
        unless "required": false. context: what the person sees, {label: "{{variable}}"} (a dict, or its JSON);
        empty shows all the process variables.
        The SLA: due_in=(amount, unit) or due_at="{{variable}}" (or an ISO date) orders the inbox and marks the
        task overdue; with deadline=True it interrupts the wait: the node gets two outputs, "timeout" (taken
        when the due time passes, the tasks expire) and "done". priority: low | normal | high | urgent.
        assignee: a username or "{{variable}}" (created claimed by that person); not_same_as: activities whose
        completer may not work this one (four-eyes); completion: all | any | N, how many of the tasks of a
        multi-instance batch resume the case (the others are cancelled).
        remind_after=(amount, unit): a non-interrupting reminder, the task stays open: when it is still waiting then,
        `manage.py run_deadlines` e-mails the task's lane; escalate_to: instead, the members of that lane, or, as
        "/path", starts the escalation workflow whose webhook has that path (with the case id)."""
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
            **people_parameters(name, due_in, due_at, deadline, priority, assignee, not_same_as, completion,
                                remind_after, escalate_to),
        }, row, same_column=same_column, webhookId=self.stable_id(name, "webhook"))

    def merge(self, name, inputs=2, row=0):
        """Join (n8n's Merge): waits for the inputs that can still deliver, then runs once."""
        return self.add(name, MERGE, {"numberInputs": inputs}, row, bpmn=False, version=3)

    def loop(self, name, back_to, until, max_rounds=3, decision_scope=None):
        """A standard loop with its guard (BPMN's loop marker, max_rounds its loop maximum): flow the step
        whose result decides into it; when `until` holds the case goes on through "done", otherwise back
        to back_to ("again"), for at most max_rounds rounds; then "limit", which you flow to a person or an
        end. until: a condition on the process variables, as a condition_gateway's ("{{approved}}"). The
        count_round task counts the rounds in the case ({{loop_rounds}}, by loop name), so the loop always
        ends; decision_scope="technical" as a condition_gateway's (housekeeping, never decisions about
        people)."""
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1:
            raise SpecError(f"{name}: max_rounds is a whole number of rounds, at least 1")
        if not str(until).strip():
            raise SpecError(f"{name}: until is the condition that ends the loop, e.g. \"{{{{approved}}}}\"")
        if not any(n["name"] == self.steps.get(back_to, (back_to,))[0] for n in self.nodes):
            raise SpecError(f"{name}: back_to {back_to!r} is no step added before the loop")
        count = f"{name}: count"
        self.task(count, "scriptTask", "count_round", fields={"loop": name, "max_rounds": max_rounds})
        self.condition_gateway(name, "exclusive", {"done": until, "again": f"{{{{{ROUNDS_LEFT}}}}}",
                                                   "limit": f"not {{{{{ROUNDS_LEFT}}}}}"}, decision_scope)
        self.flow(count, name).flow(name, back_to, output="again")
        self.steps[name] = (count, name)
        return self

    TOOL = "@n8n/n8n-nodes-langchain.toolHttpRequest"
    TOOL_BASE = "http://web:8000"  # where the agent's task tools reach bpmn8n (the bpmn8n credential's default base URL)

    def agent_tool(self, agent, task):
        """J2: n8n's HTTP Request Tool that lets `agent` call the @task `task` (POST /api/tasks/<task>, with the
        bpmn8n credential): the description is the task's docstring, the parameters its input Schema, so the agent
        calls typed, logged functions. A sub-node: it is not in the flow."""
        from ..api.tasks import TASKS  # tasks.py imports this module
        from ..clients.extraction import field_specs
        if task not in TASKS:
            raise SpecError(f"{agent}: tool {task!r} is not a @task")
        specs = field_specs(TASKS[task].schema)
        kinds = {"int": "number", "float": "number", "bool": "boolean"}
        name = f"{agent}: {task}"
        self.add(name, self.TOOL, {
            "toolDescription": TASKS[task].description or task.replace("_", " "), "method": "POST",
            "url": f"{self.TOOL_BASE}/api/tasks/{task}", "authentication": "predefinedCredentialType",
            "nodeCredentialType": "bpmn8nApi", "sendBody": True, "specifyBody": "keypair",
            "parametersBody": {"values": [{"name": s.name, "valueProvider": "modelRequired" if s.required else "modelOptional"}
                                          for s in specs]},
            "placeholderDefinitions": {"values": [{"name": s.name, "description": s.description,
                                                   "type": kinds.get(s.kind, "string")} for s in specs]}},
                 bpmn=False, version=1.1, same_column=True,
                 **({"credentials": {"bpmn8nApi": self.credential}} if self.credential else {}))
        self.connections[name] = {"ai_tool": [[{"node": agent, "type": "ai_tool", "index": 0}]]}

    def llm_agent(self, name, prompt, output, instructions="", oversight="review", audit_rate=0.1, risk=None, tools=()):
        """An LLM (n8n's AI Agent, with the chat model of admin → Settings) drafts text that needs judgement:
        a summary, a reply, a proposal with its reasons. prompt: the task, with the {{variables}} it needs and
        only those (it sees nothing else: data minimisation); output: the variable its answer becomes;
        instructions: its role and constraints. It never decides: a person reviews its answer (a user_task
        after it). Four nodes: the prompt (filled in bpmn8n, so the event log keeps what the LLM was sent),
        the agent with its chat model, the answer as `output` and its usage record (tokens, cost: E9). Retried 3 times, 5 s apart: up to 3 paid
        calls per answer. oversight="audit" (human-on-the-loop, H10) only for a step declared risk="low", with
        no legal or similar effect on a person: a fixed share (audit_rate) of the cases, drawn from the case
        id, goes out of the step's "audit" output to a person; the others go on (its first output).
        tools: names of @tasks the agent may call (J2, agent_tool): typed, logged, governed functions; an agent
        whose tool acts on the world (a send task) needs a person's review after it (rule `agent-tool`)."""
        if isinstance(tools, str) or not all(isinstance(t, str) for t in tools):
            raise SpecError(f"{name}: tools is a list of @task names")
        if oversight not in ("review", "audit") or risk not in (None, "low") or not 0 < audit_rate <= 1:
            raise SpecError(f"{name}: oversight is 'review' or 'audit', risk 'low' or omitted, audit_rate in (0, 1]")
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
        for tool in tools:
            self.agent_tool(name, tool)
        self.add(last, SET, {"mode": "manual", "assignments": {"assignments": [
            {"id": self.stable_id(last, output), "name": output, "type": "string", "value":
             "={{ $json.output ?? '(The LLM did not answer: ' + ($json.error?.message ?? $json.error ?? 'no reply') + ')' }}"}]},
            "includeOtherFields": False, "options": {}}, bpmn=False, version=3.4)
        usage = f"{name}: usage"  # E9: tokens (n8n's, else estimated), cost and the draft, kept in the event log
        self.task(usage, "scriptTask", "record_llm_call", output="merge", fields={
            "agent": name, "variable": output, "prompt": f"={{{{ $('{first}').item.json.prompt }}}}",
            "answer": f"={{{{ $json[{json.dumps(output)}] }}}}", "tokens": f"={{{{ $('{name}').item.json.tokenUsage ?? '' }}}}"})
        self.flow(first, name, last, usage)
        last = usage
        if oversight == "audit":  # the sample is a task, so the event log keeps which cases a person audits
            sample, last = f"{name}: audit sample", f"{name}: audit?"
            self.task(sample, "scriptTask", "draw_audit_sample", fields={"rate": audit_rate, "risk": risk or "not declared"})
            self.condition_gateway(last, "exclusive", {"continue": "not {{audited}}", "audit": "{{audited}}"},
                                   decision_scope="technical")
            self.flow(usage, sample, last)
        self.steps[name] = (first, last)  # after its own flow: other steps' flows reach its first and leave its last
        return self

    # --- agentic patterns (J1, [AE] Cimino, Agentic Engineering 2026): vetted templates of the steps above ----

    def guardrail(self, name, text, checks, on_block=None):
        """Guardrails and safety [AE]: checks a text before it goes on, e.g. before an external LLM reads text
        that may hold personal data, or an LLM's answer before it is used. text: with {{variables}}; checks:
        names, or {name: setting}: "pii" (emails, phone numbers, IBANs, Italian fiscal codes, card numbers),
        "length" (the most characters, 4000 by default), "blocklist" (its words), "schema" (the keys the text,
        a JSON object, must have). The check task gives {{passed}}, {{findings}} and {{redacted}} (the text
        with personal data replaced by placeholders, whatever the checks), then its first output "passed"
        goes on and "blocked" goes to a person: a user task in the lane on_block, or, without on_block,
        wherever you flow it (flow(name, step, output="blocked"))."""
        settings = dict.fromkeys(checks, True) if isinstance(checks, (list, tuple)) else dict(checks)
        if not settings or set(settings) - set(GUARDRAIL_CHECKS):
            raise SpecError(f"{name}: checks are some of {', '.join(GUARDRAIL_CHECKS)}, not {list(settings)}")
        if settings.get("blocklist") is True:
            raise SpecError(f"{name}: give the blocklist its words, {{\"blocklist\": [\"word\", ...]}}")
        fields = {"text": text, "checks": ",".join(settings)}
        for check, field in (("length", "max_length"), ("blocklist", "blocklist"), ("schema", "keys")):
            if settings.get(check) not in (None, True):
                value = settings[check]
                fields[field] = ",".join(value) if isinstance(value, (list, tuple)) else value
        gate = f"{name}: passed?"
        self.task(name, "scriptTask", "check_guardrails", fields=fields)
        self.condition_gateway(gate, "exclusive", {"passed": "{{passed}}", "blocked": "not {{passed}}"},
                               decision_scope="technical")  # a check of the text, no decision about a person
        self.flow(name, gate)
        if on_block is not None:
            self._blocked(f"{name}: blocked", on_block, name).flow(gate, f"{name}: blocked", output="blocked")
        self.steps[name] = (name, gate)
        return self

    def _blocked(self, name, lane, what):
        """The person who takes over a text a guardrail blocked."""
        return self.user_task(name, f"Text blocked by a guardrail: {what}", lane, [("resolution", "What you did", "string")],
                              context={"Findings": "{{findings}}", "The text, personal data redacted": "{{redacted}}"},
                              description="A guardrail stopped this text before it went on. Take the case over.")

    def prompt_chain(self, name, prompts, output, instructions=""):
        """Prompt chaining [AE 20-28]: LLM agents in sequence, each prompt on the answer before it, written
        {{previous}} (the agents are "<name> 1", "<name> 2"…; link i answers {{<output>_<i>}}, the last one
        {{output}}). For work in steps that build on each other: list the complaint's claims, then draft the
        reply to them. 2-5 prompts, each an LLM call (3 tries at most), so the cost grows with them. The
        caller puts a user_task after it, where a person reviews {{output}}."""
        if not 2 <= len(prompts) <= MAX_LLM_STEPS:
            raise SpecError(f"{name}: a chain has 2 to {MAX_LLM_STEPS} prompts (one is an llm_agent)")
        if PREVIOUS.search(prompts[0]):
            raise SpecError(f"{name}: the first prompt has no {{{{previous}}}} answer")
        for i, prompt in enumerate(prompts, 1):
            answer = output if i == len(prompts) else f"{output}_{i}"
            self.llm_agent(f"{name} {i}", PREVIOUS.sub(f"{{{{{output}_{i - 1}}}}}", prompt), answer, instructions)
            if i > 1:
                self.flow(f"{name} {i - 1}", f"{name} {i}")
        self.steps[name] = (self.steps[f"{name} 1"][0], self.steps[f"{name} {len(prompts)}"][1])
        return self

    def parallel_llm(self, name, prompts, output, instructions=""):
        """Parallelisation [AE 34-35]: independent LLM agents on the same case at once, one per key of prompts
        ({key: prompt}), joined by a Merge: {{output}} holds the answers by key ({{output.key}}), each also
        {{<output>_<key>}}. For views that do not depend on each other: the medical and the legal reading of
        a claim. 2-5 prompts, each an LLM call (3 tries at most). The caller puts a user_task after it, where a
        person reviews the answers."""
        if not 2 <= len(prompts) <= MAX_LLM_STEPS or not all(re.fullmatch(r"[A-Za-z_]\w*", k) for k in prompts):
            raise SpecError(f"{name}: 2 to {MAX_LLM_STEPS} prompts, by keys that are names (letters, digits, _)")
        join, collect = f"{name}: join", f"{name}: answers"
        self.parallel_gateway(name, list(prompts))
        self.add(join, MERGE, {"mode": "combine", "combineBy": "combineByPosition", "numberInputs": len(prompts),
                               "options": {}}, bpmn=False, version=3)  # one item with every answer, not one each
        for key, prompt in prompts.items():
            self.llm_agent(f"{name}: {key}", prompt, f"{output}_{key}", instructions)
            self.flow(name, f"{name}: {key}", join, output=key)
        answers = ", ".join(f'"{key}": $json.{output}_{key}' for key in prompts)
        self.add(collect, SET, {"mode": "manual", "assignments": {"assignments": [
            {"id": self.stable_id(collect, output), "name": output, "type": "object", "value": f"={{{{ ({{ {answers} }}) }}}}"}]},
            "includeOtherFields": True, "options": {}}, bpmn=False, version=3.4)
        self.flow(join, collect)
        self.steps[name] = (name, collect)
        return self

    CRITIC = ("You are the critic of a draft. Judge it against the criteria you are given, and only those. Start "
              "your answer with PASS if it meets every one, otherwise with REVISE; then say briefly why and what "
              "to change.")

    def reflection(self, name, prompt, critique, output, max_rounds=2, instructions=""):
        """Reflection, a producer and a critic [AE 36-39], with a budget (J4): an LLM agent drafts {{output}},
        a critic agent judges the draft against `critique` (its criteria; the draft is appended) and answers
        PASS or REVISE with its reasons ({{<output>_critique}}), and the draft is redone with that critique
        until it passes or max_rounds (1-5) drafts were made. The round is counted in the case
        ({{reflection_round}}; {{passed}}: whether the critic passed the last draft). Each round is two LLM
        calls (3 tries at most each), so max_rounds caps the cost. The caller puts a user_task after it, where
        a person reviews the draft and its critique, passed or not."""
        if not 1 <= max_rounds <= MAX_LLM_STEPS:
            raise SpecError(f"{name}: max_rounds is 1 to {MAX_LLM_STEPS}")
        start, critic, verdict, gate = (f"{name}: {part}" for part in ("first round", "critic", "verdict", "again?"))
        draft, feedback = f"{{{{{output}}}}}", f"{{{{{output}_critique}}}}"
        self.add(start, SET, {"mode": "manual", "assignments": {"assignments": [  # the loop's variables, empty
            {"id": self.stable_id(start, n), "name": n, "type": t, "value": v}
            for n, t, v in ((output, "string", ""), (f"{output}_critique", "string", ""), ("reflection_round", "number", 0))]},
            "includeOtherFields": True, "options": {}}, bpmn=False, version=3.4)
        self.llm_agent(name, f"{prompt}\n\nYour previous draft, if any:\n{draft}\n\nThe critique of it, to address:\n{feedback}",
                       output, instructions)
        self.llm_agent(critic, f"{critique}\n\nThe draft:\n{draft}", f"{output}_critique", self.CRITIC)
        self.task(verdict, "scriptTask", "read_critique",
                  fields={"critique": feedback, "reflection_round": "{{reflection_round}}", "max_rounds": max_rounds})
        self.condition_gateway(gate, "exclusive", {"done": "not {{again}}", "again": "{{again}}"},
                               decision_scope="technical")  # the loop's budget, no decision about a person
        self.flow(start, name, critic, verdict, gate).flow(gate, name, output="again")
        self.steps[name] = (start, gate)
        return self

    def guardrailed_agent(self, name, prompt, output, on_block, checks=("pii",), output_checks=None, instructions=""):
        """Guardrails and safety [AE]: an llm_agent between two guardrails (see guardrail). The prompt, its
        {{variables}} filled, is checked with `checks`, and the LLM gets it with personal data replaced by
        placeholders (with "pii" among the checks, personal data blocks it instead); its answer {{output}} is
        checked with output_checks (default: checks; "schema" belongs there). A blocked text goes to a person
        in the lane on_block ("<name>: blocked") instead of going on. The caller puts a user_task after it,
        where a person reviews {{output}}."""
        before, after, blocked = f"{name}: input check", f"{name}: output check", f"{name}: blocked"
        self.guardrail(before, prompt, checks)
        self.llm_agent(name, "{{redacted}}", output, instructions)
        self.guardrail(after, f"{{{{{output}}}}}", output_checks or checks)
        self._blocked(blocked, on_block, name)
        self.flow(before, name, after).flow(before, blocked, output="blocked").flow(after, blocked, output="blocked")
        self.steps[name] = (before, self.steps[after][1])
        return self

    def node(self, name):
        return next(n for n in self.nodes if n["name"] == name)

    def flow(self, *names, output=0):
        """Sequence flow through `names`; `output` is the output the first hop leaves from: its index
        or its name (a gateway's branch, "default", "timeout", "error"; of a step's last node). A hop that
        exists already is not added again (two flows through the same steps would run them twice)."""
        if isinstance(output, str):
            labels = output_names(self.node(self.steps.get(names[0], (names[0], names[0]))[1]))
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

    def layout(self, mode="levels"):
        """Levels left to right: a step's column is its longest path from the trigger (a join comes after
        all its branches). The longest path runs straight along the top row, except a detour (a branch
        that joins its sibling later goes below, so the direct flow never passes behind it); every other branch takes the
        first row below that is free until it joins the others, so branches never overlap, and a step that
        branches join goes in the highest of their rows (the one its longest branch kept free). Flows then run right and down, as n8n draws them
        (out on the right, in on the left); only loops go back. A chat model goes under its agent.
        mode="lanes": the same columns, the rows grouped in swimlanes (see swimlanes())."""
        if mode not in ("levels", "lanes"):
            raise SpecError(f"layout mode is 'levels' or 'lanes', not {mode!r}")
        self.nodes = [n for n in self.nodes if not n["name"].startswith(BAND)]  # the bands of a previous layout
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
        agents = defaultdict(list)  # agent -> its sub-nodes (chat model, tools)
        for sub, agent in models.items():
            agents[agent].append(sub)
        busy, offered, row = set(), defaultdict(list, {n: [i] for i, n in enumerate(starts)}), {}  # (column, row)
        for name in sorted(order, key=depth.get):
            row[name] = min(offered[name])
            busy.add((depth[name], row[name]))
            for sub in agents.get(name, ()):  # its chat model and tools, in the first free rows under it
                below = row[name] + 1
                while (depth[name], below) in busy:
                    below += 1
                busy.add((depth[name], below))
                row[sub], depth[sub] = below, depth[name]
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
        return self.swimlanes(graph) if mode == "lanes" else self

    def swimlanes(self, graph):
        """Rows grouped by lane, in graph.lanes' order ([WM] A51: the customer, the people's lanes, the systems
        last), each lane keeping its steps' rows from the level layout, and a sticky note behind each lane: its
        band, named "Lane: <lane>", in a colour of its own, the lane's name on it. The columns do not move."""
        at, top, bands = {n["name"]: n for n in self.nodes}, 0, []
        for lane, names in graph.lanes.items():
            rows = sorted({at[n]["position"][1] for n in names})
            for n in names:
                at[n]["position"][1] = (top + rows.index(at[n]["position"][1])) * 200
            bands.append((lane, top, len(rows)))
            top += len(rows)
        xs = [at[n]["position"][0] for n in graph.nodes] or [0]
        for i, (lane, first, rows) in enumerate(bands):  # a node is 100×100: 50 around it, 60 at the sides
            self.nodes.append({"id": self.stable_id(BAND, lane), "name": BAND + lane, "type": STICKY, "typeVersion": 1,
                               "position": [min(xs) - 60, first * 200 - 50],
                               "parameters": {"content": f"## {lane}", "width": max(xs) - min(xs) + 220,
                                              "height": rows * 200, "color": i % 7 + 1}})
        return self

    @classmethod
    def from_spec(cls, spec, credential=None, chat_model=None):
        """A workflow from a JSON spec (what bpmn8n Studio's LLM writes), laid out automatically:
        {"name": ..., "steps": [{"type": <one of STEPS>, "name": ..., <that method's arguments>}],
         "flows": [{"from": <step>, "branch": <output name, for gateways>, "to": [<step>, ...]}],
         "layout": "levels" (the default) or "lanes"}"""
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
        return workflow.layout(spec.get("layout") or "levels")

    def as_json(self):
        # v1 = what n8n's editor uses: a join runs once no branch can still deliver (inclusive join);
        # the legacy v0 forced every branch upstream of a Merge to run, even those a gateway skipped
        return {"name": self.name, "nodes": self.nodes, "connections": self.connections,
                "settings": {"executionOrder": "v1"}}
