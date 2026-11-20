"""The workflow a framework page is narrowed to (?workflow=<n8n id>) and the choices it offers: the workflows n8n has
that have something on this page, with how much, as a row of their marks (workflow_filter.html). Every page that spans
workflows says which workflow each thing belongs to and lets a person look at one at a time."""
from ..core import directory


def choices(request, counts, param="workflow"):
    """[{id, name, count, url, current}]: "All workflows", then the workflows in n8n with something here, the busiest
    first (and the chosen one, even with nothing)."""
    live = {r.id: r for r in directory.workflows() if r.in_n8n}
    current = request.GET.get(param, "")

    def url(value):
        query = request.GET.copy()
        query.pop("page", None)
        query.pop(param, None)
        if value:
            query[param] = value
        return f"?{query.urlencode()}" if query else request.path

    shown = sorted((r for wid, r in live.items() if counts.get(wid) or wid == current),
                   key=lambda r: (-counts.get(r.id, 0), r.name.lower()))
    return [{"id": "", "name": "All workflows", "count": sum(counts.get(wid, 0) for wid in live), "url": url(""),
             "current": not current},
            *({"id": r.id, "name": r.name, "count": counts.get(r.id, 0), "url": url(r.id), "current": r.id == current}
              for r in shown)]


def workflow_of(case_id):
    return str(case_id).rpartition(":")[0]
