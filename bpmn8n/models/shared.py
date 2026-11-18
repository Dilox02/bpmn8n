"""Global variables kept in the database, when no Redis is configured (core/shared.py)."""
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class GlobalVariable(models.Model):
    """A value every workflow shares ({{vars.<key>}}). Keys with a ":" are core/shared.py's own (locks)."""

    key = models.CharField(max_length=210, primary_key=True)
    value = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)  # SQL NULL is JSON null
    updated = models.DateTimeField(auto_now=True)
    expires = models.DateTimeField(null=True, blank=True, help_text="empty: never")

    def __str__(self):
        return self.key
