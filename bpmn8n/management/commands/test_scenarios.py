"""Plays only a project's scenarios (scenarios.json, see bpmn8n.workflows.scenarios) on the token
engine: quicker than check_project while a process is being changed. Exits with 1 if one fails;
--json prints the reports."""
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from bpmn8n.api.tasks import discover
from bpmn8n.workflows.scenarios import run_scenarios, summary


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []  # as check_project: a module that fails to import shows as a missing @task

    def add_arguments(self, parser):
        parser.add_argument("folder", type=Path, help="e.g. projects/acme")
        parser.add_argument("--json", dest="as_json", action="store_true", help="the reports as JSON on stdout")

    def handle(self, folder, as_json, **options):
        with redirect_stdout(sys.stderr):  # what the activities print must not break the JSON report
            discover()
            reports = run_scenarios(folder)
        if as_json:
            self.stdout.write(json.dumps(reports))
        else:
            for report in reports:
                self.stdout.write(summary(report))
            self.stdout.write(f"{folder}: {len(reports)} scenarios, {sum(not r['passed'] for r in reports)} failing")
        if not all(r["passed"] for r in reports):
            raise CommandError("scenarios fail")
