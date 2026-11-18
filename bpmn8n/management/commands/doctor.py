"""Is bpmn8n ready to run cases? The checks of GET /api/health as a table, with their errors, then hints
from the configuration. Exits with 1 when something required (the database, Redis when configured, n8n)
is down."""
from django.core.management.base import BaseCommand, CommandError

from bpmn8n.ops import health


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []  # a doctor works on a broken installation too

    def handle(self, **options):
        results = health.run()
        self.stdout.write(f"{'check':10} {'status':7} {'ms':>8}  detail")
        for r in results:
            ms = f"{r.ms:8.1f}" if r.ms is not None else " " * 8
            self.stdout.write(f"{r.name:10} {r.status:7} {ms}  {r.error or ('not configured' if r.status == 'off' else '')}")
        self.stdout.write(f"overall: {health.overall(results)}")
        if hints := health.hints():
            self.stdout.write("hints:\n" + "\n".join(f"  - {hint}" for hint in hints))
        if down := [r.name for r in results if r.name in health.REQUIRED and r.status == "down"]:
            raise CommandError(f"required and down: {', '.join(down)}")
