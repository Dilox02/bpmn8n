"""Checks before anything runs. WorkflowCheck: an n8n workflow of bpmn8n nodes, for what n8n would
reveal only when a case gets there (a task that does not exist, a branch leading nowhere, a lane
nobody is in...). ProjectCheck: a project folder, its activities run against their examples too.

Errors will fail; warnings are probably not what was meant; infos are worth knowing. Structural rules, not PM4Py's
soundness check: that one rejects correct n8n workflows (a Merge is an inclusive join, parallel
branches need no join)."""
import json
from collections import deque
from dataclasses import asdict, dataclass
from functools import cached_property
from importlib import import_module
from pathlib import Path

from django.contrib.auth.models import Group

from ..api.tasks import TASKS, discover
from ..core.variables import TEMPLATE, Condition, ConditionError
from ..clients import n8n
from .builder import AGENT, BRANCHING, MERGE, SET, TASK_KINDS, node_type, output_names

TASK_NODES = {node_type(kind): kind for kind in TASK_KINDS}  # node type -> @task kind
HUMAN = (node_type("userTask"), node_type("manualTask"))
WAITING = (*HUMAN, node_type("receiveTask"), node_type("eventBasedGateway"))
TRANSPARENT = (*BRANCHING, node_type("timerEvent"), MERGE)  # pass data on, produce none
STICKY = "n8n-nodes-base.stickyNote"
DECIDING = (node_type("exclusiveGateway"), node_type("inclusiveGateway"), AGENT)  # a model or a rule chooses


@dataclass(frozen=True)
class Problem:
    node: str
    rule: str
    message: str
    level: str = "error"

    def as_dict(self):
        return asdict(self)


def is_trigger(node):
    kind = node["type"].rsplit(".", 1)[-1].lower()
    return kind.endswith("trigger") or kind in ("webhook", "start")


class WorkflowCheck:
    """`fetch(workflow id)`: another workflow (a Call Activity's sub-process), None if unknown.
    `start_data`: the fields of the data that starts a case (the webhook's body), when known (Studio
    knows them from the interview): then the variables nothing provides are errors."""

    def __init__(self, workflow, fetch=n8n.workflow, start_data=None):
        self.fetch, self.start_data = fetch, None if start_data is None else set(start_data)
        self.nodes = {n["name"]: n for n in workflow.get("nodes", []) if not n.get("disabled") and n["type"] != STICKY}
        self.outs = {name: [[c["node"] for c in port or [] if c["node"] in self.nodes]
                            for port in workflow.get("connections", {}).get(name, {}).get("main", [])]
                     for name in self.nodes}
        self.into = {name: {src for src, ports in self.outs.items() for port in ports if name in port}
                     for name in self.nodes}
        # sub-nodes (an agent's chat model) connect to their node, not in the flow
        self.subnodes = {name for name, outs in workflow.get("connections", {}).items() if "main" not in outs}

    @cached_property
    def problems(self):
        rules = (self.has_trigger, self.reachable, self.tasks_exist, self.outputs_connected, self.conditions_valid,
                 self.lanes_exist, self.subprocesses_run_through, self.data_provided, self.people_decide,
                 self.one_wait_at_a_time)
        return [problem for rule in rules for problem in rule()]

    @property
    def errors(self):
        return [p for p in self.problems if p.level == "error"]

    def of_type(self, *types):
        return [(name, node) for name, node in self.nodes.items() if node["type"] in types]

    # --- rules ----------------------------------------------------------------------------

    def has_trigger(self):
        if all(map(is_trigger, self.nodes.values())):  # no node, or only the trigger
            yield Problem("", "empty", "the workflow does nothing: add the process's steps after its trigger")
        elif not any(map(is_trigger, self.nodes.values())):
            yield Problem("", "trigger", "no trigger: add a webhook (or another trigger) that starts the case", "warning")

    def reachable(self):
        for name, node in self.nodes.items():
            if not self.into[name] and not is_trigger(node) and name not in self.subnodes:
                yield Problem(name, "reachable", "nothing leads here and it is not a trigger: it never runs")

    def tasks_exist(self):
        for name, node in self.of_type(*TASK_NODES):
            endpoint = str(node["parameters"].get("task", "")).rsplit("/", 1)[-1]
            kind = TASK_NODES[node["type"]]
            if not (task := TASKS.get(endpoint)):
                yield Problem(name, "task", f"no @task {endpoint!r}: define it in an app's tasks.py")
            elif task.kind != kind:
                yield Problem(name, "task", f"{endpoint!r} is a {task.kind}, not a {kind}: this node does not list it")

    def outputs_connected(self):
        for name, node in self.of_type(*BRANCHING):
            ports, by_conditions = self.outs[name], node["parameters"].get("decider") == "conditions"
            for i, label in enumerate(output_names(node)):
                if label == "default" and by_conditions:
                    continue  # conditions often cover every case; Laya's "default" must lead to a person
                if not (ports[i] if i < len(ports) else []):
                    yield Problem(name, "output", f"output {label!r} leads nowhere: the items taking it end the case",
                                  "warning")

    def conditions_valid(self):
        for name, node in self.nodes.items():
            if node["parameters"].get("decider") != "conditions":
                continue
            for branch in node["parameters"].get("branches", {}).get("values", []):
                condition = branch.get("condition", "")
                if condition in ("", None):
                    yield Problem(name, "condition", f"branch {branch['name']!r} has no condition: never taken", "warning")
                elif isinstance(condition, str) and not condition.startswith("="):  # "=...": n8n evaluates it
                    try:
                        Condition.parse(condition)
                    except ConditionError as e:
                        yield Problem(name, "condition", f"branch {branch['name']!r}: {e}")

    def lanes_exist(self):
        lanes = set(Group.objects.values_list("name", flat=True))
        for name, node in self.of_type(*HUMAN):
            if (lane := node["parameters"].get("lane", "")) and lane not in lanes:
                yield Problem(name, "lane", f"no group {lane!r}: only superusers will see the task (add it in /admin/)",
                              "warning")

    def subprocesses_run_through(self):
        for name, node in self.of_type(node_type("callActivity")):
            target = node["parameters"].get("workflowId", "")
            workflow_id = target.get("value") if isinstance(target, dict) else target
            if (sub := self.fetch(workflow_id)) is None:
                yield Problem(name, "subprocess", f"cannot read sub-process {workflow_id!r} (N8N_API_KEY): "
                                                  "make sure it does not wait", "warning")
            elif waits := [n["name"] for n in sub.get("nodes", []) if n["type"] in WAITING]:
                yield Problem(name, "subprocess", f"the sub-process waits at {', '.join(waits)}: a Call Activity runs "
                                                  "it in one go, so put the waits in this workflow")

    def data_provided(self):
        """Whether what each node reads exists by then: the required inputs of a task that its node
        does not set, and the {{variables}} its fields use, against what earlier steps produce. Nodes
        after a step whose output is unknown (no output Schema, a message) are left out. Without the
        start data, the inputs it must carry are listed instead (info)."""
        needed = set()
        for name, node in self.nodes.items():
            if (produced := self.produced_before(name)) is None:
                continue
            available = produced | (self.start_data or set())
            params = node["parameters"]
            task = TASKS.get(str(params.get("task", "")).rsplit("/", 1)[-1]) if node["type"] in TASK_NODES else None
            if task and params.get("inputFrom") != "text":
                given = {k for k, v in ((params.get("input") or {}).get("value") or {}).items() if v not in ("", None)}
                missing = {f.name for f in task.inputs if f.required} - given - available
                needed |= missing
                if self.start_data is not None and missing:
                    yield Problem(name, "input", f"no earlier step nor the start data provides {', '.join(sorted(missing))}: "
                                                 "set it on the node or produce it before")
            used = {path.split(".")[0] for path in TEMPLATE.findall(json.dumps(params))}
            if self.start_data is not None and (unknown := used - available):
                yield Problem(name, "variable", f"{', '.join('{{%s}}' % v for v in sorted(unknown))}: no earlier step "
                                                "nor the start data provides it, so it would be empty")
        if self.start_data is None and needed:
            yield Problem("", "start-data", f"the data that starts a case must carry: {', '.join(sorted(needed))}",
                          "info")

    def people_decide(self):
        """A person takes the final decision (GDPR art. 22: no decision based solely on automated
        processing): after an LLM agent or a gateway, every path to an end meets a user or manual task.
        A condition gateway that reads only form fields applies a person's decision. An error for LLM
        agents, a warning for gateways (bpmn8n Studio treats both as errors)."""
        for name, node in self.of_type(*DECIDING):
            if node["parameters"].get("decider") == "conditions" and self.reads_only_forms(name, node):
                continue
            if end := self.end_without_person(name):
                what = "the LLM agent's answer" if node["type"] == AGENT else "this automatic decision"
                yield Problem(name, "human", f"{what} reaches {end!r}, an end, without a person deciding: "
                                             "put a user task on the way", "error" if node["type"] == AGENT else "warning")

    def one_wait_at_a_time(self):
        """n8n parks an execution on one wait at a time: when branches run at once (a parallel or inclusive
        gateway, or one output leading to several steps), at most one of them may wait (a person, a message,
        a long timer). Otherwise the case resumes only when the last wait ends, whatever the others got:
        a person's answer waits for a timer months away."""
        for name, node in self.nodes.items():
            concurrent = node["type"] in (node_type("parallelGateway"), node_type("inclusiveGateway"))
            starts = [n for port in self.outs[name] for n in port] if concurrent else \
                next((port for port in self.outs[name] if len(port) > 1), [])
            if len(starts) < 2:
                continue
            reach = [self.after(start) for start in starts]
            shared = set.intersection(*reach)  # after the branches join
            waiting = [sorted(n for n in r - shared if self.waits(n)) for r in reach]
            if sum(map(bool, waiting)) > 1:
                yield Problem(name, "wait", "these branches run at once and more than one waits (" +
                              "; ".join(", ".join(w) for w in waiting if w) + "): n8n waits on one thing at a "
                              "time, so the case would stall until the last wait ends. Keep the waits in one branch "
                              "(a deadline: an event-based gateway's timeout) or start the other wait as its own process")

    def after(self, start):
        """`start` and every step it leads to."""
        seen, queue = {start}, deque([start])
        while queue:
            for n in {n for port in self.outs[queue.popleft()] for n in port} - seen:
                seen.add(n)
                queue.append(n)
        return seen

    def waits(self, name):
        """Whether the step parks the execution: a person, a message, a timer longer than a minute."""
        node = self.nodes[name]
        if node["type"] in WAITING:
            return True
        if node["type"] != node_type("timerEvent"):
            return False
        p = node["parameters"]
        seconds = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800}
        return p.get("mode") == "until" or float(p.get("amount") or 0) * seconds.get(p.get("unit", "hours"), 3600) > 60

    def end_without_person(self, name):
        """The first end reached from `name` without passing a user or manual task, or None."""
        seen, queue = {name}, deque([name])
        while queue:
            current = queue.popleft()
            following = {n for port in self.outs[current] for n in port}
            if not following:
                return current
            for n in following - seen:
                seen.add(n)
                if self.nodes[n]["type"] not in HUMAN:
                    queue.append(n)
        return None

    def reads_only_forms(self, name, node):
        """Whether a condition gateway reads only fields of the forms people filled in before it."""
        used = {path.split(".")[0] for path in TEMPLATE.findall(json.dumps(node["parameters"]))}
        forms, seen, queue = set(), {name}, deque(self.into[name])
        while queue:
            current = queue.popleft()
            if current not in seen:
                seen.add(current)
                queue.extend(self.into[current])
                if self.nodes[current]["type"] in HUMAN:
                    forms |= {f["name"] for f in self.nodes[current]["parameters"].get("form", {}).get("fields", [])}
        return bool(used) and used <= forms

    def produced_before(self, name):
        """The variables the steps before `name` produce; None when one of them is unknown."""
        produced, seen, queue = set(), {name}, deque(self.into[name])
        while queue:
            node = self.nodes[current := queue.popleft()]
            if current in seen:
                continue
            seen.add(current)
            queue.extend(self.into[current])
            task = TASKS.get(str(node["parameters"].get("task", "")).rsplit("/", 1)[-1])
            if node["type"] in TASK_NODES and task and task.output:
                produced |= {f.name for f in task.outputs}
            elif node["type"] in HUMAN:
                produced |= {f["name"] for f in node["parameters"].get("form", {}).get("fields", [])}
            elif node["type"] == SET:  # an LLM agent's answer, as its variable
                produced |= {a["name"] for a in node["parameters"].get("assignments", {}).get("assignments", [])}
            elif node["type"] == AGENT:
                produced.add("output")
            elif not (is_trigger(node) or node["type"] in TRANSPARENT):
                return None
        return produced


class ProjectCheck:
    """A project folder (projects/<name>/) before it is loaded: every module that registers @tasks
    imports; each activity does what its examples say (examples.json: {task: [{"input": {...},
    "output": {the fields expected}}]}); the workflow (workflow.json) passes WorkflowCheck, with the
    start data of process.py's SPEC. Until its modules import, the rest is not checked; another
    project that fails to import is left out, as the server does (it is logged)."""

    def __init__(self, folder, fetch=n8n.workflow):
        self.folder = Path(folder)
        package = f"{self.folder.parent.name}.{self.folder.name}."
        # module -> the end of its traceback, for the project's modules that fail
        self.imports = {module: error for module, error in discover().items() if module.startswith(package)}
        self.examples = {} if self.imports else self._run_examples()
        self.problems = [] if self.imports else self._check_workflow(fetch)

    @property
    def ok(self):
        return not (self.imports or self.examples or any(p.level == "error" for p in self.problems))

    def as_dict(self):
        return {"imports": self.imports, "examples": self.examples, "problems": [p.as_dict() for p in self.problems]}

    def _run_examples(self):
        """task -> what its failing examples got wrong."""
        path = self.folder / "examples.json"
        failed = {}
        for name, examples in (json.loads(path.read_text()) if path.exists() else {}).items():
            task = TASKS.get(name)
            if problems := [f"no @task {name!r}"] if task is None else [p for e in examples if (p := task.check(e))]:
                failed[name] = problems
        return failed

    def _check_workflow(self, fetch):
        path = self.folder / "workflow.json"
        if not path.exists():
            return []
        try:
            start_data = import_module(f"{self.folder.parent.name}.{self.folder.name}.process").SPEC.get("start_data")
        except (ImportError, AttributeError):  # no process.py, or not a spec: the start data is unknown
            start_data = None
        return WorkflowCheck(json.loads(path.read_text()), fetch, start_data).problems
