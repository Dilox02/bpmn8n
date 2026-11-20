"""Workflows checked before they run (bpmn8n/workflows/check.py), and the BPMN elements they are made of."""
from django.http import HttpResponse
from ninja import Body, Router
from ninja.errors import HttpError

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


@router.get("/{path:workflow_id}.bpmn")
def export_bpmn(request, workflow_id: str, data: int = 0):
    """The workflow as BPMN 2.0 XML with diagram interchange (bpmn_xml.py), for bpmn.io, Camunda Modeler, Signavio or
    BIMP. `workflow_id`: as for the contract (an n8n id, a project, `acme/ticket-triage`). `?data=1` adds the
    data lens: a data object per variable and its data associations. 404 when nobody knows the workflow."""
    from ..workflows.bpmn_xml import to_bpmn
    from .contracts import find
    if (found := find(workflow_id)) is None:
        raise HttpError(404, f"no workflow {workflow_id!r} in n8n nor in projects/")
    return HttpResponse(to_bpmn(found[0], data=bool(data)), content_type="application/xml")
