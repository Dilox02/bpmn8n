"""Scenario tests: a project's process played on the token engine without n8n, the way a case goes given
what people answer, which messages come and what Laya decides. examples.json tests the activities one by
one; scenarios.json, next to it, tests the process. A list of:

  {"name": "an approved claim",
   "workflow": "workflow.json",          relative to the project folder (the default)
   "start": {...},                       the data that starts the case (the webhook's body)
   "decisions": {gateway: "branch" | ["a", "b"] | "timeout"},
   "people": {user or manual task: {form answers}},
   "messages": {receive task, signal catch or event-based gateway: {"name": message or signal, "payload": {...}}},
   "tasks": "run" | "stub",              run (the default): the real @task functions; stub: none runs
   "outputs": {node: {...}},             what a node returns instead of running (an LLM agent's: its text)
   "expect": {"path": [...], "visits": [...], "not": [...], "ends": node | "error:<code>",
              "variables": {...}, "max_steps": n}}

How a case goes: a task runs on the case's variables, its input bound by name and by {{templates}} as the
n8n node binds it, and its result becomes variables; what it writes in the database is rolled back and its
mail stays in memory. A conditions gateway is evaluated on the variables (a decision given for it is
checked against them); a Laya gateway, an LLM agent and a person never run: the scenario says what they
answer, and a missing answer fails it naming the node. A message adds only the variables the case does not
have, as delivery does. A list for a node gives one value per visit (a loop) and running out of them fails;
on an inclusive gateway ["a", "b"] is the branches it takes, one per visit is [["a"], ["a", "b"]]. Time is
virtual (the engine's clock).
ponytail: one token = one item, so a user task with several items (multi-instance) is out of scope; a
timer, a call activity and other n8n nodes pass the case on unchanged unless `outputs` says what they add.

`manage.py record_scenario <case id> projects/<name>` writes one from a real case (record())."""
import json
import re
import traceback
from collections import Counter
from pathlib import Path

from django.db import transaction
from django.http import HttpRequest
from django.test.utils import override_settings

from ..api.tasks import TASKS, as_data, same
from ..core.variables import MISSING, Condition, ConditionError, bind, render
from ..engine import COMPLETED, ERROR, Outcome, Resolver, TokenEngine
from .graph import ProcessGraph

JSON_FIELD = re.compile(r"=\{\{\s*\$json\.(\w+)")  # the one n8n expression a Set node gets: "={{ $json.output ?? …"
GATEWAYS = ("exclusive_gateway", "inclusive_gateway", "conditions_gateway")  # their endpoints in the event log
WAITS = ("userTask", "manualTask", "receiveTask", "signalCatch")


class ScenarioError(Exception):
    """The case cannot go on as the scenario says: a missing answer, a task that fails."""


def as_dict(value):
    return {"output": value} if isinstance(value, str) else value if isinstance(value, dict) else {}


def task_name(params):
    return str(params.get("task", "")).rsplit("/", 1)[-1]


def as_list(value):
    return list(value) if isinstance(value, (list, tuple)) else [value]


class Play(Resolver):
    """Decides each visit as the scenario says, on the case's variables: one dict for the whole case, as
    bpmn8n's case store (parallel branches share it)."""

    def __init__(self, graph, scenario):
        self.graph, self.scenario = graph, scenario
        self.variables = dict(scenario.get("start") or {})
        self.stub = scenario.get("tasks", "run") == "stub"
        self.visits = Counter()
        self.ledger = []  # (task, its input, its output) of the tasks run so far: what a compensation undoes

    def given(self, key, node):
        """What the scenario gives `node` under `key` for this visit, MISSING when nothing."""
        value = (self.scenario.get(key) or {}).get(node, MISSING)
        if isinstance(value, list):
            i = self.visits[node] - 1
            if i >= len(value):
                raise ScenarioError(f"{node!r} is visited {i + 1} times, {key} gives it {len(value)}")
            value = value[i]
        return value

    def decision(self, node, inclusive=False):
        value = (self.scenario.get("decisions") or {}).get(node, MISSING)
        if inclusive and isinstance(value, list) and not any(isinstance(v, list) for v in value):
            return value  # the branches it takes, every visit
        return self.given("decisions", node)

    def resolve(self, visit):
        node = visit.node
        self.visits[node] += 1
        e, params = self.graph.element(node), self.graph.nodes[node].get("parameters") or {}
        if e.chooses:
            return self.choose(node, e, params)
        if e.human:
            return self.person(node, params)
        if e.kind in ("receiveTask", "signalCatch"):
            return self.receive(node, [params.get("message" if e.kind == "receiveTask" else "signal")])
        if e.kind == "callActivity" and (listed := params.get("outputs")):  # only the listed outputs come back
            output = self.given("outputs", node)
            return self.add({} if output is MISSING else {k: v for k, v in as_dict(output).items() if k in listed})
        if (output := self.given("outputs", node)) is not MISSING:
            return self.add({"output": output} if isinstance(output, str) else output)
        if e.task and task_name(params) == "compensate_case":
            return self.add(self.compensate())
        if e.task:
            return self.add({} if self.stub else self.run(node, params))
        if e.kind == "llmAgent":
            raise ScenarioError(f"{node!r} is an LLM agent: give its answer in outputs")
        if e.kind == "set":
            return self.add(self.assignments(params))
        return Outcome()

    def add(self, data, outputs=None):
        data = data if isinstance(data, dict) else {}
        self.variables.update(data)
        return Outcome(outputs=outputs, data=data)

    def choose(self, node, e, params):
        decided = self.decision(node, e.kind == "inclusiveGateway")
        if e.kind == "eventBasedGateway":
            if decided == "timeout":
                return Outcome(outputs=["timeout"])
            return self.receive(node, self.graph.outputs(node)[1:], decided, race=True)
        taken = self.conditions(node, e, params) if params.get("decider") == "conditions" else None
        if taken is not None:
            if decided is not MISSING and sorted(as_list(decided)) != sorted(taken):
                raise ScenarioError(f"{node!r}: its conditions take {taken}, the scenario says {decided}")
            return Outcome(outputs=taken)
        if decided is MISSING:
            why = "is decided by Laya" if params.get("decider") != "conditions" else "has an n8n expression"
            raise ScenarioError(f"{node!r} {why}: give its branch in decisions")
        return Outcome(outputs=as_list(decided))

    def conditions(self, node, e, params):
        """The branches its conditions take (exclusive: the first), ["default"] if none; None when one is an
        n8n expression, which only n8n evaluates."""
        taken = []
        for branch in params.get("branches", {}).get("values", []):
            condition = branch.get("condition", "")
            if condition in ("", None):
                continue  # never taken
            if isinstance(condition, str) and condition.startswith("="):
                return None
            try:
                holds = Condition.holds(condition, self.variables)
            except ConditionError as err:  # the gateway answers 422: n8n stops the case there
                raise ScenarioError(f"{node!r}: {err}") from None
            if holds:
                taken.append(branch["name"])
                if e.kind == "exclusiveGateway":
                    break
        return taken or ["default"]

    def person(self, node, params):
        outputs = None
        if self.graph.outputs(node) == ["timeout", "done"]:  # an interrupting deadline
            if self.decision(node) == "timeout":
                return Outcome(outputs=["timeout"])
            outputs = ["done"]
        fields = [f["name"] for f in params.get("form", {}).get("fields", [])]
        answers = self.given("people", node)
        if answers is MISSING:
            if fields:
                raise ScenarioError(f"{node!r} waits for a person: give their answers in people ({', '.join(fields)})")
            answers = {}
        if unknown := sorted(set(answers) - set(fields)):
            raise ScenarioError(f"{node!r}: its form has no {', '.join(unknown)}")
        return self.add(answers, outputs)

    def receive(self, node, names, decided=MISSING, race=False):
        """A message arrives; its payload adds the variables the case does not have yet."""
        message = self.given("messages", node)
        if message is MISSING:
            if decided is MISSING:
                raise ScenarioError(f"{node!r} waits for a message: give it in messages"
                                    + (' (or "timeout" in decisions)' if race else ""))
            message = {"name": decided}
        if (name := message.get("name")) not in names:
            raise ScenarioError(f"{node!r} waits for {' or '.join(map(repr, names))}, not {name!r}")
        payload = {k: v for k, v in (message.get("payload") or {}).items() if k not in self.variables}
        return self.add(payload, [name] if race else None)

    def run(self, node, params):
        """The @task, as the n8n node calls it: the node's fields ({{templates}} filled), then the variables
        named like the remaining inputs. ponytail: {{vars.…}} (global variables) are not read."""
        name = str(params.get("task", "")).rsplit("/", 1)[-1]
        if (task := TASKS.get(name)) is None:
            raise ScenarioError(f"{node!r}: no @task {name!r}")
        if params.get("inputFrom") == "text":
            raise ScenarioError(f"{node!r} extracts its input from text (GLiNER, Laya): give its result in outputs")
        values = bind(task.schema, (params.get("input") or {}).get("value") or {}, self.variables)
        try:
            result = as_data(task.validated(task.fn(HttpRequest(), task.schema(**values))))
            if task.compensate:
                self.ledger.append((task, as_data(task.schema(**values)), result))
            return result
        except Exception:  # whatever the code does wrong is the finding
            raise ScenarioError(f"{node!r} ({name}) fails: {traceback.format_exc(limit=-2).strip()}") from None

    def compensate(self):
        """The undo of every task run so far that declared one, newest first (core/saga.py, on the scenario's ledger)."""
        from ..core.saga import undo_payload
        for task, given, result in reversed(self.ledger):
            comp = TASKS.get(task.compensate)
            if comp is None:
                raise ScenarioError(f"{task.name!r} is compensated by {task.compensate!r}, which is no @task")
            try:
                comp.validated(comp.fn(HttpRequest(), comp.schema(**undo_payload(comp, {"input": given, "output": result}))))
            except Exception:  # whatever the code does wrong is the finding
                raise ScenarioError(f"compensation {comp.name!r} fails: {traceback.format_exc(limit=-2).strip()}") from None
        self.ledger.clear()
        return {}

    def assignments(self, params):
        """A Set node's fields: values and {{templates}}; of n8n's expressions only `$json.<field>`."""
        fields = {}
        for a in params.get("assignments", {}).get("assignments", []):
            value = a.get("value")
            if isinstance(value, str) and value.startswith("="):
                found = JSON_FIELD.match(value)
                value = self.variables.get(found.group(1), MISSING) if found else MISSING
            else:
                value = render(value, self.variables)
            if value is not MISSING:
                fields[a["name"]] = value
        return fields


class ScenarioRunner:
    """Plays scenarios on one workflow: run() is a scenario's report (passed, the first difference from what
    it expects, the case's status, the path, the variables at the end)."""

    def __init__(self, workflow):
        self.graph = workflow if isinstance(workflow, ProcessGraph) else ProcessGraph(workflow)

    def run(self, scenario):
        play, failure = Play(self.graph, scenario), None
        engine = TokenEngine(self.graph, play)
        # the tasks run for real: what they write is undone and their mail goes nowhere, so a check leaves
        # no trace and plays the same every time
        with transaction.atomic(), override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            case = engine.start(data=play.variables)
            try:
                engine.run()
            except (ScenarioError, ValueError) as e:  # ValueError: the engine's, an output the node does not have
                failure = str(e)
            transaction.set_rollback(True)
        path = [v.node for v in case.trace]
        failure = failure or difference(self.graph, case, path, play.variables, scenario.get("expect") or {})
        return {"name": scenario.get("name", ""), "passed": failure is None, "failure": failure,
                "status": case.status, "path": path, "variables": play.variables}


def difference(graph, case, path, variables, expect):
    """The first way the case differs from what the scenario expects; None when it does not."""
    ends = str(expect.get("ends", ""))
    if case.status not in (COMPLETED, ERROR):
        return f"the case ends {case.status}: {case.error}"
    if case.status == ERROR and not ends.startswith("error:") and ends not in case.ends:
        return f"the case ends with an error at {case.ends[-1]!r}: {case.error}"
    if (expected := expect.get("path")) is not None and path != expected:
        i = next((i for i, (a, b) in enumerate(zip(path, expected)) if a != b), min(len(path), len(expected)))
        if i == len(path):
            return f"path: ends after {i} steps, expected {expected[i]!r} next"
        if i == len(expected):
            return f"path: goes on to {path[i]!r} after the expected end"
        return f"path: step {i + 1} is {path[i]!r}, expected {expected[i]!r}"
    if missing := [n for n in expect.get("visits", []) if n not in path]:
        return f"never visits {', '.join(map(repr, missing))}"
    if visited := [n for n in expect.get("not", []) if n in path]:
        return f"visits {', '.join(map(repr, visited))}, which it must not"
    codes = [graph.nodes[n].get("parameters", {}).get("code") for n in case.ends
             if graph.element(n).kind == "errorEndEvent"]
    if ends.startswith("error:") and ends[6:] not in codes or ends and not ends.startswith("error:") and ends not in case.ends:
        actual = f"with error {', '.join(map(str, codes))}" if case.status == ERROR else f"at {', '.join(map(repr, case.ends))}"
        return f"the case ends {actual}, expected {ends}"
    for name, value in (expect.get("variables") or {}).items():
        if (actual := variables.get(name, MISSING)) is MISSING or not same(actual, value):
            return f"variable {name!r} is {'missing' if actual is MISSING else repr(actual)}, expected {value!r}"
    if (most := expect.get("max_steps")) is not None and len(path) > most:
        return f"the case takes {len(path)} steps, more than {most}"
    return None


def failed(scenario, why):
    name = scenario.get("name", "") if isinstance(scenario, dict) else ""
    return {"name": name, "passed": False, "failure": why, "status": "", "path": [], "variables": {}}


def run_scenarios(folder):
    """The reports of a project folder's scenarios.json, [] without one."""
    folder = Path(folder)
    if not (path := folder / "scenarios.json").exists():
        return []
    try:
        scenarios = json.loads(path.read_text())
    except ValueError as e:
        return [failed({"name": "scenarios.json"}, f"not JSON: {e}")]
    reports = []
    for scenario in scenarios:
        try:
            workflow = json.loads((folder / scenario.get("workflow", "workflow.json")).read_text())
            reports.append(ScenarioRunner(workflow).run(scenario))
        except Exception as e:  # a scenario that cannot even start is a failing one, not a broken check
            reports.append(failed(scenario, f"cannot play it: {type(e).__name__}: {e}"))
    return reports


def summary(report):
    """A report as the commands print it."""
    if report["passed"]:
        return f"scenario {report['name']}: ok"
    return f"scenario {report['name']}: {report['failure']}\n         path: {' → '.join(report['path']) or '(none)'}"


# --- recording a scenario from a real case ----------------------------------------------------

def record(case_id, workflow, name=None):
    """(a scenario, its replay's report) from a case of the event log, so that what a case did in production
    becomes a regression test: the start data, the branches its gateways took, people's answers, the
    messages, and what its tasks returned as `outputs` (delete one to run that task for real). The replay
    gives the expected path; when it does not visit what the log has, the logged steps are the expectation.
    ponytail: an LLM agent's answer and a timeout leave no event: the replay names what is missing."""
    from ..models import Case, Event
    events = list(Event.objects.cases().filter(case_id=case_id).order_by("started", "pk")
                  .values("activity", "endpoint", "resource", "data"))
    if not events:
        raise ValueError(f"no event of case {case_id!r} in the log")
    case = Case.objects.filter(case_id=case_id).first()
    return from_log(events, case.variables if case else {}, workflow, name or f"case {case_id}")


def from_log(events, variables, workflow, name):
    """record() on a case's events, in the log's order ({"activity", "endpoint", "resource", "data"} each), and
    its variables at the end: also for a log read through the API (e2e/test_differential.py)."""
    graph = ProcessGraph(workflow)
    given, produced, start = {"decisions": {}, "people": {}, "messages": {}, "outputs": {}}, set(), {}
    for event in events:
        node, endpoint, data = event["activity"], event["endpoint"], event["data"] or {}
        output = data.get("output")
        if node not in graph.nodes:
            continue  # a step of another version of the workflow
        if endpoint == "conditions_gateway":  # what the case's variables were when it decided
            state = (data.get("input") or {}).get("state")
            start |= {k: v for k, v in (state if isinstance(state, dict) else {}).items()
                      if k not in produced and k not in start}
        if endpoint in GATEWAYS:
            branches = (output or {}).get("branches") or ["default"]
            key, value = "decisions", branches if graph.element(node).kind == "inclusiveGateway" else branches[0]
        elif endpoint in WAITS and event["resource"].startswith(("message:", "signal:")):
            key, value = "messages", {"name": event["resource"].partition(":")[2], "payload": output or {}}
        elif endpoint in WAITS:
            key, value = "people", output or {}
        elif graph.element(node).task and isinstance(output, dict):
            key, value = "outputs", output
        else:
            continue
        given[key].setdefault(node, []).append(value)
        if key != "decisions":
            produced |= set(value.get("payload", value) if key == "messages" else value)
    start |= {k: v for k, v in (variables or {}).items() if k not in produced and k not in start}
    scenario = {"name": name, "start": start,
                **{key: {node: values[0] if len(values) == 1 else values for node, values in found.items()}
                   for key, found in given.items() if found},
                "expect": {"visits": list(dict.fromkeys(e["activity"] for e in events if e["activity"] in graph.nodes))}}
    report = ScenarioRunner(graph).run(scenario)
    if report["passed"]:
        scenario["expect"] = {"path": report["path"]}
    return scenario, report
