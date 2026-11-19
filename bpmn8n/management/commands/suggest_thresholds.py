"""J6: proposes, per Laya gateway, the min_confidence that keeps the share of its decisions a person overrode
(the form after it chose another branch: core/overrides.py) at or below a target, from confidence against
override in the event log. Only suggests: changing the gateway's `min_confidence` is a person's decision."""
from django.core.management.base import BaseCommand

from bpmn8n.core.overrides import override_rate, reviewed_by_gateway, suggest
from bpmn8n.models import Event


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--process", help="only this process (the n8n workflow's name)")
        parser.add_argument("--target", type=float, default=0.05, help="the override rate to stay under (default 0.05)")
        parser.add_argument("--min-decisions", type=int, default=10, help="reviewed decisions needed (default 10)")

    def handle(self, process, target, min_decisions, **options):
        events = Event.objects.filter(data__has_key="overridden")
        if process:
            events = events.filter(process=process)
        found = reviewed_by_gateway(events.iterator())
        for (proc, gateway), pairs in sorted(found.items()):
            now = override_rate(pairs)
            best = suggest(pairs, target, min_decisions)
            line = f"{proc} · {gateway}: {now['reviewed']} reviewed, {now['rate']:.0%} overridden. "
            self.stdout.write(line + (f"min_confidence {best['min_confidence']:g} keeps {best['decisions']} decisions "
                                      f"at {best['override_rate']:.0%}" if best else
                                      f"no min_confidence keeps it at {target:.0%} or less (with {min_decisions}+ decisions)"))
        if not found:
            self.stdout.write("no reviewed decisions yet: a form after a Laya gateway with a field named like it "
                              "(see core/overrides.py)")
