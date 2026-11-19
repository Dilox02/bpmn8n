"""BPMN signals: a Signal Catch node waits for a named signal (no correlation key); one POST delivers it to
every execution waiting for it, each of which resumes once. Unlike a message, a signal reaches many."""
from typing import Any

from ninja import Router, Schema
from ninja.errors import HttpError

from ..clients.http import ServiceUnavailable
from ..models import Cancelled, MessageSubscription, TransitionError

router = Router(tags=["signals"])


class SignalIn(Schema):
    payload: dict[str, Any] = {}


class SignalOut(Schema):
    name: str
    delivered: int  # executions resumed
    failed: list[str] = []  # why n8n did not take it from the others (they stay waiting: post it again)


def broadcast(name, payload):
    """Delivers signal `name` to every execution waiting for it: (how many resumed, why the others did not).
    Idempotent: a catch that resumed no longer waits, so posting it again reaches only new waiters.
    ponytail: one after the other, each a resume call to n8n; hundreds of waiters want a queue."""
    delivered, failed = 0, []
    for sub in MessageSubscription.objects.for_signal(name):
        try:
            sub.deliver(payload)
            delivered += 1
        except (Cancelled, TransitionError):
            continue  # that execution is gone, or another delivery won
        except ServiceUnavailable as e:
            failed.append(f"{sub.case_id}: {e}")
    return delivered, failed


@router.post("/{name}", response=SignalOut)
def throw_signal(request, name: str, data: SignalIn):
    """Delivers the signal to every execution waiting for it. 404 when none waits (signals are not queued),
    502 when n8n took it from none of them."""
    delivered, failed = broadcast(name, data.payload)
    if not delivered and failed:
        raise HttpError(502, f"n8n unreachable, signal not delivered: {failed[0]}")
    if not delivered:
        raise HttpError(404, f"no process is waiting for signal {name!r}")
    return {"name": name, "delivered": delivered, "failed": failed}
