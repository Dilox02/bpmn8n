"""The rules the workflow check runs, one class each, in the order they report. A rule reads the
WorkflowCheck it is given (`ctx`: its ProcessGraph `ctx.graph`, the data flow, the start data, `fetch`)
and yields Problems. `RULES` is the registry; `manage.py rules [--md]` lists it, and the guide's table
is its output (a test keeps them equal).

`level` is a rule's usual level; its `doc` says when it reports another. Categories: structure (the
flow), data (what steps read), people (who works and decides), metrics, style (the modeling method)."""
import json
import re
from dataclasses import asdict, dataclass

from django.contrib.auth.models import Group

from ..api.tasks import MUSHY_VERBS, TASKS
from ..core.variables import Condition, ConditionError
from .elements import element

CATEGORIES = ("structure", "data", "people", "metrics", "style")
LEVELS = ("error", "warning", "info")


@dataclass(frozen=True)
class Problem:
    node: str
    rule: str
    message: str
    level: str = "error"

    def as_dict(self):
        return asdict(self)


class Rule:
    id = ""
    level = "error"
    category = "structure"
    doc = ""  # one line for the guide's table

    def check(self, ctx):
        raise NotImplementedError

    def problem(self, node, message, level=None):
        return Problem(node, self.id, message, level or self.level)


RULES = []


def rule(cls):
    """Registers a rule: the registry's order is the report's."""
    assert cls.id and cls.doc and cls.category in CATEGORIES and cls.level in LEVELS, cls
    assert cls.id not in {r.id for r in RULES}, f"two rules {cls.id!r}"
    RULES.append(cls())
    return cls


def table():
    """The rules as the guide's Markdown table (docs/GUIDE.md, "Workflow check")."""
    return "\n".join(["| Rule | Level | Category | What it finds |", "|---|---|---|---|",
                      *(f"| `{r.id}` | {r.level} | {r.category} | {r.doc} |" for r in RULES)])


@rule
class Empty(Rule):
    id, doc = "empty", "a workflow with no step after its trigger"

    def check(self, ctx):
        if all(name in ctx.triggers for name in ctx.nodes):  # no node, or only the trigger
            yield self.problem("", "the workflow does nothing: add the process's steps after its trigger")


@rule
class Trigger(Rule):
    id, level, doc = "trigger", "warning", "no trigger: nothing starts a case"

    def check(self, ctx):
        if ctx.nodes and not ctx.triggers:
            yield self.problem("", "no trigger: add a webhook (or another trigger) that starts the case")


@rule
class Connection(Rule):
    id, doc = "connection", "a connection from or to a node that does not exist"

    def check(self, ctx):
        for source, kinds in ctx.connections.items():
            if source not in ctx.node_names:
                yield self.problem(source, "connection starts at a node that does not exist")
            for ports in kinds.values():
                for port in ports:
                    for link in port or []:
                        if link["node"] not in ctx.node_names:
                            yield self.problem(source, f"connection leads to missing node {link['node']!r}")


@rule
class Reachable(Rule):
    id, doc = "reachable", "a node no path from a trigger reaches, detached cycles included: it never runs"

    def check(self, ctx):
        reached = set().union(*map(ctx.after, ctx.triggers)) if ctx.triggers else None
        for name in ctx.nodes:
            if name not in ctx.subnodes and (name not in reached if reached is not None else not ctx.into[name]):
                yield self.problem(name, "no path from a trigger leads here: it never runs")


@rule
class TaskExists(Rule):
    id, doc = "task", "a `@task` that does not exist, or of another kind than its node"

    def check(self, ctx):
        for name in ctx.graph.of(task=True):
            kind = ctx.graph.element(name).kind
            endpoint = str(ctx.nodes[name]["parameters"].get("task", "")).rsplit("/", 1)[-1]
            if not (task := TASKS.get(endpoint)):
                yield self.problem(name, f"no @task {endpoint!r}: define it in an app's tasks.py")
            elif task.kind != kind:
                yield self.problem(name, f"{endpoint!r} is a {task.kind}, not a {kind}: this node does not list it")


@rule
class OutputsConnected(Rule):
    id, level = "output", "warning"
    doc = ("an output that leads nowhere, on a gateway or any node with several outputs (not a conditions "
           "gateway's `default`; Laya's `default` must lead to a person)")

    def check(self, ctx):
        for name, node in ctx.nodes.items():
            labels = ctx.graph.outputs(name)
            if not (ctx.graph.element(name).branching or len(labels) > 1):
                continue
            ports, by_conditions = ctx.outs[name], node["parameters"].get("decider") == "conditions"
            for i, label in enumerate(labels):
                if label == "default" and by_conditions:
                    continue  # conditions often cover every case; Laya's "default" must lead to a person
                if not (ports[i] if i < len(ports) else []):
                    yield self.problem(name, f"output {label!r} leads nowhere: the items taking it end the case")


@rule
class ConditionValid(Rule):
    id, category = "condition", "data"
    doc = "an invalid branch condition; a warning for a branch with no condition (never taken)"

    def check(self, ctx):
        for name, node in ctx.nodes.items():
            if node["parameters"].get("decider") != "conditions":
                continue
            for branch in node["parameters"].get("branches", {}).get("values", []):
                condition = branch.get("condition", "")
                if condition in ("", None):
                    yield self.problem(name, f"branch {branch['name']!r} has no condition: never taken", "warning")
                elif isinstance(condition, str) and not condition.startswith("="):  # "=...": n8n evaluates it
                    try:
                        Condition.parse(condition)
                    except ConditionError as e:
                        yield self.problem(name, f"branch {branch['name']!r}: {e}")


@rule
class LaneExists(Rule):
    id, level, category = "lane", "warning", "people"
    doc = "a human task whose lane is no group: only superusers will see the task"

    def check(self, ctx):
        lanes = set(Group.objects.values_list("name", flat=True))
        for name in ctx.graph.of(human=True):
            if (lane := ctx.nodes[name]["parameters"].get("lane", "")) and lane not in lanes:
                yield self.problem(name, f"no group {lane!r}: only superusers will see the task (add it in /admin/)")


@rule
class SubprocessRunsThrough(Rule):
    id = "subprocess"
    doc = ("a Call Activity whose sub-process waits (people, messages); a warning when the sub-process cannot be "
           "read or is not published")

    def check(self, ctx):
        for name in ctx.graph.of(kind="callActivity"):
            target = ctx.nodes[name]["parameters"].get("workflowId", "")
            workflow_id = target.get("value") if isinstance(target, dict) else target
            if (sub := ctx.fetch(workflow_id)) is None:
                yield self.problem(name, f"cannot read sub-process {workflow_id!r} (N8N_API_KEY): make sure it does "
                                         "not wait", "warning")
            elif waits := [n["name"] for n in sub.get("nodes", []) if element(n).waits]:
                yield self.problem(name, f"the sub-process waits at {', '.join(waits)}: a Call Activity runs it in "
                                         "one go, so put the waits in this workflow")
            elif sub.get("active") is False:
                yield self.problem(name, f"sub-process {workflow_id!r} is not published: n8n 2 refuses to run it "
                                         "(\"Workflow is not active\"); publish it", "warning")


@rule
class InputProvided(Rule):
    id, category = "input", "data"
    doc = ("with the start data declared: a required input of a task that neither its node nor, on every path, an "
           "earlier step or the start data provides")

    def check(self, ctx):
        if ctx.start_data is None:
            return
        for name, missing in ctx.missing_inputs.items():
            if missing:
                yield self.problem(name, f"no earlier step nor the start data provides {', '.join(sorted(missing))}: "
                                         "set it on the node or produce it before")


def templates(names):
    return ", ".join("{{" + name + "}}" for name in sorted(names))


@rule
class VariableProvided(Rule):
    id, category = "variable", "data"
    doc = ("with the start data declared: a `{{variable}}` nothing provides on every path (`{{vars.…}}` always "
           "exists); a warning for an optional entry of a human task's context, left out when empty")

    def check(self, ctx):
        if ctx.start_data is None:
            return
        for name, node in ctx.nodes.items():
            if (available := ctx.available(name)) is None:
                continue
            params = node["parameters"]
            used, optional = ctx.references(params), set()
            if ctx.graph.element(name).human:
                context = params.get("context")
                if isinstance(context, str):  # HumanTaskNode accepts an object or JSON text.
                    try:
                        context = json.loads(context or "{}")
                    except ValueError:
                        pass
                if isinstance(context, dict):
                    context_used, optional = ctx.context_references(context)
                    used = ctx.references({k: v for k, v in params.items() if k != "context"}) | context_used
            if unknown := used - available:
                yield self.problem(name, f"{templates(unknown)}: no earlier step nor "
                                         "the start data provides it, so it would be empty")
            if unknown := optional - used - available:
                yield self.problem(name, f"{templates(unknown)}: optional human context "
                                         "entries may be omitted because no earlier step nor the start data provides "
                                         "them", "warning")


@rule
class StartData(Rule):
    id, level, category = "start-data", "info", "data"
    doc = "without declared start data: the fields the webhook body must carry (the required inputs no step produces)"

    def check(self, ctx):
        if ctx.start_data is None and (needed := set().union(*ctx.missing_inputs.values())):
            yield self.problem("", f"the data that starts a case must carry: {', '.join(sorted(needed))}")


@rule
class PeopleDecide(Rule):
    """GDPR art. 22, no decision based solely on automated processing: after an LLM agent or a gateway,
    every path to an end meets a user or manual task. bpmn8n Studio treats the warning as an error too."""
    id, category = "human", "people"
    doc = ("after an LLM agent, a path that reaches an end without a person (User or Manual Task); a warning after "
           "a gateway (Laya or conditions), not after a conditions gateway that reads only form fields filled in "
           "before it or declares `decision_scope=\"technical\"` (housekeeping, never decisions about people)")

    def check(self, ctx):
        for name in ctx.graph.of(decides=True):
            params, agent = ctx.nodes[name]["parameters"], ctx.graph.element(name).kind == "llmAgent"
            # Technical loops route counters/files; this explicit scope never exempts a model's decision.
            if not agent and params.get("decider") == "conditions" and params.get("decisionScope") == "technical":
                continue
            if params.get("decider") == "conditions" and ctx.reads_only_forms(name):
                continue
            if end := ctx.end_without_person(name):
                what = "the LLM agent's answer" if agent else "this automatic decision"
                yield self.problem(name, f"{what} reaches {end!r}, an end, without a person deciding: put a user task "
                                         "on the way", "error" if agent else "warning")


@rule
class OneWaitAtATime(Rule):
    """n8n parks an execution on one wait at a time: when branches run at once, at most one of them may
    wait. Otherwise the case resumes only when the last wait ends: a person's answer waits for a timer
    months away."""
    id = "wait"
    doc = ("branches that run at once (a parallel or inclusive gateway, one output leading to several steps) where "
           "more than one waits (a person, a message, a timer over a minute)")

    def check(self, ctx):
        for name in ctx.nodes:
            e = ctx.graph.element(name)
            starts = [n for port in ctx.outs[name] for n in port] if e.splits or e.kind == "inclusiveGateway" else \
                next((port for port in ctx.outs[name] if len(port) > 1), [])
            if len(starts) < 2:
                continue
            reach = [ctx.after(start) for start in starts]
            shared = set.intersection(*reach)  # after the branches join
            waiting = [sorted(n for n in r - shared if ctx.waits(n)) for r in reach]
            if sum(map(bool, waiting)) > 1:
                yield self.problem(name, "these branches run at once and more than one waits (" +
                                   "; ".join(", ".join(w) for w in waiting if w) + "): n8n waits on one thing at a "
                                   "time, so the case would stall until the last wait ends. Keep the waits in one "
                                   "branch (a deadline: an event-based gateway's timeout) or start the other wait as "
                                   "its own process")


@rule
class MetricsDecide(Rule):
    """A measurement earns its node when it can change something: it comes after a step that produced
    what it measures, it has a target to tell good from bad, and missing the target can act on the flow."""
    id, level, category = "metrics", "warning", "metrics"
    doc = ("a Metrics node with nothing produced before it, or without a threshold; an info when missing the "
           "threshold changes nothing (no conditions gateway after it)")

    def check(self, ctx):
        for name in ctx.graph.of(kind="metrics"):
            values = ctx.nodes[name]["parameters"].get("metrics", {}).get("values", [])
            if not any(n not in ctx.triggers and not ctx.graph.element(n).transparent  # something made the data
                       for n in ctx.graph.before(name) - {name}):
                yield self.problem(name, "nothing has been produced yet: measure after the step whose result matters")
            if not any(v.get("checkThreshold") for v in values):
                yield self.problem(name, "no threshold: the values are recorded but nothing tells good from bad; give "
                                         "each metric the target that decides, or drop it")
            elif not any(ctx.nodes[n]["parameters"].get("decider") == "conditions" for n in ctx.after(name) - {name}):
                yield self.problem(name, "missing a threshold changes nothing: add a condition gateway on the same "
                                         "values if the run should stop or take another path", "info")


# --- implicit gateways ([AW] §2.3, §3.3, §4.2) ----------------------------------------------------

@rule
class ImplicitSplit(Rule):
    id, level = "implicit-split", "info"
    doc = "one output leading to several steps (not a Parallel Gateway's): they all run, a split nobody drew"

    def check(self, ctx):
        for name in ctx.graph.flow_nodes:
            if not ctx.graph.element(name).splits and any(len(set(port)) > 1 for port in ctx.outs[name]):
                yield self.problem(name, "implicit parallel split: a Parallel Gateway makes it visible")


@rule
class ImplicitJoin(Rule):
    """n8n runs a node once per arrival. Arrivals that can come in the same case are those whose nearest
    common dominator starts them together: a parallel split, an inclusive gateway, or one output leading to
    several steps; the branches of an exclusive or event-based choice (one output each) never do."""
    id, level = "implicit-join", "warning"
    doc = ("a step that is not a Merge, reached by branches that run at once: it runs once per arriving branch; "
           "the branches of an exclusive or event-based choice may meet anywhere")

    def check(self, ctx):
        g = ctx.graph
        for name in g.flow_nodes:
            sources = {source for source, _ in g.incoming[name] if source != name}
            if g.element(name).joins or len(sources) < 2:
                continue
            common = set.intersection(*(g.dominators[s] for s in sources))
            if common and self.together(g, max(common, key=lambda n: len(g.dominators[n])), name, sources):
                yield self.problem(name, "runs once per arriving branch; join them with a Merge")

    @staticmethod
    def together(g, fork, name, sources):
        """Whether two of the sources are reached from starts of `fork` that run in the same case."""
        e = g.element(fork)
        every_output = e.splits or e.kind == "inclusiveGateway"
        starts = [(port, target) for port, targets in enumerate(g.outs[fork]) for target in dict.fromkeys(targets)]
        reach = {start: g.after(start[1], stop=name) for start in starts}
        leading = [{start for start in starts if s in reach[start] or s == fork and start[1] == name} for s in sources]
        return any(a != b and (every_output or a[0] == b[0])  # an exclusive choice: only one output's steps together
                   for i, first in enumerate(leading) for second in leading[i + 1:] for a in first for b in second)


# --- loops (D6-lite: structural; the engine's state search is the upgrade path) -------------------

def loops(ctx):
    """The loops a case can enter (a detached one is the `reachable` rule's)."""
    return [loop for loop in ctx.graph.cycles if loop & ctx.graph.reachable]


def leaves(g, name, loop):
    """Whether a flow leaves the loop at this step: a successor outside it, or an output left open (the case
    ends there). ponytail: an n8n node the registry does not know (If, Switch, Loop Over Items) may have
    outputs its connections do not show, so it counts as a way out; the engine (A3) will know its outputs."""
    ports = g.outs[name]
    return g.element(name).kind == "task" or bool(g.successors(name) - loop) or \
        any(i >= len(ports) or not ports[i] for i in range(max(len(g.outputs(name)), len(ports))))


def head(g, loop):
    """Where a case enters the loop: its first step in the flow's order."""
    return min(loop, key=lambda n: (g.rank.get(n, len(g.rank)), n))


@rule
class EndlessLoop(Rule):
    id, doc = "endless-loop", "a loop no flow leaves (no output out of it, none left open): it never ends"

    def check(self, ctx):
        for loop in loops(ctx):
            if not any(leaves(ctx.graph, name, loop) for name in loop):
                yield self.problem(head(ctx.graph, loop), f"this loop never ends: no flow leaves it "
                                                          f"({', '.join(sorted(loop))})")


@rule
class LoopGuard(Rule):
    id, level = "loop", "warning"
    doc = "a loop with nothing in it that chooses, waits or keeps time: it may spin without end"

    def check(self, ctx):
        for loop in loops(ctx):
            kinds = [ctx.graph.element(name) for name in loop]
            if any(leaves(ctx.graph, name, loop) for name in loop) and \
                    not any(e.chooses or e.waits or e.kind in ("timerEvent", "task") for e in kinds):
                yield self.problem(head(ctx.graph, loop), "nothing in this loop chooses, waits or keeps time "
                                                          f"({', '.join(sorted(loop))}): a gateway should decide "
                                                          "when it ends")


# --- names ([WM] A06–A16, A59–A62) ----------------------------------------------------------------

STATE_WORDS = {"done", "sent", "paid", "ready", "complete", "pending", "ok"}


def naming(name):
    """Why a name is not an action, verb + noun ("Approve Expense"); None when it may be one. A prefix up
    to "·" ("A · Check slots") and a builder suffix after ": " ("Draft: prompt") are not the name."""
    words = re.findall(r"[a-z]+", name.rsplit("·", 1)[-1].split(": ")[0].lower())
    if not words:
        return None
    if words[0] in MUSHY_VERBS:
        return f"{words[0]!r} is a mushy verb, it names no countable result: say what it produces, verb + noun"
    last = words[-1]
    if last in STATE_WORDS or len(last) >= 5 and last.endswith("ed") and not last.endswith("eed") \
            or {"is", "are"} & set(words[1:3]):  # "Claim Received", "Request is approved"
        return "named as an event or a state: name a step by its action: verb + noun"
    return None


@rule
class ActionNames(Rule):
    id, level, category = "naming", "info", "style"
    doc = ("a task, or the process, named with a mushy verb (`handle`, `process`, `review`…) or as an event or a "
           "state (`Claim Received`, `Request is approved`); triggers and events may be past tense")

    def check(self, ctx):
        if why := naming(ctx.graph.name):
            yield self.problem("", f"the process's name: {why}")
        for name in ctx.graph.flow_nodes:
            if ctx.graph.element(name).category == "task" and (why := naming(name)):
                yield self.problem(name, why)
