"""Charts drawn on the server as inline SVG, for any page: no script, no library. One series per chart
(two measures are two charts, never two y-axes), in the accent colour of the page's tokens, so light and
dark follow the operating system as the rest of the CSS does (styles in static/bpmn8n/insights.css).
The plot stretches to its box; the axis labels are HTML, so text keeps its size from 390 to 1440 px.
Every bar or point carries a <title>: the browser shows the exact value on hover."""
import math

from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

WIDTH, HEIGHT = 1000, 100  # the plot's user units; preserveAspectRatio="none" stretches them to the box


def nice(value):
    """The smallest 1, 2 or 5 × 10^k at or above value (an axis maximum)."""
    if value <= 0:
        return 1
    power = 10 ** math.floor(math.log10(value))
    return next(m * power for m in (1, 2, 5, 10) if m * power >= value)


def _axis(top, fmt):
    """Three recessive gridlines (0, half, top) with their labels."""
    lines = format_html_join("", '<line x1="0" x2="{}" y1="{}" y2="{}" vector-effect="non-scaling-stroke"/>',
                             ((WIDTH, HEIGHT * (1 - f), HEIGHT * (1 - f)) for f in (0, .5, 1)))
    labels = format_html_join("", '<span style="top: {}%">{}</span>', ((100 - f * 100, fmt(top * f)) for f in (0, .5, 1)))
    return lines, labels


def _frame(label, summary, svg, y_labels, x_labels):
    return format_html(
        '<figure class="chart"><figcaption>{}</figcaption><div class="plot" role="img" aria-label="{}">'
        '<div class="y-labels" aria-hidden="true">{}</div>{}</div><div class="x-labels" aria-hidden="true">{}</div></figure>',
        label, summary, y_labels, svg, x_labels)


def series(points, *, label, value=lambda p: p, when=lambda p: "", fmt=str, area=True):
    """A line (with its area) over evenly spaced points, e.g. a timeline's buckets."""
    values = [value(p) for p in points]
    if not values:
        return ""
    top = nice(max(values))
    step = WIDTH / max(len(values) - 1, 1)
    xy = [(i * step, HEIGHT - v / top * HEIGHT) for i, v in enumerate(values)]
    if len(xy) == 1:
        xy.append((WIDTH, xy[0][1]))
    line = " ".join(f"{x:.1f},{y:.2f}" for x, y in xy)
    lines, y_labels = _axis(top, fmt)
    hits = format_html_join("", '<rect x="{}" width="{}" y="0" height="{}"><title>{}: {}</title></rect>',
                            ((max(i * step - step / 2, 0), step, HEIGHT, when(p), fmt(v))
                             for i, (p, v) in enumerate(zip(points, values))))
    svg = format_html(
        '<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true"><g class="grid">{lines}</g>'
        '{area}<polyline class="line" points="{line}" vector-effect="non-scaling-stroke"/><g class="hits">{hits}</g></svg>',
        w=WIDTH, h=HEIGHT, lines=lines, line=line, hits=hits,
        area=format_html('<polygon class="area" points="0,{h} {line} {w},{h}"/>', h=HEIGHT, line=line, w=xy[-1][0])
        if area else "")
    middle = points[len(points) // 2]
    x_labels = format_html("<span>{}</span><span>{}</span><span>{}</span>", when(points[0]), when(middle), when(points[-1]))
    peak = max(range(len(values)), key=values.__getitem__)
    summary = f"{label}: from {when(points[0])} to {when(points[-1])}, highest {fmt(values[peak])} at {when(points[peak])}"
    return _frame(label, summary, svg, y_labels, x_labels)


def bars(bins, *, label, value=lambda b: b, name=str, fmt=str, edges=None):
    """Vertical bars side by side, e.g. a histogram. `edges` labels the x axis (first, middle, last)."""
    values = [value(b) for b in bins]
    if not values:
        return ""
    top = nice(max(values))
    width = 100 / len(values)
    rects = format_html_join("", '<rect x="{}%" width="{}%" y="{}%" height="{}%" rx="2"><title>{}: {}</title></rect>',
                             ((i * width, width, 100 - v / top * 100, v / top * 100, name(b), fmt(v))
                              for i, (b, v) in enumerate(zip(bins, values))))
    lines = format_html_join("", '<line x1="0" x2="100%" y1="{}%" y2="{}%"/>', ((100 - f * 100,) * 2 for f in (0, .5, 1)))
    _, y_labels = _axis(top, fmt)
    svg = format_html('<svg class="bars" width="100%" height="100%" aria-hidden="true"><g class="grid">{}</g>{}</svg>',
                      lines, rects)
    x_labels = mark_safe("".join(format_html("<span>{}</span>", e) for e in (edges or ())))  # noqa: S308 (escaped)
    peak = max(range(len(values)), key=values.__getitem__)
    return _frame(label, f"{label}: most at {name(bins[peak])} ({fmt(values[peak])})", svg, y_labels, x_labels)
