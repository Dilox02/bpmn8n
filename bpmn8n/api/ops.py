"""Operations: health for load balancers and the image's HEALTHCHECK (no token), Prometheus metrics (token)."""
from django.http import HttpResponse, JsonResponse
from ninja import Router

from ..ops import health, prometheus

router = Router(tags=["operations"])


@router.get("/health", auth=None)
def get_health(request):
    """The database, Redis, n8n, the models server and search: up, down or off, with latencies; overall ok,
    degraded or down. 503 only when the database is down: restarting this server cannot fix the others."""
    report = health.report()
    return JsonResponse(report, status=503 if report["status"] == "down" else 200)


@router.get("/metrics/prometheus")
def get_prometheus(request):
    """Prometheus text format: calls, errors and time per endpoint, cases, waiting tasks and messages, Studio."""
    return HttpResponse(prometheus.exposition(), content_type=prometheus.CONTENT_TYPE)
