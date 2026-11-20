"""The configuration in effect (bpmn8n/config.py, read from this process's environment), secrets masked as
"set" or "empty": a table, `--env` as NAME=value lines, `--json`, or `--md` the table of docs/GUIDE.md."""
import json

from django.core.management.base import BaseCommand

from bpmn8n.conf import CONFIG
from bpmn8n.config import markdown


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []  # it shows the configuration of a broken installation too

    def add_arguments(self, parser):
        formats = parser.add_mutually_exclusive_group()
        formats.add_argument("--env", action="store_true", help="NAME=value lines")
        formats.add_argument("--json", dest="as_json", action="store_true", help="a JSON object")
        formats.add_argument("--md", action="store_true", help="the documentation's table (defaults, not values)")

    def handle(self, env, as_json, md, **options):
        shown = CONFIG.shown()
        if md:
            self.stdout.write(markdown())
        elif as_json:
            self.stdout.write(json.dumps(shown, indent=2))
        else:
            width = max(map(len, shown))
            self.stdout.write("\n".join(f"{k}={v}" if env else f"{k:{width}}  {v}" for k, v in shown.items()))
