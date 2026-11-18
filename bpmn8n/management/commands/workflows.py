"""A project's workflow.json in git against its workflow in n8n (through N8N_URL and N8N_API_KEY):
  diff  what differs, ignoring node ids, positions, versions and dates; exits with 1 on drift (a CI gate)
  push  creates or updates the workflows in n8n, sub-processes first; never activates them
  pull  writes n8n's version back into workflow.json, so `git diff` shows the editor's changes
Without folders: every projects/<name>/ with a workflow.json. Exits with 2 when it cannot compare or deploy."""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bpmn8n.clients import n8n
from bpmn8n.ops import source


def shown(value):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 80 else text[:77] + "..."


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []  # no need to import every project's tasks

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("diff", "push", "pull"))
        parser.add_argument("folders", nargs="*", type=Path, help="projects/<name> (default: all with a workflow.json)")
        parser.add_argument("--json", dest="as_json", action="store_true", help="the reports as JSON on stdout")

    def handle(self, action, folders, as_json, **options):
        try:
            folders = [source.Folder(f) for f in folders or sorted(
                p.parent for p in (Path(settings.BASE_DIR) / "projects").glob("*/workflow.json")
                if not p.parent.name.startswith("_"))]
            reports = source.push(folders) if action == "push" else \
                [(source.compare if action == "diff" else source.pull)(f) for f in folders]
        except (source.SourceError, n8n.N8nError) as e:
            raise CommandError(str(e), returncode=2) from e
        if as_json:
            self.stdout.write(json.dumps(reports, ensure_ascii=False, indent=2))
        else:
            for report in reports:
                self.write(report)
        if action == "diff" and (drift := [r["project"] for r in reports if r["status"] != "same"]):
            raise CommandError(f"drift between git and n8n: {', '.join(drift)}", returncode=1)

    def write(self, report):
        self.stdout.write(f"{report['project']}  n8n {report['workflow'] or '-'}  {report['status']}")
        if report["status"] == "missing":
            self.stdout.write("  n8n has no workflow with this name: workflows push creates it")
        if report["status"] == "draft":
            self.stdout.write("  saved as a draft of the active workflow: publish it in the n8n editor")
        if report["status"] != "drift":
            return
        self.stdout.write("  (git → n8n)")
        for change in report["properties"]:
            self.stdout.write(f"  ~ workflow {self.change(change)}")
        for name in report["nodes"]["added"]:
            self.stdout.write(f"  + node {name}  (in n8n, not in git)")
        for name in report["nodes"]["removed"]:
            self.stdout.write(f"  - node {name}  (in git, not in n8n)")
        for name, changes in report["nodes"]["changed"].items():
            for change in changes:
                self.stdout.write(f"  ~ {name}: {self.change(change)}")
        for edge in report["connections"]["added"]:
            self.stdout.write(f"  + connection {edge}")
        for edge in report["connections"]["removed"]:
            self.stdout.write(f"  - connection {edge}")

    @staticmethod
    def change(change):
        return f"{change['path']}: {shown(change['git']) if 'git' in change else '(none)'} → " \
               f"{shown(change['n8n']) if 'n8n' in change else '(none)'}"
