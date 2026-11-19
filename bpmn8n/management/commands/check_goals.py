"""J8: evaluates the process goals of the projects' evaluations.json (core/goals.py) against the event log.
Exits 1 when a goal is missed, 2 when a goal is not valid or none is declared, 0 when every goal holds."""
from django.core.management.base import BaseCommand, CommandError

from bpmn8n.core import goals


def report(command, project=None, process=None):
    """Prints every goal and returns them; raises CommandError (2) for an invalid goal."""
    try:
        found = goals.evaluate(project=project, process=process)
    except goals.GoalError as e:
        raise CommandError(str(e), returncode=2) from None
    for g in found:
        state = "no data" if g["ok"] is None else "ok" if g["ok"] else "MISSED"
        sign = ">=" if g["direction"] == "higher" else "<="
        got = "-" if g["value"] is None else f"{g['value']:g}"
        command.stdout.write(f"{state}: {g['project']} · {g['name']} · {g['kpi']} = {got} (target {sign} {g['target']:g}, "
                             f"{g['window_days']:g} days, {g['cases']} cases)")
    return found


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--project", help="only this project's goals")
        parser.add_argument("--process", help="only this process's goals")

    def handle(self, project, process, **options):
        found = report(self, project, process)
        if not found:
            raise CommandError("no goals declared: add \"goals\" to a project's evaluations.json", returncode=2)
        if missed := [g for g in found if g["ok"] is False]:
            raise CommandError(f"{len(missed)} of {len(found)} goals missed", returncode=1)
        self.stdout.write(f"ok: {len(found)} goals met or without data yet")
