"""What the event log says about one process, mined with PM4Py (scikit-learn for decision rules):
- discovery: variants, directly-follows graph with branch probabilities, a BPMN model (inductive miner);
- performance: case lead times, slowest transitions (bottlenecks), activity timings and resources;
- conformance: alignments of the closed cases against the n8n workflow, the model as designed;
- decision mining: how each gateway decides, how sure Laya is, which data explains the branch;
- concept drift: when the process changed (Bose et al., CAiSE 2011)."""
from collections import Counter
from functools import cached_property

import pandas as pd
import pm4py

from ..clients import n8n
from .log import ACTIVITY, CASE, TIMESTAMP
from .model import WorkflowModel, bpmn_xml

START, END = "▶", "■"
LOW_CONFIDENCE = 0.6  # a Laya decision less sure than this is worth a second look
MIN_DECISIONS = 20  # fewer decisions explain nothing
DRIFT_MIN_CASES = 2 * 50 * 8  # Bose's defaults: sub-logs of 50 cases, windows of 8, on both sides of a change


def is_decision(data):
    return isinstance(data, dict) and "decider" in data


def certainty(scores):
    """How sure Laya was: the chosen branch's confidence (exclusive), the least sure yes/no (inclusive)."""
    values = [v for v in scores.values() if v is not None]
    if not values:
        return None
    return values[0] if len(values) == 1 else min(max(p, 1 - p) for p in values)


def stats(values):
    s = pd.Series(values, dtype=float)
    return {k: round(float(v), 3) for k, v in zip(("p50", "p90", "p95", "max"), (*s.quantile([.5, .9, .95]), s.max()))} \
        if len(s) else {}


class ProcessAnalysis:
    """`models` caches workflow id -> WorkflowModel (None: not available) across processes."""

    def __init__(self, log, models=None, closed=None):
        self.log, self.df = log, log.df
        self.models = {} if models is None else models
        self.closed = (log.closed_cases() if closed is None else closed) & set(self.df[CASE])

    def as_dict(self):
        return {"cases": len(self.log), "closed_cases": len(self.closed), "variants": self.variants(),
                "directly_follows": self.directly_follows(), "activities": self.activities(),
                "performance": self.performance(), "conformance": self.conformance, "decisions": self.decisions(),
                "drift": self.drift}

    # --- discovery ------------------------------------------------------------------------

    def variants(self):
        counts = pm4py.get_variants(self.df)
        return [{"trace": list(trace), "cases": int(n)} for trace, n in sorted(counts.items(), key=lambda v: -v[1])]

    def directly_follows(self):
        """Edges with how many times they were followed and the branch probability out of `from`."""
        dfg, starts, ends = pm4py.discover_dfg(self.df)
        edges = Counter(dfg) + Counter({(START, a): n for a, n in starts.items()}) + Counter({(a, END): n for a, n in ends.items()})
        out = Counter()
        for (a, _), n in edges.items():
            out[a] += n
        return [{"from": a, "to": b, "cases": int(n), "probability": round(n / out[a], 3)}
                for (a, b), n in sorted(edges.items(), key=lambda e: -e[1])]

    def discovered_bpmn(self, noise_threshold=0.2):
        """The process as it really runs, as BPMN XML (inductive miner; bpmn.io and Camunda open it)."""
        return bpmn_xml(pm4py.discover_bpmn_inductive(self.df, noise_threshold=noise_threshold))

    # --- performance ----------------------------------------------------------------------

    def activities(self):
        return {name: {"executions": len(rows), "errors": int((rows.status == "error").sum()),
                       "median_ms": round(float(rows.duration_ms.median()), 1),
                       "max_ms": round(float(rows.duration_ms.max()), 1),
                       "resources": {k: int(v) for k, v in rows["org:resource"].value_counts().items()}}
                for name, rows in self.df.groupby(ACTIVITY)}

    def performance(self):
        """Lead time of the closed cases and the transitions that take longest (start to start)."""
        closed = self.df[self.df[CASE].isin(self.closed)]
        cases = closed.groupby(CASE).agg(start=(TIMESTAMP, "min"), end=("end_timestamp", "max"))
        transitions, _, _ = pm4py.discover_performance_dfg(self.df)
        slowest = sorted(transitions.items(), key=lambda e: -e[1]["median"])[:5]
        return {"lead_time_s": stats((cases.end - cases.start).dt.total_seconds()),
                "slowest_transitions": [{"from": a, "to": b, "median_s": round(t["median"], 3), "max_s": round(t["max"], 3)}
                                        for (a, b), t in slowest]}

    # --- conformance ----------------------------------------------------------------------

    def model(self, workflow_id):
        if workflow_id not in self.models:
            workflow = n8n.workflow(workflow_id)
            self.models[workflow_id] = WorkflowModel(workflow) if workflow else None
        return self.models[workflow_id]

    @cached_property
    def alignments(self):
        """case id -> alignment (PM4Py, optimal), for the closed cases whose workflow is available."""
        aligned = {}
        for workflow_id, log in self.log.where(self.closed).by_workflow().items():
            if model := self.model(workflow_id):
                cases = log.df[CASE].unique()  # PM4Py keeps the order of the cases
                aligned.update(zip(cases, pm4py.conformance_diagnostics_alignments(log.df, *model.petri_net())))
        return aligned

    @cached_property
    def deviating(self):
        return {case for case, a in self.alignments.items() if a["fitness"] < 1}

    @cached_property
    def conformance(self):
        """Fitness against the n8n model and the most common deviations: "unexpected X" (in the log,
        not allowed by the model) and "skipped X" (required by the model, missing from the log)."""
        if not self.alignments:
            return {"checked_cases": 0, "reason": "no closed case with its n8n workflow available (N8N_API_KEY)"}
        moves = Counter()
        for case in self.deviating:
            for logged, modeled in self.alignments[case]["alignment"]:
                if logged != modeled and modeled is not None:
                    moves[f"skipped {modeled}" if logged == ">>" else f"unexpected {logged}"] += 1
        fitness = [a["fitness"] for a in self.alignments.values()]
        return {"checked_cases": len(fitness), "fitness": round(sum(fitness) / len(fitness), 3),
                "conformant": round(1 - len(self.deviating) / len(fitness), 3),
                "deviations": dict(moves.most_common(10))}

    # --- decisions ------------------------------------------------------------------------

    def decisions(self):
        """Per gateway: who decides, which branches, Laya's certainty, the outcome each branch led to
        and the rules that explain the branch from the data (a decision tree)."""
        rows = self.df[self.df.data.map(is_decision)]
        outcome = self.log.outcomes()
        report = {}
        for gateway, events in rows.groupby(ACTIVITY):
            data = events.data.tolist()
            taken = [" + ".join(d["output"]["branches"]) or "(default)" for d in data]
            sure = [c for d in data if (c := certainty(d["output"].get("scores") or {})) is not None]
            reviewed = [bool(d["overridden"]) for d in data if "overridden" in d]
            closed = [(t, outcome[c]) for t, c in zip(taken, events[CASE]) if c in self.closed]
            report[gateway] = {
                "decided_by": dict(Counter(d["decider"] for d in data)),
                "branches": dict(Counter(taken)),
                "certainty": stats(sure),
                "low_certainty": round(sum(c < LOW_CONFIDENCE for c in sure) / len(sure), 3) if sure else None,
                "override_rate": round(sum(reviewed) / len(reviewed), 3) if reviewed else None,  # J6
                "outcomes": {branch: dict(Counter(o for t, o in closed if t == branch)) for branch in set(taken)},
                "rules": self.rules([d["input"].get("state") for d in data], taken),
            }
        return report

    @staticmethod
    def rules(states, labels):
        """A shallow decision tree from the case data to the branch; None if too little to learn."""
        if len(labels) < MIN_DECISIONS or len(set(labels)) < 2 or not all(isinstance(s, dict) for s in states):
            return None
        from sklearn.tree import DecisionTreeClassifier, export_text
        x = pd.json_normalize(states)
        # lists (e.g. hosts: ["h-1"]) are not rule material, and pandas cannot count their distinct values
        x = x.drop(columns=[c for c in x.columns if x[c].map(lambda v: isinstance(v, (list, dict))).any()])
        text = [c for c in x.columns if x[c].dtype == object and x[c].nunique() > 20]  # free text: no rule material
        x = pd.get_dummies(x.drop(columns=text)).apply(pd.to_numeric, errors="coerce").fillna(-1)
        if x.empty:
            return None
        tree = DecisionTreeClassifier(max_depth=3, min_samples_leaf=5, random_state=0).fit(x, labels)
        return {"fit_accuracy": round(tree.score(x, labels), 3), "tree": export_text(tree, feature_names=list(x.columns))}

    # --- drift ----------------------------------------------------------------------------

    @cached_property
    def drift(self):
        if len(self.log) < DRIFT_MIN_CASES:
            return {"checked": False, "reason": f"needs at least {DRIFT_MIN_CASES} cases"}
        from pm4py.algo.concept_drift import algorithm as concept_drift
        _, changes, p_values = concept_drift.apply(self.df)
        return {"checked": True, "change_points": [pd.Timestamp(t, unit="s", tz="UTC").isoformat() for t in changes],
                "p_values": [round(p, 3) for p in p_values]}
