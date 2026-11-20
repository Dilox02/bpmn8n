"""The process contract of a project's workflows (bpmn8n.workflows.contract): what starts a case and with which
data, the messages it waits for and sends, its lanes, deadlines, results and sub-processes. In words by default;
--json the contract with the JSON Schema of its start body; --openapi the OpenAPI fragment of its start webhook and
message deliveries. A project with processes.py (ACME) has one contract per process."""
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from bpmn8n.api.tasks import discover
from bpmn8n.conf import settings
from bpmn8n.workflows.contract import Contract, project_workflows


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("folder", type=Path, help="e.g. projects/motor_insurance_claim, projects/acme")
        output = parser.add_mutually_exclusive_group()
        output.add_argument("--json", dest="as_json", action="store_true", help="the contracts as JSON")
        output.add_argument("--openapi", action="store_true", help="the OpenAPI fragments as JSON")

    def handle(self, folder, as_json, openapi, **options):
        with redirect_stdout(sys.stderr):  # what the activities print must not break the JSON
            discover()
            found = project_workflows(folder)
        if not found:
            raise CommandError(f"{folder}: no workflow.json, nor processes.py with PROCESSES")
        contracts = {key: Contract(workflow, declared) for key, workflow, declared in found}
        if openapi:
            self.stdout.write(json.dumps({k: c.openapi(settings.N8N_PUBLIC_URL) for k, c in contracts.items()}, indent=2))
        elif as_json:
            self.stdout.write(json.dumps({k: {**c.as_dict(), "start_schema": c.start_schema()} for k, c in contracts.items()},
                                         indent=2, ensure_ascii=False))
        else:
            self.stdout.write("\n\n".join(c.summary() for c in contracts.values()))
