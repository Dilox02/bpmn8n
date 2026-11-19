"""The token engine: plays cases of an n8n workflow (a ProcessGraph) with n8n's semantics on a virtual
clock, without n8n. The simulator runs it with sampled times; dry-run scenario tests will run it with
stubs ("given these answers, this case takes this path").

n8n's semantics, one token = one item:
- a trigger starts a case with one token; an output connected to several nodes forks the token to
  each (an AND split); a node that is not a Merge runs once per arriving token (an XOR join);
- a Merge fires once every input has a token or can no longer get one in this case: no live token of
  the case can reach that input without passing the Merge (n8n's execution order v1, loops included).
  In n8n's default mode (append) every item it joined goes on; in the others (combine, choose
  branch) one item does;
- a parallel gateway takes every output; an exclusive gateway the one output the Resolver picks, an
  inclusive one any subset of its branches (none: "default"), an event-based one "timeout" or one
  message;
- an error end event ends the case with an error and cancels its other tokens; a token whose output
  leads nowhere ends; a case completes when it has no token left;
- a node visited more than `visit_limit` times in one case stops it ("loop limit");
- tokens of a case that wait for something that never comes (a join no token can complete, a
  resource that never works) leave the case "stuck" when nothing else can happen.
ponytail: n8n runs one node of an execution at a time; here parallel branches overlap in time, which
is what people working on them do (automated steps take milliseconds).

Two strategies keep the engine free of what a visit means: a Resolver says how long a visit takes,
which outputs it takes and what data it adds (a stub, a sample, a real call); Resources make tokens
wait for a limited capacity. Every case keeps its trace. Pure Python, no Django."""
import heapq
import itertools
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ..workflows.graph import ProcessGraph

COMPLETED, ERROR, LOOP_LIMIT, RUNNING, STUCK = "completed", "error", "loop limit", "running", "stuck"


@dataclass(frozen=True)
class Outcome:
    """What a Resolver says about one visit: how long it takes, the outputs it takes (names, or port
    numbers; None: the element's default) and the data it adds to the item."""

    duration: float = 0.0
    outputs: tuple | list | None = None
    data: dict = field(default_factory=dict)


@dataclass(eq=False)
class Visit:
    """One run of a node for one token (for a Merge, for the tokens it joined): a line of the trace."""

    case: "Case"
    node: str
    token: int
    arrived: float
    data: dict
    started: float | None = None
    ended: float | None = None
    resource: str | None = None
    outputs: tuple = ()
    outcome: Outcome | None = None
    joined: list = field(default_factory=list)  # a Merge's: [(token, data)] it passes on

    @property
    def queued(self):
        """Time spent waiting for the resource (or, at a Merge, for the other branches)."""
        return None if self.started is None else self.started - self.arrived

    def as_dict(self):
        return {"node": self.node, "token": self.token, "arrived": self.arrived, "started": self.started,
                "ended": self.ended, "resource": self.resource, "outputs": list(self.outputs)}


@dataclass(eq=False)
class Case:
    id: int
    started: float
    data: dict
    trigger: str
    ended: float | None = None
    status: str = RUNNING
    error: str = ""
    trace: list = field(default_factory=list)
    decisions: list = field(default_factory=list)  # (node, outputs taken) of every choice, in order
    ends: list = field(default_factory=list)  # where its tokens ended
    visits: Counter = field(default_factory=Counter)
    tokens: Counter = field(default_factory=Counter)  # created = ended + joined + cancelled once done
    at: Counter = field(default_factory=Counter)  # live tokens per node (not those waiting at a Merge)
    active: set = field(default_factory=set)  # visits in progress

    @property
    def done(self):
        return self.status != RUNNING

    @property
    def live(self):
        t = self.tokens
        return t["created"] - t["ended"] - t["joined"] - t["cancelled"]

    @property
    def cycle_time(self):
        return None if self.ended is None else self.ended - self.started

    @property
    def waiting_time(self):
        return sum(v.queued for v in self.trace if v.queued is not None and v.resource)

    @property
    def label(self):
        """Its scenario: the outputs of its choices, in order ("on loan & hold-book & available")."""
        return " & ".join("+".join(map(str, outputs)) for _, outputs in self.decisions) or "(no choice)"

    def as_dict(self):
        return {"id": self.id, "status": self.status, "started": self.started, "ended": self.ended,
                "error": self.error, "scenario": self.label, "ends": self.ends,
                "trace": [v.as_dict() for v in self.trace]}


class Resolver:
    """Decides each visit: override resolve(). This one takes no time and the element's default outputs:
    every output of a parallel gateway, "default" of an exclusive or inclusive one, "timeout" of an
    event-based one, the first output of any other node."""

    def resolve(self, visit) -> Outcome:
        return Outcome()


class Stub(Resolver):
    """Fixed answers by node name, for tests and dry runs. `outputs`: {node: "name"} every visit, or
    {node: ["a", "b"]} that subset every visit, or {node: [["a"], ["b"]]} one per visit (the last one
    repeats); `durations`: {node: time}; `data`: {node: {field: value}}."""

    def __init__(self, outputs=None, durations=None, data=None):
        self.outputs, self.durations, self.data = outputs or {}, durations or {}, data or {}
        self._seen = Counter()

    def resolve(self, visit):
        answer = self.outputs.get(visit.node)
        if isinstance(answer, str):
            answer = [answer]
        elif answer and isinstance(answer[0], (list, tuple)):
            answer = answer[min(self._seen[visit.node], len(answer) - 1)]
        self._seen[visit.node] += 1
        return Outcome(self.durations.get(visit.node, 0.0), answer, self.data.get(visit.node, {}))


class TokenEngine:
    def __init__(self, workflow, resolver=None, resources=None, visit_limit=100):
        self.graph = workflow if isinstance(workflow, ProcessGraph) else ProcessGraph(workflow)
        self.resolver = resolver or Resolver()
        self.resources = resources
        self.visit_limit = visit_limit
        self.now = 0.0
        self.cases = []
        self._events, self._order, self._tokens, self._wakeups = [], itertools.count(), itertools.count(1), set()
        g = self.graph
        self._merges = [n for n in g.order if g.element(n).joins]
        self._merge_set = set(self._merges)
        # (source, port, merge) -> the Merge input it reaches (n8n's connection "index")
        self._input = {(source, port, link["node"]): link.get("index", 0)
                       for source, ports in g.outs.items()
                       for port, links in enumerate(g.connections.get(source, {}).get("main", [])[:len(ports)])
                       for link in links or [] if link["node"] in self._merges}
        self._inputs = {m: max([int(g.nodes[m].get("parameters", {}).get("numberInputs", 2))]
                               + [i + 1 for (_, _, mm), i in self._input.items() if mm == m]) for m in self._merges}
        self._feeds = defaultdict(set)  # (merge, input) -> nodes a token can reach that input from
        for (source, _, merge), i in self._input.items():
            self._feeds[(merge, i)] |= self._upstream(source, merge)
        self._waiting = {}  # (case id, merge) -> {input: [(token, data, arrived)]}

    def _upstream(self, node, merge):
        """`node` and the nodes that lead to it without passing `merge`."""
        if node == merge:
            return set()
        seen, todo = {node}, [node]
        while todo:
            for n in self.graph.into[todo.pop()] - seen - {merge}:
                seen.add(n)
                todo.append(n)
        return seen

    # --- the clock --------------------------------------------------------------------------

    def at(self, time, action, *args):
        heapq.heappush(self._events, (time, next(self._order), action, args))

    def start(self, at=0.0, data=None, trigger=None):
        """A new case: one token at its trigger (the first start of the workflow by default) at `at`."""
        if not (trigger or self.graph.starts):
            raise ValueError("the workflow has no start")
        case = Case(len(self.cases) + 1, at, dict(data or {}), trigger or self.graph.starts[0])
        self.cases.append(case)
        case.tokens["created"] += 1
        self.at(at, self._begin, case, next(self._tokens))
        return case

    def run(self, until=math.inf):
        """Plays every event up to `until`; returns the cases. Cases with tokens left once nothing else can
        happen are stuck; with events left after `until` they are still running."""
        while self._events and self._events[0][0] <= until:
            self.now, _, action, args = heapq.heappop(self._events)
            action(*args)
        if self._events:
            self.now = until
        else:
            for case in self.cases:
                if not case.done:
                    self._finish(case, STUCK, "tokens wait for what never comes: "
                                 + ", ".join(sorted({*+case.at, *(m for (c, m) in self._waiting if c == case.id)})))
        return self.cases

    # --- a token's life ---------------------------------------------------------------------

    def _begin(self, case, token):
        self._arrive(case, token, case.trigger, None, case.data)
        self._settle(case)

    def _arrive(self, case, token, node, came_from, data):
        if node in self._merge_set:
            port = self._input.get((*came_from, node), 0) if came_from else 0
            self._waiting.setdefault((case.id, node), defaultdict(list))[port].append((token, data, self.now))
            return
        if not self._count(case, node):
            return
        visit = Visit(case, node, token, self.now, data)
        case.trace.append(visit)
        case.at[node] += 1
        visit.resource = self.resources.of(node) if self.resources else None
        if visit.resource:
            self.resources.wait(visit.resource, visit)
            self._dispatch(visit.resource)
        else:
            self._start(visit)

    def _count(self, case, node):
        """One more visit of `node`; False (and the case stops) beyond the visit limit."""
        case.visits[node] += 1
        if case.visits[node] > self.visit_limit:
            self._finish(case, LOOP_LIMIT, f"{node} visited more than {self.visit_limit} times")
        return not case.done

    def _start(self, visit):
        visit.started = self.now
        visit.case.active.add(visit)
        visit.outcome = self.resolver.resolve(visit) or Outcome()
        self.at(self.now + max(visit.outcome.duration, 0.0), self._end, visit)

    def _end(self, visit):
        case = visit.case
        if visit not in case.active:  # cancelled with its case
            return
        case.active.discard(visit)
        visit.ended = self.now
        case.at[visit.node] -= 1
        if visit.resource:
            self._release(visit.resource)
        element = self.graph.element(visit.node)
        if element.kind == "errorEndEvent":
            case.tokens["ended"] += 1
            case.ends.append(visit.node)
            self._finish(case, ERROR, f"{visit.node}: {visit.data.get('error') or element.label}")
            return
        ports = self._ports(visit, element)
        names = self.graph.outputs(visit.node)
        visit.outputs = tuple(names[p] if p < len(names) else p for p in ports)
        if element.chooses:
            case.decisions.append((visit.node, visit.outputs))
        outs = self.graph.outs[visit.node]
        targets = [(p, t) for p in ports if p < len(outs) for t in outs[p]]
        added = visit.outcome.data
        for token, data in visit.joined or [(visit.token, visit.data)]:
            data = {**data, **added}
            if not targets:
                case.tokens["ended"] += 1
                case.ends.append(visit.node)
            for k, (port, target) in enumerate(targets):
                if case.done:  # a loop limit on the way: its live tokens were cancelled with it
                    return
                if k:
                    case.tokens["created"] += 1
                self._arrive(case, token if k == 0 else next(self._tokens), target, (visit.node, port), data)
        self._settle(case)

    def _ports(self, visit, element):
        names, taken = self.graph.outputs(visit.node), visit.outcome.outputs
        if taken is None:
            taken = names if element.splits else ["timeout"] if element.timeout_first else \
                ["default"] if element.chooses else [0]
        ports = []
        for t in taken:
            if isinstance(t, str) and t not in names:
                raise ValueError(f"{visit.node!r} has no output {t!r}, only {names}")
            ports.append(names.index(t) if isinstance(t, str) else int(t))
        ports = list(dict.fromkeys(ports))
        if element.chooses and not ports and element.default_last:
            ports = [len(names) - 1]  # an inclusive gateway that takes no branch takes "default"
        if (element.kind == "exclusiveGateway" or element.timeout_first) and len(ports) != 1:
            raise ValueError(f"{visit.node!r} takes exactly one output, not {list(taken)}")
        return ports

    # --- joins -------------------------------------------------------------------------------

    def _settle(self, case):
        """Fires the Merges that can fire, then completes the case if no token is left."""
        fired = True
        while fired and not case.done:
            fired = False
            for merge in self._merges:
                waiting = self._waiting.get((case.id, merge))
                if waiting and self._ready(case, merge, waiting):
                    del self._waiting[(case.id, merge)]
                    self._fire(case, merge, waiting)
                    fired = True
        if not case.done and case.live == 0:
            self._finish(case, COMPLETED)

    def _ready(self, case, merge, waiting):
        for i in range(self._inputs[merge]):
            if waiting.get(i):
                continue
            feeds = self._feeds.get((merge, i), ())
            if any(case.at[n] for n in feeds) or any(
                    m in feeds for (c, m) in self._waiting if c == case.id and m != merge):
                return False  # a token can still arrive there
        return True

    def _fire(self, case, merge, waiting):
        if not self._count(case, merge):
            return
        arrived = [entry for i in sorted(waiting) for entry in waiting[i]]
        append = self.graph.nodes[merge].get("parameters", {}).get("mode", "append") == "append"
        data = {}
        for _, d, _ in arrived:
            data.update(d)
        visit = Visit(case, merge, arrived[0][0], min(t for _, _, t in arrived), data)
        visit.joined = [(token, d) for token, d, _ in arrived] if append else [(arrived[0][0], data)]
        case.tokens["joined"] += len(arrived) - len(visit.joined)
        case.trace.append(visit)
        case.at[merge] += 1
        self._start(visit)

    # --- resources and the end of a case --------------------------------------------------------

    def _dispatch(self, name):
        res = self.resources
        while res.free(name) and res.queues[name]:
            opening = res.opens(name, self.now)
            if opening > self.now:
                if name not in self._wakeups and math.isfinite(opening):
                    self._wakeups.add(name)
                    self.at(opening, self._wake, name)
                return
            visit = res.next(name)
            if not visit.case.done:
                res.take(name, self.now)
                self._start(visit)

    def _wake(self, name):
        self._wakeups.discard(name)
        self._dispatch(name)

    def _release(self, name):
        self.resources.give(name, self.now)
        self._dispatch(name)

    def _finish(self, case, status, error=""):
        case.status, case.error, case.ended = status, error, self.now
        case.tokens["cancelled"] += case.live
        for visit in list(case.active):  # stop the work in progress and free its resource
            case.active.discard(visit)
            visit.ended = self.now
            if visit.resource:
                self._release(visit.resource)
        case.at.clear()
        for key in [k for k in self._waiting if k[0] == case.id]:
            del self._waiting[key]
