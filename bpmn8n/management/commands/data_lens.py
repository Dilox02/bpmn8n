"""The data lens of a project's workflows: per step, the variables it reads and the variables it writes, from the
workflow check's data flow ({{variables}} in its fields, the inputs a task binds by name, a task's outputs, a
person's answers). The same as the data objects of `export_bpmn --data`."""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from bpmn8n.api.tasks import discover
from bpmn8n.workflows.check import WorkflowCheck
from bpmn8n.workflows.contract import project_workflows


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("folder", type=Path, help="e.g. projects/acme")

    def handle(self, folder, **options):
        discover()
        if not (found := project_workflows(folder)):
            raise CommandError(f"{folder}: no workflow.json, nor processes.py with PROCESSES")
        for key, workflow, declared in found:
            self.stdout.write(f"{key}")
            for step, (reads, writes) in WorkflowCheck(workflow, fetch=lambda _: None, start_data=declared).data_lens().items():
                self.stdout.write(f"  {step}: reads {', '.join(reads) or '-'} | writes {', '.join(writes) or '-'}")
