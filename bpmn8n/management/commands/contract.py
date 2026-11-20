"""The contract with the n8n nodes (bpmn8n/api/contract.py): its version, the X-Bpmn8n-* headers and the request
shapes, as JSON. --ts rewrites n8n-nodes-bpmn8n/nodes/shared/contract.ts from them; then `npm run build`."""
import json

from django.core.management.base import BaseCommand

from bpmn8n.api import contract
from bpmn8n.core.headers import HEADERS


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--ts", action="store_true", help=f"rewrite {contract.TS_FILE.name}")

    def handle(self, ts, **options):
        if ts:
            contract.TS_FILE.write_text(contract.typescript())
            self.stdout.write(f"wrote {contract.TS_FILE} (contract {contract.version()})")
        else:
            self.stdout.write(json.dumps({"version": contract.version(), "headers": HEADERS,
                                          "shapes": contract.schemas()}, indent=2))
