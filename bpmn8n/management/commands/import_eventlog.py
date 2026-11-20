"""Imports an event log (CSV or XES) into the Event table under a process name, as the import wizard at
/insights/import/ does: bpmn8n's own export (case_id, activity, endpoint, start_timestamp, end_timestamp, status,
resource), an older file-based log, or a log from another system. Columns are recognised by their usual names;
`--map case=Case ID,activity=Task,start=Begin,end=End,resource=Who` says which is which, and every other column
is kept in the event's data. `--check` only prints the readiness report."""
import os

from django.core.management.base import BaseCommand, CommandError

from bpmn8n.analytics import importer


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--process", help="the process name (default: the file's name)")
        parser.add_argument("--map", default="", help="role=column,...  roles: " + ", ".join(importer.ROLES[:-2]))
        parser.add_argument("--timestamp-format", default="auto", help="auto, iso, epoch, epoch_ms or a strptime pattern")
        parser.add_argument("--check", action="store_true", help="report on the file and write nothing")

    def handle(self, path, process, map, timestamp_format, check, **options):
        try:
            with open(path, "rb") as f:
                headers, rows = importer.read_table(f.read(), path)
            mapping = importer.Mapping.parse(map, headers) if map else importer.Mapping.from_roles(importer.guess_roles(headers))
            events, report = importer.prepare(rows, mapping, timestamp_format)
            for key in ("rows", "events", "cases", "format", "missing_case_or_activity", "missing_timestamps",
                        "start_after_end", "ties", "existing", "ignored"):
                self.stdout.write(f"{key}: {report[key]}")
            if not check:
                name = process or os.path.splitext(os.path.basename(path))[0]
                self.stdout.write(f"imported {importer.write(events, name)} events as {name!r}")
        except (OSError, importer.LogImportError) as e:
            raise CommandError(e) from None
