from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import path
from django.views.generic import RedirectView

from .api import api, discover
from .web import studio, views

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
    path("studio/", studio.projects, name="studio"),
    path("studio/<slug:slug>/", studio.project, name="project"),
    path("studio/<slug:slug>/build/", studio.build, name="build"),
    path("studio/<slug:slug>/events/", studio.events, name="events"),
    path("admin/", admin.site.urls),
]
urlpatterns += staticfiles_urlpatterns()  # admin CSS when DEBUG; in production use collectstatic + a web server
