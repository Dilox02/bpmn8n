"""A .bpmn file (bpmn.io, Camunda Modeler, Signavio, or export_bpmn) as a project: writes projects/<name>/process.py (a
SPEC for Workflow.from_spec) and workflow.json, and runs the workflow check. What bpmn8n cannot build as it is
(a task with no @task of its name, an element n8n has no node for) is listed first; exits with 1 on errors."""
import json
import re
from pathlib import Path
from pprint import pformat

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bpmn8n.api.tasks import TASKS, discover
from bpmn8n.workflows import SpecError, Workflow
from bpmn8n.workflows.bpmn_import import bpmn_to_spec
from bpmn8n.workflows.check import WorkflowCheck

TEMPLATE = '''"""{title}: imported from {source}. SPEC is what Workflow.from_spec builds; workflow.json is the same, for n8n."""
from bpmn8n.workflows import Workflow

SPEC = {spec}


def workflow(credential=None):
    return Workflow.from_spec(SPEC, credential)
'''


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("file", type=Path)
        parser.add_argument("--project", required=True, help="the project: projects/<name>/ (letters, digits, _)")

    def handle(self, file, project, **options):
        if not re.fullmatch(r"[A-Za-z0-9_]+", project):
            raise CommandError("--project: letters, digits and _ only")
        if failed := discover():
            raise CommandError("tasks that do not import:\n" + "\n".join(f"{m}:\n{e}" for m, e in failed.items()))
        try:
            spec, problems = bpmn_to_spec(file.read_text(encoding="utf-8"), TASKS)
            workflow = Workflow.from_spec(spec).as_json()
        except (ValueError, SpecError, OSError) as e:  # SpecError is a ValueError
            raise CommandError(f"{file}: {e}") from e
        folder = Path(settings.BASE_DIR) / "projects" / project
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "__init__.py").touch()
        (folder / "process.py").write_text(TEMPLATE.format(title=spec["name"], source=file.name, spec=pformat(spec, sort_dicts=False)))
        (folder / "workflow.json").write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n")
        self.stdout.write(f"{folder / 'process.py'}\n{folder / 'workflow.json'}")
        for problem in problems:
            self.stdout.write(f"import  {problem}")
        errors = False
        for p in WorkflowCheck(workflow, fetch=lambda _: None).problems:
            self.stdout.write(f"{p.level:7} {p.node or '(workflow)'}: {p.message}")
            errors |= p.level == "error"
        if errors:
            raise CommandError("errors found")
