"""A synthetic event log, so the Insights page and the canvas heatmap can be seen without running n8n,
and the tests have a log whose rates are known (F11, in small).

A process is a small spec: Poisson arrivals, steps with their kind and duration, gateways with branch
probabilities (Laya's with a confidence, below which the default branch hands the case to a person),
a loop, errors, and lanes of people. People work their lane's tasks first come, first served, each
taking the next task as soon as one is finished: a small discrete-event simulation, so the queues in the
log are real queues. The clock ends at `now`: the last cases are still open (a task waiting or claimed,
a message not arrived), as in a live system. It writes what bpmn8n writes when n8n runs: Event rows,
UserTask and MessageSubscription rows, Case variables, and the lanes as Django groups of people."""
import heapq
import math
import random
from datetime import UTC, datetime

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone

from ..models import Case, Event, MessageSubscription, UserTask

# kind: serviceTask… (a @task: `seconds` mean, lognormal), userTask/manualTask (a person of `lane`:
# `minutes` mean, exponential), receiveTask (an outside party: `minutes` mean, exponential), laya and
# conditions (gateways: `branches` name -> (next step, probability), else `default`)
CLAIMS = {
    "arrivals_per_hour": 4,
    "start": "Register Claim",
    "lanes": {"clerks": ["alice", "bob", "chiara"], "assessors": ["dan", "eva"]},
    "steps": {
        "Register Claim": {"kind": "serviceTask", "seconds": 0.3, "next": "Check Policy"},
        "Check Policy": {"kind": "businessRuleTask", "seconds": 0.1, "errors": 0.02, "next": "Route Claim"},
        "Route Claim": {"kind": "laya", "question": "Is the claim simple or does it need an assessment?",
                        "branches": {"simple": ("Approve Claim", 0.6), "complex": ("Assess Damage", 0.4)},
                        "default": "Review Claim", "min_confidence": 0.7},
        "Review Claim": {"kind": "userTask", "lane": "clerks", "minutes": 20, "next": "Approve Claim"},
        "Assess Damage": {"kind": "userTask", "lane": "assessors", "minutes": 40, "next": "Documents Complete?"},
        "Documents Complete?": {"kind": "conditions", "branches": {"yes": ("Approve Claim", 0.7)},
                                "default": "Request Documents"},
        "Request Documents": {"kind": "sendTask", "seconds": 0.4, "next": "Receive Documents"},
        "Receive Documents": {"kind": "receiveTask", "message": "documents", "minutes": 120, "next": "Assess Damage"},
        "Approve Claim": {"kind": "userTask", "lane": "clerks", "minutes": 12, "outputs": {"approved": 0.85},
                          "next": "Approved?"},
        "Approved?": {"kind": "conditions", "field": "approved", "branches": {"yes": ("Pay Claim", None)},
                      "default": "Send Rejection"},
        "Pay Claim": {"kind": "serviceTask", "seconds": 1.2, "errors": 0.03, "next": "Notify Customer"},
        "Notify Customer": {"kind": "sendTask", "seconds": 0.5},
        "Send Rejection": {"kind": "sendTask", "seconds": 0.5},
    },
}
GATEWAY_ENDPOINT = {"laya": "exclusive_gateway", "conditions": "conditions_gateway"}
HUMAN = ("userTask", "manualTask")
MAX_VISITS = 5  # a loop is left by its first branch after this many rounds


def endpoint(name, step):
    """What bpmn8n records as Event.endpoint: the @task function, the gateway endpoint or the wait kind."""
    kind = step["kind"]
    return GATEWAY_ENDPOINT.get(kind) or (kind if kind in (*HUMAN, "receiveTask") else name.lower().replace(" ", "_"))


class Simulation:
    """Cases through the spec on a virtual clock; `rows` collects what to write."""

    def __init__(self, spec, cases, seed, now, process, workflow):
        self.spec, self.rng, self.now, self.process, self.workflow = spec, random.Random(seed), now, process, workflow
        self.heap, self.order = [], 0
        self.queue = {lane: [] for lane in spec["lanes"]}  # FIFO: (created, order, case, step name, work)
        self.free = {lane: {p: 0.0 for p in people} for lane, people in spec["lanes"].items()}  # person -> free since
        self.events, self.tasks, self.messages, self.cases = [], [], [], {}
        gaps = [self.rng.expovariate(spec["arrivals_per_hour"] / 3600) for _ in range(cases)]
        t = now - sum(gaps) - 60
        for i, gap in enumerate(gaps, 1):
            t += gap
            case = {"id": f"{workflow}:{i}", "visits": {}, "variables": {"amount": self.rng.randint(80, 6000)}}
            self.cases[case["id"]] = case
            self.at(t, self.step, case, spec["start"])

    def at(self, t, action, *args):
        self.order += 1
        heapq.heappush(self.heap, (t, self.order, action, args))

    def run(self):
        while self.heap:
            t, _, action, args = heapq.heappop(self.heap)
            action(t, *args)
        return self

    def latency(self):
        """n8n moving the token to the next node (a worker picks it up, an HTTP call)."""
        return self.rng.uniform(0.2, 2.0)

    def event(self, case, name, started, ended, resource, status="complete", data=None, error=""):
        if ended <= self.now:
            self.events.append(dict(case_id=case["id"], process=self.process, activity=name,
                                    endpoint=endpoint(name, self.spec["steps"][name]), started=started, ended=ended,
                                    duration_ms=(ended - started) * 1000, status=status, resource=resource,
                                    error=error, data=data or {}))
        return ended <= self.now

    def step(self, t, case, name):
        spec = self.spec["steps"][name]
        case["visits"][name] = case["visits"].get(name, 0) + 1
        kind = spec["kind"]
        if kind in HUMAN:
            work = self.rng.expovariate(1 / (spec["minutes"] * 60))
            self.queue[spec["lane"]].append((t, self.order, case, name, work))
            self.order += 1
            self.dispatch(t, spec["lane"])
        elif kind == "receiveTask":
            arrives = t + self.rng.expovariate(1 / (spec["minutes"] * 60))
            payload = {"documents": self.rng.randint(1, 4)}
            self.messages.append(dict(case=case, name=name, message=spec["message"], created=t, completed=arrives,
                                      payload=payload))
            if self.event(case, name, t, arrives, f"message:{spec['message']}", data={"output": payload}):
                self.at(arrives + self.latency(), self.step, case, spec["next"])
        elif kind in GATEWAY_ENDPOINT:
            self.decide(t, case, name, spec)
        else:
            sigma = 0.5
            took = self.rng.lognormvariate(math.log(spec["seconds"]) - sigma ** 2 / 2, sigma)
            failed = self.rng.random() < spec.get("errors", 0)
            done = self.event(case, name, t, t + took, "system", "error" if failed else "complete",
                              data={"input": {"amount": case["variables"]["amount"]}},
                              error="TaskError: the service answered 503" if failed else "")
            if done and not failed and spec.get("next"):
                self.at(t + took + self.latency(), self.step, case, spec["next"])

    def decide(self, t, case, name, spec):
        branches, scores = list(spec["branches"]), {}
        if field := spec.get("field"):  # a condition on a variable a person set
            taken = [branches[0]] if case["variables"].get(field) else []
        else:
            weights = [p for _, p in spec["branches"].values()]
            if case["visits"][name] >= MAX_VISITS:
                choice = branches[0]
            else:
                choice = self.rng.choices(branches, weights=[w / sum(weights) for w in weights])[0] \
                    if sum(weights) >= 1 or self.rng.random() < sum(weights) else None
            if spec["kind"] == "laya":
                confidence = round(self.rng.betavariate(9, 1.5), 3)
                scores = {choice: confidence}
                taken = [choice] if confidence >= spec["min_confidence"] else []
            else:
                taken = [choice] if choice else []
        decider = "laya" if spec["kind"] == "laya" else "conditions"
        options = {b: f"{b} claim" if decider == "laya" else f"{{{{{spec.get('field', 'complete')}}}}}" for b in branches}
        data = {"decider": decider, "input": {"question": spec.get("question", ""), "state": dict(case["variables"]),
                                              "options": options},
                "output": {"branches": taken, "scores": scores}}
        took = self.rng.uniform(0.01, 0.05)
        if self.event(case, name, t, t + took, "system", data=data):
            following = spec["branches"][taken[0]][0] if taken else spec["default"]
            self.at(t + took + self.latency(), self.step, case, following)

    def dispatch(self, t, lane):
        """Free people take the oldest tasks: the one free longest first."""
        queue, free = self.queue[lane], self.free[lane]
        while queue and (idle := [p for p, since in free.items() if since <= t]):
            created, _, case, name, work = queue.pop(0)
            person = min(idle, key=lambda p: (free[p], p))
            free[person] = t + work
            self.at(t + work, self.finish, case, name, lane, person, created, t)

    def finish(self, t, case, name, lane, person, created, claimed):
        spec = self.spec["steps"][name]
        result = {k: self.rng.random() < p for k, p in spec.get("outputs", {}).items()}
        case["variables"].update(result)
        self.tasks.append(dict(case=case, name=name, lane=lane, person=person, created=created, claimed=claimed,
                               completed=t, result=result))
        if self.event(case, name, created, t, person, data={"output": result}) and spec.get("next"):
            self.at(t + self.latency(), self.step, case, spec["next"])
        self.dispatch(t, lane)


def when(seconds):
    return datetime.fromtimestamp(seconds, UTC)


def generate(process="Claim Handling", cases=200, seed=0, workflow=None, now=None, spec=CLAIMS, replace=False):
    """Writes `cases` synthetic cases of `process`; returns how many events it wrote. Case ids are
    "<workflow>:<n>" (workflow: the process name as a slug, unless given: pass a real workflow id to see
    the heatmap on its canvas, whose node names must match the steps)."""
    now = (now or timezone.now()).timestamp()
    workflow = workflow or "seed-" + "-".join(process.lower().split())
    sim = Simulation(spec, cases, seed, now, process, workflow).run()
    claim = "claimed" in {f.name for f in UserTask._meta.fields}  # the model records claims from migration 0013
    with transaction.atomic():
        if replace:
            for model in (Event, UserTask, MessageSubscription, Case):
                model.objects.filter(case_id__startswith=f"{workflow}:").delete()
        elif Event.objects.filter(case_id__startswith=f"{workflow}:").exists():
            raise ValueError(f"cases of {workflow!r} exist already: pass replace=True (--replace) to write them again")
        people = {}
        for lane, names in spec["lanes"].items():
            group, _ = Group.objects.get_or_create(name=lane)
            for name in names:
                if name not in people:
                    user, created = get_user_model().objects.get_or_create(username=name)
                    if created:
                        user.set_unusable_password()  # a name for the log: nobody signs in as it
                        user.save()
                    people[name] = user
                people[name].groups.add(group)
        Event.objects.bulk_create([Event(**{**e, "started": when(e["started"]), "ended": when(e["ended"])})
                                   for e in sim.events], batch_size=2000)
        tasks = []
        for task in sim.tasks:
            if task["created"] > now:
                continue
            done, claimed = task["completed"] <= now, task["claimed"] <= now
            status = UserTask.COMPLETED if done else UserTask.CLAIMED if claimed else UserTask.OPEN
            fields = {"claimed": when(task["claimed"])} if claim and claimed else {}
            tasks.append((task["created"], UserTask(
                activity=task["name"], case_id=task["case"]["id"], process=process, title=task["name"],
                resume_url=f"http://seed.invalid/webhook-waiting/{task['case']['id']}", lane=task["lane"],
                kind=spec["steps"][task["name"]]["kind"], status=status, completed=when(task["completed"]) if done else None,
                result=task["result"] if done else None, assignee=people[task["person"]] if claimed else None, **fields)))
        messages = [(m["created"], MessageSubscription(
            activity=m["name"], case_id=m["case"]["id"], process=process, name=m["message"],
            correlation_key=m["case"]["id"], resume_url=f"http://seed.invalid/webhook-waiting/{m['case']['id']}/m",
            status=MessageSubscription.COMPLETED if m["completed"] <= now else MessageSubscription.OPEN,
            completed=when(m["completed"]) if m["completed"] <= now else None,
            payload=m["payload"] if m["completed"] <= now else None)) for m in sim.messages if m["created"] <= now]
        started = {}
        for e in sim.events:
            started[e["case_id"]] = min(started.get(e["case_id"], e["started"]), e["started"])
        records = [(started[c["id"]], Case(case_id=c["id"], process=process, variables=c["variables"]))
                   for c in sim.cases.values() if c["id"] in started]
        for model, rows in ((UserTask, tasks), (MessageSubscription, messages), (Case, records)):
            objects = model.objects.bulk_create([o for _, o in rows], batch_size=2000)
            for (created, _), o in zip(rows, objects):  # created is auto_now_add: set the simulated time after
                o.created = when(created)
            model.objects.bulk_update(objects, ["created"], batch_size=2000)
    return len(sim.events)
