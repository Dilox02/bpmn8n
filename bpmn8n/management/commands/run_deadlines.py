"""Sends the reminders and escalations of user and manual tasks that are due (C2): a task still waiting at its
`remind_after` time gets its lane (or the `escalate_to` lane) an e-mail, or starts the escalation workflow through
its webhook with the case id. The task stays open; each reminder goes once. It also tells people about tasks that
are due within a day or overdue (H8: once per task, kind and person). Run it on a schedule (every minute)."""
from django.core.management.base import BaseCommand
from django.utils import timezone

from ...models import Notification, Reminder


class Command(BaseCommand):
    help = __doc__

    def handle(self, *args, **options):
        sent = failed = 0
        for reminder in Reminder.objects.filter(sent__isnull=True, at__lte=timezone.now()).select_related("task"):
            try:
                sent += reminder.fire()
            except Exception as e:  # one failing send must not hold back the others; it is tried again next run
                failed += 1
                self.stderr.write(f"{reminder}: {type(e).__name__}: {e}")
        told = Notification.sweep()
        self.stdout.write(f"{sent} sent, {failed} failed" + (f", {told} notified" if told else ""))
