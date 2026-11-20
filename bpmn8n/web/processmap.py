"""The discovered process map as inline SVG, drawn on the server (no script): activities as boxes in columns,
directly-follows edges as arrows. Frequency mode sizes the lines by how often; performance mode tints by mean
time. Colours are the page's tokens (static/bpmn8n/insights.css, .pmap)."""
from django.utils.html import format_html, format_html_join

from ..analytics import dfg
from ..analytics.log import duration_text

W, H, GX, GY, PAD = 160, 46, 110, 26, 14  # box size, gaps between columns and rows, margin
MAX_ACTIVITIES = 60  # a page of boxes, not a hairball


def heat(value, top):
    return round(100 * min(max(value or 0, 0) / top, 1)) if top else 0


def render(acts, edges, measure="frequency"):
    """The SVG of a (pruned) map; "" when there is nothing to draw."""
    if not acts:
        return ""
    where = dfg.layout(acts, edges)
    pos = {a: (PAD + c * (W + GX), PAD + r * (H + GY)) for a, (c, r) in where.items()}
    width = max(x for x, _ in pos.values()) + W + PAD
    height = max(y for _, y in pos.values()) + H + PAD
    perf = measure == "performance"
    key = "mean" if perf else "frequency"
    top = max((e[key] or 0 for e in edges.values()), default=0)
    top_act = max((a[key] or 0 for a in acts.values()), default=0)
    paths = []
    for (a, b), e in sorted(edges.items()):
        (x1, y1), (x2, y2) = pos[a], pos[b]
        if x2 > x1:  # forward: right side of the source to the left side of the target
            sx, sy, tx, ty = x1 + W, y1 + H / 2, x2, y2 + H / 2
            d, lx, ly = f"M{sx} {sy} C{sx + GX / 2} {sy} {tx - GX / 2} {ty} {tx} {ty}", (sx + tx) / 2, (sy + ty) / 2
        else:  # a loop, or back: out of the bottom, round and into the bottom
            sx, sy, tx, ty = x1 + W / 2, y1 + H, x2 + W / 2, y2 + H
            drop = max(sy, ty) + GY
            d, lx, ly = f"M{sx} {sy} C{sx} {drop} {tx} {drop} {tx} {ty}", (sx + tx) / 2, drop - GY / 4
        paths.append(format_html(
            '<g class="edge" style="--w: {}px; --heat: {}%"><title>{} to {}: {} time{}, mean {} between them</title>'
            '<path d="{}" marker-end="url(#pm-arrow)"/><text x="{}" y="{}" text-anchor="middle">{}</text></g>',
            1 if perf or not top else round(1 + 4 * e["frequency"] / top, 1), heat(e[key], top) if perf else 100, a, b,
            e["frequency"], "" if e["frequency"] == 1 else "s", duration_text(e["mean"]), d, round(lx), round(ly - 4),
            duration_text(e["mean"]) if perf else e["frequency"]))
    boxes = format_html_join("", '<g class="act" style="--heat: {}%"><title>{}: {} time{}, mean {}</title>'
                                 '<rect x="{}" y="{}" width="{}" height="{}" rx="8"/>'
                                 '<text x="{}" y="{}" text-anchor="middle">{}</text>'
                                 '<text class="sub" x="{}" y="{}" text-anchor="middle">{}</text></g>',
                             ((heat(a[key], top_act), name, a["frequency"], "" if a["frequency"] == 1 else "s",
                               duration_text(a["mean"]), *pos[name], W, H, pos[name][0] + W / 2, pos[name][1] + 19,
                               name if len(name) <= 22 else name[:21] + "…", pos[name][0] + W / 2, pos[name][1] + 36,
                               duration_text(a["mean"]) if perf else f"{a['frequency']}×")
                              for name, a in sorted(acts.items(), key=lambda kv: pos[kv[0]])))
    return format_html(
        '<svg class="pmap" viewBox="0 0 {} {}" role="img" aria-label="Directly-follows map: {} activities, {} paths">'
        '<defs><marker id="pm-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
        '<path d="M0 0L10 5L0 10z"/></marker></defs>{}{}</svg>',
        width, height, len(acts), len(edges), format_html_join("", "{}", ((p,) for p in paths)), boxes)


def percent(text):
    """A slider's value (0 to 100); 100 when empty or crafted."""
    return min(int(text), 100) if str(text).isdigit() else 100


def section(log, state):
    """The map's context for the page: the sliders' values, the toggle, the SVG and what was left out."""
    acts, edges = dfg.discover(log)
    top = dict(sorted(acts.items(), key=lambda kv: (-kv[1]["frequency"], kv[0]))[:MAX_ACTIVITIES])
    a_pct, p_pct = percent(state.get("acts", "")), percent(state.get("paths", ""))
    shown, kept = dfg.prune(top, edges, a_pct, p_pct)
    measure = "performance" if state.get("map") == "performance" else "frequency"
    return {"svg": render(shown, kept, measure), "acts_pct": a_pct, "paths_pct": p_pct, "measure": measure,
            "acts_shown": len(shown), "acts_total": len(acts), "paths_shown": len(kept), "paths_total": len(edges)}
