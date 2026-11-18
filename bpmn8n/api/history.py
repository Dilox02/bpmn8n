"""Workflow history (core/history.py): the versions bpmn8n has kept of each n8n workflow and what changed."""
from datetime import datetime

from ninja import Router, Schema
from ninja.errors import HttpError

from ..core.history import cases_per_version
from ..models import WorkflowVersion
from ..workflows.changes import changes

router = Router(tags=["history"])


class VersionOut(Schema):
    version_id: str
    name: str
    content_hash: str
    created: datetime
    cases: int


@router.get("/{workflow_id}", response=list[VersionOut])
def list_versions(request, workflow_id: str):
    """The workflow's versions, oldest first, with how many cases ran each."""
    versions = list(WorkflowVersion.objects.filter(workflow_id=workflow_id).defer("workflow"))
    counts = cases_per_version(workflow_id)
    return [{**{f: getattr(v, f) for f in ("version_id", "name", "content_hash", "created")}, "cases": counts.get(v.version_id, 0)}
            for v in versions]


@router.get("/{workflow_id}/{version_id}/diff")
def diff_versions(request, workflow_id: str, version_id: str, against: str = ""):
    """What changed from `against` (by default the version before) to this version."""
    versions = WorkflowVersion.objects.filter(workflow_id=workflow_id)
    after = versions.filter(version_id=version_id).first()
    if not after:
        raise HttpError(404, f"no version {version_id!r} of workflow {workflow_id!r}")
    before = versions.filter(version_id=against).first() if against else previous(after)
    if against and not before:
        raise HttpError(404, f"no version {against!r} of workflow {workflow_id!r}")
    return {"workflow_id": workflow_id, "version_id": version_id, "against": before.version_id if before else None,
            "changes": changes(before.workflow if before else {}, after.workflow)}


def previous(version):
    return (WorkflowVersion.objects.filter(workflow_id=version.workflow_id, created__lte=version.created)
            .exclude(pk=version.pk).exclude(created=version.created, id__gt=version.pk).order_by("-created", "-id").first())

