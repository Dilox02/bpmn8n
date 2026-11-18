"""Filters of the Insights page: durations, shares and the width of an inline bar."""
from django import template

from ..analytics.log import duration_text

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
