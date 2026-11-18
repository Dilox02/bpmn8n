"""Marks expired the user tasks whose interrupting deadline passed (n8n went on through "timeout" by
itself). Reading a task does it too; run this on a schedule so that lists and analytics see them closed."""
from django.core.management.base import BaseCommand

from ...models import UserTask


class Command(BaseCommand):
    help = __doc__

    def handle(self, *args, **options):
        self.stdout.write(f"{UserTask.objects.expire_due()} expired")
