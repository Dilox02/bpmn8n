"""BPMN messages: a Receive Task node subscribes and waits; another system (a bank, a partner,
a sensor gateway) delivers the message and the waiting execution continues with its payload."""
from datetime import datetime
from typing import Any

from ninja import Router, Schema
from ninja.errors import HttpError

from ..clients.http import ServiceUnavailable
from ..core.cases import CaseData
from ..models import Cancelled, MessageSubscription, TransitionError

router = Router(tags=["messages"])


class SubscriptionIn(Schema):
    name: str
    correlation_key: str  # usually a template, e.g. {{order_id}}
    resume_url: str
    expires: datetime | None = None  # an event-based gateway's timeout
    item: dict[str, Any] = {}  # the n8n item that reached the node


class SubscriptionOut(Schema):
    id: int
    name: str
    correlation_key: str
    activity: str
    case_id: str
    process: str
    status: str
    reason: str
    payload: dict | None
    expires: datetime | None
    created: datetime
    completed: datetime | None


class MessageIn(Schema):
    correlation_key: str
    payload: dict = {}
    process: str | None = None  # the workflow it is for, when several wait for the same message


@router.post("/subscriptions", response={201: SubscriptionOut})
def subscribe(request, data: SubscriptionIn):
    """Idempotent: n8n running the node again gets the subscription it already has."""
    case = CaseData(request, data.item)
    activity = case.trace.activity or data.name
    return 201, MessageSubscription.register(
        name=data.name, correlation_key=str(case.text(data.correlation_key)), resume_url=data.resume_url,
        expires=data.expires, case_id=case.trace.case_id, process=case.trace.process, activity=activity)


@router.get("/subscriptions", response=list[SubscriptionOut])
def list_subscriptions(request, status: str | None = None):
    """Waiting subscriptions, or those in `status`."""
    return MessageSubscription.objects.filter(status=status) if status else MessageSubscription.objects.waiting()


@router.post("/{name}", response=SubscriptionOut)
def deliver_message(request, name: str, data: MessageIn):
    """Delivers the message to the oldest execution waiting for it with this correlation key
    (an event-based gateway: the first message wins, its alternatives are cancelled). 404 when none
    is waiting or its timeout passed: BPMN messages are not queued."""
    waiting = MessageSubscription.objects.for_message(name, data.correlation_key)
    if data.process is not None:
        waiting = waiting.filter(process=data.process)
    if len(processes := set(waiting.values_list("process", flat=True))) > 1:  # never deliver by guess
        raise HttpError(409, f"{sorted(processes)} all wait for {name!r} with key {data.correlation_key!r}: "
                             f"say which one with \"process\"")
    for sub in waiting:
        try:
            sub.deliver(data.payload)
            return sub
        except Cancelled:
            continue  # that execution is gone: the next one waiting gets it
        except TransitionError as e:
            raise HttpError(409, str(e))
        except ServiceUnavailable as e:
            raise HttpError(502, f"n8n unreachable, message not delivered: {e}")
    raise HttpError(404, f"no process is waiting for {name!r} with key {data.correlation_key!r}")
