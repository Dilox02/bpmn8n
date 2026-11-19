from django.db import models


class Snapshot(models.Model):
    """A named analysis on the Insights page: its whole state (process, window, filters, cohort, target wait, the
    map's level of detail), so reopening it by URL shows the same view of the log as it is now. The results are
    recomputed, not stored: a snapshot is a saved question."""

    name = models.CharField(max_length=120)
    state = models.JSONField(default=dict)
    author = models.CharField(max_length=150, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created", "-id")

    def __str__(self):
        return self.name
