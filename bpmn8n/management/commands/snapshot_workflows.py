"""Keeps the current versions of n8n workflows in bpmn8n's history (WorkflowVersion). n8n's public API shows
only the current version, so run it from cron to keep every version past n8n's free 24 hours:

    python manage.py snapshot_workflows            # the workflows bpmn8n has seen (cases, earlier snapshots)
    python manage.py snapshot_workflows --all      # every workflow in n8n
    python manage.py snapshot_workflows <id> ...   # these ones
"""
from django.core.management.base import BaseCommand, CommandError

from ...clients.n8n import N8nError
from ...conf import settings
from ...core import history


class Command(BaseCommand):
    help = "Keep the current version of n8n workflows in bpmn8n's workflow history"

    def add_arguments(self, parser):
        parser.add_argument("workflow_ids", nargs="*")
        parser.add_argument("--all", action="store_true", help="every workflow in n8n")

    def handle(self, *args, **options):
        if not settings.N8N_API_KEY:
            raise CommandError("no N8N_API_KEY: ./start.sh creates one")
        try:
            if options["all"]:
                versions = [history.record(data) for data in history.workflows_in_n8n()]
            else:
                versions = [history.snapshot(i) for i in options["workflow_ids"] or history.seen_workflow_ids()]
        except N8nError as e:
            raise CommandError(str(e)) from e
        for version in filter(None, versions):
            self.stdout.write(f"{version.workflow_id}  {version.version_id}  {version.name}")
        self.stdout.write(f"{sum(v is not None for v in versions)} workflow(s), "
                          f"{history.WorkflowVersion.objects.count()} version(s) kept")
