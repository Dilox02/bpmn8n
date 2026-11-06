"""What a paused n8n execution waits on: people (UserTask) or a message (MessageSubscription).

Each one moves through the same states, only by transitions (conditional updates: two people or
two messages can never both win):

    open ──claim──▶ claimed ──complete──▶ completed
      ▲  ◀─release──┘    │
      └──────────────────┴─────────────▶ cancelled   (n8n no longer waits: stopped, deleted, ended)
"""
import math
import uuid
from urllib.parse import urlsplit, urlunsplit

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from ..clients.http import ServiceUnavailable, post_json
from ..conf import settings
from .cases import Case, Event


class TransitionError(Exception):
    """The activity is not in a state that allows it (e.g. someone else completed it first)."""


class Cancelled(TransitionError):
    """n8n no longer waits for the activity: it was cancelled instead."""


def n8n_gone(error):
    """n8n will never take this resume: the execution was deleted (404) or ended, also when stopped
    (409 "... has finished ..."). ponytail: matches n8n's message; its 409 "running" is transient."""
    return error.status == 404 or error.status == 409 and "finished" in str(error)


class WaitingQuerySet(models.QuerySet):
    def waiting(self):
        """What executions still wait on."""
        return self.filter(status__in=WaitingActivity.WAITING)


class WaitingActivity(models.Model):
    OPEN, CLAIMED, COMPLETED, CANCELLED = "open", "claimed", "completed", "cancelled"
    WAITING = (OPEN, CLAIMED)
    TRANSITIONS = {  # action: (from, to)
        "claim": ((OPEN,), CLAIMED),
        "release": ((CLAIMED,), OPEN),
        "complete": (WAITING, COMPLETED),
        "cancel": (WAITING, CANCELLED),
    }

    activity = models.CharField(max_length=200, help_text="BPMN activity (n8n node name), for the event log")
    case_id = models.CharField(max_length=100, blank=True)
    process = models.CharField(max_length=200, blank=True, help_text="n8n workflow waiting on it")
    resume_url = models.CharField(max_length=1000)
    status = models.CharField(max_length=10, default=OPEN,
                              choices=[(s, s) for s in (OPEN, CLAIMED, COMPLETED, CANCELLED)])
    reason = models.CharField(max_length=500, blank=True, help_text="why it was cancelled")
    created = models.DateTimeField(auto_now_add=True)
    completed = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
        ordering = ["created"]

    @property
    def execution(self):
        """The n8n execution (what n8n's executions list shows as its ID)."""
        return self.case_id.rpartition(":")[2]

    def transition(self, action, **fields):
        """Moves to the state `action` leads to, if the current state allows it."""
        sources, target = self.TRANSITIONS[action]
        moved = type(self).objects.filter(pk=self.pk, status__in=sources).update(status=target, **fields)
        self.refresh_from_db()
        if not moved:
            raise TransitionError(f"cannot {action} “{self}”: it is {self.status}")

    def cancel(self, reason):
        self.transition("cancel", completed=timezone.now(), reason=reason[:500])

    def finish(self, resource, **fields):
        """Completes it and, once nothing else of the same wait is pending, resumes n8n. All in one
        transaction: if n8n does not take the resume nothing changes and it can be finished again;
        if n8n no longer waits, the whole wait is cancelled (raises Cancelled)."""
        now = timezone.now()
        try:
            with transaction.atomic():
                list(self.group().select_for_update().order_by("pk"))  # one finisher at a time per wait
                self.transition("complete", completed=now, **fields)
                self.close_rivals(now)
                if self.case_id:  # what the person or the message brought becomes process data
                    Case.merge(self.case_id, self.process, self.output())
                Event.objects.create(case_id=self.case_id, activity=self.activity, process=self.process,
                                     endpoint=self.bpmn_type, started=self.created, ended=now, status="complete",
                                     resource=resource, duration_ms=(now - self.created).total_seconds() * 1000,
                                     data={"output": self.output()})
                if (payload := self.resume_payload()) is not None:
                    post_json(self.n8n_resume_url(), payload)
        except ServiceUnavailable as e:
            self.refresh_from_db()
            if not n8n_gone(e):
                raise
            type(self).objects.filter(resume_url=self.resume_url).waiting().update(
                status=self.CANCELLED, completed=now, reason=f"the process no longer waits: {e}"[:500])
            self.refresh_from_db()
            raise Cancelled(f"“{self}” was cancelled: the process no longer waits for it") from e

    def group(self):
        """The activities of the same wait, itself included; by default just itself."""
        return type(self).objects.filter(pk=self.pk)

    def close_rivals(self, now):
        """Closes what lost to this one (an event-based gateway's race); nothing by default."""

    def resume_payload(self):
        """What resumes n8n (the waiting node's output), None while the wait goes on."""
        return self.output()

    def output(self):
        """The process variables finishing it brings; subclasses say which."""
        return {}

    def n8n_resume_url(self):
        """n8n signs resume URLs with its public host; call it at settings.N8N_URL instead.
        Only the path and query are taken from the stored URL (no request forgery to other hosts)."""
        base, url = urlsplit(settings.N8N_URL), urlsplit(self.resume_url)
        return urlunsplit((base.scheme, base.netloc, url.path, url.query, ""))


class UserTaskQuerySet(WaitingQuerySet):
    def for_user(self, user):
        """Tasks in the user's lanes (Django groups) or in no lane; superusers see all."""
        if user.is_superuser:
            return self
        return self.filter(Q(lane="") | Q(lane__in=user.groups.values("name")))


class UserTask(WaitingActivity):
    """BPMN User Task (a person helped by a form) or Manual Task (work done without IT, just
    confirmed) for a person of `lane`. A node that receives several items creates one task per
    item (BPMN multi-instance, one `batch`) and resumes when all of them are completed."""

    KINDS = ("userTask", "manualTask")
    FIELD_TYPES = ("string", "number", "boolean")

    kind = models.CharField(max_length=20, choices=[(k, k) for k in KINDS], default="userTask")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    lane = models.CharField(max_length=100, blank=True, help_text="Django group that may complete it")
    form = models.JSONField(default=list, blank=True)  # [{"name", "label", "type"}]
    context = models.JSONField(default=dict, blank=True)  # data shown to the person
    result = models.JSONField(null=True, blank=True)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 help_text="who claimed or completed it")
    batch = models.CharField(max_length=32, blank=True, help_text="the tasks of one node execution")
    instance = models.PositiveIntegerField(default=0, help_text="the item it is for")

    objects = UserTaskQuerySet.as_manager()

    def __str__(self):
        return self.title

    @property
    def bpmn_type(self):
        return self.kind

    @classmethod
    def register(cls, resume_url, activity, instances):
        """The tasks of one node execution, one per item: `instances` are their fields. Idempotent:
        n8n running the node again (a retry, a restart) gets the tasks it already has."""
        if existing := list(cls.objects.waiting().filter(resume_url=resume_url, activity=activity)):
            return sorted(existing, key=lambda t: t.instance)
        batch = uuid.uuid4().hex
        return [cls.objects.create(resume_url=resume_url, activity=activity, batch=batch, instance=i, **fields)
                for i, fields in enumerate(instances)]

    def group(self):
        return UserTask.objects.filter(batch=self.batch) if self.batch else super().group()

    def can_work(self, user):
        return user.is_superuser or not self.lane or user.groups.filter(name=self.lane).exists()

    def claim(self, user):
        if not self.can_work(user):
            raise TransitionError(f"“{self}” is for the {self.lane} lane")
        self.transition("claim", assignee=user)

    def release(self, user):
        if self.status == self.CLAIMED and self.assignee_id != user.pk and not user.is_superuser:
            raise TransitionError(f"“{self}” is claimed by {self.assignee}")
        self.transition("release", assignee=None)

    def complete(self, user, result):
        """`user` None: a system completes it through the API."""
        if user and self.status == self.CLAIMED and self.assignee_id != user.pk and not user.is_superuser:
            raise TransitionError(f"“{self}” is claimed by {self.assignee}")
        self.finish(getattr(user, "username", None) or "api", result=result, assignee=user or self.assignee)

    def clean_result(self, data):
        """Coerces submitted values to the form's field types; raises ValidationError."""
        result, errors = {}, {}
        for field in self.form:
            name, kind, raw = field["name"], field.get("type", "string"), data.get(field["name"])
            if kind == "boolean":
                result[name] = raw in (True, "true", "on", "1", 1)
            elif raw in (None, ""):
                errors[name] = "required"
            elif kind == "number":
                try:
                    result[name] = float(raw)
                except (TypeError, ValueError):
                    errors[name] = "must be a number"
                else:
                    if not math.isfinite(result[name]):  # NaN and inf are not JSON
                        errors[name] = "must be a finite number"
            else:
                result[name] = str(raw)
        if errors:
            raise ValidationError(errors)
        return result

    def output(self):
        return self.result or {}

    def resume_payload(self):
        """Once every task of the batch is done, one item per task, in the order of the items."""
        if self.group().waiting().exists():
            return None
        return {"_items": [{**t.output(), "_user_task_id": t.pk, "_completed_by": getattr(t.assignee, "username", None)}
                           for t in self.group().order_by("instance").select_related("assignee")]}


class MessageSubscriptionQuerySet(WaitingQuerySet):
    def waiting(self):
        """Open and not expired (an event-based gateway's timeout moves the execution on)."""
        return super().waiting().filter(Q(expires__isnull=True) | Q(expires__gt=timezone.now()))

    def for_message(self, name, correlation_key):
        """Waiting subscriptions for a message, oldest first."""
        return self.waiting().filter(name=name, correlation_key=correlation_key).order_by("created")


class MessageSubscription(WaitingActivity):
    """BPMN Receive Task: an execution waiting for message `name` about `correlation_key`
    (e.g. "payment-confirmed" for order 42). Another system delivers it via the API."""

    bpmn_type = "receiveTask"

    name = models.CharField(max_length=200)
    correlation_key = models.CharField(max_length=200)
    payload = models.JSONField(null=True, blank=True)
    expires = models.DateTimeField(null=True, blank=True, help_text="after this the execution has moved on (timeout)")

    objects = MessageSubscriptionQuerySet.as_manager()

    class Meta(WaitingActivity.Meta):
        indexes = [models.Index(fields=["name", "correlation_key", "status"])]

    def __str__(self):
        return f"{self.name} [{self.correlation_key}]"

    def group(self):  # an event-based gateway waits on several messages with one resume URL
        return MessageSubscription.objects.filter(resume_url=self.resume_url)

    def close_rivals(self, now):
        self.group().waiting().exclude(pk=self.pk).update(status=self.CANCELLED, completed=now,
                                                          reason=f"“{self.name}” came first")

    def deliver(self, payload):
        """A message comes from outside (a bank, a customer's reply): it adds process variables, it
        never changes the ones the case has (an IBAN, an amount, a decision). Those are left out and
        named in `reason`."""
        known = Case.objects.filter(case_id=self.case_id).values_list("variables", flat=True).first() or {}
        kept = sorted(k for k in payload if k in known)
        self.finish(f"message:{self.name}", payload={k: v for k, v in payload.items() if k not in known},
                    **({"reason": f"left out, the case already has them: {', '.join(kept)}"[:500]} if kept else {}))

    def output(self):
        return self.payload or {}

    def resume_payload(self):
        return {**self.output(), "_message": self.name, "_correlation_key": self.correlation_key}
