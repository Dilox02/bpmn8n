"""The scheduler service: run_deadlines every minute, the daily digest if this checkout has one, and `prune`
daily when PRUNE_DAYS is set. A Postgres advisory lock keeps two schedulers from running the same job: the one
that does not get it waits and tries again."""
import time

from django.conf import settings as django_settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection

from ...conf import settings
from ...ops import scheduler

LOCK = 8675310  # pg_advisory_lock key of the scheduler


def locked():
    """True when this process holds the scheduler lock (kept while its connection lives). ponytail: other
    databases have no advisory lock, and run a single process anyway (development)."""
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK])
        return cursor.fetchone()[0]


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--ticks", type=int, help="stop after this many ticks (tests)")

    def handle(self, ticks=None, **options):
        while not locked():
            self.stdout.write("another scheduler runs; waiting")
            time.sleep(30)
        todo = scheduler.jobs(django_settings.BASE_DIR, settings.PRUNE_DAYS)
        self.stdout.write("scheduler: " + ", ".join(f"{j.name} every {j.every}s" for j in todo))
        scheduler.loop(todo, lambda job: call_command(job.name, *job.args), ticks=ticks)
