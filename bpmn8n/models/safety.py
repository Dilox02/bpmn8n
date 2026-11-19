"""Data safety: the audit log of what people and systems did, and the results of task runs kept for retries."""
from datetime import timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from ..core.audit import actor_of, client_ip, current_request


class AppendOnly(Exception):
    """An audit entry is never changed or deleted."""


class AuditEntry(models.Model):
    """One action: who (a username, "api" for the API token, "system"), what, on which object and case, from where.
    Append-only (save refuses changes, delete raises; the admin shows it read-only). `details` names what changed
    (fields, a person handed a task, a variable's key), never process data: the log outlives an erasure.
    ponytail: append-only in the code; on Postgres, REVOKE UPDATE, DELETE from the web role enforces it, and nothing
    prunes it yet (export, then delete, past the retention a policy sets)."""

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.CharField(max_length=150)
    action = models.CharField(max_length=50, db_index=True)  # <object type>.<verb>, e.g. usertask.claim
    object_type = models.CharField(max_length=50, blank=True)
    object_id = models.CharField(max_length=200, blank=True)
    case_id = models.CharField(max_length=100, blank=True, db_index=True)
    details = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    ip = models.GenericIPAddressField(null=True, blank=True, help_text="the client's, when there was a request")

    class Meta:
        ordering = ("-at", "-id")
        verbose_name_plural = "audit entries"

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M:%S} {self.actor} {self.action} {self.object_type} {self.object_id}".strip()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnly("audit entries are never changed")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnly("audit entries are never deleted")

    @classmethod
    def write(cls, action, target=None, *, actor=None, object_type="", object_id="", case_id="", **details):
        """Records `action` on `target` (a model instance) or on object_type/object_id; who acts is `actor`, or
        whoever the current request is from (core/audit.py)."""
        request = current_request()
        if target is not None:
            object_type, object_id = object_type or target._meta.model_name, object_id or target.pk
            case_id = case_id or getattr(target, "case_id", "")
        return cls.objects.create(actor=actor_of(actor, request)[:150], action=action, object_type=object_type,
                                  object_id=str(object_id)[:200], case_id=case_id or "", details=details,
                                  ip=client_ip(request))


class TaskRun(models.Model):
    """The result of a task run an n8n node made in a case (its case, node id, run index and item index, see
    core/observability.Trace): n8n repeating the same run (retryOnFail, a worker restart) gets it back without
    the function running again. "running" while the first call works; a failure deletes the row.
    ponytail: kept for ever; prune rows older than n8n's execution retention when the table grows
    (TaskRun.objects.filter(created__lt=...).delete()); an erasure deletes its case's."""

    RUNNING, DONE = "running", "done"
    STALE = timedelta(seconds=900)  # gunicorn's graceful timeout: no call works longer, so an older "running" died

    task = models.CharField(max_length=200)
    case_id = models.CharField(max_length=100)
    node_id = models.CharField(max_length=200)
    run_index = models.PositiveIntegerField()
    item_index = models.PositiveIntegerField()
    status = models.CharField(max_length=10, default=RUNNING, choices=[(s, s) for s in (RUNNING, DONE)])
    response = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)  # {"result", "extracted"}
    created = models.DateTimeField(auto_now_add=True)
    started = models.DateTimeField(help_text="when the call that holds it began")

    class Meta:
        constraints = (models.UniqueConstraint(fields=("task", "case_id", "node_id", "run_index", "item_index"),
                                               name="unique_task_run"),)
        indexes = (models.Index(fields=["case_id"]),)

    def __str__(self):
        return f"{self.task} {self.case_id} {self.node_id}#{self.run_index}.{self.item_index}"

    @classmethod
    def claim(cls, task, trace):
        """For a call that carries its run's identity (`trace`: an n8n node in a case): (its row, None) when this call
        runs the function, (None, the answer) when the same run already ran (200, its response) or still runs (409:
        n8n's retryOnFail tries again and then gets the response). (None, None) without the identity. The unique row
        is created first, so two identical calls at once never both run the function."""
        if not (trace.case_id and trace.node_id and trace.run_index is not None and trace.item_index is not None):
            return None, None
        key = {"task": task, "case_id": trace.case_id, "node_id": trace.node_id, "run_index": trace.run_index,
               "item_index": trace.item_index}
        now = timezone.now()
        try:
            with transaction.atomic():
                return cls.objects.create(**key, started=now), None
        except IntegrityError:
            pass
        if (run := cls.objects.filter(**key).first()) is None:  # the call that held it failed meanwhile
            return cls.claim(task, trace)
        if run.status == cls.DONE:
            return None, (200, run.response)
        if cls.objects.filter(pk=run.pk, status=cls.RUNNING, started__lt=now - cls.STALE).update(started=now):
            return run, None  # the call that held it died with its worker: this one runs it
        return None, (409, {"detail": f"{task} is already running for this node, run and item of the case: "
                                      "retry to get its result"})

    def done(self, response):
        type(self).objects.filter(pk=self.pk).update(status=self.DONE, response=response)
