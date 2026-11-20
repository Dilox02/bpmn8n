"""BPMN User and Manual Tasks: the n8n node creates one per item and waits; people work them in
the inbox (/inbox/) or a system completes them through this API."""
from datetime import datetime
from typing import Annotated, Any, Literal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Body, Router, Schema
from ninja.errors import HttpError
from pydantic import Field, field_validator, model_validator

from ..clients.http import ServiceUnavailable
from ..core.cases import CaseData
from ..models import Cancelled, Notification, Reminder, TransitionError, UserTask

router = Router(tags=["userTask"])


class FormField(Schema):
    name: str
    label: str = ""
    type: Literal[UserTask.FIELD_TYPES] = "string"
    required: bool = True  # a boolean never is: unchecked is false
    help: str = ""  # shown under the field
    options: list[str] = []  # a choice's options; comma-separated text also works (the n8n node's)

    @field_validator("options", mode="before")
    @classmethod
    def split(cls, value):
        return [o.strip() for o in value.split(",") if o.strip()] if isinstance(value, str) else value

    @model_validator(mode="after")
    def choice_has_options(self):
        if self.type == "choice" and not self.options:
            raise ValueError(f"{self.name}: a choice needs its options")
        return self


class UserTaskIn(Schema):
    kind: Literal["userTask", "manualTask"] = "userTask"
    title: str  # {{variable}} templates allowed, as in description, context, due and assignee
    description: str = ""
    lane: str = ""
    form: list[FormField] = []
    context: dict = {}  # shown to the person; empty: all the process variables
    resume_url: str
    items: list[dict[str, Any]] = [{}]  # the n8n items that reached the node: one task each
    priority: Literal[UserTask.PRIORITIES] = "normal"
    due: str = ""  # the SLA: an ISO date-time or date (the end of that day), or a {{variable}} holding one
    deadline: bool = False  # the due time interrupts the wait: n8n goes on through "timeout"
    assignee: str = ""  # a username or a {{variable}}: created claimed by that person, if they may work it
    not_same_as: list[str] = []  # four-eyes: whoever completed these activities in the case may not work it
    completion: Literal["all", "any"] | Annotated[int, Field(ge=1)] = "all"  # how many of the batch resume the case
    remind_at: str = ""  # C2: an ISO date-time; if the task still waits then, `run_deadlines` reminds (it stays open)
    escalate_to: str = ""  # a lane to notify instead of the task's own, or "/path" of an escalation workflow's webhook


class UserTaskOut(Schema):
    id: int
    kind: str
    title: str
    activity: str
    description: str
    lane: str
    case_id: str
    form: list[FormField]
    context: dict
    status: str
    reason: str
    result: dict | None
    instance: int
    created: datetime
    completed: datetime | None
    priority: str
    due: datetime | None
    deadline: bool
    overdue: bool
    claimed: datetime | None
    assignee: str | None
    not_same_as: list[str]
    quorum: int  # 0: all the batch's tasks

    @staticmethod
    def resolve_assignee(task):
        return task.assignee.username if task.assignee else None


class Person(Schema):
    username: str


def due_of(case, data):
    """The due time for one item's case: `due` with its {{variables}} filled in, None when not set."""
    if not data.due:
        return None
    value = case.text(data.due)
    try:
        return UserTask.parse_due(value)
    except ValueError:
        raise HttpError(422, f"due {data.due!r} is {value!r}: not an ISO date or date-time") from None


@router.post("", response={201: list[UserTaskOut]})
def create_user_tasks(request, data: UserTaskIn):
    """One task per item (BPMN multi-instance); the execution resumes when all are completed, or
    `completion` of them ("any", or at least N): then the others are cancelled."""
    cases = [CaseData(request, item) for item in data.items or [{}]]
    dues = [due_of(case, data) for case in cases]
    if data.deadline:
        if None in dues:
            raise HttpError(422, "a deadline needs a due time")
        dues = [min(dues)] * len(dues)  # n8n waits once for the whole batch
    quorum = {"all": 0, "any": 1}.get(data.completion, data.completion)
    instances = [UserTask.assigned({
        "kind": data.kind, "title": case.text(data.title), "description": case.text(data.description),
        "lane": data.lane, "form": [f.dict() for f in data.form], "case_id": case.trace.case_id,
        "process": case.trace.process, "context": case.render(data.context) if data.context else case.variables,
        "priority": data.priority, "due": due, "deadline": data.deadline, "not_same_as": data.not_same_as,
        "quorum": quorum}, str(case.text(data.assignee))) for case, due in zip(cases, dues)]
    tasks = UserTask.register(data.resume_url, cases[0].trace.activity or data.title, instances)
    for task in tasks:
        if task.assignee_id:  # created already claimed: its person is told (once, also when n8n retries)
            Notification.notify(task, "assigned")
    if data.remind_at:
        try:
            at = UserTask.parse_due(data.remind_at)
        except ValueError:
            raise HttpError(422, f"remind_at {data.remind_at!r} is not an ISO date-time") from None
        for task in tasks:
            Reminder.objects.get_or_create(task=task, defaults={"at": at, "target": data.escalate_to})
    return 201, tasks


@router.get("", response=list[UserTaskOut])
def list_user_tasks(request, status: str | None = None, lane: str | None = None, priority: str | None = None,
                    overdue: bool | None = None):
    """Waiting tasks (open, claimed or suspended), or those in `status`; `overdue`: past their due time or not."""
    UserTask.objects.expire_due()
    tasks = UserTask.objects.filter(status=status) if status else UserTask.objects.waiting()
    if lane is not None:
        tasks = tasks.filter(lane=lane)
    if priority:
        tasks = tasks.filter(priority=priority)
    if overdue is not None:
        tasks = tasks.overdue() if overdue else tasks.exclude(due__lt=timezone.now())
    return tasks.select_related("assignee")


def act(task_id, do):
    """Runs `do(task)` and answers with the task, or with why not: 409 its state does not allow it, 410 n8n
    no longer waits (cancelled, expired), 422 invalid data, 502 n8n unreachable."""
    task = get_object_or_404(UserTask, pk=task_id)
    try:
        do(task)
    except ValidationError as e:
        raise HttpError(422, str(e.message_dict if hasattr(e, "error_dict") else e.messages)) from None
    except Cancelled as e:
        raise HttpError(410, str(e)) from None
    except TransitionError as e:
        raise HttpError(409, str(e)) from None
    except ServiceUnavailable as e:
        raise HttpError(502, f"n8n unreachable, task left open: {e}") from None
    return task


def person(username):
    if user := get_user_model().objects.filter(username=username, is_active=True).first():
        return user
    raise ValidationError(f"no active person is called {username!r}")


@router.post("/{task_id}/complete", response=UserTaskOut)
def complete_user_task(request, task_id: int, result: dict = Body(...)):
    return act(task_id, lambda task: task.complete(None, task.clean_result(result)))


@router.post("/{task_id}/suspend", response=UserTaskOut)
def suspend_user_task(request, task_id: int):
    """n8n keeps waiting; nobody may claim or complete the task until it is resumed."""
    return act(task_id, lambda task: task.suspend(None))


@router.post("/{task_id}/resume", response=UserTaskOut)
def resume_user_task(request, task_id: int):
    """A suspended task goes back to its lane, open."""
    return act(task_id, lambda task: task.resume(None))


@router.post("/{task_id}/reassign", response=UserTaskOut)
def reassign_user_task(request, task_id: int, data: Person):
    """An open or claimed task becomes claimed by `username`, who must be able to work it."""
    return act(task_id, lambda task: task.reassign(None, person(data.username)))


@router.post("/{task_id}/delegate", response=UserTaskOut)
def delegate_user_task(request, task_id: int, data: Person):
    """A claimed task passes from its assignee to `username`, who must be able to work it."""
    return act(task_id, lambda task: task.delegate(None, person(data.username)))
