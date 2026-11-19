"""N4 people: who is told what (Notification, Preference) and what is remembered to find similar cases (CaseEmbedding)."""
import logging

from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import models
from django.utils import timezone

from ..conf import CONFIG, settings
from .cases import Case
from .safety import AuditEntry
from .waiting import UserTask

log = logging.getLogger(__name__)
SUBJECTS = {"assigned": "Assigned to you", "due_soon": "Due soon", "overdue": "Overdue", "escalated": "Escalated"}


class Preference(models.Model):
    """What a person wants to hear: each task event by e-mail as it happens (on unless they opt out), and a daily
    digest (off unless they opt in, `manage.py send_digests`). No row: the defaults."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preference")
    email = models.BooleanField(default=True, help_text="an e-mail when a task is assigned to you, due soon, overdue or escalated")
    digest = models.BooleanField(default=False, help_text="one e-mail a day with what waits for you")
    digest_sent = models.DateTimeField(null=True, blank=True)

    @classmethod
    def of(cls, user):
        return cls.objects.filter(user=user).first() or cls(user=user)


class Notification(models.Model):
    """One e-mail about a task, the record that makes it go once: at most one per task, kind and person."""

    task = models.ForeignKey(UserTask, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=10, choices=[(k, k) for k in SUBJECTS])
    to = models.CharField(max_length=150, help_text="the username")
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (models.UniqueConstraint(fields=("task", "kind", "to"), name="one_notification_per_task_kind_person"),)

    def __str__(self):
        return f"{self.kind} {self.task_id} to {self.to}"

    @staticmethod
    def recipients(task):
        """Who is told: its assignee, else the members of its lane (no lane, nobody: anyone could be)."""
        if task.assignee_id:
            return [task.assignee] if task.assignee.is_active else []
        users = get_user_model().objects.filter(is_active=True, groups__name=task.lane).distinct() if task.lane else []
        return list(users)

    @classmethod
    def notify(cls, task, kind):
        """Tells the recipients who want e-mail, each once; returns how many were told. Best effort: a mail that
        cannot go is logged and left to the next run (the record is removed), it never fails the action."""
        told = 0
        for user in cls.recipients(task):
            if not user.email or not Preference.of(user).email:
                continue
            row, created = cls.objects.get_or_create(task=task, kind=kind, to=user.username)
            if not created:
                continue
            try:
                send_mail(f"{SUBJECTS[kind]}: {task.title}",
                          f"“{task.title}” ({task.process or 'bpmn8n'}) is {SUBJECTS[kind].lower()}"
                          f"{f', due {task.due:%Y-%m-%d %H:%M} UTC' if task.due else ''}.\n\n"
                          f"{CONFIG.BPMN8N_PUBLIC_URL}/inbox/{task.pk}/\n", None, [user.email])
            except Exception:  # ponytail: no retry queue; run_deadlines sends the time-based kinds again next run
                log.exception("notification %s for task %s to %s failed", kind, task.pk, user.username)
                row.delete()
                continue
            AuditEntry.write("usertask.notify", task, actor="system", kind=kind, to=user.username)
            told += 1
        return told

    @classmethod
    def sweep(cls):
        """The time-based kinds: tasks past their due time (overdue) and those due within a day (due soon)."""
        now, told = timezone.now(), 0
        for task in UserTask.objects.workable().filter(due__isnull=False, due__lt=now + UserTask.DUE_SOON).select_related("assignee"):
            told += cls.notify(task, "overdue" if task.due < now else "due_soon")
        return told

    @classmethod
    def digest(cls, user):
        """The daily digest of `user`: what waits in their lanes and for them. False when nothing waits."""
        tasks = UserTask.objects.workable().for_user(user).filter(
            models.Q(assignee=user) | models.Q(assignee__isnull=True)).by_urgency()
        lines = [f"- {t.title}{' (overdue)' if t.overdue else ' (due soon)' if t.due_soon else ''}: "
                 f"{CONFIG.BPMN8N_PUBLIC_URL}/inbox/{t.pk}/" for t in tasks[:50]]
        if not lines:
            return False
        send_mail(f"{len(lines)} task{'s' * (len(lines) != 1)} waiting for you", "\n".join(lines), None, [user.email])
        AuditEntry.write("usertask.digest", actor="system", object_type="user", object_id=user.pk, tasks=len(lines))
        return True


class CaseEmbedding(models.Model):
    """The sentence embedding of the text of a case's variables (what the similar-cases panel compares), kept
    per text so that a case is embedded once; `text_hash` changes when the text does."""

    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="embeddings")
    text_hash = models.CharField(max_length=40)
    vector = models.JSONField()

    class Meta:
        constraints = (models.UniqueConstraint(fields=("case", "text_hash"), name="one_embedding_per_case_text"),)
