"""Checks before anything runs. WorkflowCheck: an n8n workflow of bpmn8n nodes, for what n8n would
reveal only when a case gets there (a task that does not exist, a branch leading nowhere, a lane
nobody is in...). ProjectCheck: a project folder, its activities run against their examples too.

Errors will fail; warnings are probably not what was meant; infos are worth knowing. The rules are
classes in rules.py; WorkflowCheck is what they read: the workflow's ProcessGraph and its data flow.
Structural rules, not PM4Py's soundness check: that one rejects correct n8n workflows (a Merge is an
inclusive join, parallel branches need no join)."""
import json
from collections import deque
from functools import cached_property
from importlib import import_module
from pathlib import Path

from ..api.tasks import TASKS, discover
from ..clients import n8n
from ..core.variables import TEMPLATE
from .builder import ROUNDS_LEFT
from .graph import ProcessGraph
from .rules import RULES, Problem, Rule
from .scenarios import run_scenarios

__all__ = ["RULES", "VARS", "Problem", "ProjectCheck", "Rule", "WorkflowCheck", "is_trigger"]

VARS = "vars"  # the root of the global variables, {{vars.<name>}}: always available


def is_trigger(node):
    kind = node["type"].rsplit(".", 1)[-1].lower()
    return kind.endswith("trigger") or kind in ("webhook", "start")


class WorkflowCheck:
    """`fetch(workflow id)`: another workflow (a Call Activity's sub-process), None if unknown.
    `start_data`: the fields of the data that starts a case (the webhook's body), when known (Studio
    knows them from the interview): then the variables nothing provides are errors."""

    def __init__(self, workflow, fetch=n8n.workflow, start_data=None):
        self.fetch, self.start_data = fetch, None if start_data is None else set(start_data)
        self.graph = graph = ProcessGraph(workflow)
        # what the rules read, from the graph: disabled nodes pass through, stickies are left out
        self.connections, self.node_names, self.nodes = graph.connections, graph.node_names, graph.nodes
        self.outs, self.into, self.subnodes = graph.outs, graph.into, graph.subnodes
        self.triggers = set(graph.triggers)

    @cached_property
    def problems(self):
        return [problem for rule in RULES for problem in rule.check(self)]

    @property
    def errors(self):
        return [p for p in self.problems if p.level == "error"]

    # --- what the rules read --------------------------------------------------------------

    def after(self, start, stop=None):
        """`start` and every step it leads to."""
        return self.graph.after(start, stop)

    @property
    def dominators(self):
        """Steps that every path to a node passes: only a guaranteed fork can justify a join's union."""
        return self.graph.dominators

    def waits(self, name):
        """Whether the step parks the execution: a person, a message, a timer longer than a minute."""
        e, p = self.graph.element(name), self.nodes[name]["parameters"]
        if e.waits:
            return True
        if e.kind != "timerEvent":
            return False
        seconds = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800}
        return p.get("mode") == "until" or float(p.get("amount") or 0) * seconds.get(p.get("unit", "hours"), 3600) > 60

    def end_without_person(self, name, stop=()):
        """The first end reached from `name` without passing a user or manual task (nor a step in `stop`), or
        None (breadth first, in output order: the same end every time)."""
        seen, queue = {name}, deque([name])
        while queue:
            current = queue.popleft()
            following = [n for port in self.outs[current] for n in port if n not in seen]
            if not self.graph.successors(current):
                return current
            for n in dict.fromkeys(following):
                seen.add(n)
                if not self.graph.element(n).human and n not in stop:
                    queue.append(n)
        return None

    def reads_only_forms(self, name):
        """Whether every path supplies a human answer that no later automated step overwrites (a loop step's
        count of rounds aside: its budget decides nothing about a person)."""
        used = self.references(self.nodes[name]["parameters"]) - {ROUNDS_LEFT}
        return bool(used) and used <= (self.form_data[name] or set())

    @staticmethod
    def references(value):
        """The variables a value reads, by their root ({{customer.email}}: customer). The global variables,
        {{vars.<name>}}, always exist: they are not among them."""
        return {path.split(".")[0] for path in TEMPLATE.findall(json.dumps(value))} - {VARS}

    def context_references(self, value):
        """render() omits missing lone-template entries; embedded text still needs its variables."""
        if isinstance(value, (dict, list)):
            children = value.values() if isinstance(value, dict) else value
            references = [self.context_references(child) for child in children]
            return (set().union(*(required for required, _ in references)),
                    set().union(*(optional for _, optional in references)))
        used = self.references(value)
        return (set(), used) if isinstance(value, str) and TEMPLATE.fullmatch(value.strip()) else (used, set())

    def available(self, name):
        """The variables surely set when the node runs (on every path to it, and the start data); None when
        an unknown output schema on the way makes it unknown."""
        produced = self.available_data[name]
        return None if produced is None else produced | (self.start_data or set())

    @cached_property
    def missing_inputs(self):
        """Task node -> its required inputs that neither the node sets nor the available data provides
        (left out: nodes whose available data is unknown, and nodes that extract their input from text)."""
        missing = {}
        for name in self.graph.of(task=True):
            params = self.nodes[name]["parameters"]
            task = TASKS.get(str(params.get("task", "")).rsplit("/", 1)[-1])
            if (available := self.available(name)) is None or not task or params.get("inputFrom") == "text":
                continue
            given = {k for k, v in ((params.get("input") or {}).get("value") or {}).items() if v not in ("", None)}
            missing[name] = {f.name for f in task.inputs if f.required} - given - available
        return missing

    # --- the data flow --------------------------------------------------------------------

    def produced_before(self, name):
        """Fields available on every path to this node; None means an unknown output schema."""
        return self.available_data[name]

    def produced_by(self, name):
        node, e = self.nodes[name], self.graph.element(name)
        task = TASKS.get(str(node["parameters"].get("task", "")).rsplit("/", 1)[-1])
        if e.task and task and task.output:
            return {f.name for f in task.outputs}
        if e.human:
            return {f["name"] for f in node["parameters"].get("form", {}).get("fields", [])}
        if e.kind == "set":
            return {a["name"] for a in node["parameters"].get("assignments", {}).get("assignments", [])}
        if e.kind == "llmAgent":
            return {"output"}
        return set() if name in self.triggers or e.transparent else None

    @staticmethod
    def common_data(values):
        """An unknown schema cannot disprove a field, but a known alternative can."""
        known = [value for value in values if value is not None]
        return set.intersection(*known) if known else None

    def joined_data(self, name, inputs, used=frozenset()):
        """Merge waits for parallel branches, but their internal alternatives remain alternatives."""
        common = set.intersection(*(self.dominators[source] for source, _, _ in inputs)) - used
        for fork in sorted(common, key=lambda n: len(self.dominators[n]), reverse=True):
            ports = self.outs[fork]
            branches = [[(i, n) for n in port] for i, port in enumerate(ports)]
            if self.graph.element(fork).splits:
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
        produced = {name: self.produced_by(name) for name in self.nodes}
        changed = True
        while changed:
            changed = False
            for name in self.nodes:
                e = self.graph.element(name)
                inputs = [(source, port, self.carried(source, port, before, after, forms_only))
                          for source, port in dict.fromkeys(self.graph.incoming[name])]
                value = set() if name in self.triggers or not inputs else self.joined_data(name, inputs) \
                    if e.joins and not forms_only else self.common_data([value for _, _, value in inputs])
                if forms_only:
                    # At a join, require trusted provenance on every input. A field answered in only
                    # one parallel branch may be overwritten by its sibling; review after the join.
                    output = set() if produced[name] is None else None if value is None else \
                        value - produced[name] | (produced[name] if e.human else set())
                else:
                    output = None if value is None or produced[name] is None else value | produced[name]
                if value != before[name] or output != after[name]:
                    before[name], after[name], changed = value, output, True
        return before

    def carried(self, source, port, before, after, forms_only):
        """What leaves `source` on output `port`: on a "timeout" (an event-based gateway's, a user task's with a
        deadline) only what reached it, since nothing was answered; an event-based gateway's message outputs
        carry a payload of unknown shape; any other output what the step produced too."""
        labels = self.graph.outputs(source)
        if port < len(labels) and labels[port] == "timeout" or port == self.graph.error_port(source):
            return before[source]  # a failed step (its error output) produced nothing either
        if port and self.graph.element(source).kind == "eventBasedGateway":
            return set() if forms_only else None
        return after[source]


class ProjectCheck:
    """A project folder (projects/<name>/) before it is loaded: every module that registers @tasks
    imports; each activity does what its examples say (examples.json: {task: [{"input": {...},
    "output": {the fields expected}}]}); the workflow (workflow.json) passes WorkflowCheck, with the
    start data of process.py's SPEC; the process goes as its scenarios say (scenarios.json, see
    scenarios.py). Until its modules import, the rest is not checked; another project that fails to
    import is left out, as the server does (it is logged)."""

    def __init__(self, folder, fetch=n8n.workflow):
        self.folder = Path(folder)
        package = f"{self.folder.parent.name}.{self.folder.name}."
        # module -> the end of its traceback, for the project's modules that fail
        self.imports = {module: error for module, error in discover().items() if module.startswith(package)}
        self.examples = {} if self.imports else self._run_examples()
        self.scenarios = [] if self.imports else run_scenarios(self.folder)
        self.problems = [] if self.imports else self._check_workflow(fetch)

    @property
    def ok(self):
        return not (self.imports or self.examples or any(p.level == "error" for p in self.problems)
                    or any(not s["passed"] for s in self.scenarios))

    def as_dict(self):
        return {"imports": self.imports, "examples": self.examples, "scenarios": self.scenarios,
                "problems": [p.as_dict() for p in self.problems]}

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
        return WorkflowCheck(json.loads(path.read_text()), fetch, declared_start_data(self.folder)).problems


def declared_start_data(folder):
    """The start data a project declares (projects/<p>/process.py's SPEC["start_data"]), None when unknown."""
    folder = Path(folder)
    try:
        return import_module(f"{folder.parent.name}.{folder.name}.process").SPEC.get("start_data")
    except (ImportError, AttributeError):  # no process.py, or not a spec: the start data is unknown
        return None
