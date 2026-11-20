"""Lists the rules of the workflow check (bpmn8n/workflows/rules.py) in the order they report; --md prints
the table of docs/GUIDE.md's "Workflow check" section (paste it there when a rule changes: a test compares)."""
from django.core.management.base import BaseCommand

from bpmn8n.workflows.rules import RULES, table


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--md", action="store_true", help="the guide's Markdown table")

    def handle(self, md, **options):
        if md:
            self.stdout.write(table())
        else:
            for r in RULES:
                self.stdout.write(f"{r.id:15} {r.level:8} {r.category:10} {r.doc}")
