"""Writes a scenario from a real case of the event log into a project's scenarios.json (see
bpmn8n.workflows.scenarios.record): what a case did in production becomes a regression test. A
scenario of the same name is replaced. Then change its expectation to what should have happened."""
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from bpmn8n.api.tasks import discover
from bpmn8n.workflows.scenarios import record, summary


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("case_id", help='"<workflow id>:<execution id>", as the event log has it')
        parser.add_argument("folder", type=Path, help="e.g. projects/acme")
        parser.add_argument("--name", help='the scenario\'s name (default "case <case id>")')
        parser.add_argument("--workflow", default="workflow.json", help="relative to the folder (default workflow.json)")

    def handle(self, case_id, folder, name, workflow, **options):
        try:
            source = json.loads((folder / workflow).read_text())
        except (OSError, ValueError) as e:
            raise CommandError(f"cannot read {folder / workflow}: {e}") from None
        with redirect_stdout(sys.stderr):
            discover()
            try:
                scenario, report = record(case_id, source, name)
            except ValueError as e:
                raise CommandError(str(e)) from None
        if workflow != "workflow.json":
            scenario = {"name": scenario["name"], "workflow": workflow, **scenario}
        path = folder / "scenarios.json"
        scenarios = [s for s in (json.loads(path.read_text()) if path.exists() else []) if s.get("name") != scenario["name"]]
        path.write_text(json.dumps([*scenarios, scenario], indent=2, ensure_ascii=False) + "\n")
        self.stdout.write(f"{path}: {scenario['name']!r} recorded")
        self.stdout.write(summary(report) if report["passed"] else
                          f"the replay differs, complete the scenario by hand:\n{summary(report)}")
