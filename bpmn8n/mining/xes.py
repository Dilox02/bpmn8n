"""XES in and out (IEEE 1849) with PM4Py, which is why this is the only place the import wizard and the export
reach it: AGPL stays in bpmn8n/mining/."""
import os
import tempfile

import pandas as pd
import pm4py


def to_csv(raw, name="log.xes"):
    """An XES file's events as CSV text (one column per attribute; case attributes are prefixed `case:`)."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "log.xes.gz" if name.lower().endswith(".gz") else "log.xes")
        with open(path, "wb") as f:
            f.write(raw)
        try:
            df = pm4py.read_xes(path)
        except Exception as e:  # noqa: BLE001 - PM4Py raises whatever the parser does; the user needs the message
            raise ValueError(f"not an XES file: {e}") from None
    return pd.DataFrame(df).to_csv(index=False)


def to_xes(rows):
    """XES bytes from export rows (dicts: case_id, activity, started, ended, resource, status, process, attrs):
    the standard keys concept:name, time:timestamp (the start), org:resource, plus end_timestamp and the attributes."""
    df = pd.DataFrame([{"case:concept:name": r["case_id"], "concept:name": r["activity"], "time:timestamp": r["started"],
                        "end_timestamp": r["ended"], "org:resource": r["resource"], "status": r["status"],
                        "case:process": r["process"], **r["attrs"]} for r in rows])
    if df.empty:
        raise ValueError("no events to export")
    df = pm4py.format_dataframe(df, case_id="case:concept:name", activity_key="concept:name", timestamp_key="time:timestamp")
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "log.xes")
        pm4py.write_xes(df, path)
        with open(path, "rb") as f:
            return f.read()
