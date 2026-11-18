"""The versions of n8n workflows bpmn8n has seen (core/history.py): n8n keeps 24 hours of history for free."""
from django.db import models


class WorkflowVersion(models.Model):
    """One saved version of an n8n workflow: what a case ran (Case.process_version), what changed since the
    one before, what to restore."""

    workflow_id = models.CharField(max_length=100)
    version_id = models.CharField(max_length=100, help_text="n8n's versionId")
    name = models.CharField(max_length=200, blank=True)
    workflow = models.JSONField(help_text="the workflow as n8n returned it")
    content_hash = models.CharField(max_length=64, help_text="sha256 of its nodes and connections")
    created = models.DateTimeField(auto_now_add=True, help_text="when bpmn8n first saw it")

    class Meta:
        ordering = ("created", "id")
        constraints = (models.UniqueConstraint(fields=("workflow_id", "version_id"), name="unique_workflow_version"),)

    def __str__(self):
        return f"{self.name or self.workflow_id} @ {self.version_id[:8]}"
