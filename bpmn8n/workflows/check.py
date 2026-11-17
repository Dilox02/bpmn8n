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
NOOP = "n8n-nodes-base.noOp"
TRANSPARENT = (*BRANCHING, node_type("timerEvent"), node_type("metrics"), MERGE, NOOP)  # pass data on, produce none
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
        self.connections = workflow.get("connections", {})
        self.node_names = {n["name"] for n in workflow.get("nodes", [])}
        # n8n executes a disabled node as a pass-through on output 0, not a missing step.
        self.nodes = {n["name"]: {**n, "type": NOOP, "parameters": {}} if n.get("disabled") else n
                      for n in workflow.get("nodes", []) if n["type"] != STICKY}
        self.outs = {name: [[c["node"] for c in port or [] if c["node"] in self.nodes]
                            for port in self.connections.get(name, {}).get("main", [])[:1 if node.get("disabled") else None]]
                     for name, node in self.nodes.items()}
        self.into = {name: {src for src, ports in self.outs.items() for port in ports if name in port}
                     for name in self.nodes}
        # sub-nodes (an agent's chat model) connect to their node, not in the flow
        self.subnodes = {name for name, outs in self.connections.items() if "main" not in outs}

    @cached_property
    def problems(self):
        rules = (self.has_trigger, self.connections_valid, self.reachable, self.tasks_exist, self.outputs_connected,
                 self.conditions_valid, self.lanes_exist, self.subprocesses_run_through, self.data_provided,
                 self.people_decide, self.one_wait_at_a_time, self.metrics_decide)
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
        starts = [name for name, node in self.nodes.items() if is_trigger(node)]
        reached = set().union(*(self.after(name) for name in starts)) if starts else None
        for name, node in self.nodes.items():
            if name not in self.subnodes and (name not in reached if reached is not None else not self.into[name]):
                yield Problem(name, "reachable", "no path from a trigger leads here: it never runs")

    def connections_valid(self):
        for source, kinds in self.connections.items():
            if source not in self.node_names:
                yield Problem(source, "connection", "connection starts at a node that does not exist")
            for ports in kinds.values():
                for port in ports:
                    for link in port or []:
                        if link["node"] not in self.node_names:
                            yield Problem(source, "connection", f"connection leads to missing node {link['node']!r}")

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
        does not set, and the {{variables}} its fields use, against what earlier steps produce.
        An unknown output (no Schema, a message) cannot disprove a field, but a known alternative
        path can. Without the start data, the inputs it must carry are listed instead (info)."""
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
            used, optional = self.references(params), set()
            if node["type"] in HUMAN:
                context = params.get("context")
                if isinstance(context, str):  # HumanTaskNode accepts an object or JSON text.
                    try:
                        context = json.loads(context or "{}")
                    except ValueError:
                        pass
                if isinstance(context, dict):
                    context_used, optional = self.context_references(context)
                    used = self.references({k: v for k, v in params.items() if k != "context"}) | context_used
            if self.start_data is not None and (unknown := used - available):
                yield Problem(name, "variable", f"{', '.join('{{%s}}' % v for v in sorted(unknown))}: no earlier step "
                                                "nor the start data provides it, so it would be empty")
            if self.start_data is not None and (unknown := optional - used - available):
                yield Problem(name, "variable", f"{', '.join('{{%s}}' % v for v in sorted(unknown))}: optional human "
                                                "context entries may be omitted because no earlier step nor the "
                                                "start data provides them", "warning")
        if self.start_data is None and needed:
            yield Problem("", "start-data", f"the data that starts a case must carry: {', '.join(sorted(needed))}",
                          "info")

    def people_decide(self):
        """A person takes the final decision (GDPR art. 22: no decision based solely on automated
        processing): after an LLM agent or a gateway, every path to an end meets a user or manual task.
        A condition gateway that reads only form fields applies a person's decision. An error for LLM
        agents, a warning for gateways (bpmn8n Studio treats both as errors)."""
        for name, node in self.of_type(*DECIDING):
            params = node["parameters"]
            # Technical loops route counters/files; this explicit scope never exempts a model's decision.
            if node["type"] != AGENT and params.get("decider") == "conditions" and \
                    params.get("decisionScope") == "technical":
                continue
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

    def metrics_decide(self):
        """A measurement earns its node when it can change something: it comes after a step that produced
        what it measures, it has a target to tell good from bad, and missing the target can act on the flow."""
        for name, node in self.of_type(node_type("metrics")):
            values = node["parameters"].get("metrics", {}).get("values", [])
            seen, queue, produced = {name}, deque(self.into[name]), False
            while queue and not produced:  # back through pass-through steps to whatever made the data
                current = queue.popleft()
                if current in seen:
                    continue
                seen.add(current)
                if not is_trigger(self.nodes[current]) and self.nodes[current]["type"] not in TRANSPARENT:
                    produced = True
                queue.extend(self.into[current])
            if not produced:
                yield Problem(name, "metrics", "nothing has been produced yet: measure after the step whose "
                                               "result matters", "warning")
            if not any(v.get("checkThreshold") for v in values):
                yield Problem(name, "metrics", "no threshold: the values are recorded but nothing tells good from "
                                               "bad; give each metric the target that decides, or drop it", "warning")
            elif not any(self.nodes[n]["parameters"].get("decider") == "conditions" for n in self.after(name) - {name}):
                yield Problem(name, "metrics", "missing a threshold changes nothing: add a condition gateway on the "
                                               "same values if the run should stop or take another path", "info")

    def after(self, start, stop=None):
        """`start` and every step it leads to."""
        seen, queue = {start}, deque([start])
        while queue:
            current = queue.popleft()
            if current == stop:
                continue
            for n in {n for port in self.outs[current] for n in port} - seen:
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
        """Whether every path supplies a human answer that no later automated step overwrites."""
        used = self.references(node["parameters"])
        return bool(used) and used <= (self.form_data[name] or set())

    @staticmethod
    def references(value):
        return {path.split(".")[0] for path in TEMPLATE.findall(json.dumps(value))}

    def context_references(self, value):
        """render() omits missing lone-template entries; embedded text still needs its variables."""
        if isinstance(value, (dict, list)):
            children = value.values() if isinstance(value, dict) else value
            references = [self.context_references(child) for child in children]
            return (set().union(*(required for required, _ in references)),
                    set().union(*(optional for _, optional in references)))
        used = self.references(value)
        return (set(), used) if isinstance(value, str) and TEMPLATE.fullmatch(value.strip()) else (used, set())

    def produced_before(self, name):
        """Fields available on every path to this node; None means an unknown output schema."""
        return self.available_data[name]

    def produced_by(self, node):
        task = TASKS.get(str(node["parameters"].get("task", "")).rsplit("/", 1)[-1])
        if node["type"] in TASK_NODES and task and task.output:
            return {f.name for f in task.outputs}
        if node["type"] in HUMAN:
            return {f["name"] for f in node["parameters"].get("form", {}).get("fields", [])}
        if node["type"] == SET:
            return {a["name"] for a in node["parameters"].get("assignments", {}).get("assignments", [])}
        if node["type"] == AGENT:
            return {"output"}
        return set() if is_trigger(node) or node["type"] in TRANSPARENT else None

    @staticmethod
    def common_data(values):
        """An unknown schema cannot disprove a field, but a known alternative can."""
        known = [value for value in values if value is not None]
        return set.intersection(*known) if known else None

    @cached_property
    def dominators(self):
        """Steps that every path to a node passes: only a guaranteed fork can justify a join's union."""
        starts = {name for name, node in self.nodes.items() if is_trigger(node) or not self.into[name]}
        reached = set().union(*(self.after(name) for name in starts)) if starts else set()
        dom = {name: {name} if name in starts or name not in reached else set(reached) for name in self.nodes}
        changed = True
        while changed:
            changed = False
            for name in reached - starts:
                parents = self.into[name] & reached
                value = {name} | set.intersection(*(dom[parent] for parent in parents))
                if value != dom[name]:
                    dom[name], changed = value, True
        return dom

    def joined_data(self, name, inputs, used=frozenset()):
        """Merge waits for parallel branches, but their internal alternatives remain alternatives."""
        common = set.intersection(*(self.dominators[source] for source, _, _ in inputs)) - used
        for fork in sorted(common, key=lambda n: len(self.dominators[n]), reverse=True):
            ports = self.outs[fork]
            branches = [[(i, n) for n in port] for i, port in enumerate(ports)]
            if self.nodes[fork]["type"] == node_type("parallelGateway"):
                branches = [[start for branch in branches for start in branch]]
            for starts in branches:
                if len(starts) < 2:
                    continue
                reach = [self.after(start, stop=name) for _, start in starts]
                membership = [[i for i, ((port, start), branch) in enumerate(zip(starts, reach))
                               if source in branch or source == fork and source_port == port and start == name]
                              for source, source_port, _ in inputs]
                active = {i for i, branch in enumerate(reach) if name in branch}
                if len(active) < 2 or any(len(b) != 1 for b in membership) or {b[0] for b in membership} != active:
                    continue
                groups = [[entry for entry, b in zip(inputs, membership) if b == [i]] for i in sorted(active)]
                values = [self.joined_data(name, group, used | {fork}) for group in groups]
                return None if any(value is None for value in values) else set().union(*values)
        return self.common_data([value for _, _, value in inputs])

    @cached_property
    def available_data(self):
        return self.data_before()

    @cached_property
    def form_data(self):
        return self.data_before(forms_only=True)

    def data_before(self, forms_only=False):
        # Start at the unknown top value, then intersect until loops no longer change it. This never
        # lets a later loop iteration provide data that is missing on the first entry.
        before = dict.fromkeys(self.nodes)
        after = dict.fromkeys(self.nodes)
        incoming = {name: [(source, i) for source, ports in self.outs.items() for i, port in enumerate(ports)
                           if name in port] for name in self.nodes}
        produced = {name: self.produced_by(node) for name, node in self.nodes.items()}
        changed = True
        while changed:
            changed = False
            for name, node in self.nodes.items():
                inputs = [(source, port, (set() if forms_only else None)
                           if port and self.nodes[source]["type"] == node_type("eventBasedGateway")
                           else after[source]) for source, port in incoming[name]]
                value = set() if is_trigger(node) or not inputs else self.joined_data(name, inputs) \
                    if node["type"] == MERGE and not forms_only else self.common_data([value for _, _, value in inputs])
                if forms_only:
                    # At a join, require trusted provenance on every input. A field answered in only
                    # one parallel branch may be overwritten by its sibling; review after the join.
                    output = set() if produced[name] is None else None if value is None else \
                        value - produced[name] | (produced[name] if node["type"] in HUMAN else set())
                else:
                    output = None if value is None or produced[name] is None else value | produced[name]
                if value != before[name] or output != after[name]:
                    before[name], after[name], changed = value, output, True
        return before


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
