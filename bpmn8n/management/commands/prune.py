"""Housekeeping (G10): deletes TaskRun rows older than --taskruns-older-than days, and archives Event rows older
than --events-older-than days to archive/events-<time>.csv.gz before deleting them (`manage.py import_eventlog`
reads such a file back; process mining keeps working on what is left). A case that still waits (a user task or a
message subscription) keeps all its rows. --dry-run only counts. Without options nothing is pruned.
Retention defaults: keep events 365 days and task runs 30 (n8n's own execution retention is shorter); the
scheduler prunes daily with PRUNE_DAYS for both."""
import csv
import gzip
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from ...models import Event, TaskRun
from ...models.waiting import WaitingActivity

# import_eventlog's columns: an archive reads back with it
COLUMNS = {"case_id": "case_id", "activity": "activity", "endpoint": "endpoint", "start_timestamp": "started",
           "end_timestamp": "ended", "status": "status", "resource": "resource"}


def waiting_cases():
    """The cases something still waits on: their rows stay."""
    return {c for kind in WaitingActivity.__subclasses__() for c in kind.objects.waiting().values_list("case_id", flat=True) if c}


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--events-older-than", type=int, metavar="DAYS")
        parser.add_argument("--taskruns-older-than", type=int, metavar="DAYS")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, events_older_than, taskruns_older_than, dry_run, **options):
        keep, now = waiting_cases(), timezone.now()
        if taskruns_older_than is not None:
            old = TaskRun.objects.filter(created__lt=now - timedelta(days=taskruns_older_than)).exclude(case_id__in=keep)
            self.stdout.write(f"{old.count()} task runs {'would be ' if dry_run else ''}deleted")
            if not dry_run:
                old.delete()
        if events_older_than is not None:
            old = Event.objects.filter(started__lt=now - timedelta(days=events_older_than)).exclude(case_id__in=keep)
            total = old.count()
            if dry_run or not total:
                self.stdout.write(f"{total} events {'would be ' if dry_run else ''}archived and deleted")
                return
            target = settings.BASE_DIR / "archive" / f"events-{now:%Y%m%d-%H%M%S}.csv.gz"
            target.parent.mkdir(exist_ok=True)
            ids = []
            with gzip.open(target, "wt", newline="", encoding="utf-8") as out:
                writer = csv.writer(out)
                writer.writerow(COLUMNS)
                for row in old.order_by("id").iterator():
                    writer.writerow([getattr(row, f).isoformat() if hasattr(getattr(row, f), "isoformat") else getattr(row, f)
                                     for f in COLUMNS.values()])
                    ids.append(row.id)
            # the file is closed (flushed) before a row goes; ponytail: ids in memory, 5 M rows are ~100 MB
            for i in range(0, len(ids), 5000):
                Event.objects.filter(id__in=ids[i:i + 5000]).delete()
            self.stdout.write(f"{total} events archived to {target.relative_to(settings.BASE_DIR)} and deleted")
