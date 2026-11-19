"""The process contract of a workflow (bpmn8n/workflows/contract.py): how a case starts and with which data (as JSON
Schema), the messages it waits for and sends, its lanes, deadlines, results and sub-processes, an OpenAPI fragment
for its start webhook and message deliveries, and the same in words."""
from django.conf import settings as django_settings
from ninja import Router
from ninja.errors import HttpError

from ..clients import n8n
from ..conf import settings
from ..models import Project

router = Router(tags=["contracts"])


def find(workflow_id):
    """(workflow, declared start data) of an n8n workflow (read through n8n's API; a Studio project published as
    it adds its declared start data, and is read from its folder when n8n cannot give it) or of a project key
    (`motor_insurance_claim`, `acme/ticket-triage`); None when nobody knows it."""
    from ..workflows.contract import local_workflows  # the contract imports this package (for the tasks)
    local = local_workflows(django_settings.BASE_DIR)
    if workflow_id in local:
        return local[workflow_id]
    project = Project.objects.filter(n8n_id=workflow_id).first()
    saved, declared = local.get(project.slug, (None, None)) if project else (None, None)
    workflow = n8n.workflow(workflow_id) or saved
    return (workflow, declared) if workflow else None


@router.get("/{path:workflow_id}")
def get_contract(request, workflow_id: str):
    """`workflow_id`: an n8n workflow id, a project (`motor_insurance_claim`) or one process of a project with
    processes.py (`acme/ticket-triage`). 404 when neither n8n nor a project has it."""
    from ..workflows.contract import Contract
    if (found := find(workflow_id)) is None:
        raise HttpError(404, f"no workflow {workflow_id!r} in n8n nor in projects/")
    contract = Contract(found[0], found[1], workflow_id)
    return {**contract.as_dict(), "start_schema": contract.start_schema(), "summary": contract.summary(),
            "openapi": contract.openapi(settings.N8N_PUBLIC_URL, request.build_absolute_uri("/").rstrip("/"))}
