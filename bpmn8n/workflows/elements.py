"""The BPMN elements a bpmn8n workflow is made of, and what each one is: the one place that says it.

Every n8n node type that can appear in a bpmn8n workflow has an `Element`: its BPMN kind, category,
label, glyph and the traits the builder, the workflow check, process mining, the simulator and the
editor hooks read (does it wait? does a person do it? does a model or rule choose the path? does it
pass data on without producing any?). n8n's own nodes that play a BPMN role (webhook = message start,
Merge = the join of parallel and inclusive branches, the AI Agent = an LLM task) are elements too.
Pure Python, no Django: `manage.py elements` and `/api/elements` publish it as JSON for the editor."""
from dataclasses import asdict, dataclass

NODE = "CUSTOM.bpmn8n"  # bpmn8n's own n8n node types: CUSTOM.bpmn8n<Kind>
TASK_KINDS = ("serviceTask", "scriptTask", "businessRuleTask", "sendTask")  # BPMN tasks run by a @task, one node each


def node_type(kind):
    """The n8n type of a bpmn8n node, from its BPMN element: "serviceTask" -> "CUSTOM.bpmn8nServiceTask"."""
    return NODE + kind[0].upper() + kind[1:]


MERGE = "n8n-nodes-base.merge"
AGENT = "@n8n/n8n-nodes-langchain.agent"
SET = "n8n-nodes-base.set"
NOOP = "n8n-nodes-base.noOp"
STICKY = "n8n-nodes-base.stickyNote"
WEBHOOK = "n8n-nodes-base.webhook"


@dataclass(frozen=True)
class Element:
    """What a node is in BPMN. `category`: task | gateway | event | annotation | other.
    Traits (all default False):
      task          runs a @task (`TASK_KINDS`): the node's menu lists the tasks of this kind
      human         a person works it (user or manual task): an inbox entry, the case waits
      waits         parks the execution until something outside arrives (people, a message)
      decides       a model or a rule chooses the path, so a person must review what follows (GDPR art. 22)
      chooses       takes a subset of its outputs (exclusive, inclusive, event-based): a choice, not a split
      splits        every output runs (parallel gateway)
      transparent   passes the data it receives on, produces none
      joins         waits for the inputs that can still deliver, then runs once (n8n's Merge)
      logged        writes an Event, so it is a visible activity of the process-mining log
      ends          has no outputs: the path ends here
      trigger       starts a case
    `default_last` / `timeout_first`: the extra output after the branches ("default") or before them ("timeout")."""

    kind: str
    n8n_type: str
    category: str
    label: str
    summary: str = ""
    symbol: str = ""  # bpmn-font glyph under n8n-nodes-bpmn8n/nodes/icons/bpmn/, e.g. "gateways/gateway-xor"
    task: bool = False
    human: bool = False
    waits: bool = False
    decides: bool = False
    chooses: bool = False
    splits: bool = False
    transparent: bool = False
    joins: bool = False
    logged: bool = False
    ends: bool = False
    trigger: bool = False
    default_last: bool = False
    timeout_first: bool = False

    @property
    def bpmn8n(self):
        """One of bpmn8n's own nodes (not an n8n node that plays a BPMN role)."""
        return self.n8n_type.startswith(NODE)

    @property
    def branching(self):
        """Its outputs are the configured branches (plus "default" or "timeout")."""
        return self.chooses or self.splits

    def outputs(self, parameters):
        """The node's outputs in order: a gateway's branches (with "default" or "timeout"); a human task with
        an interrupting deadline (a boundary timer) "timeout" then "done"; else ["main"]."""
        if self.human and (parameters or {}).get("deadline") and parameters.get("due", "none") != "none":
            return ["timeout", "done"]  # "timeout" first: n8n times a wait out through output 0 (WaitingNode.ts)
        if not self.branching:
            return ["main"]
        branches = [b["name"] for b in (parameters or {}).get("branches", {}).get("values", [])]
        if self.default_last:
            return [*branches, "default"]
        if self.timeout_first:
            return ["timeout", *branches]
        return branches

    def as_dict(self):
        return {**asdict(self), "bpmn8n": self.bpmn8n, "branching": self.branching}


def _bpmn8n(kind, category, label, summary, symbol, **traits):
    return Element(kind, node_type(kind), category, label, summary, symbol, **traits)


_TASK_GLYPH = {"serviceTask": "service", "scriptTask": "script", "businessRuleTask": "business-rule",
               "sendTask": "send"}
_TASK_SUMMARY = {"serviceTask": "Software calls a service: a Python function on bpmn8n.",
                 "scriptTask": "Software transforms data: a Python function on bpmn8n.",
                 "businessRuleTask": "Software applies a policy: a Python function on bpmn8n.",
                 "sendTask": "Software notifies someone: a Python function on bpmn8n."}

ELEMENTS = {e.n8n_type: e for e in (
    *(_bpmn8n(kind, "task", label, _TASK_SUMMARY[kind], f"activities/{_TASK_GLYPH[kind]}-task", task=True, logged=True)
      for kind, label in zip(TASK_KINDS, ("Service task", "Script task", "Business rule task", "Send task"))),
    _bpmn8n("receiveTask", "task", "Receive task", "The case waits here for a message.", "activities/receive-task",
            waits=True, logged=True),
    _bpmn8n("userTask", "task", "User task", "A person fills in a form in the bpmn8n inbox; the case waits.",
            "activities/user-task", human=True, waits=True, logged=True),
    _bpmn8n("manualTask", "task", "Manual task", "A person does it and confirms it in the bpmn8n inbox; the case waits.",
            "activities/manual-task", human=True, waits=True, logged=True),
    _bpmn8n("callActivity", "task", "Call activity", "Runs another workflow as a sub-process.", "activities/call-activity",
            logged=True),  # a running event while the sub-process works, finished when it returns
    _bpmn8n("exclusiveGateway", "gateway", "Exclusive gateway", "The case takes one branch.", "gateways/gateway-xor",
            decides=True, chooses=True, transparent=True, logged=True, default_last=True),
    _bpmn8n("inclusiveGateway", "gateway", "Inclusive gateway", "The case takes every branch that applies.",
            "gateways/gateway-or", decides=True, chooses=True, transparent=True, logged=True, default_last=True),
    _bpmn8n("parallelGateway", "gateway", "Parallel gateway", "The case takes every branch at once.",
            "gateways/gateway-parallel", splits=True, transparent=True),
    _bpmn8n("eventBasedGateway", "gateway", "Event-based gateway",
            "The first message (or the timeout) decides the branch.", "gateways/gateway-eventbased",
            waits=True, chooses=True, transparent=True, logged=True, timeout_first=True),
    _bpmn8n("timerEvent", "event", "Timer event", "The case waits for a time.", "events/intermediate-event-catch-timer",
            transparent=True),
    _bpmn8n("errorEndEvent", "event", "Error end event", "The case ends with an error.", "events/end-event-error",
            logged=True, ends=True),
    _bpmn8n("terminateEndEvent", "event", "Terminate end event", "The case ends here and its other branches stop.",
            "events/end-event-terminate", logged=True, ends=True),
    _bpmn8n("signalCatch", "event", "Signal catch event",
            "The case waits for a named signal; one throw resumes every case waiting for it.",
            "events/intermediate-event-catch-signal", waits=True, logged=True),
    _bpmn8n("metrics", "other", "Metrics",
            "Records configured measurements in the project run folder.", "", transparent=True),
    # a task, not a data store: it sits on the sequence flow and runs like a script task; the data-store
    # glyph is its marker. It produces data (the value under its key), so it is not transparent.
    _bpmn8n("globalVariable", "task", "Global variable",
            "Reads or writes a variable every workflow shares ({{vars.<key>}}).", "data/data-store"),
    Element("start", WEBHOOK, "event", "Message start event", "A request starts the case.",
            "events/start-event-message", trigger=True),
    Element("formStart", "n8n-nodes-base.formTrigger", "event", "Message start event", "A form starts the case.",
            "events/start-event-message", trigger=True),
    Element("timerStart", "n8n-nodes-base.scheduleTrigger", "event", "Timer start event", "A schedule starts the case.",
            "events/start-event-timer", trigger=True),
    Element("subprocessStart", "n8n-nodes-base.executeWorkflowTrigger", "event", "Start event",
            "A Call Activity starts this sub-process.", "events/start-event-none", trigger=True),
    Element("merge", MERGE, "gateway", "Join", "Waits for the branches that can still arrive, then goes on once.",
            "gateways/gateway-or", joins=True, transparent=True),
    Element("llmAgent", AGENT, "task", "LLM agent", "An LLM drafts text that needs judgement; a person reviews it.",
            "activities/task-none", decides=True),
    Element("set", SET, "task", "Set data", "Sets fields of the item.", "activities/script-task"),
    Element("noOp", NOOP, "other", "No operation", "Passes the items on.", "", transparent=True),
    Element("annotation", STICKY, "annotation", "Text annotation", "A note on the canvas.", "text-annotation"),
)}

GENERIC = Element("task", "", "task", "Task", "Any other n8n node: a task.", "activities/task-none")


def element(node_or_type):
    """The Element of a node (an n8n node dict) or of an n8n type; any unknown node is a generic task,
    and any node whose type ends in "Trigger" is a start event."""
    n8n_type = node_or_type.get("type", "") if isinstance(node_or_type, dict) else node_or_type
    if found := ELEMENTS.get(n8n_type):
        return found
    if n8n_type.rsplit(".", 1)[-1].lower().endswith("trigger"):
        return Element("start", n8n_type, "event", "Start event", "Starts the case.", "events/start-event-none",
                       trigger=True)
    return Element(GENERIC.kind, n8n_type, GENERIC.category, GENERIC.label, GENERIC.summary, GENERIC.symbol)


def types(**traits):
    """The n8n types of the elements with all these traits, e.g. types(human=True)."""
    return frozenset(t for t, e in ELEMENTS.items() if all(getattr(e, k) == v for k, v in traits.items()))


def manifest():
    """Every element as JSON, for the editor hooks and the API."""
    return {"node_prefix": NODE, "task_kinds": list(TASK_KINDS),
            "elements": [e.as_dict() for e in ELEMENTS.values()]}


def demo():
    """Self-check: python -c 'import bpmn8n.workflows.elements as e; e.demo()'"""
    assert element({"type": node_type("userTask")}).human and element(node_type("userTask")).waits
    assert element("n8n-nodes-base.whatever").kind == "task"
    assert element("n8n-nodes-base.manualTrigger").trigger
    xor = element(node_type("exclusiveGateway"))
    assert xor.outputs({"branches": {"values": [{"name": "a"}, {"name": "b"}]}}) == ["a", "b", "default"]
    assert element(node_type("eventBasedGateway")).outputs({"branches": {"values": [{"name": "m"}]}}) == ["timeout", "m"]
    assert element(node_type("serviceTask")).outputs({}) == ["main"]
    assert types(task=True) == {node_type(k) for k in TASK_KINDS}
