"""A stochastic simulation answers with an interval, not a number ([BS] p.89, B50–B57): every headline
metric is the mean of independent replications with its Student t confidence interval at 90, 95 and
99 %; two alternatives run on the same random numbers are compared by the interval of their paired
differences. Pure Python, numpy and scipy."""
import math

import numpy as np
from scipy import stats

LEVELS = (0.90, 0.95, 0.99)


def _finite(values):
    return [float(v) for v in values if v is not None and math.isfinite(v)]


def summary(values):
    """mean, p50, p90 and max of samples (None when there are none)."""
    v = np.asarray(_finite(values))
    if not len(v):
        return {"n": 0, "mean": None, "p50": None, "p90": None, "max": None}
    return {"n": len(v), "mean": float(v.mean()), "p50": float(np.percentile(v, 50)),
            "p90": float(np.percentile(v, 90)), "max": float(v.max())}


def half_width(values, level=0.95):
    """Half the width of the t confidence interval of the mean (None with fewer than 2 values)."""
    v = _finite(values)
    if len(v) < 2:
        return None
    return float(stats.t.ppf((1 + level) / 2, len(v) - 1) * np.std(v, ddof=1) / math.sqrt(len(v)))


def interval(values):
    v = _finite(values)
    return {"n": len(v), "mean": float(np.mean(v)) if v else None, "sd": float(np.std(v, ddof=1)) if len(v) > 1 else None,
            "ci": {f"{round(level * 100)}": half_width(v, level) for level in LEVELS}}


def replicated(rows):
    """{metric: interval} over replications, each a {metric: value}."""
    keys = list(dict.fromkeys(k for row in rows for k in row))
    return {k: interval([row.get(k) for row in rows]) for k in keys}


def paired(a_rows, b_rows):
    """{metric: interval of b − a} over replications run on common random numbers; `significant` when
    the 95 % interval leaves out 0."""
    out = {}
    for k in dict.fromkeys(k for row in a_rows for k in row):
        diffs = [b[k] - a[k] for a, b in zip(a_rows, b_rows)
                 if a.get(k) is not None and b.get(k) is not None and math.isfinite(a[k]) and math.isfinite(b[k])]
        result = interval(diffs)
        ci = result["ci"]["95"]
        result["significant"] = ci is not None and result["mean"] is not None and abs(result["mean"]) > ci
        out[k] = result
    return out
