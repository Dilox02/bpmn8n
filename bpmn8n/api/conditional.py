"""Conditional JSON responses shared by the live editor views."""
import hashlib
import json

from django.http import HttpResponse, JsonResponse


def revision(value):
    return '"' + hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest() + '"'


def conditional(request, data, etag):
    response = HttpResponse(status=304) if request.headers.get("If-None-Match") == etag else JsonResponse(data)
    response["ETag"] = etag
    return response
