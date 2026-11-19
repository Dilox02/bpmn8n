"""Exporting the filtered event log ([DI] §7) as CSV or XES, optionally anonymised: case ids, resources and every
attribute value that is not a number or a time become stable pseudonyms (HMAC-SHA256 keyed with SECRET_KEY), so
the same person is the same pseudonym throughout the file and across exports, and nobody can reverse them
without the key. Activities, process names, statuses, numbers and times stay: they are what analysis needs.
Only the attributes an import kept (Event.data["attributes"]) are exported: the data a running case read and
produced stays in bpmn8n."""
import csv
import hashlib
import hmac
import io
import re
from datetime import UTC, datetime

from django.conf import settings

from ..models import Event
from . import importer
from .log import NOT_PEOPLE

BASE = ("case_id", "process", "activity", "start_timestamp", "end_timestamp", "status", "resource")
NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")


def rows(log):
    """One dict per step of the log's cases, in case and start order: case_id, process, activity, started and
    ended (aware datetimes), status, resource and attrs (the imported attributes, as text or numbers)."""
    ids = [s.id for t in log.traces for s in t.steps]
    info = {}
    for i in range(0, len(ids), 5000):
        info.update({pk: (p, d) for pk, p, d in Event.objects.filter(id__in=ids[i:i + 5000]).values_list("id", "process", "data")})
    for t in log.traces:
        for s in t.steps:
            process, data = info.get(s.id, (t.process, {}))
            attrs = (data or {}).get("attributes") or {}
            yield {"case_id": t.case_id, "process": process or t.process, "activity": s.activity,
                   "started": datetime.fromtimestamp(s.started, UTC), "ended": datetime.fromtimestamp(s.ended, UTC),
                   "status": s.status, "resource": s.resource,
                   "attrs": {k: v for k, v in attrs.items() if isinstance(v, str | int | float) and not isinstance(v, bool)}}


def plain(value):
    """A number or a time: not an identifier."""
    if isinstance(value, int | float):
        return True
    text = str(value).strip()
    return bool(NUMBER.fullmatch(text)) or any(importer.parse_time(text, f) for f in importer.FORMATS[:-2])


class Anonymiser:
    """Stable pseudonyms: `alias("case", "w:1")` is "case-" and 12 hex digits of HMAC(key, "case" + value)."""

    def __init__(self, key=None):
        self.key = (key or settings.SECRET_KEY).encode()

    def alias(self, kind, value):
        digest = hmac.new(self.key, "\0".join((kind, str(value))).encode(), hashlib.sha256).hexdigest()
        return f"{kind}-{digest[:12]}"

    def row(self, r):
        return {**r, "case_id": self.alias("case", r["case_id"]),
                "resource": r["resource"] if r["resource"] in NOT_PEOPLE else self.alias("resource", r["resource"]),
                "attrs": {k: v if plain(v) else self.alias("value", v) for k, v in r["attrs"].items()}}


def to_csv(events):
    """CSV text: the base columns, then one column per attribute (sorted by name)."""
    events = list(events)
    keys = sorted({k for e in events for k in e["attrs"]})
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([*BASE, *keys])
    for e in events:
        writer.writerow([e["case_id"], e["process"], e["activity"], e["started"].isoformat(), e["ended"].isoformat(),
                         e["status"], e["resource"], *(e["attrs"].get(k, "") for k in keys)])
    return out.getvalue()
