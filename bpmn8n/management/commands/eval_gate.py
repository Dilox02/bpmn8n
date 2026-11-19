"""Evaluation gate (F9): judges the newest runs of the local evaluations (the Metrics nodes' samples under
projects/<project>/run/) against their thresholds. Exits 1 when a run missed a threshold, naming the metric,
its value and the threshold; 0 when every threshold held; 2 when there is nothing to judge (no run, or no
run with a threshold)."""
from django.core.management.base import BaseCommand, CommandError

from bpmn8n.core.evaluations import EvaluationStore


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--project", help="only this project's runs (its folder under projects/)")
        parser.add_argument("--workflow", help="only this n8n workflow's runs (its id)")
        parser.add_argument("--goals", action="store_true", help="also fail when a process goal is missed (check_goals)")
        parser.add_argument("--last", type=int, default=1, help="the newest runs of each workflow to judge (default 1)")

    def handle(self, project, workflow, last, goals=False, **options):
        try:
            runs = EvaluationStore().judged(project, workflow, max(last, 1))
        except ValueError as e:
            raise CommandError(str(e), returncode=2) from None
        for run in runs:
            self.stdout.write(f"{run['status']}: {run['project']} · workflow {run['workflow_id']} · "
                              f"execution {run['execution_id']}")
            for m in run["missed"]:
                unit, sign = f" {m['unit']}" if m["unit"] else "", ">=" if m["direction"] == "higher" else "<="
                self.stdout.write(f"  {m['node']} · {m['name']} = {m['value']:g}{unit}, "
                                  f"threshold {sign} {m['threshold']:g}")
        scored = [run for run in runs if run["status"] != "unscored"]
        judged = []  # J8: the process goals, from the event log
        if goals:
            from bpmn8n.management.commands.check_goals import report
            judged = report(self, project)
        if not scored and not judged:
            raise CommandError("nothing to judge: no evaluation run" + (" with a threshold" if runs else ""), returncode=2)
        failed = [run for run in scored if run["status"] == "failed"]
        if failed or (missed := [g for g in judged if g["ok"] is False]):
            raise CommandError(f"{len(failed)} of {len(scored)} evaluation runs missed a threshold"
                               + (f", {len(missed)} of {len(judged)} goals missed" if goals else ""), returncode=1)
        self.stdout.write(f"ok: {len(scored)} evaluation runs met every threshold" + (f", {len(judged)} goals met" if goals else ""))
