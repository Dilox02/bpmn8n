"""Checks a project folder before it is loaded (see bpmn8n.workflows.check.ProjectCheck): its
activities import and pass their examples (examples.json), its process passes its scenarios
(scenarios.json), its workflow (workflow.json) passes the workflow check. Exits with 1 on errors;
--json prints the report, as bpmn8n Studio reads it."""
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from bpmn8n.workflows.check import ProjectCheck
from bpmn8n.workflows.scenarios import summary


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []  # they import the URLs, hence the tasks: ProjectCheck reports what fails

    def add_arguments(self, parser):
        parser.add_argument("folder", type=Path, help="e.g. projects/acme")
        parser.add_argument("--json", dest="as_json", action="store_true", help="the report as JSON on stdout")

    def handle(self, folder, as_json, **options):
        with redirect_stdout(sys.stderr):  # what the activities print must not break the JSON report
            check = ProjectCheck(folder)
        if as_json:
            self.stdout.write(json.dumps(check.as_dict()))
        else:
            for module, error in check.imports.items():
                self.stdout.write(f"import  {module}:\n{error}")
            for task, problems in check.examples.items():
                self.stdout.write("".join(f"example {task}: {p}\n" for p in problems))
            for report in check.scenarios:
                self.stdout.write(summary(report))
            for p in check.problems:
                self.stdout.write(f"{p.level:7} {p.node or '(workflow)'}: {p.message}")
            self.stdout.write(f"{folder}: {'ok' if check.ok else 'errors found'}")
        if not check.ok:
            raise CommandError("errors found")
