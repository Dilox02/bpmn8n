"""What a paused n8n execution waits on: people (UserTask) or a message (MessageSubscription).

Each one moves through the same states, only by transitions (conditional updates: two people or
two messages can never both win):

    open ──claim──▶ claimed ──complete──▶ completed
      ▲  ◀─release──┘    │
      └──────────────────┴─────────────▶ cancelled   (n8n no longer waits: stopped, deleted, ended)

A user task also has suspended (n8n still waits, nobody may claim it) and expired (its interrupting
deadline passed and n8n went on through "timeout"): see UserTask.
"""
import json
import math
import uuid
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit, urlunsplit

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import models, transaction
from django.db.models import F, Q
from django.utils import timezone

from ..clients.http import ServiceUnavailable, post_json
from ..conf import settings
from ..core.streaming import publish
from ..workflows import builder
from .cases import Case, Event
from .safety import AuditEntry


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
    registration_key = models.CharField(max_length=64, null=True, blank=True, unique=True, editable=False)
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

    def transition(self, action, actor=None, **fields):
        """Moves to the state `action` leads to, if the current state allows it, and writes it in the audit log:
        `actor` (a user or a name) did it, or whoever the request being served is from (AuditEntry.write)."""
        sources, target = self.TRANSITIONS[action]
        moved = type(self).objects.filter(pk=self.pk, status__in=sources).update(status=target, **fields)
        self.refresh_from_db()
        if not moved:
            raise TransitionError(f"cannot {action} “{self}”: it is {self.status}")
        details = {"activity": self.activity, "status": self.status}
        if action in ("reassign", "delegate"):
            details["assignee"] = getattr(self.assignee, "username", None)
        if action == "cancel":
            details["reason"] = self.reason
        AuditEntry.write(f"{self._meta.model_name}.{action}", self, actor=actor, **details)

    def cancel(self, reason, actor=None):
        self.transition("cancel", actor=actor, completed=timezone.now(), reason=reason[:500])

    @staticmethod
    def registration_id(*parts):
        return uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(parts)).hex

    def finish(self, resource, actor=None, **fields):
        """Completes it and, once nothing else of the same wait is pending, resumes n8n. All in one
        transaction: if n8n does not take the resume nothing changes and it can be finished again;
        if n8n no longer waits, the whole wait is cancelled (raises Cancelled)."""
        now = timezone.now()
        try:
            with transaction.atomic():
                list(self.group().select_for_update().order_by("pk"))  # one finisher at a time per wait
                self.refresh_from_db()
                fields = self.completion_fields(fields)
                self.transition("complete", actor=actor, completed=now, **fields)
                self.close_rivals(now)
                if self.case_id:  # what the person or the message brought becomes process data
                    from ..core.overrides import capture  # J6: a form that changes an automatic outcome is kept
                    capture(self.case_id, self.output())
                    Case.merge(self.case_id, self.process, self.output())
                publish(Event.objects.create(case_id=self.case_id, activity=self.activity, process=self.process,
                                     endpoint=self.bpmn_type, started=self.created, ended=now, status="complete",
                                     resource=resource, duration_ms=(now - self.created).total_seconds() * 1000,
                                     data={"output": self.output()}))  # + log streaming
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

    def completion_fields(self, fields):
        """Validate and prepare the result while the wait's rows are locked."""
        return fields

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
    def waiting(self):
        """What n8n still waits on: open, claimed or suspended, and not past an interrupting deadline
        (by then n8n has gone on through "timeout", as with a message's `expires`)."""
        return self.filter(status__in=UserTask.WAITING).exclude(deadline=True, due__lte=timezone.now())

    def workable(self):
        """Waiting and not suspended: what people can work on now (the inbox and its count)."""
        return self.waiting().exclude(status=UserTask.SUSPENDED)

    def for_user(self, user):
        """Tasks in the user's lanes (Django groups) or in no lane; superusers see all."""
        if user.is_superuser:
            return self
        return self.filter(Q(lane="") | Q(lane__in=user.groups.values("name")))

    def overdue(self):
        return self.filter(due__lt=timezone.now())

    def by_urgency(self):
        """Overdue first, then by priority (urgent first), then the earliest due, then the oldest."""
        return self.annotate(
            late=models.Case(models.When(due__lt=timezone.now(), then=0), default=1),
            rank=models.Case(*(models.When(priority=p, then=i) for i, p in enumerate(reversed(UserTask.PRIORITIES))),
                             default=len(UserTask.PRIORITIES)),
        ).order_by("late", "rank", F("due").asc(nulls_last=True), "created")

    def expire_due(self):
        """Marks expired the tasks whose interrupting deadline passed: lazily, whenever tasks are read,
        and by `manage.py expire_tasks`. One conditional update, so a completion cannot slip past it.
        Returns how many expired."""
        with transaction.atomic():
            due = list(self.filter(status__in=UserTask.WAITING, deadline=True, due__lte=timezone.now())
                       .select_for_update().values_list("pk", "case_id", "activity"))
            if due:
                UserTask.objects.filter(pk__in=[pk for pk, _, _ in due]).update(
                    status=UserTask.EXPIRED, completed=F("due"), reason="deadline passed")
            for pk, case_id, activity in due:
                AuditEntry.write("usertask.expire", actor="system", object_type="usertask", object_id=pk,
                                 case_id=case_id, activity=activity, status=UserTask.EXPIRED)
        return len(due)


class UserTask(WaitingActivity):
    """BPMN User Task (a person helped by a form) or Manual Task (work done without IT, just
    confirmed) for a person of `lane`. A node that receives several items creates one task per
    item (BPMN multi-instance, one `batch`) and resumes when all of them are completed, or the
    `quorum` of them (the rest are cancelled).

    Beyond claim, release and complete ([BO] §4.3.1): suspend (n8n still waits, nobody may claim it)
    and resume; reassign (staff) and delegate (the assignee) hand it to someone of the lane. A `due`
    time is its SLA (inbox order, overdue); with `deadline` it interrupts the wait: n8n leaves on
    "timeout" at that moment and the task expires. `not_same_as` is four-eyes: whoever completed one
    of those activities in the case may not work this one."""

    KINDS = ("userTask", "manualTask")
    FIELD_TYPES, PRIORITIES = builder.FIELD_TYPES, builder.PRIORITIES
    SUSPENDED, EXPIRED = "suspended", "expired"
    WORKABLE = (WaitingActivity.OPEN, WaitingActivity.CLAIMED)
    WAITING = (*WORKABLE, SUSPENDED)  # n8n still waits on these
    STATES = (*WAITING, WaitingActivity.COMPLETED, WaitingActivity.CANCELLED, EXPIRED)
    TRANSITIONS = {  # action: (from, to)
        "claim": ((WaitingActivity.OPEN,), WaitingActivity.CLAIMED),
        "release": ((WaitingActivity.CLAIMED,), WaitingActivity.OPEN),
        "complete": (WORKABLE, WaitingActivity.COMPLETED),
        "suspend": (WORKABLE, SUSPENDED),
        "resume": ((SUSPENDED,), WaitingActivity.OPEN),
        "reassign": (WORKABLE, WaitingActivity.CLAIMED),
        "delegate": ((WaitingActivity.CLAIMED,), WaitingActivity.CLAIMED),
        "cancel": (WAITING, WaitingActivity.CANCELLED),
        "expire": (WAITING, EXPIRED),
    }
    ACTIONS = ("claim", "release", "complete", "suspend", "resume", "reassign", "delegate")  # what people do
    DUE_SOON = timedelta(days=1)  # the inbox marks a task "due soon" this close to its due time

    status = models.CharField(max_length=10, default=WaitingActivity.OPEN, choices=[(s, s) for s in STATES])
    kind = models.CharField(max_length=20, choices=[(k, k) for k in KINDS], default="userTask")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    lane = models.CharField(max_length=100, blank=True, help_text="Django group that may complete it")
    form = models.JSONField(default=list, blank=True)  # [{"name", "label", "type", "required", "help", "options"}]
    context = models.JSONField(default=dict, blank=True)  # data shown to the person
    result = models.JSONField(null=True, blank=True)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 help_text="who claimed or completed it")
    batch = models.CharField(max_length=32, blank=True, help_text="the tasks of one node execution")
    instance = models.PositiveIntegerField(default=0, help_text="the item it is for")
    priority = models.CharField(max_length=6, choices=[(p, p) for p in PRIORITIES], default="normal")
    due = models.DateTimeField(null=True, blank=True, help_text="its SLA: when it should be done")
    deadline = models.BooleanField(default=False, help_text="the due time interrupts the wait: n8n goes on through timeout")
    claimed = models.DateTimeField(null=True, blank=True,
                                   help_text="when its assignee took it: before is waiting, after is work")
    not_same_as = models.JSONField(default=list, blank=True,
                                   help_text="activities whose completer may not work this one (four-eyes)")
    quorum = models.PositiveIntegerField(default=0, help_text="completed tasks of the batch that resume the case; 0: all")

    objects = UserTaskQuerySet.as_manager()

    class Meta(WaitingActivity.Meta):
        indexes = [models.Index(fields=["status", "due"])]

    def __str__(self):
        return self.title

    @property
    def overdue(self):
        return self.due is not None and self.status in self.WAITING and self.due < timezone.now()

    @property
    def due_soon(self):
        return self.due is not None and self.status in self.WAITING and not self.overdue and \
            self.due - timezone.now() < self.DUE_SOON

    def past_deadline(self):
        """Its interrupting deadline passed while n8n waited: n8n has gone on through "timeout"."""
        return self.deadline and self.due is not None and self.status in self.WAITING and self.due <= timezone.now()

    def expire_if_due(self):
        UserTask.objects.filter(pk=self.pk).expire_due()
        self.refresh_from_db()

    @staticmethod
    def parse_due(value):
        """An ISO date-time, or a date (the end of that day); without a zone, the server's. Raises ValueError."""
        text = str(value).strip()
        try:
            moment = datetime.combine(date.fromisoformat(text), time(23, 59, 59))
        except ValueError:
            moment = datetime.fromisoformat(text)
        return timezone.make_aware(moment) if timezone.is_naive(moment) else moment

    @classmethod
    def assigned(cls, fields, username):
        """`fields` of a new task, created already claimed by `username` when that person exists and may
        work it (lane, four-eyes); else it opens to the lane."""
        user = get_user_model().objects.filter(username=username, is_active=True).first() if username else None
        if user is None or not cls(**fields).can_work(user):
            return fields
        return {**fields, "status": cls.CLAIMED, "assignee": user, "claimed": timezone.now()}

    @property
    def bpmn_type(self):
        return self.kind

    @classmethod
    def register(cls, resume_url, activity, instances):
        """The tasks of one node execution, one per item: `instances` are their fields. Idempotent:
        n8n running the node again (a retry, a restart) gets the tasks it already has."""
        batch = cls.registration_id(resume_url, activity)
        with transaction.atomic():
            if existing := list(cls.objects.filter(resume_url=resume_url, activity=activity).order_by("instance")):
                return existing
            return [cls.objects.get_or_create(
                registration_key=f"{batch}:{i}", defaults=dict(
                    resume_url=resume_url, activity=activity, batch=batch, instance=i, **fields))[0]
                    for i, fields in enumerate(instances)]

    def group(self):
        return UserTask.objects.filter(batch=self.batch) if self.batch else super().group()

    # --- who may do what ------------------------------------------------------------------

    def in_lane(self, user):
        return user.is_superuser or not self.lane or user.groups.filter(name=self.lane).exists()

    def four_eyes(self, user):
        """The activities named in `not_same_as` that `user` completed in this case."""
        if not (self.not_same_as and self.case_id):
            return []
        return sorted(set(UserTask.objects.filter(case_id=self.case_id, activity__in=self.not_same_as, assignee=user,
                                                  status=self.COMPLETED).values_list("activity", flat=True)))

    def barred(self, user):
        """Why `user` may not work it ("" when they may): not in its lane, or four-eyes."""
        if not self.in_lane(user):
            return f"“{self}” is for the {self.lane} lane"
        if done := self.four_eyes(user):
            return f"“{self}” needs another person: {user} completed {', '.join(done)} in this case (four-eyes)"
        return ""

    def can_work(self, user):
        return not self.barred(user)

    @staticmethod
    def manages(user):
        """Staff and superusers suspend, resume and reassign anyone's tasks."""
        return user.is_staff or user.is_superuser

    def refusal(self, user, action):
        """Why `user` may not take `action` now ("" when they may); `user` None is a system (the API)."""
        if self.status not in self.TRANSITIONS[action][0]:
            return f"cannot {action} “{self}”: it is {self.status}"
        if user is None or action in ("suspend", "resume", "reassign") and self.manages(user):
            return ""
        if action == "reassign":
            return f"only staff reassign “{self}”"
        if action == "delegate" and self.assignee_id != user.pk:
            return f"only {self.assignee}, who works on “{self}”, can delegate it"
        if why := self.barred(user):
            return why
        if self.status == self.CLAIMED and self.assignee_id != user.pk and not user.is_superuser:
            return f"“{self}” is claimed by {self.assignee}"
        return ""

    def allowed(self, user):
        """The actions `user` may take now, in the order the task page shows them."""
        return [] if self.past_deadline() else [a for a in self.ACTIONS if not self.refusal(user, a)]

    def check_action(self, user, action):
        """Raises unless `user` may take `action` now: Cancelled once n8n no longer waits (cancelled, or
        expired: its deadline passed), else TransitionError with the reason."""
        if self.status == self.CANCELLED:
            raise Cancelled(f"cannot {action} “{self}”: it was cancelled ({self.reason or 'the process no longer waits'})")
        if self.status == self.EXPIRED or self.past_deadline():
            raise Cancelled(f"cannot {action} “{self}”: its deadline passed ({self.due:%Y-%m-%d %H:%M} UTC) and the "
                            "process went on through “timeout”")
        if why := self.refusal(user, action):
            raise TransitionError(why)

    def candidates(self):
        """Who may work it, four-eyes applied: the lane's members and superusers (without a lane, everyone).
        The people a reassign or a delegation can hand it to. ponytail: one list of every candidate;
        a lane of hundreds needs a search field instead."""
        users = get_user_model().objects.filter(is_active=True)
        if self.lane:
            users = users.filter(Q(groups__name=self.lane) | Q(is_superuser=True)).distinct()
        if self.not_same_as and self.case_id:
            users = users.exclude(pk__in=UserTask.objects.filter(
                case_id=self.case_id, activity__in=self.not_same_as, status=self.COMPLETED, assignee__isnull=False
            ).values("assignee"))
        return users.order_by("username")

    # --- transitions ----------------------------------------------------------------------

    def claim(self, user):
        self.act(user, "claim", assignee=user, claimed=timezone.now())

    def release(self, user):
        self.act(user, "release", assignee=None, claimed=None)

    def suspend(self, user):
        """n8n keeps waiting; nobody may claim or complete it until it is resumed."""
        self.act(user, "suspend")

    def resume(self, user):
        """Back to its lane, open."""
        self.act(user, "resume", assignee=None, claimed=None)

    def reassign(self, user, to):
        """Staff hand it to `to`, a person who may work it: claimed by them."""
        self.act(user, "reassign", to)

    def delegate(self, user, to):
        """Its assignee hands it to `to`, a person who may work it: claimed by them."""
        self.act(user, "delegate", to)

    def act(self, user, action, to=None, **fields):
        """`user` (None: a system) takes `action`, checked under the task's row lock so that the check sees
        the state the transition moves; `to` is the person a reassign or a delegation hands it to."""
        if self.past_deadline():
            self.expire_if_due()
        with transaction.atomic():
            type(self).objects.select_for_update().get(pk=self.pk)
            self.refresh_from_db()
            self.check_action(user, action)
            if action in ("reassign", "delegate"):
                if to.pk == self.assignee_id:
                    raise TransitionError(f"“{self}” is already {to}’s")
                if why := self.barred(to):
                    raise TransitionError(why)
                fields |= {"assignee": to, "claimed": timezone.now()}
            self.transition(action, actor=user, **fields)
        if action in ("reassign", "delegate"):
            from .people import Notification  # people.py imports this module
            Notification.notify(self, "assigned")

    def complete(self, user, result):
        """`user` None: a system completes it through the API."""
        if self.past_deadline():
            self.expire_if_due()  # committed before finish(), whose transaction a refusal rolls back
        self.finish(getattr(user, "username", None) or "api", actor=user, result=result, assignee=user)

    def completion_fields(self, fields):
        self.check_action(fields.get("assignee"), "complete")
        if fields.get("assignee") is None:
            fields["assignee"] = self.assignee
        return fields

    # --- the form -------------------------------------------------------------------------

    def clean_result(self, data):
        """Coerces submitted values to the form's field types; raises ValidationError {field: message}.
        Fields are required unless they say otherwise; an optional one left empty is None; a boolean is
        never missing (an unchecked HTML checkbox is absent: false)."""
        result, errors = {}, {}
        for field in self.form:
            name, kind, raw = field["name"], field.get("type", "string"), data.get(field["name"])
            if kind == "boolean":
                if raw in (True, "true", "on", "1", 1):
                    result[name] = True
                elif raw in (None, False, "false", "off", "0", 0, ""):
                    result[name] = False
                else:
                    errors[name] = "must be a boolean"
            elif raw is None or isinstance(raw, str) and not raw.strip():
                if field.get("required", True):
                    errors[name] = "required"
                else:
                    result[name] = None
            else:
                try:
                    result[name] = self.coerce(kind, raw, field.get("options", []))
                except ValueError as e:
                    errors[name] = str(e)
        if errors:
            raise ValidationError(errors)
        return result

    @staticmethod
    def coerce(kind, raw, options):
        """One submitted value as its field type; raises ValueError with what is wrong."""
        if kind == "number":
            try:
                if isinstance(raw, bool):
                    raise TypeError
                number = float(raw)
            except (TypeError, ValueError):
                raise ValueError("must be a number") from None
            if not math.isfinite(number):  # NaN and inf are not JSON
                raise ValueError("must be a finite number")
            return number
        if kind == "date":
            try:
                return date.fromisoformat(str(raw).strip()).isoformat()
            except ValueError:
                raise ValueError("must be a date, YYYY-MM-DD") from None
        if kind == "email":
            try:
                validate_email(text := str(raw).strip())
            except ValidationError:
                raise ValueError("must be an email address") from None
            return text
        if kind == "choice" and str(raw) not in options:
            raise ValueError(f"must be one of: {', '.join(options)}")
        return str(raw)

    # --- resuming n8n ---------------------------------------------------------------------

    def output(self):
        return self.result or {}

    def needed(self):
        """How many completed tasks of the batch resume the case: all of them, or the quorum."""
        size = self.group().count()
        return min(self.quorum, size) if self.quorum else size

    def close_rivals(self, now):
        """Once the quorum is reached, the batch's tasks still waiting lose, like an event-based gateway's
        other messages."""
        if self.quorum and self.group().filter(status=self.COMPLETED).count() >= self.needed():
            self.group().filter(status__in=self.WAITING).update(status=self.CANCELLED, completed=now,
                                                                reason="the quorum was reached")

    def resume_payload(self):
        """Once nothing of the batch waits (all done, or the quorum reached and the rest cancelled): one item
        per completed task, in the order of the items."""
        if self.group().filter(status__in=self.WAITING).exists():
            return None
        done = self.group().filter(status=self.COMPLETED).order_by("instance").select_related("assignee")
        return {"_items": [{**t.output(), "_user_task_id": t.pk, "_completed_by": getattr(t.assignee, "username", None)}
                           for t in done]}


class MessageSubscriptionQuerySet(WaitingQuerySet):
    def waiting(self):
        """Open and not expired (an event-based gateway's timeout moves the execution on)."""
        return super().waiting().filter(Q(expires__isnull=True) | Q(expires__gt=timezone.now()))

    def for_message(self, name, correlation_key):
        """Waiting subscriptions for a message, oldest first (a signal's catches wait for no key: for_signal)."""
        return self.waiting().filter(name=name, correlation_key=correlation_key, broadcast=False).order_by("created")

    def for_signal(self, name):
        """Every execution waiting for signal `name`, oldest first."""
        return self.waiting().filter(name=name, broadcast=True).order_by("created")


class MessageSubscription(WaitingActivity):
    """BPMN Receive Task: an execution waiting for message `name` about `correlation_key`
    (e.g. "payment-confirmed" for order 42). Another system delivers it via the API."""

    name = models.CharField(max_length=200)
    correlation_key = models.CharField(max_length=200)
    broadcast = models.BooleanField(default=False, help_text="a signal: no correlation key, every waiting catch gets it")
    payload = models.JSONField(null=True, blank=True)
    expires = models.DateTimeField(null=True, blank=True, help_text="after this the execution has moved on (timeout)")

    objects = MessageSubscriptionQuerySet.as_manager()

    class Meta(WaitingActivity.Meta):
        indexes = [models.Index(fields=["name", "correlation_key", "status"])]

    def __str__(self):
        return f"{self.name} [{self.correlation_key}]"

    @property
    def bpmn_type(self):
        return "signalCatch" if self.broadcast else "receiveTask"

    @property
    def kind(self):
        return "signal" if self.broadcast else "message"

    def group(self):  # an event-based gateway waits on several messages with one resume URL
        return MessageSubscription.objects.filter(resume_url=self.resume_url)

    def close_rivals(self, now):
        self.group().waiting().exclude(pk=self.pk).update(status=self.CANCELLED, completed=now,
                                                          reason=f"“{self.name}” came first")

    @classmethod
    def register(cls, resume_url, activity, name, **fields):
        key = cls.registration_id(resume_url, activity, name)
        if existing := cls.objects.filter(resume_url=resume_url, activity=activity, name=name).first():
            return existing
        return cls.objects.get_or_create(registration_key=key, defaults=dict(
            resume_url=resume_url, activity=activity, name=name, **fields))[0]

    def deliver(self, payload):
        """A message comes from outside (a bank, a customer's reply): it adds process variables, it
        never changes the ones the case has (an IBAN, an amount, a decision). Those are left out and
        named in `reason`."""
        self.finish(f"{self.kind}:{self.name}", payload=payload)

    def completion_fields(self, fields):
        known = {}
        if self.case_id:
            case, _ = Case.objects.select_for_update().get_or_create(
                case_id=self.case_id, defaults={"process": self.process})
            known = case.variables
        payload = fields["payload"]
        kept = sorted(k for k in payload if k in known)
        return {**fields, "payload": {k: v for k, v in payload.items() if k not in known},
                "reason": f"left out, the case already has them: {', '.join(kept)}"[:500] if kept else ""}

    def output(self):
        return self.payload or {}

    def resume_payload(self):
        return {**self.output(), f"_{self.kind}": self.name, "_correlation_key": self.correlation_key}


class Reminder(models.Model):
    """C2: a non-interrupting deadline of a user or manual task: at `at`, if the task is still waiting, bpmn8n
    reminds its lane (or `target`'s) by e-mail, or starts the escalation workflow `target` (a webhook path,
    "/..."), once (`manage.py run_deadlines`). The task stays open, n8n keeps waiting."""

    task = models.OneToOneField(UserTask, on_delete=models.CASCADE, related_name="reminder")
    at = models.DateTimeField()
    target = models.CharField(max_length=300, blank=True, help_text="a lane, or an escalation workflow's webhook path")
    sent = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["sent", "at"])]

    def __str__(self):
        return f"{self.task} at {self.at:%Y-%m-%d %H:%M}"

    @property
    def webhook(self):
        return self.target.startswith("/")

    def recipients(self):
        """E-mail addresses of the lane's members: the target lane's, else the task's; no lane, no one."""
        lane = "" if self.webhook else self.target or self.task.lane
        users = get_user_model().objects.filter(is_active=True, groups__name=lane).exclude(email="") if lane else []
        return sorted({u.email for u in users})

    def fire(self):
        """Sends it once; False when it was sent already or the task no longer waits (then it is dropped)."""
        if not Reminder.objects.filter(pk=self.pk, sent__isnull=True).update(sent=timezone.now()):
            return False
        try:
            task = UserTask.objects.waiting().filter(pk=self.task_id).first()
            if task is None:
                return False
            how = self.escalate(task) if self.webhook else self.remind(task)
        except Exception:
            Reminder.objects.filter(pk=self.pk).update(sent=None)  # a failed send is tried again
            raise
        AuditEntry.write("usertask.remind", task, actor="system", activity=task.activity, how=how, to=self.target)
        if task.assignee_id:  # an unassigned task's lane has the reminder mail, or its escalation workflow
            from .people import Notification  # people.py imports this module
            Notification.notify(task, "escalated")
        return True

    def remind(self, task):
        to = self.recipients()
        if to:
            send_mail(f"Reminder: {task.title}", f"“{task.title}” is still waiting in the {task.lane or 'bpmn8n'} lane "
                      f"(case {task.case_id}). Open the bpmn8n inbox to work it.", None, to)
        return f"mail to {len(to)}"

    def escalate(self, task):
        path = self.target if self.target.startswith("/webhook") else "/webhook" + self.target
        post_json(f"{settings.N8N_URL}{path}", {"case_id": task.case_id, "task_id": task.pk, "title": task.title,
                                              "lane": task.lane, "activity": task.activity, "process": task.process})
        return "workflow"
