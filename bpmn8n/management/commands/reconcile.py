"""Brings bpmn8n in line with n8n: a person's task or a message wait whose workflow n8n no longer has can never be
finished (nothing would resume it), so it is cancelled with that reason, and the inbox stops counting it. Only when
n8n's API answers: an unreadable n8n says nothing about what it has. The scheduler runs it every hour."""
from django.core.management.base import BaseCommand

from bpmn8n.core import directory
from bpmn8n.models import AuditEntry, MessageSubscription, UserTask

REASON = "its workflow was deleted in n8n"


def orphans(model, deleted):
    """The waits of `model` in a workflow of `deleted`."""
    return [w for w in model.objects.waiting() if w.case_id.rpartition(":")[0] in deleted]


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="say what would be cancelled, change nothing")

    def handle(self, dry_run=False, **options):
        if directory.from_n8n() is None:
            self.stdout.write("n8n's API does not answer: nothing reconciled")
            return
        deleted = {r.id for r in directory.workflows() if r.state == "deleted"}
        found = {model: orphans(model, deleted) for model in (UserTask, MessageSubscription)}
        for model, waits in found.items():
            for wait in waits:
                if not dry_run:
                    wait.cancel(REASON)
                    AuditEntry.write(f"{model._meta.model_name}.reconcile", object_type=model._meta.model_name,
                                     object_id=str(wait.pk), case_id=wait.case_id, reason=REASON)
        verb = "would cancel" if dry_run else "cancelled"
        self.stdout.write(f"{verb} {len(found[UserTask])} task(s) and {len(found[MessageSubscription])} message wait(s) "
                          f"of {len(deleted)} workflow(s) deleted in n8n")
