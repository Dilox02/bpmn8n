"""Filters of the Insights page: durations, shares and the width of an inline bar."""
from django import template

from ..analytics.log import duration_text
from ..simulation.spec import UNITS

register = template.Library()


@register.filter
def seconds(value):
    """Seconds in one or two units: "12 min", "3 h 20 min"."""
    return duration_text(value)


@register.filter
def percent(value, digits=0):
    """A share (0–1) as a percentage: 0.125 -> "13 %"; a dash when unknown."""
    if value is None:
        return "—"
    return f"{value * 100:.{int(digits)}f} %"


@register.filter
def width(value, top=1):
    """The width of an inline bar: value over top, as a CSS percentage (0–100)."""
    if not value or not top:
        return "0%"
    return f"{max(0.0, min(100.0, 100 * value / top)):.1f}%"


@register.filter
def number(value, digits=1):
    """A number with at most `digits` decimals; a dash when unknown."""
    if value is None:
        return "—"
    return f"{value:,.{int(digits)}f}".rstrip("0").rstrip(".") if digits else f"{value:,.0f}"


@register.filter
def signed(value, kind="number"):
    """A difference with its sign: "+12 %", "−3 min", "+2"."""
    if value is None:
        return "—"
    sign = "+" if value > 0 else "−" if value < 0 else "±"
    if kind == "seconds":
        return sign + duration_text(abs(value))
    if kind == "percent":
        return f"{sign}{abs(value) * 100:.0f} pt"
    return f"{sign}{number(abs(value), 2)}"


@register.filter
def span(value, unit="minutes"):
    """A time in a simulation spec's unit, in words: 12.5 (minutes) -> "12.5 min"."""
    return "0" if value == 0 else duration_text(None if value is None else value * UNITS.get(unit, 1.0) * 60)


@register.filter
def per_hour(value, unit="minutes"):
    """A rate per unit of a simulation spec, per hour: 0.1 (a minute) -> "6 /h"."""
    return "—" if value is None else f"{value * 60 / UNITS.get(unit, 1.0):,.3g} /h"


@register.filter
def relative(value):
    """A relative error with its sign: 0.123 -> "+12 %"; a dash when unknown."""
    if value is None:
        return "—"
    return f"{'+' if value > 0 else '−' if value < 0 else '±'}{abs(value) * 100:.0f} %"
