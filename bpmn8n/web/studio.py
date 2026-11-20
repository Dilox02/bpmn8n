"""bpmn8n Studio's pages (staff only: it writes code the server then runs): the projects, the
interview with its domain research and the build, their steps as a kanban kept up to date by
server-sent events."""
import json
import time

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..clients.llm import LLMError
from ..conf import settings
from ..models import Card, Project, Settings
from ..workflows.builder import SpecError, Workflow
from ..studio.build import ProjectBuilder
from ..studio.interview import Interview
from .simulation import headline

POLL, KEEP_ALIVE = 1, 15  # seconds: the build runs in another process, its state is in the database


@staff_member_required
def projects(request):
    if request.method == "POST":
        name, description = request.POST.get("name", "").strip(), request.POST.get("description", "").strip()
        if name and description:
            project = Project.objects.create(name=name)
            interview(request, project, description)
            return redirect("project", project.slug)
        messages.error(request, "Give the process a name and describe it.")
    projects = [p.settle() for p in Project.objects.all()]
    for p in projects:
        p.activity_count = len((p.spec or {}).get("activities", [])) if p.spec else None
    return render(request, "bpmn8n/studio.html", {"projects": projects,
                                                  "ready_count": sum(p.status == Project.READY for p in projects)})


@staff_member_required
def project(request, slug):
    project = get_object_or_404(Project, slug=slug).settle()
    if request.method == "POST" and (text := request.POST.get("answer", "").strip()):
        if project.busy:
            messages.warning(request, f"Wait for the {'research' if project.status == Project.RESEARCH else 'build'} to end.")
        else:
            interview(request, project, text)
        return redirect("project", slug)
    return render(request, "bpmn8n/project.html", {
        "project": project, "cards": project.cards.all(), "columns": Card.COLUMNS, "workflow": workflow(project),
        "preview_url": settings.N8N_PREVIEW_URL, "answers": sum(m["role"] == "user" for m in project.messages),
        "capacity": headline(project.slug)})


def workflow(project):
    """The project's workflow as n8n draws it, for the embedded canvas; None before a design exists."""
    try:
        provider = Settings.load().llm  # the LLM agents' chat model, drawn under them
        chat_model = provider.n8n_chat_model() if provider else None
        return Workflow.from_spec(project.spec["workflow"], chat_model=chat_model).as_json() if project.spec else None
    except (SpecError, KeyError, TypeError):  # a design the checks rejected
        return None


def interview(request, project, text):
    try:
        Interview(project).answer(text)
    except LLMError as e:
        messages.error(request, f"The LLM did not answer: {e}")


@staff_member_required
@require_POST
def build(request, slug):
    project = get_object_or_404(Project, slug=slug).settle()
    if project.busy:
        messages.warning(request, "Wait for the research or the build to end.")
    elif not project.brief:
        messages.error(request, "Finish the interview first: the build starts from its brief.")
    else:
        try:
            ProjectBuilder.start(project)
        except LLMError as e:
            messages.error(request, str(e))
    return redirect("project", slug)


@staff_member_required
def events(request, slug):
    """The project's state and cards as server-sent events, whenever they change, until its job ends."""
    project = get_object_or_404(Project, slug=slug).settle()

    def stream():
        last, quiet = None, 0
        while True:
            project.refresh_from_db()
            project.settle()
            state = {"status": project.status, "cards": [c.as_dict() for c in project.cards.all()]}
            if state != last:
                yield f"data: {json.dumps(state)}\n\n"
                last, quiet = state, 0
            elif (quiet := quiet + POLL) >= KEEP_ALIVE:
                yield ": keep-alive\n\n"
                quiet = 0
            if not project.busy:
                return
            time.sleep(POLL)

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # no proxy buffering
    return response
