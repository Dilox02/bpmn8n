"""Writes a synthetic event log (bpmn8n/analytics/seed.py): arrivals, variants, a loop, a Laya gateway
with its decisions, people in lanes with their queues, some errors, the last cases still open. For
demos of the Insights page and the canvas heatmap without running n8n."""
from django.core.management.base import BaseCommand, CommandError

from bpmn8n.analytics.seed import generate


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--process", default="Claim Handling", help="the process (workflow) name")
        parser.add_argument("--cases", type=int, default=300)
        parser.add_argument("--seed", type=int, default=0, help="the same seed writes the same log")
        parser.add_argument("--workflow", help="the workflow id in the case ids (default: seed-<process>); "
                                               "a real one shows the heatmap on its canvas")
        parser.add_argument("--replace", action="store_true", help="delete this workflow's seeded cases first")

    def handle(self, process, cases, seed, workflow, replace, **options):
        try:
            events = generate(process, cases, seed, workflow, replace=replace)
        except ValueError as e:
            raise CommandError(str(e)) from None
        self.stdout.write(f"wrote {cases} cases of {process!r}: {events} events")
