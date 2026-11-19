"""Erases a case's personal data (GDPR art. 17): its variables, its steps' input and output, its user tasks' and
messages' data (bpmn8n/core/erasure.py says what stays for process mining). Refuses a case that still waits
unless --force, which cancels its user tasks and messages first."""
from django.core.management.base import BaseCommand, CommandError

from ...core.erasure import NoSuchCase, StillWaiting, erase


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("case_id", help="<workflow id>:<execution id>")
        parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
        parser.add_argument("--force", action="store_true", help="cancel what the case still waits for, then erase it")

    def handle(self, *args, case_id, yes, force, **options):
        if not yes and input(f"Erase the personal data of case {case_id}? It cannot be undone. [y/N] ").lower() != "y":
            raise CommandError("not erased")
        try:
            done = erase(case_id, force=force)
        except (NoSuchCase, StillWaiting) as e:
            raise CommandError(str(e)) from None
        self.stdout.write(f"erased {case_id}: " + ", ".join(f"{n} {what}" for what, n in done.items()))
