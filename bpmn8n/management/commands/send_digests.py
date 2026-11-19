"""Sends each person who opted in (/notifications/) their daily digest: what waits for them, most urgent first.
Nothing waiting, no mail; a person gets at most one a day. Run it once a day."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from ...models import Notification, Preference

SPACING = timedelta(hours=20)  # a run a little early or late must not skip or double a day


class Command(BaseCommand):
    help = __doc__

    def handle(self, *args, **options):
        now, sent = timezone.now(), 0
        for pref in Preference.objects.filter(digest=True, user__is_active=True).exclude(user__email="").select_related("user"):
            if pref.digest_sent and now - pref.digest_sent < SPACING:
                continue
            if Notification.digest(pref.user):
                Preference.objects.filter(pk=pref.pk).update(digest_sent=now)
                sent += 1
        self.stdout.write(f"{sent} digests sent")
