"""Gives a bpmn8n staff user the same account in n8n. n8n Community Edition takes no identity from bpmn8n, so
the two logins stay, with one email and one password (docs/spikes/one-origin.md). The password, set on both
sides, is asked (or --password). A fresh n8n gets the user as its owner; once it has one, the owner signs in
(--owner-email/--owner-password, default N8N_EMAIL/N8N_PASSWORD) to create the user as a member or reset its
password. bpmn8n's own password changes only after n8n accepted it."""
import getpass
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from ...clients import n8n
from ...models import AuditEntry


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--password", help="default: asked (a password on the command line shows in ps)")
        parser.add_argument("--owner-email", default=os.environ.get("N8N_EMAIL", ""))
        parser.add_argument("--owner-password", default=os.environ.get("N8N_PASSWORD", ""))

    def handle(self, *args, username, password, owner_email, owner_password, **options):
        user = get_user_model().objects.filter(username=username, is_staff=True).first()
        if not user:
            raise CommandError(f"{username}: no such staff user (n8n's editor is for staff only)")
        if not user.email:
            raise CommandError(f"{username} has no email: n8n signs in with it")
        password = password or getpass.getpass(f"Password for {username} on bpmn8n and n8n: ")
        owner = (owner_email, owner_password) if owner_email else None
        try:
            done = n8n.sync_account(user.email, password, user.first_name or user.username, user.last_name or "-", owner)
        except n8n.N8nError as e:
            raise CommandError(str(e)) from e
        user.set_password(password)
        user.save(update_fields=["password"])
        AuditEntry.write("user.n8n_sync", user, actor="manage.py n8n_sync_owner", n8n=done)
        self.stdout.write(f"{user.email}: {done} in n8n, the same password on bpmn8n")
