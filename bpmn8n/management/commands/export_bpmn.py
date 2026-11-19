"""A project's workflows as BPMN 2.0 XML with diagram interchange (bpmn_xml.py), one <name>.bpmn per workflow in
the project folder (or --out): opens in bpmn.io, Camunda Modeler, Signavio and BIMP. --data adds the data lens
(a data object per variable, with its data associations)."""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from bpmn8n.api.tasks import discover
from bpmn8n.workflows.bpmn_xml import to_bpmn
from bpmn8n.workflows.contract import project_workflows


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("folder", type=Path, help="e.g. projects/acme, projects/motor_insurance_claim")
        parser.add_argument("--out", type=Path, help="the folder to write to (default: the project's)")
        parser.add_argument("--data", action="store_true", help="add the data objects and data associations")

    def handle(self, folder, out, data, **options):
        discover()  # the data lens reads the tasks' inputs and outputs
        if not (found := project_workflows(folder)):
            raise CommandError(f"{folder}: no workflow.json, nor processes.py with PROCESSES")
        out = out or folder
        out.mkdir(parents=True, exist_ok=True)
        for key, workflow, _ in found:
            path = out / f"{key.rsplit('/', 1)[-1]}.bpmn"
            path.write_text(to_bpmn(workflow, data=data), encoding="utf-8")
            self.stdout.write(str(path))
