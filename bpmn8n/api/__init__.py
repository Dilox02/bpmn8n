"""bpmn8n's HTTP API (/api/, Swagger at /api/docs). Every @task becomes POST /api/tasks/<name>,
tagged `x-bpmn-task: <kind>` in /api/openapi.json so the matching n8n node lists it."""
from django.utils.crypto import constant_time_compare
from ninja import NinjaAPI

from ..conf import settings
from . import gateways, messages, mining, observability, tasks, usertasks, workflows
from .tasks import TASKS, Task, TaskError, discover, task, task_modules


def token_auth(request):
    return not settings.API_TOKEN or constant_time_compare(
        request.headers.get("Authorization", ""), f"Bearer {settings.API_TOKEN}"
    )


api = NinjaAPI(title="bpmn8n", version="1", auth=token_auth)
for prefix, module in (("/tasks", tasks), ("/gateways", gateways), ("/usertasks", usertasks),
                       ("/messages", messages), ("/workflows", workflows), ("", observability), ("", mining)):
    api.add_router(prefix, module.router)


def openapi_cors(get_response):
    """Middleware: n8n's editor (n8n-editor/node-panel.js) reads the tasks' OpenAPI from its own origin."""
    def middleware(request):
        response = get_response(request)
        if request.path == "/api/openapi.json":
            response["Access-Control-Allow-Origin"] = settings.N8N_PUBLIC_URL
        return response
    return middleware


@api.exception_handler(TaskError)
def task_error(request, error):  # the n8n node shows `detail`: say which task broke its contract
    return api.create_response(request, {"detail": str(error)}, status=500)


__all__ = ["TASKS", "Task", "TaskError", "api", "discover", "task", "task_modules"]
