"""Global variables over HTTP (core/shared.py): what the Global Variable node calls, and any other system.
Keys are letters, digits, _ . and -; values are JSON."""
from datetime import datetime
from typing import Any

from ninja import Router, Schema
from ninja.errors import HttpError
from pydantic import Field

from ..core import shared
from ..models import AuditEntry

router = Router(tags=["variables"])


class VariableOut(Schema):
    key: str
    value: Any = None
    updated: datetime | None = None
    expires: datetime | None = None  # empty: never


class ValueIn(Schema):
    value: Any = None
    ttl: float | None = Field(None, gt=0, description="seconds it lives; empty: for ever")


class IncrementIn(Schema):
    by: int | float = 1
    ttl: float | None = Field(None, gt=0, description="seconds, when the increment creates the variable")


class SwapIn(Schema):
    expected: Any = None  # a variable that does not exist counts as null
    value: Any = None
    ttl: float | None = Field(None, gt=0)


def audit(verb, key, **details):
    """In the audit log: the key and how, never the value (it may be process data)."""
    AuditEntry.write(f"variable.{verb}", object_type="variable", object_id=key, **details)


def checked(key):
    try:
        return shared.check_key(key)
    except shared.InvalidKey as e:
        raise HttpError(422, str(e)) from None


@router.get("", response={200: list[VariableOut], 422: dict})
def list_variables(request, prefix: str = ""):
    """Every global variable whose key starts with `prefix`, by key (at most 1000)."""
    if prefix:
        checked(prefix)
    return 200, shared.store().list(prefix)


@router.get("/{key}", response={200: VariableOut, 404: dict})
def get_variable(request, key: str):
    if variable := shared.store().read(checked(key)):
        return 200, variable
    return 404, {"detail": f"no global variable {key!r}"}


@router.put("/{key}", response={200: VariableOut})
def set_variable(request, key: str, data: ValueIn):
    store = shared.store()
    try:
        store.set(checked(key), data.value, data.ttl)
    except ValueError as e:
        raise HttpError(422, str(e)) from None
    audit("set", key, ttl=data.ttl)
    return store.read(key) or shared.Variable(key, data.value)


@router.delete("/{key}", response={204: None, 404: dict})
def delete_variable(request, key: str):
    if shared.store().delete(checked(key)):
        audit("delete", key)
        return 204, None
    return 404, {"detail": f"no global variable {key!r}"}


@router.post("/{key}/increment", response={200: VariableOut, 409: dict})
def increment_variable(request, key: str, data: IncrementIn):
    """Adds `by` (negative to subtract) atomically and returns the new value; a missing variable counts as 0."""
    try:
        value = shared.store().increment(checked(key), data.by, data.ttl)
    except shared.NotANumber as e:
        return 409, {"detail": str(e)}
    audit("increment", key, by=data.by)
    return 200, shared.Variable(key, value)


@router.post("/{key}/compare-and-set", response={200: VariableOut, 409: dict})
def compare_and_set_variable(request, key: str, data: SwapIn):
    """Sets `value` only if the variable still holds `expected`; 409 with the current value otherwise."""
    store = shared.store()
    try:
        swapped = store.compare_and_set(checked(key), data.expected, data.value, data.ttl)
    except ValueError as e:
        raise HttpError(422, str(e)) from None
    if swapped:
        audit("compare_and_set", key, ttl=data.ttl)
        return 200, shared.Variable(key, data.value)
    current = store.read(key)
    return 409, {"detail": f"{key!r} no longer holds the expected value", "value": current.value if current else None}
