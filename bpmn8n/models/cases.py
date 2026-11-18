"""The process instances (cases) with their variables, and the event log of every measured step."""
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, transaction


class Case(models.Model):
    """A process instance ("<workflow id>:<execution id>") with its process variables."""

    case_id = models.CharField(max_length=100, primary_key=True)
    process = models.CharField(max_length=200, blank=True)
    # the n8n versionId of the workflow the case runs (WorkflowVersion), when n8n's API could say
    process_version = models.CharField(max_length=100, blank=True, db_index=True)
    variables = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = (models.Index(fields=["created"]),)

    def __str__(self):
        return self.case_id

    @classmethod
    def merge(cls, case_id, process, data):
        """Adds `data` to the case's variables and returns them all (see upsert)."""
        return cls.upsert(case_id, process, data)[0]

    @classmethod
    def upsert(cls, case_id, process, data):
        """(the case's variables with `data` added, whether this call created the case). Row-locked:
        parallel branches of one case may write at the same moment. Nothing to add: a plain read."""
        if not data:
            case, created = cls.objects.get_or_create(case_id=case_id, defaults={"process": process})
            return case.variables, created
        with transaction.atomic():
            case, created = cls.objects.select_for_update().get_or_create(case_id=case_id, defaults={"process": process})
            case.variables = {**case.variables, **data}
            case.save(update_fields=["variables", "updated"])
        return case.variables, created


class EventQuerySet(models.QuerySet):
    def cases(self, since=None):
        """Events that belong to a case (an n8n execution), of cases that started at/after `since`."""
        events = self.exclude(case_id="").exclude(status="running")
        if since:
            recent = events.values("case_id").annotate(first=models.Min("started")).filter(first__gte=since)
            events = events.filter(case_id__in=recent.values("case_id"))
        return events.order_by("started")


class Event(models.Model):
    """One measured step: an activity of a case (process mining) and a sample for the metrics.
    Direct API calls outside any case have an empty case_id."""

    case_id = models.CharField(max_length=100, blank=True)  # "<workflow id>:<execution id>"
    activity = models.CharField(max_length=200)
    process = models.CharField(max_length=200, blank=True)  # the n8n workflow
    endpoint = models.CharField(max_length=200)  # the task / gateway / wait that ran
    started = models.DateTimeField()
    ended = models.DateTimeField()
    duration_ms = models.FloatField()
    status = models.CharField(max_length=10)  # complete | error
    resource = models.CharField(max_length=200)  # who: system, a username, message:<name>
    error = models.TextField(blank=True)
    # what the step read and produced, e.g. a gateway's state, branches and confidence: the
    # material process mining turns into training data
    data = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)

    objects = EventQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=["case_id", "started"]), models.Index(fields=["endpoint"]),
                   models.Index(fields=["started"])]

    def __str__(self):
        return f"{self.case_id or '-'} {self.activity}"
