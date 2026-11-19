"""Re-encrypts the LLM providers' API keys with the current key (FIELD_ENCRYPTION_KEY, else SECRET_KEY) after
that changed: --from-secret-key is the one they were encrypted with (the old FIELD_ENCRYPTION_KEY, or the old
SECRET_KEY when there was none). Keys the current one already reads are left as they are."""
from django.core.management.base import BaseCommand, CommandError

from ...core import crypto
from ...models import AuditEntry, LLMProvider


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--from-secret-key", required=True, metavar="OLD")

    def handle(self, *args, from_secret_key, **options):
        rotated, failed = [], []
        for provider in LLMProvider.objects.all():
            if not crypto.encrypted(provider.api_key):  # the current key reads it (or it is empty)
                continue
            try:
                plain = crypto.decrypt(provider.api_key, from_secret_key)
            except crypto.CannotDecrypt:
                failed.append(provider.name)
                continue
            LLMProvider.objects.filter(pk=provider.pk).update(api_key=plain)  # the field encrypts it again
            rotated.append(provider.name)
        if rotated:
            AuditEntry.write("llmprovider.rotate_keys", object_type="llmprovider", providers=rotated)
        self.stdout.write(f"{len(rotated)} re-encrypted" + (f": {', '.join(rotated)}" if rotated else ""))
        if failed:
            raise CommandError(f"neither key decrypts the API key of {', '.join(failed)}: type it again in the admin")
