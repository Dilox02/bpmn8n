"""The discovered directly-follows map of a log, and Disco's level of detail on it ([DI] §4.1.2): the
activities slider keeps the most frequent activities that cover X% of the events, the paths slider the most
frequent edges up to Y% of the edge frequency, and every kept activity stays connected."""
from collections import defaultdict

from .stats import mean


def discover(log):
    """Activities (frequency, mean duration) and directly-follows edges (frequency, mean time between the end of
    one step and the start of the next), from consecutive steps of each case."""
    acts, edges = defaultdict(lambda: [0, []]), defaultdict(lambda: [0, []])
    for t in log.traces:
        for s in t.steps:
            acts[s.activity][0] += 1
            acts[s.activity][1].append(s.duration)
        for a, b in zip(t.steps, t.steps[1:]):
            e = edges[(a.activity, b.activity)]
            e[0] += 1
            e[1].append(max(b.started - a.ended, 0.0))
    return ({k: {"frequency": n, "mean": mean(d)} for k, (n, d) in acts.items()},
            {k: {"frequency": n, "mean": mean(d)} for k, (n, d) in edges.items()})


def prune(acts, edges, activities=100, paths=100):
    """The map at a level of detail: `activities` and `paths` are percentages (0 to 100)."""
    ranked = sorted(acts, key=lambda a: (-acts[a]["frequency"], a))
    total, covered, keep = sum(v["frequency"] for v in acts.values()), 0, []
    for a in ranked:
        if keep and covered >= total * activities / 100:
            break
        keep.append(a)
        covered += acts[a]["frequency"]
    kept = set(keep)
    pool = sorted((k for k in edges if k[0] in kept and k[1] in kept), key=lambda k: (-edges[k]["frequency"], k))
    budget, spent, chosen = sum(edges[k]["frequency"] for k in pool) * paths / 100, 0, set()
    for k in pool:
        if spent >= budget:
            break
        chosen.add(k)
        spent += edges[k]["frequency"]
    for a in keep:  # never leave a kept activity alone: its strongest edge stays
        if len(kept) > 1 and not any(a in k for k in chosen):
            if best := next((k for k in pool if a in k and k[0] != k[1]), None):
                chosen.add(best)
    return {a: acts[a] for a in keep}, {k: edges[k] for k in pool if k in chosen}


def layout(acts, edges):
    """{activity: (column, row)}: the column is the shortest distance from where cases start (nothing points at
    it), the rows follow frequency."""
    targets = {b for a, b in edges if a != b}
    roots = [a for a in acts if a not in targets] or sorted(acts, key=lambda a: (-acts[a]["frequency"], a))[:1]
    out, level, seen = {}, roots, set(roots)
    for column in range(len(acts)):
        if not level:
            break
        for row, a in enumerate(sorted(level, key=lambda a: (-acts[a]["frequency"], a))):
            out[a] = (column, row)
        level = [y for a in level for (x, y) in sorted(edges) if x == a and not (y in seen or seen.add(y))]
    last = max((c for c, _ in out.values()), default=-1) + 1
    for row, a in enumerate(a for a in sorted(acts) if a not in out):  # a cycle nothing leads into
        out[a] = (last, row)
    return out
