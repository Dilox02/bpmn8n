"""Pages of a long list: the range shown ("1–50 of 2,377"), the page numbers with gaps, and links that keep the
rest of the query (filters, tabs), for templates/bpmn8n/pager.html."""
from django.core.paginator import Paginator


def paginate(request, items, per_page=50):
    """(the items of the page asked for by ?page=, the pager's context). A page past the end shows the last one."""
    page = Paginator(items, per_page).get_page(request.GET.get("page"))
    query = request.GET.copy()

    def url(number):
        query["page"] = number
        return f"?{query.urlencode()}"

    numbers = page.paginator.get_elided_page_range(page.number, on_each_side=1, on_ends=1) if page.paginator.num_pages > 1 else []
    return page.object_list, {
        "total": page.paginator.count, "range": f"{page.start_index():,}–{page.end_index():,} of {page.paginator.count:,}",
        "pages": page.paginator.num_pages,
        "links": [{"number": n, "url": "" if n == Paginator.ELLIPSIS else url(n), "current": n == page.number} for n in numbers],
        "previous": url(page.previous_page_number()) if page.has_previous() else "",
        "next": url(page.next_page_number()) if page.has_next() else ""}
