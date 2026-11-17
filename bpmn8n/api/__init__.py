"""bpmn8n's HTTP API (/api/, Swagger at /api/docs). Every @task becomes POST /api/tasks/<name>,
tagged `x-bpmn-task: <kind>` in /api/openapi.json so the matching n8n node lists it."""
from urllib.parse import urlsplit

from django.http import HttpResponse
from django.utils.cache import patch_vary_headers
from django.utils.crypto import constant_time_compare
from ninja import NinjaAPI

from ..conf import settings
from . import evaluations, executions, gateways, messages, mining, observability, tasks, usertasks, workflows
from .tasks import TASKS, Task, TaskError, discover, task, task_modules


def token_auth(request):
    return not settings.API_TOKEN or constant_time_compare(
        request.headers.get("Authorization", ""), f"Bearer {settings.API_TOKEN}"
    )


api = NinjaAPI(title="bpmn8n", version="1", auth=token_auth)
for prefix, module in (("/tasks", tasks), ("/gateways", gateways), ("/usertasks", usertasks),
                       ("/messages", messages), ("/workflows", workflows), ("/evaluations", evaluations),
                       ("/executions", executions),
                       ("", observability), ("", mining)):
    api.add_router(prefix, module.router)


def openapi_cors(get_response):
    """Middleware: n8n's editor (n8n-editor/node-panel.js) reads the tasks' OpenAPI from its own origin."""
    def middleware(request):
        read_path = request.path.startswith(("/api/evaluations/", "/api/executions/"))
        preflight = read_path and request.method == "OPTIONS"
        response = HttpResponse(status=204) if preflight else get_response(request)
        evaluations_read = read_path and request.method in ("GET", "OPTIONS")
        if request.path == "/api/openapi.json" or evaluations_read:
            public = urlsplit(settings.N8N_PUBLIC_URL)
            configured = f"{public.scheme}://{public.netloc}"
            origins = {configured}
            if public.hostname in {"localhost", "127.0.0.1"}:
                port = f":{public.port}" if public.port is not None else ""
                origins.update(f"{public.scheme}://{host}{port}" for host in ("localhost", "127.0.0.1"))
            origin = request.headers.get("Origin", configured)
            if origin in origins:
                response["Access-Control-Allow-Origin"] = origin
            patch_vary_headers(response, ["Origin"])
        if evaluations_read:
            response["Access-Control-Allow-Credentials"] = "true"
            response["Cache-Control"] = "no-store"
            patch_vary_headers(response, ["Cookie"])
            response["Access-Control-Expose-Headers"] = "ETag, Content-Disposition"
        if request.method == "OPTIONS" and request.path.startswith(("/api/evaluations/", "/api/executions/")):
            response["Access-Control-Allow-Headers"] = "If-None-Match, Last-Event-ID, browser-id"
            response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return response
    return middleware


@api.exception_handler(TaskError)
def task_error(request, error):  # the n8n node shows `detail`: say which task broke its contract
    return api.create_response(request, {"detail": str(error)}, status=500)


__all__ = ["TASKS", "Task", "TaskError", "api", "discover", "task", "task_modules"]
