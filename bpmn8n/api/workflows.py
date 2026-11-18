"""Workflows checked before they run (bpmn8n/workflows/check.py), and the BPMN elements they are made of."""
from ninja import Body, Router

from ..workflows.elements import manifest

router = Router(tags=["workflows"])
elements = Router(tags=["workflows"])  # mounted at /api: /api/elements


@router.post("/check")
def check_workflow(request, workflow: dict = Body(...), start_data: str | None = None):
    """Checks an n8n workflow (as exported or built) of bpmn8n nodes: `errors` will fail when a case
    gets there, `warnings` are probably not what was meant. `start_data` (comma-separated): the fields
    of the data that starts a case; then every variable is checked."""
    # imported here: the check imports this package (for its tasks)
    from ..workflows.check import WorkflowCheck
    fields = None if start_data is None else [f for f in start_data.split(",") if f]
    checked = WorkflowCheck(workflow, start_data=fields)
    return {"ok": not checked.errors, "problems": [p.as_dict() for p in checked.problems]}


@elements.get("/elements", auth=None)  # public, like the OpenAPI: the n8n editor's node card reads it
def list_elements(request):
    """Every BPMN element a workflow can contain (bpmn8n/workflows/elements.py): kind, n8n type,
    category, label, summary, glyph and traits."""
    return manifest()
