"""GET /api/goals: the process goals of the projects' evaluations.json against the event log (core/goals.py)."""
from ninja import Router
from ninja.errors import HttpError

from ..core import goals

router = Router(tags=["goals"])


@router.get("")
def get_goals(request, process: str | None = None, project: str | None = None):
    """Every goal (a KPI, its target and window) with its current value and whether it is met (`ok`: null
    while there is nothing to measure)."""
    try:
        return {"goals": goals.evaluate(project=project, process=process)}
    except goals.GoalError as e:
        raise HttpError(422, str(e)) from None
