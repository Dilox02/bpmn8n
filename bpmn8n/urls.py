from django.conf import settings
from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path
from django.views.generic import RedirectView

from .api import api, discover
from .web import importer, insights, landscape, operations, people, simulation, studio, views, workflow

discover()  # every app's tasks register their @task endpoints (before api.urls); broken ones are logged

urlpatterns = [
    # the framework: what spans every workflow
    path("", RedirectView.as_view(pattern_name="inbox")),  # the most frequent job
    path("inbox/", views.inbox, name="inbox"),
    path("inbox/<int:pk>/", views.task, name="task"),
    path("inbox/<int:pk>/<str:action>/", views.act, name="act"),
    path("start/", people.start_case, name="start_case"),
    path("start/<path:hook>/", people.start_run, name="start_run"),
    path("workload/", people.workload, name="workload"),
    path("notifications/", people.notifications, name="notifications"),
    path("cases/", operations.cases, name="cases"),
    path("cases/<str:case_id>/", workflow.old_case, name="case"),  # moved under its workflow: links in mails and nodes
    path("insights/", insights.insights, name="insights"),
    path("insights/snapshots/", insights.save_snapshot, name="save_snapshot"),
    path("insights/snapshots/<int:pk>/", insights.open_snapshot, name="snapshot"),
    path("insights/import/", importer.import_log, name="insights_import"),
    path("simulation/", simulation.simulation, name="simulation"),
    path("studio/", studio.projects, name="studio"),
    path("studio/<slug:slug>/", studio.project, name="project"),
    path("studio/<slug:slug>/build/", studio.build, name="build"),
    path("studio/<slug:slug>/events/", studio.events, name="events"),
    path("activities/", views.activities, name="activities"),
    path("landscape/", landscape.landscape, name="landscape"),
    path("variables/", operations.variables, name="variables"),
    path("status/", operations.status, name="status"),
    path("editor/", views.n8n, name="n8n"),  # n8n's screens inside bpmn8n
    path("docs/", views.docs, name="docs"),
    path("docs/<slug:section>/", views.docs, name="docs_section"),
    # one workflow, keyed by its n8n id as n8n keys it (bpmn8n/web/workflow.py)
    path("workflows/", operations.workflows, name="workflows"),
    path("workflows/<str:workflow_id>/", workflow.overview, name="workflow"),
    path("workflows/<str:workflow_id>/cases/", workflow.cases, name="workflow_cases"),
    path("workflows/<str:workflow_id>/editor/", workflow.editor, name="workflow_editor"),
    path("workflows/<str:workflow_id>/cases/<str:execution>/", workflow.case, name="workflow_case"),
    path("workflows/<str:workflow_id>/cases/<str:execution>/n8n/", workflow.case_in_n8n, name="workflow_case_n8n"),
    path("workflows/<str:workflow_id>/insights/", workflow.insights, name="workflow_insights"),
    path("workflows/<str:workflow_id>/simulation/", workflow.simulation, name="workflow_simulation"),
    path("workflows/<str:workflow_id>/versions/", workflow.versions, name="workflow_versions"),
    path("workflows/<str:workflow_id>/versions/<str:version_id>/restore/", operations.restore_version, name="restore_version"),
    path("workflows/<str:workflow_id>/start/", workflow.start, name="workflow_start"),
    # accounts, the theme, the API and the admin
    path("login/", LoginView.as_view(template_name="bpmn8n/login.html"), name="login"),
    path("logout/", LogoutView.as_view(next_page="login"), name="logout"),
    path("theme/", views.theme, name="theme"),
    path("api/", api.urls),
    path("admin/", admin.site.urls),
]
if settings.DEBUG:  # in production whitenoise serves what collectstatic gathered (bpmn8n/settings.py)
    urlpatterns += staticfiles_urlpatterns()
