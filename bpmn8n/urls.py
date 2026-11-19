from django.conf import settings
from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path
from django.views.generic import RedirectView

from .api import api, discover
from .web import cases, insights, landscape, operations, people, simulation, studio, views

discover()  # every app's tasks register their @task endpoints (before api.urls); broken ones are logged

urlpatterns = [
    path("", RedirectView.as_view(url="/docs/")),
    path("api/", api.urls),
    path("docs/", views.docs, name="docs"),
    path("activities/", views.activities, name="activities"),
    path("login/", LoginView.as_view(template_name="bpmn8n/login.html"), name="login"),
    path("logout/", LogoutView.as_view(next_page="login"), name="logout"),
    path("theme/", views.theme, name="theme"),
    path("inbox/", views.inbox, name="inbox"),
    path("inbox/<int:pk>/", views.task, name="task"),
    path("inbox/<int:pk>/<str:action>/", views.act, name="act"),
    path("workload/", people.workload, name="workload"),
    path("notifications/", people.notifications, name="notifications"),
    path("start/", people.start_case, name="start_case"),
    path("start/<path:hook>/", people.start_run, name="start_run"),
    path("cases/<str:case_id>/", cases.case, name="case"),  # "<workflow id>:<execution id>"
    path("insights/", insights.insights, name="insights"),
    path("simulation/", simulation.simulation, name="simulation"),
    path("studio/", studio.projects, name="studio"),
    path("studio/<slug:slug>/", studio.project, name="project"),
    path("studio/<slug:slug>/build/", studio.build, name="build"),
    path("studio/<slug:slug>/events/", studio.events, name="events"),
    path("landscape/", landscape.landscape, name="landscape"),
    path("status/", operations.status, name="status"),
    path("variables/", operations.variables, name="variables"),
    path("cases/", operations.cases, name="cases"),
    path("workflows/", operations.workflows, name="workflows"),
    path("workflows/<str:workflow_id>/", operations.workflow_history, name="workflow_history"),
    path("workflows/<str:workflow_id>/<str:version_id>/restore/", operations.restore_version, name="restore_version"),
    path("admin/", admin.site.urls),
]
if settings.DEBUG:  # in production whitenoise serves what collectstatic gathered (bpmn8n/settings.py)
    urlpatterns += staticfiles_urlpatterns()
