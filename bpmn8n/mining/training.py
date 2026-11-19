"""Training data for a neural network that learns the processes, from the event log:
- prefixes: at every step of a case, what happened so far and the data known by then, with the
  next activity, the remaining time and the outcome (predictive process monitoring);
- decisions: every gateway decision with what it was decided on, the branches, Laya's scores and
  how the case ended (to distill Laya, or to reward a reasoning model).
Only closed cases that follow their model are used. Splits are temporal, by case start, and a
training case still running when validation starts is dropped: random splits leak the future
(Weytjens & De Weerdt, "Creating unbiased public benchmark datasets with data leakage prevention
for predictive process monitoring", BPM 2021)."""
import pandas as pd

from .analysis import END, is_decision
from .log import ACTIVITY, CASE, PROCESS, TIMESTAMP

SPLITS = (("train", 0.7), ("val", 0.85))  # cases by start time, cumulative; the rest is test
MIN_EXAMPLES = 20  # a label rarer than this in train is hardly learnable


def seconds(delta):
    return round(delta.total_seconds(), 3)


class TrainingData:
    def __init__(self, log, exclude=(), closed=None):
        """`closed`: the closed cases, if already known (the same moment as the analysis)."""
        self.log = log.where((log.closed_cases() if closed is None else closed) - set(exclude))
        self.split = self._split()
        self.outcome = self.log.outcomes()

    def _split(self):
        """case -> train / val / test."""
        df = self.log.df
        if df.empty:
            return pd.Series(dtype=object)
        cases = df.groupby(CASE).agg(start=(TIMESTAMP, "min"), end=("end_timestamp", "max"))
        split = pd.Series("test", index=cases.index)
        for name, share in reversed(SPLITS):
            split = split.mask(cases.start < cases.start.quantile(share), name)
        val_start = cases.start[split != "train"].min()
        return split[~((split == "train") & (cases.end >= val_start))]

    def cases(self, split=None):
        for case_id, events in self.log.df.groupby(CASE, sort=False):
            if (s := self.split.get(case_id)) and split in (None, s):
                yield case_id, s, events.to_dict("records")

    def prefixes(self, split=None):
        for case_id, s, events in self.cases(split):
            start, end = events[0][TIMESTAMP], max(e["end_timestamp"] for e in events)
            steps, known = [], {}
            for i, e in enumerate(events):
                steps.append({"activity": e[ACTIVITY], "resource": e["org:resource"], "elapsed_s": seconds(e[TIMESTAMP] - start)})
                if not is_decision(e["data"]):  # a decision's state repeats the known variables
                    for part in ("input", "output"):
                        known.update(v if isinstance(v := (e["data"] or {}).get(part), dict) else {})
                yield {"case_id": case_id, "process": e[PROCESS], "split": s, "prefix": list(steps), "known": dict(known),
                       "next_activity": events[i + 1][ACTIVITY] if i + 1 < len(events) else END,
                       "remaining_s": seconds(end - e["end_timestamp"]), "outcome": self.outcome[case_id]}

    def decisions(self, split=None):
        for case_id, s, events in self.cases(split):
            for e in filter(lambda e: is_decision(e["data"]), events):
                d = e["data"]
                yield {"case_id": case_id, "process": e[PROCESS], "split": s, "gateway": e[ACTIVITY],
                       "decider": d["decider"], "question": d["input"].get("question"), "options": d["input"].get("options"),
                       "state": d["input"].get("state"), "taken": d["output"]["branches"], "scores": d["output"]["scores"],
                       "outcome": self.outcome[case_id], "overridden": d.get("overridden"), "final": d.get("final")}

    DATASETS = ("prefixes", "decisions")

    def card(self):
        """What to know before training: sizes, how balanced each target is per split, and warnings
        for labels too rare in train or met only in validation/test."""
        df = self.log.df[self.log.df[CASE].isin(self.split.index)]
        if df.empty:
            return {"cases": {}, "targets": {}, "warnings": ["no closed, conformant case yet"]}
        split = df[CASE].map(self.split)
        nxt = df.groupby(CASE)[ACTIVITY].shift(-1).fillna(END)
        decisions = df[df.data.map(is_decision)]
        targets = {"next_activity": (nxt, split), "outcome": (self.outcome[self.split.index], self.split)}
        for gateway, rows in decisions.groupby(ACTIVITY):
            taken = rows.data.map(lambda d: " + ".join(d["output"]["branches"]) or "(default)")
            targets[f"decision: {gateway}"] = (taken, split[rows.index])
        report, warnings = {}, []
        for target, (labels, splits) in targets.items():
            table = pd.crosstab(labels, splits)
            report[target] = {label: {s: int(n) for s, n in row.items()} for label, row in table.iterrows()}
            train = table["train"] if "train" in table else pd.Series(0, index=table.index)
            warnings += [f"{target}: '{label}' never in train" for label in train[train == 0].index]
            warnings += [f"{target}: '{label}' only {n}x in train" for label, n in train[(train > 0) & (train < MIN_EXAMPLES)].items()]
        return {"cases": {s: int(n) for s, n in self.split.value_counts().items()}, "targets": report, "warnings": warnings}
