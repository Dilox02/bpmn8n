"""bpmn8n Studio: a process designed with an LLM, from an interview (with research on the domain) to a
project folder with its activities (@task code) and its n8n workflow. Cards are a job's steps, shown as
a kanban."""
import re
from pathlib import Path
from datetime import timedelta

from django.conf import settings as django_settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from ..conf import settings


class Project(models.Model):
    INTERVIEW, RESEARCH, BUILDING, READY, FAILED = "interview", "research", "building", "ready", "failed"
    STALE = timedelta(minutes=3)  # a job beats every 30 s

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=60, unique=True, editable=False,
                            help_text="projects/<slug>/: a Python package, so letters, digits and _")
    status = models.CharField(max_length=10, default=INTERVIEW,
                              choices=[(s, s) for s in (INTERVIEW, RESEARCH, BUILDING, READY, FAILED)])
    messages = models.JSONField(default=list, blank=True)  # the interview: [{"role", "content"}]
    research = models.JSONField(default=dict, blank=True, help_text="the domain research: {\"count\", "
                                "\"topics\": [{\"topic\", \"notes\", \"sources\": [{\"title\", \"url\"}]}]}")
    brief = models.TextField(blank=True, help_text="the process as the interview understood it")
    spec = models.JSONField(null=True, blank=True, help_text="the design: activities and workflow")
    n8n_id = models.CharField(max_length=50, blank=True, help_text="the workflow imported into n8n")
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = re.sub(r"^(?=\d)", "p_", slugify(self.name).replace("-", "_")) or "process"
            self.slug, n = base, 1
            folders = Path(django_settings.BASE_DIR) / "projects"  # not a project Studio did not build
            while Project.objects.filter(slug=self.slug).exists() or (folders / self.slug).exists():
                n += 1
                self.slug = f"{base}_{n}"
        super().save(*args, **kwargs)

    @property
    def building(self):
        return self.status == self.BUILDING

    @property
    def busy(self):
        """A job (the research or the build) is running."""
        return self.status in (self.RESEARCH, self.BUILDING)

    def settle(self):
        """A job whose process died (no heartbeat for STALE, see studio.jobs.heartbeat) ends: the build
        failed, the interview goes on. Returns the project."""
        if self.busy and timezone.now() - self.updated > self.STALE:
            self.status = self.FAILED if self.building else self.INTERVIEW
            self.save(update_fields=["status", "updated"])
        return self

    @property
    def unanswered(self):
        """The person spoke last and no job will answer: the LLM call (or the research before it) failed."""
        return not self.busy and bool(self.messages) and self.messages[-1]["role"] == "user"

    @property
    def research_topics(self):
        return self.research.get("topics", [])

    @property
    def n8n_url(self):
        return f"{settings.N8N_PUBLIC_URL}/workflow/{self.n8n_id}" if self.n8n_id else ""


class Card(models.Model):
    """One step of a project's build: to do, in progress, then done, skipped or failed."""

    TODO, DOING, DONE, SKIPPED, FAILED = "todo", "doing", "done", "skipped", "failed"
    COLUMNS = ((TODO, "To do"), (DOING, "In progress"), (DONE, "Done"))  # skipped and failed are done too

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="cards")
    title = models.CharField(max_length=200)
    status = models.CharField(max_length=10, default=TODO,
                              choices=[(s, s) for s in (TODO, DOING, DONE, SKIPPED, FAILED)])
    detail = models.TextField(blank=True)
    position = models.PositiveIntegerField(default=0)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "pk"]

    def __str__(self):
        return self.title

    def move(self, status, detail=None):
        """Its new state; a card the next job deleted meanwhile is left alone."""
        self.status, self.updated = status, timezone.now()
        if detail is not None:
            self.detail = detail
        Card.objects.filter(pk=self.pk).update(status=self.status, detail=self.detail, updated=self.updated)

    @property
    def column(self):
        return self.DONE if self.status in (self.SKIPPED, self.FAILED) else self.status

    def as_dict(self):
        return {"id": self.pk, "title": self.title, "status": self.status, "column": self.column, "detail": self.detail}
