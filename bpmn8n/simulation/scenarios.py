"""Scenario analysis by token counting ([BS] p.18–21, 53–60), exact instead of drawn by hand.

- expected_visits: how many times each step runs per case. Routing a token from step i to step j with
  the branch probabilities gives a matrix Q (an AND split sends one token down every output, so a row
  can sum to more than 1); the expected visits are the start's row of the fundamental matrix
  (I − Q)⁻¹ of the absorbing chain, which counts every turn of a loop exactly.
- scenarios: the table students draw: every path a case can take (the outputs its choices take, in
  order), its probability and the tokens it gets out of N start tokens; loops are unrolled until a
  path's probability falls below `threshold`, and what is left is one "unrolled further" row, so the
  ending tokens sum to N. Paths are found by playing the case on the TokenEngine with scripted
  choices, so parallel branches, joins and loops follow n8n's semantics. A case through a parallel
  split ends once: its row is the combination of the choices on every branch, and lists every end
  its tokens reached.
Pure Python and numpy, no Django."""
import numpy as np

from ..engine import Outcome, Resolver, TokenEngine
from .spec import SimModel, SpecError


def routing(model: SimModel):
    """(flow nodes, Q): Q[i, j] = expected tokens step i sends to step j per visit."""
    g = model.graph
    nodes = g.flow_nodes
    index = {n: i for i, n in enumerate(nodes)}
    q = np.zeros((len(nodes), len(nodes)))
    for node in nodes:
        names, element = g.outputs(node), g.element(node)
        if element.splits:
            taken = dict.fromkeys(range(len(names)), 1.0)
        elif node in model.options:
            taken = {}
            for outputs, p in model.options[node]:
                for name in outputs:
                    taken[names.index(name)] = taken.get(names.index(name), 0.0) + p
        else:
            taken = {0: 1.0}
        for port, targets in enumerate(g.outs[node]):
            for target in targets:
                q[index[node], index[target]] += taken.get(port, 0.0)
    return nodes, q


def expected_visits(model: SimModel, start=None):
    """node -> expected visits per case, loops included. ponytail: a Merge passes on each token it joins
    (n8n's append mode, the builder's); one in combine mode after a parallel split is counted once per
    branch here (the engine and the simulator count it right). Upgrade: weight it from scenarios()."""
    nodes, q = routing(model)
    start = start or model.graph.starts[0]
    if len(nodes) and max(abs(np.linalg.eigvals(q))) >= 1 - 1e-12:
        raise SpecError("a case never ends: a loop is taken with probability 1 (or keeps multiplying tokens)")
    e = np.zeros(len(nodes))
    e[nodes.index(start)] = 1.0
    visits = np.linalg.solve(np.eye(len(nodes)) - q.T, e)  # = e (I − Q)⁻¹
    return {n: float(v) for n, v in zip(nodes, visits)}


class _Unrolled(Exception):
    """The path's probability fell below the threshold."""


class _Explorer(Resolver):
    """Takes the choices of `script` (option indexes, one per choice), then the first option, noting each
    new choice; `time(node, outputs)` gives a visit's duration (none by default)."""

    def __init__(self, model, script, threshold, time=None):
        self.model, self.script, self.threshold, self.time = model, script, threshold, time
        self.taken, self.fresh, self.probability = [], [], 1.0

    def resolve(self, visit):
        options = self.model.options.get(visit.node)
        outputs = None
        if options:
            k = len(self.taken)
            i = self.script[k] if k < len(self.script) else 0
            if k >= len(self.script) and len(options) > 1:
                self.fresh.append((k, self.probability, [p for _, p in options]))
            self.taken.append(i)
            outputs, p = options[i]
            self.probability *= p
            if self.probability < self.threshold:
                raise _Unrolled
        return Outcome(self.time(visit.node, outputs) if self.time else 0.0, outputs)


def scenarios(model: SimModel, tokens=100, threshold=1e-4, limit=2000, time=None):
    """The scenario table for `tokens` start tokens. `limit`: the most paths played (the rest is
    "unrolled further"); `time`: see _Explorer, then each row has the time its case takes."""
    rows, unrolled, todo, played = [], 0.0, [((), 1.0)], 0
    while todo:
        script, mass = todo.pop()
        if played >= limit:
            unrolled += mass
            continue
        played += 1
        explorer = _Explorer(model, script, threshold, time)
        engine = TokenEngine(model.graph, explorer, visit_limit=model.spec.visit_limit)
        case = engine.start()
        try:
            engine.run()
        except _Unrolled:
            unrolled += explorer.probability
            case = None
        for k, before, probabilities in explorer.fresh:
            for j, p in enumerate(probabilities[1:], 1):
                if before * p < threshold:
                    unrolled += before * p
                else:
                    todo.append(((*explorer.taken[:k], j), before * p))
        if case is not None:
            rows.append({"scenario": case.label, "probability": explorer.probability,
                         "tokens": tokens * explorer.probability, "status": case.status,
                         "decisions": [[node, list(outputs)] for node, outputs in case.decisions],
                         "path": [v.node for v in case.trace], "ends": case.ends,
                         **({"time": case.ended} if time else {})})
    rows.sort(key=lambda r: (len(r["decisions"]), -r["probability"]))
    total = sum(r["tokens"] for r in rows) + tokens * unrolled
    return {"tokens": tokens, "threshold": threshold, "rows": rows, "unrolled": tokens * unrolled,
            "total": total, "balanced": abs(total - tokens) <= 1e-6 * max(tokens, 1)}
