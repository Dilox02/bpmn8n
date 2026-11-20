"""Insights on the event log: a filter stack (filters), statistics (stats) and queueing theory on what
really happened (queues), read by /api/analytics, the Insights page and the canvas heatmap. Pure Django
and numpy: no PM4Py here (its AGPL stays in bpmn8n/mining/)."""
from .filters import FilterError, FilterStack
from .log import Log, Scope

__all__ = ["FilterError", "FilterStack", "Log", "Scope"]
