"""Builds a bpmn8n Studio project from its interview (what /studio/ starts): see bpmn8n/studio/build.py."""
from django.core.management.base import BaseCommand

from bpmn8n.api import discover
from bpmn8n.models import Project
from bpmn8n.studio.jobs import heartbeat
from bpmn8n.studio.build import ProjectBuilder


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("slug")

    def handle(self, slug, **options):
        discover()  # the catalog of tasks the design reuses
        project = Project.objects.get(slug=slug)
        heartbeat(project)
        ProjectBuilder(project).run()  # no default LLM (removed meanwhile): its first card fails, so does the build
