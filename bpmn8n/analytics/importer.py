"""Importing an event log from another system ([DI] §1.2-1.3, §3.1-3.3): read a CSV (or XES, through
bpmn8n/mining/xes.py: PM4Py stays there), say which column plays which role, read the timestamps in a
chosen or detected format, report what is wrong before anything is written, then write the Event rows in
one transaction. The import wizard (web/importer.py) and `manage.py import_eventlog` share all of this."""
import csv
import io
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from django.db import transaction

from ..models import Event

MAX_BYTES = 25 * 1024 * 1024  # an upload's size cap; a bigger log goes through the command
ROLES = ("case", "activity", "start", "end", "resource", "status", "endpoint", "attribute", "ignore")
REQUIRED = ("case", "activity")
GUESS = {
    "case": ("case:concept:name", "case_id", "case id", "caseid", "case"),
    "activity": ("concept:name", "activity", "task", "event", "action"),
    "start": ("start_timestamp", "start", "start time", "start_time", "starttime", "begin"),
    "end": ("end_timestamp", "end", "end time", "end_time", "finish", "complete", "time:timestamp", "timestamp", "time"),
    "resource": ("org:resource", "resource", "user", "performer", "owner", "who"),
    "status": ("status",), "endpoint": ("endpoint",),
}
# a format is "iso", "epoch", "epoch_ms", or a strptime pattern; detection takes the first all samples fit
FORMATS = ("iso", "%Y-%m-%d %H:%M:%S.%f", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
           "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y/%m/%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d", "epoch", "epoch_ms")
SAMPLE = 200


class LogImportError(ValueError):
    """The file or the mapping cannot be read; the message says why."""


def read_table(raw, name=""):
    """(headers, rows) of an uploaded file: XES by its extension, else CSV with the delimiter sniffed."""
    if len(raw) > MAX_BYTES:
        raise LogImportError(f"the file is larger than {MAX_BYTES // 2**20} MB: import it with manage.py import_eventlog")
    if name.lower().endswith((".xes", ".xes.gz")):
        from ..mining import xes
        try:
            raw = xes.to_csv(raw, name).encode()
        except ValueError as e:
            raise LogImportError(str(e)) from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text, newline=""), dialect=dialect)
    rows = list(reader)
    if not reader.fieldnames or not rows:
        raise LogImportError("no rows: is the file a CSV with a header line?")
    return [h.strip() for h in reader.fieldnames], rows


def guess_roles(headers):
    """header -> role: the usual names of each role, the rest are attributes."""
    roles, taken = {}, set()
    for role, names in GUESS.items():
        for h in headers:
            if h.strip().lower() in names and h not in roles and role not in taken:
                roles[h] = role
                taken.add(role)
    return {h: roles.get(h, "attribute") for h in headers}


@dataclass
class Mapping:
    """Which column plays which role (`columns`: role -> header; a single timestamp column may be start or end)
    and which of the others are kept in Event.data (`attributes`)."""

    columns: dict = field(default_factory=dict)
    attributes: list = field(default_factory=list)

    @classmethod
    def from_roles(cls, roles):
        """From header -> role (the wizard's form and guess_roles)."""
        columns = {}
        for header, role in roles.items():
            if role in ROLES[:-2]:
                columns.setdefault(role, header)
        return cls(columns, [h for h, r in roles.items() if r == "attribute"])

    @classmethod
    def parse(cls, text, headers):
        """From `--map case=Case ID,activity=Task`: the other columns become attributes."""
        columns = {}
        for part in filter(None, (p.strip() for p in text.split(","))):
            role, _, header = part.partition("=")
            if role not in ROLES[:-2] or header not in headers:
                raise LogImportError(f"--map {part!r}: use role=column with a role of {', '.join(ROLES[:-2])} and a column of the file")
            columns[role] = header
        return cls(columns, [h for h in headers if h not in columns.values()])

    def problems(self):
        out = [f"no column is the {r}" for r in REQUIRED if r not in self.columns]
        if "start" not in self.columns and "end" not in self.columns:
            out.append("no column is a timestamp (start or end)")
        return out


def parse_time(text, fmt):
    """A timestamp as an aware datetime (no zone means UTC); None when empty or not in the format."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        if fmt == "iso":
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        elif fmt in ("epoch", "epoch_ms"):
            d = datetime.fromtimestamp(float(text) / (1000 if fmt == "epoch_ms" else 1), UTC)
        else:
            d = datetime.strptime(text, fmt)
    except (ValueError, OverflowError, OSError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def detect_format(values):
    """The first of FORMATS in which every non-empty sample reads; "iso" when none does."""
    samples = [v.strip() for v in values if v and v.strip()][:SAMPLE]
    return next((f for f in FORMATS if samples and all(parse_time(v, f) for v in samples)), "iso")


def prepare(rows, mapping, fmt="auto"):
    """The events a mapping makes of the rows, and the readiness report: rows, cases, rows left out and why,
    ties, and the columns kept or dropped. Nothing is written."""
    c = mapping.columns
    if problems := mapping.problems():
        raise LogImportError("; ".join(problems))
    if fmt == "auto":
        fmt = detect_format([r.get(c.get("start", ""), "") for r in rows] + [r.get(c.get("end", ""), "") for r in rows])
    events, report = [], Counter()
    for row in rows:
        case, activity = (row.get(c["case"]) or "").strip(), (row.get(c["activity"]) or "").strip()
        if not case or not activity:
            report["missing_case_or_activity"] += 1
            continue
        start, end = parse_time(row.get(c.get("start", "")), fmt), parse_time(row.get(c.get("end", "")), fmt)
        if (start or end) is None:
            report["missing_timestamps"] += 1
            continue
        start, end = start or end, end or start
        if start > end:
            report["start_after_end"] += 1  # kept as an instant at the start
            end = start
        events.append({"case": case[:100], "activity": activity[:200], "start": start, "end": end,
                       "resource": (row.get(c.get("resource", "")) or "").strip()[:200] or "system",
                       "status": (row.get(c.get("status", "")) or "").strip()[:10] or "complete",
                       "endpoint": (row.get(c.get("endpoint", "")) or "").strip()[:200] or activity[:200],
                       "data": {h: v.strip() for h in mapping.attributes if (v := row.get(h)) and v.strip()}})
    seen = Counter((e["case"], e["start"]) for e in events)
    cases = {e["case"] for e in events}
    out = {k: report[k] for k in ("missing_case_or_activity", "missing_timestamps", "start_after_end")}
    out.update(rows=len(rows), events=len(events), cases=len(cases), format=fmt,
               ties=len({case for (case, _), n in seen.items() if n > 1}),
               attributes=list(mapping.attributes), ignored=[h for h in (rows[0] if rows else {}) if h not in mapping.attributes
                                                             and h not in c.values()],
               first=min((e["start"] for e in events), default=None), last=max((e["end"] for e in events), default=None),
               existing=len(existing(cases)), ok=bool(events))
    return events, out


def existing(cases):
    """The case ids of this set that the Event table already has (an import adds, it does not replace)."""
    cases, found = list(cases), set()
    for i in range(0, len(cases), 500):
        found.update(Event.objects.filter(case_id__in=cases[i:i + 500]).values_list("case_id", flat=True).distinct())
    return found


def write(events, process):
    """The events into the Event table under a process name, in one transaction; returns how many."""
    if not process.strip():
        raise LogImportError("a process name is needed")
    with transaction.atomic():
        Event.objects.bulk_create([
            Event(case_id=e["case"], process=process.strip()[:200], activity=e["activity"], endpoint=e["endpoint"],
                  started=e["start"], ended=e["end"], duration_ms=(e["end"] - e["start"]).total_seconds() * 1000,
                  status=e["status"], resource=e["resource"], data={"attributes": e["data"]} if e["data"] else {})
            for e in events], batch_size=2000)
    return len(events)
