"""What the cases in scope say, as Disco's statistics and cases views (User Guide §4.2–4.3): the overview,
events and cases in progress over time, the case-duration histogram, per activity and per resource,
variants, handoffs between people ([WM] A64: where delay, errors and expense arise), the decisions of
each gateway with Laya's confidence ([OC] Table 7) and two cohorts side by side (I9).

Durations are seconds. Case durations count closed cases only: an open case's duration is not known yet."""
import math
from collections import Counter, defaultdict

import numpy as np
from django.db.models import Count, Max

from ..models import Event
from .log import iso

LOW_CONFIDENCE = 0.6  # a Laya decision less sure than this is worth a second look (as process mining)
BUCKETS = (60, 300, 900, 3600, 3 * 3600, 6 * 3600, 86400, 7 * 86400, 30 * 86400)  # seconds, for timelines
MAX_POINTS = 60
NICE = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 3 * 3600, 6 * 3600, 12 * 3600, 86400,
        2 * 86400, 7 * 86400, 14 * 86400, 30 * 86400)  # bin widths for the duration histogram


def r(value, digits=3):
    return None if value is None or not math.isfinite(value) else round(float(value), digits)


def summary(values):
    """mean, median, p90 (linear interpolation), min and max; None when there is nothing to describe."""
    if not len(values):
        return None
    a = np.asarray(values, dtype=float)
    return {"mean": r(a.mean()), "median": r(np.median(a)), "p90": r(np.percentile(a, 90)),
            "min": r(a.min()), "max": r(a.max())}


def mean(values):
    return r(sum(values) / len(values)) if values else None


def overview(log):
    traces = log.traces
    closed = [t for t in traces if not t.open]
    events = sum(len(t.steps) for t in traces)
    errors = sum(s.status == "error" for t in traces for s in t.steps)
    return {"cases": len(traces), "open": len(traces) - len(closed), "closed": len(closed), "events": events,
            "errors": errors, "activities": len({s.activity for t in traces for s in t.steps}),
            "start": iso(log.start) if traces else None, "end": iso(log.end) if traces else None,
            "case_duration": summary([t.duration for t in closed])}


def bucket_size(span):
    """The smallest of BUCKETS that draws the window in at most MAX_POINTS points."""
    return next((b for b in BUCKETS if span / b <= MAX_POINTS), BUCKETS[-1])


def timeline(log):
    """Per time bucket: events started in it, and cases in progress during it (open ones until the end)."""
    if not log.traces:
        return {"bucket": None, "points": []}
    size = bucket_size(log.span)
    origin = math.floor(log.start / size) * size
    n = max(1, math.ceil((log.end - origin) / size))
    def index(t):
        return min(max(int((t - origin) // size), 0), n - 1)

    events, active = np.zeros(n, dtype=int), np.zeros(n + 1, dtype=int)
    for t in log.traces:
        for s in t.steps:
            events[index(s.started)] += 1
        active[index(t.start)] += 1
        active[index(log.end if t.open else t.end) + 1] -= 1
    active = np.cumsum(active)[:n]
    return {"bucket": size, "points": [{"start": iso(origin + i * size), "events": int(events[i]), "active": int(active[i])}
                                       for i in range(n)]}


def histogram(log, bins=16):
    """Closed cases by duration, in bins of a round width from 0."""
    durations = [t.duration for t in log.traces if not t.open]
    if not durations:
        return []
    top = max(durations)
    width = next((w for w in NICE if top / w <= bins), top / bins) if top > 0 else 1
    n = int(top // width) + 1  # half-open bins: the longest case falls in the last one
    counts = Counter(min(int(d // width), n - 1) for d in durations)
    return [{"from": r(i * width), "to": r((i + 1) * width), "cases": counts.get(i, 0)} for i in range(n)]


def activities(log):
    """Per activity: how often, in how many cases, how long, how long it waited before starting, errors."""
    per = defaultdict(lambda: {"durations": [], "waits": [], "cases": set(), "errors": 0})
    for t in log.traces:
        for s, wait in zip(t.steps, t.waits()):
            a = per[s.activity]
            a["durations"].append(s.duration)
            a["cases"].add(t.case_id)
            a["errors"] += s.status == "error"
            if wait is not None:
                a["waits"].append(wait)
    rows = [{"activity": name, "executions": len(a["durations"]), "cases": len(a["cases"]),
             "duration": summary(a["durations"]), "wait": mean(a["waits"]), "errors": a["errors"],
             "error_rate": r(a["errors"] / len(a["durations"]))} for name, a in per.items()]
    return sorted(rows, key=lambda row: (-row["executions"], row["activity"]))


def busy_time(intervals, start, end):
    """The length of the union of the intervals, clipped to [start, end]."""
    total, reach = 0.0, start
    for a, b in sorted(intervals):
        a, b = max(a, reach), min(b, end)
        if b > a:
            total += b - a
            reach = b
    return total


def resources(log):
    """Per resource: events, cases, busy time (the union of its events' spans) and utilisation over the
    window. A person's User Task spans creation to completion, waiting included: an upper bound."""
    per = defaultdict(lambda: {"intervals": [], "cases": set()})
    for t in log.traces:
        for s in t.steps:
            per[s.resource]["intervals"].append((s.started, s.ended))
            per[s.resource]["cases"].add(t.case_id)
    rows = []
    for name, p in per.items():
        busy = busy_time(p["intervals"], log.start, log.end)
        rows.append({"resource": name, "events": len(p["intervals"]), "cases": len(p["cases"]), "busy": r(busy),
                     "utilisation": r(busy / log.span) if log.span else None})
    return sorted(rows, key=lambda row: (-row["events"], row["resource"]))


def handoffs(log):
    """Where a case changes hands: consecutive steps by different resources, how often and how long the
    work waited at the handoff."""
    pairs = defaultdict(lambda: {"waits": [], "cases": set()})
    for t in log.traces:
        waits = t.waits()
        for i in range(1, len(t.steps)):
            before, after = t.steps[i - 1], t.steps[i]
            if before.resource != after.resource:
                pair = pairs[(before.resource, after.resource)]
                pair["waits"].append(waits[i])
                pair["cases"].add(t.case_id)
    total = sum(len(p["waits"]) for p in pairs.values())
    rows = [{"from": a, "to": b, "handoffs": len(p["waits"]), "cases": len(p["cases"]), "mean_wait": mean(p["waits"])}
            for (a, b), p in pairs.items()]
    return {"handoffs": total, "per_case": r(total / len(log.traces)) if log.traces else None,
            "pairs": sorted(rows, key=lambda row: (-row["handoffs"], row["from"], row["to"]))}


def variants(log, limit=None):
    """Sequences of activities, most frequent first, with their share of the cases (and the running
    total) and the mean duration of their closed cases."""
    groups = defaultdict(list)
    for t in log.traces:
        groups[t.variant].append(t)
    ranked = sorted(groups.items(), key=lambda v: (-len(v[1]), v[0]))
    total, covered, rows = len(log.traces), 0, []
    for rank, (variant, traces) in enumerate(ranked[:limit], 1):
        covered += len(traces)
        rows.append({"rank": rank, "activities": list(variant), "cases": len(traces),
                     "coverage": r(100 * len(traces) / total, 2), "cumulative": r(100 * covered / total, 2),
                     "duration": mean([t.duration for t in traces if not t.open])})
    return {"variants": len(groups), "rows": rows}


def certainty(scores):
    """How sure Laya was: the chosen branch's confidence (exclusive), the least sure yes/no (inclusive)."""
    values = [v for v in (scores or {}).values() if isinstance(v, int | float)]
    if not values:
        return None
    return values[0] if len(values) == 1 else min(max(p, 1 - p) for p in values)


def decision_rows(log):
    """The gateway events of the cases in scope, with what they decided (Event.data), streamed."""
    keep = log.case_ids
    rows = log.scope.events().filter(data__has_key="decider").values_list("case_id", "activity", "data")
    for case_id, activity, data in rows.iterator(chunk_size=2000):
        if case_id in keep and isinstance(data.get("output"), dict):
            yield activity, data


def decisions(log):
    """Per gateway: how many decisions, who decided, each branch's share, and Laya's confidence."""
    per = defaultdict(lambda: {"branches": Counter(), "deciders": Counter(), "sure": [], "reviewed": []})
    for activity, data in decision_rows(log):
        g = per[activity]
        g["branches"][" + ".join(data["output"].get("branches") or []) or "(default)"] += 1
        g["deciders"][str(data.get("decider"))] += 1
        if (c := certainty(data["output"].get("scores"))) is not None:
            g["sure"].append(c)
        if "overridden" in data:  # J6: a person reviewed it
            g["reviewed"].append(bool(data["overridden"]))
    rows = []
    for gateway, g in sorted(per.items()):
        n = sum(g["branches"].values())
        sure = g["sure"]
        rows.append({"gateway": gateway, "decisions": n, "decided_by": dict(g["deciders"]),
                     "branches": [{"branch": b, "count": c, "share": r(c / n)} for b, c in g["branches"].most_common()],
                     "confidence": {"mean": mean(sure), "median": r(float(np.median(sure))),
                                    "low": r(sum(c < LOW_CONFIDENCE for c in sure) / len(sure))} if sure else None,
                     "override": {"reviewed": len(g["reviewed"]), "overridden": sum(g["reviewed"]),
                                  "rate": r(sum(g["reviewed"]) / len(g["reviewed"]))} if g["reviewed"] else None})
    return rows


def overlay(log):
    """Per activity (an n8n node name): frequency, cases, mean and median duration, errors, and a 0–1
    heat for frequency and for mean duration (relative to the busiest and the slowest node)."""
    rows = {a["activity"]: a for a in activities(log)}
    most = max((a["executions"] for a in rows.values()), default=0)
    slowest = max((a["duration"]["mean"] for a in rows.values()), default=0)
    return {name: {"frequency": a["executions"], "cases": a["cases"], "mean": a["duration"]["mean"],
                   "median": a["duration"]["median"], "errors": a["errors"],
                   "heat": {"frequency": r(a["executions"] / most) if most else 0.0,
                            "duration": r(a["duration"]["mean"] / slowest) if slowest else 0.0}}
            for name, a in rows.items()}


def delta(a, b):
    """b − a, and b relative to a; None where either side is missing."""
    if a is None or b is None:
        return {"a": a, "b": b, "delta": None, "relative": None}
    return {"a": a, "b": b, "delta": r(b - a), "relative": r((b - a) / a) if a else None}


def compare(a, b):
    """Two cohorts side by side: the same figures for each, with the difference b − a."""
    oa, ob = overview(a), overview(b)
    da, db = oa["case_duration"] or {}, ob["case_duration"] or {}
    def rate(o):
        return r(o["errors"] / o["events"]) if o["events"] else None

    def per_case(o):
        return r(o["events"] / o["cases"]) if o["cases"] else None

    def share(x, o):  # of the cohort's cases, the share that ran the activity
        return r(x["cases"] / o["cases"]) if x and o["cases"] else (0.0 if o["cases"] else None)

    figures = [("cases", oa["cases"], ob["cases"]), ("events per case", per_case(oa), per_case(ob)),
               ("error rate", rate(oa), rate(ob))] + \
              [(f"case duration {k}", da.get(k), db.get(k)) for k in ("mean", "median", "p90")]
    acts_a, acts_b = ({x["activity"]: x for x in activities(c)} for c in (a, b))
    acts = []
    for name in sorted(acts_a.keys() | acts_b.keys(), key=lambda n: -(acts_a.get(n) or acts_b[n])["executions"]):
        xa, xb = acts_a.get(name), acts_b.get(name)
        acts.append({"activity": name, "cases_share": delta(share(xa, oa), share(xb, ob)),
                     "duration": delta(xa and xa["duration"]["mean"], xb and xb["duration"]["mean"])})
    gates_a, gates_b = ({g["gateway"]: g for g in decisions(c)} for c in (a, b))
    gates = []
    for name in sorted(gates_a.keys() | gates_b.keys()):
        sa = {x["branch"]: x["share"] for x in (gates_a.get(name) or {}).get("branches", [])}
        sb = {x["branch"]: x["share"] for x in (gates_b.get(name) or {}).get("branches", [])}
        gates.append({"gateway": name, "branches": [{"branch": br, **delta(sa.get(br, 0.0) if name in gates_a else None,
                                                                          sb.get(br, 0.0) if name in gates_b else None)}
                                                    for br in sorted(sa.keys() | sb.keys())]})
    return {"figures": [{"figure": k, **delta(x, y)} for k, x, y in figures], "activities": acts, "decisions": gates}


def processes():
    """The processes in the event log, the busiest first: the picker of the Insights page."""
    rows = Event.objects.cases().exclude(process="").order_by().values("process").annotate(
        cases=Count("case_id", distinct=True), last=Max("started")).order_by("-cases", "process")
    return list(rows)
