"""Insights: each filter on a tiny log, each statistic against figures computed by hand, the queueing view
against queueing.py on a generated log with known rates, the API and the page."""
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from ..analytics import FilterError, FilterStack, Log, Scope, queues, stats
from ..analytics.seed import generate
from ..models import Event, UserTask
from ..simulation import queueing
from .support import add_events, step

AT = datetime(2026, 1, 1, 10, tzinfo=UTC)


def tiny():
    """Three cases of P and one of Q (times in minutes after 10:00, durations in ms):
    1: Register 0–1, Approve (anna) 2–4, Pay 5–5:30              330 s
    2: Register 10–11, Approve (bob) 13–17, Pay 19–19:30 error   570 s
    3: Register 20–21, Reject (anna) 22–23                       180 s
    4 (Q): Scan 30–30:01"""
    add_events(
        ("w:1", "P", [step("Register", 0, 60_000), step("Approve", 2, 120_000, resource="anna"), step("Pay", 5, 30_000)]),
        ("w:2", "P", [step("Register", 10, 60_000), step("Approve", 13, 240_000, resource="bob"),
                      step("Pay", 19, 30_000, status="error")]),
        ("w:3", "P", [step("Register", 20, 60_000), step("Reject", 22, 60_000, resource="anna")]),
        ("w:4", "Q", [step("Scan", 30, 1000)]),
    )


class FiltersTest(TestCase):
    def setUp(self):
        tiny()
        self.log = Log.load()

    def kept(self, *filters):
        log, _ = self.log.filter(FilterStack.parse(list(filters)))
        return sorted(t.case_id[2:] for t in log.traces)

    def at(self, minute):
        return (AT + timedelta(minutes=minute)).isoformat()

    def test_timeframe_modes(self):
        self.assertEqual(self.kept({"type": "timeframe", "mode": "started", "start": self.at(10), "end": self.at(25)}), ["2", "3"])
        self.assertEqual(self.kept({"type": "timeframe", "mode": "contained", "start": self.at(0), "end": self.at(20)}), ["1", "2"])
        self.assertEqual(self.kept({"type": "timeframe", "start": self.at(5), "end": self.at(10)}), ["1", "2"])  # intersecting
        self.assertEqual(self.kept({"type": "timeframe", "mode": "ended", "start": self.at(15), "end": self.at(24)}), ["2", "3"])
        self.assertEqual(self.kept({"type": "timeframe", "mode": "started", "start": self.at(25)}), ["4"])

    def test_variants_top_and_coverage(self):
        self.assertEqual(self.kept({"type": "variants", "top": 1}), ["1", "2"])
        self.assertEqual(self.kept({"type": "variants", "coverage": 50}), ["1", "2"])
        self.assertEqual(self.kept({"type": "variants", "coverage": 60}), ["1", "2", "3"])  # ties: alphabetical

    def test_performance_duration_and_between(self):
        self.assertEqual(self.kept({"type": "performance", "min": 300}), ["1", "2"])
        self.assertEqual(self.kept({"type": "performance", "max": 200}), ["3", "4"])
        between = {"type": "performance", "measure": "between", "from": "Approve", "to": "Pay"}
        self.assertEqual(self.kept({**between, "max": 60}), ["1"])
        self.assertEqual(self.kept({**between, "min": 100}), ["2"])

    def test_endpoints(self):
        self.assertEqual(self.kept({"type": "endpoints", "start": "Register", "end": ["Pay"]}), ["1", "2"])
        self.assertEqual(self.kept({"type": "endpoints", "end": ["Reject", "Scan"]}), ["3", "4"])

    def test_attribute_include_and_exclude(self):
        self.assertEqual(self.kept({"type": "attribute", "key": "resource", "values": ["anna"]}), ["1", "3"])
        self.assertEqual(self.kept({"type": "attribute", "key": "resource", "values": "anna", "mode": "exclude"}), ["2", "4"])
        self.assertEqual(self.kept({"type": "attribute", "key": "status", "values": ["error"]}), ["2"])
        self.assertEqual(self.kept({"type": "attribute", "key": "process", "values": ["Q"]}), ["4"])
        self.assertEqual(self.kept({"type": "attribute", "key": "activity", "values": ["Reject"], "mode": "exclude"}), ["1", "2", "4"])

    def test_follower_directly_eventually_within(self):
        self.assertEqual(self.kept({"type": "follower", "from": "Register", "to": "Approve", "relation": "directly"}), ["1", "2"])
        self.assertEqual(self.kept({"type": "follower", "from": "Register", "to": "Pay", "relation": "directly"}), [])
        self.assertEqual(self.kept({"type": "follower", "from": "Register", "to": "Pay"}), ["1", "2"])
        self.assertEqual(self.kept({"type": "follower", "from": "Register", "to": "Pay", "within": 300}), ["1"])
        self.assertEqual(self.kept({"type": "follower", "from": "Register", "to": "Approve", "mode": "exclude"}), ["3", "4"])

    def test_stack_applies_in_order_and_reports_each_step(self):
        stack = FilterStack.parse('[{"type": "attribute", "key": "resource", "values": ["anna"]}, {"type": "variants", "top": 1}]')
        log, steps = self.log.filter(stack)
        self.assertEqual([t.case_id for t in log.traces], ["w:1"])
        self.assertEqual([(s["before"], s["kept"]) for s in steps], [(4, 2), (2, 1)])
        self.assertEqual(steps[0]["label"], "With resource anna")
        self.assertEqual(FilterStack.parse(stack.query()).as_json(), stack.as_json())  # a round trip through the URL
        self.assertEqual(len(stack.without(0)), 1)

    def test_unreadable_stacks_say_why(self):
        for text, reason in (("not json", "JSON"), ('[{"type": "nope"}]', "type"), ('[{"type": "variants"}]', "top or coverage"),
                             ('[{"type": "timeframe"}]', "start or an end"), ('[{"type": "variants", "top": 1, "x": 2}]', "x"),
                             ('[{"type": "performance", "measure": "between", "max": 1}]', "from")):
            with self.subTest(text=text), self.assertRaisesRegex(FilterError, reason):
                FilterStack.parse(text)


class StatsTest(TestCase):
    def setUp(self):
        tiny()
        self.log = Log.load(Scope(process="P"))

    def test_overview(self):
        o = stats.overview(self.log)
        self.assertEqual((o["cases"], o["open"], o["closed"], o["events"], o["errors"], o["activities"]), (3, 0, 3, 8, 1, 4))
        self.assertEqual(o["case_duration"], {"mean": 360.0, "median": 330.0, "p90": 522.0, "min": 180.0, "max": 570.0})
        self.assertEqual((o["start"], o["end"]), (AT.isoformat(), (AT + timedelta(minutes=23)).isoformat()))

    def test_timeline_buckets_events_and_cases_in_progress(self):
        t = stats.timeline(self.log)
        self.assertEqual(t["bucket"], 60)  # 23 minutes: one point a minute
        points = t["points"]
        self.assertEqual(len(points), 23)
        self.assertEqual(sum(p["events"] for p in points), 8)
        self.assertEqual([points[i]["active"] for i in (0, 5, 6, 10, 19, 20, 22)], [1, 1, 0, 1, 1, 1, 1])

    def test_histogram_of_case_durations(self):
        bins = stats.histogram(self.log)
        self.assertEqual((len(bins), bins[0]["to"]), (10, 60.0))  # 60 s bins up to 570 s
        self.assertEqual({i for i, b in enumerate(bins) if b["cases"]}, {3, 5, 9})

    def test_activities(self):
        rows = {a["activity"]: a for a in stats.activities(self.log)}
        self.assertEqual(list(rows), ["Register", "Approve", "Pay", "Reject"])
        self.assertEqual((rows["Register"]["executions"], rows["Register"]["cases"], rows["Register"]["wait"]), (3, 3, None))
        self.assertEqual(rows["Approve"]["duration"], {"mean": 180.0, "median": 180.0, "p90": 228.0, "min": 120.0, "max": 240.0})
        self.assertEqual(rows["Approve"]["wait"], 90.0)  # 60 s and 120 s after Register ended
        self.assertEqual((rows["Pay"]["wait"], rows["Pay"]["errors"], rows["Pay"]["error_rate"]), (90.0, 1, 0.5))

    def test_waits_in_parallel_branches_count_from_the_latest_end(self):
        add_events(("w:9", "R", [step("A", 0, 60_000), step("B", 1, 600_000), step("C", 2, 60_000), step("D", 5, 1000)]))
        trace = Log.load(Scope(process="R")).traces[0]
        self.assertEqual(trace.waits(), [None, 0.0, 60.0, 120.0])  # D: C ended at 3, B was still running

    def test_resources_and_handoffs(self):
        rows = {x["resource"]: x for x in stats.resources(self.log)}
        self.assertEqual((rows["system"]["events"], rows["system"]["cases"], rows["system"]["busy"]), (5, 3, 240.0))
        self.assertEqual((rows["anna"]["busy"], rows["anna"]["utilisation"]), (180.0, round(180 / 1380, 3)))
        h = stats.handoffs(self.log)
        self.assertEqual((h["handoffs"], h["per_case"]), (5, 1.667))
        pairs = {(p["from"], p["to"]): (p["handoffs"], p["mean_wait"]) for p in h["pairs"]}
        self.assertEqual(pairs, {("system", "anna"): (2, 60.0), ("anna", "system"): (1, 60.0),
                                 ("system", "bob"): (1, 120.0), ("bob", "system"): (1, 120.0)})

    def test_variants(self):
        v = stats.variants(self.log)
        self.assertEqual(v["variants"], 2)
        first, second = v["rows"]
        self.assertEqual((first["activities"], first["cases"], first["coverage"], first["duration"]),
                         (["Register", "Approve", "Pay"], 2, 66.67, 450.0))
        self.assertEqual((second["cumulative"], second["duration"]), (100.0, 180.0))

    def test_decisions_per_gateway(self):
        def laya(branches, p):
            return step("Route", 1, data={"decider": "laya", "output": {"branches": branches, "scores": {"simple": p}}})

        add_events(("g:1", "G", [laya(["simple"], 0.9)]), ("g:2", "G", [laya(["simple"], 0.9)]), ("g:3", "G", [laya([], 0.5)]),
                   ("g:4", "G", [step("Route", 1, data={"decider": "conditions", "output": {"branches": ["complex"], "scores": {}}})]))
        log = Log.load(Scope(process="G"))
        [route] = stats.decisions(log)
        self.assertEqual((route["gateway"], route["decisions"], route["decided_by"]), ("Route", 4, {"laya": 3, "conditions": 1}))
        self.assertEqual({b["branch"]: b["share"] for b in route["branches"]}, {"simple": 0.5, "(default)": 0.25, "complex": 0.25})
        self.assertEqual(route["confidence"], {"mean": 0.767, "median": 0.9, "low": 0.333})
        self.assertEqual(stats.certainty({"a": 0.8, "b": 0.3}), 0.7)  # inclusive: the least sure yes/no
        self.assertEqual(stats.decisions(log.where(log.traces[:1]))[0]["decisions"], 1)  # follows the cases in scope

    def test_compare_two_cohorts(self):
        a, _ = self.log.filter(FilterStack.parse({"type": "attribute", "key": "resource", "values": ["anna"]}))
        b, _ = self.log.filter(FilterStack.parse({"type": "attribute", "key": "resource", "values": ["bob"]}))
        c = stats.compare(a, b)
        figures = {f["figure"]: f for f in c["figures"]}
        self.assertEqual((figures["cases"]["a"], figures["cases"]["b"], figures["cases"]["delta"]), (2, 1, -1))
        self.assertEqual((figures["case duration mean"]["a"], figures["case duration mean"]["b"]), (255.0, 570.0))
        approve = next(x for x in c["activities"] if x["activity"] == "Approve")
        self.assertEqual((approve["cases_share"]["a"], approve["cases_share"]["b"], approve["duration"]["delta"]), (0.5, 1.0, 120.0))

    def test_overlay_heat_is_relative_to_the_busiest_and_slowest_node(self):
        nodes = stats.overlay(self.log)
        self.assertEqual(nodes["Register"]["heat"], {"frequency": 1.0, "duration": round(60 / 180, 3)})
        self.assertEqual((nodes["Approve"]["heat"]["duration"], nodes["Pay"]["errors"], nodes["Reject"]["cases"]), (1.0, 1, 1))


TICKETS = {"arrivals_per_hour": 6, "start": "Work Ticket", "lanes": {"agents": ["ann", "ben"]},
           "steps": {"Work Ticket": {"kind": "userTask", "lane": "agents", "minutes": 15}}}


class QueuesTest(TestCase):
    """An M/M/2 lane: arrivals 6 an hour, work 15 minutes on average, two people (ρ = 0.75)."""

    @classmethod
    def setUpTestData(cls):
        generate("Tickets", 3000, seed=7, spec=TICKETS)

    def test_estimates_match_the_rates_and_queueing_formulas(self):
        q = queues.queues(Log.load(Scope(process="Tickets")), target_wait=300)
        [lane] = q["lanes"]
        self.assertEqual((lane["name"], lane["servers"], lane["servers_from"]), ("agents", 2, "group"))
        self.assertIn(lane["measured"], ("claimed", "estimated"))
        self.assertAlmostEqual(lane["arrival_rate"], 6, delta=0.3)
        self.assertAlmostEqual(lane["work"], 900, delta=45)
        self.assertAlmostEqual(lane["utilisation"], 0.75, delta=0.04)
        self.assertAlmostEqual(lane["ca2"], 1, delta=0.1)  # Poisson arrivals, exponential work
        self.assertAlmostEqual(lane["cs2"], 1, delta=0.1)
        lam, s = lane["arrival_rate"] / 3600, lane["work"]
        st = queueing.ggc(lam, s, 2, lane["ca2"], lane["cs2"])  # the prediction is queueing.py's, on these figures
        self.assertAlmostEqual(lane["predicted"]["wait"], st.waiting_time, delta=0.5)
        theory = queueing.mmc(6 / 3600, 900, 2).waiting_time  # 1157 s
        self.assertAlmostEqual(lane["wait"], theory, delta=0.2 * theory)  # what really happened agrees
        self.assertIsNone(lane["what_if"]["minus_one"])  # one person cannot keep up
        self.assertLess(lane["what_if"]["plus_one"], lane["predicted"]["wait"] / 5)
        self.assertEqual(lane["staff"], queueing.staff(lam, s, max_wait=300, ca2=lane["ca2"], cs2=lane["cs2"]))
        self.assertLess(abs(lane["little"]["residual"]), 0.1)
        self.assertLess(abs(q["process"]["little"]["residual"]), 0.1)
        [activity] = q["activities"]
        self.assertEqual((activity["lane"], activity["utilisation"]), ("agents", lane["utilisation"]))
        self.assertEqual(q["bottlenecks"], ["Work Ticket"])

    def test_inactive_people_are_no_servers(self):
        User.objects.create(username="gone", is_active=False).groups.add(Group.objects.get(name="agents"))
        [lane] = queues.queues(Log.load(Scope(process="Tickets")))["lanes"]
        self.assertEqual(lane["servers"], 2)

    def test_work_starts_at_the_claim_else_at_the_persons_previous_completion(self):
        def visit(arrival, end, who, claimed=None):
            return queues.Visit("T", "c", arrival, end, who, "human", claimed=claimed)

        first, second, third, claimed, unknown = (visit(0, 100, "ann"), visit(10, 150, "ann"), visit(200, 260, "ann"),
                                                  visit(0, 50, "ben", claimed=30), visit(0, 40, "api"))
        queues.starts([third, second, first, claimed, unknown])
        self.assertEqual([(v.start, v.source) for v in (first, second, third, claimed)],
                         [(0, "estimated"), (100, "estimated"), (200, "estimated"), (30, "claimed")])
        self.assertIsNone(unknown.start)  # no person named: waiting and work stay together


class UnseparableQueuesTest(TestCase):
    def test_tasks_completed_through_the_api_only_have_a_response_time(self):
        add_events(*((f"u:{i}", "U", [step("Check", i * 10, 300_000, resource="api")]) for i in range(5)))
        Event.objects.update(endpoint="userTask")
        q = queues.queues(Log.load(Scope(process="U")))
        [check] = q["activities"]
        self.assertEqual((check["kind"], check["measured"], check["work_includes_waiting"]), ("human", "response only", True))
        self.assertIsNone(check["predicted"])
        self.assertTrue(any("cannot separate waiting from work" in n for n in q["notes"]))

    def test_system_steps_are_delays(self):
        tiny()
        q = queues.queues(Log.load(Scope(process="P")))
        register = next(a for a in q["activities"] if a["name"] == "Register")
        self.assertEqual((register["kind"], register["utilisation"], register["servers"]), ("automated", None, None))
        approve = next(a for a in q["activities"] if a["name"] == "Approve")  # anna and bob: an imported log's people
        self.assertEqual((approve["kind"], approve["lane"]), ("human", "(no lane)"))


class SeedTest(TestCase):
    def test_writes_a_realistic_log_once(self):
        call_command("seed_events", "--cases", "60", "--seed", "3", stdout=StringIO())
        log = Log.load(Scope(process="Claim Handling"))
        o = stats.overview(log)
        self.assertEqual(o["cases"], 60)
        self.assertGreater(o["errors"], 0)
        self.assertTrue(any(t.open for t in log.traces) or UserTask.objects.waiting().exists())
        self.assertGreater(len(stats.variants(log)["rows"]), 3)
        self.assertIn("Route Claim", {d["gateway"] for d in stats.decisions(log)})
        self.assertEqual(set(Group.objects.values_list("name", flat=True)), {"clerks", "assessors"})
        with self.assertRaisesRegex(CommandError, "replace"):
            call_command("seed_events", "--cases", "60", "--seed", "3")
        events = Event.objects.count()
        call_command("seed_events", "--cases", "60", "--seed", "3", "--replace", stdout=StringIO())
        self.assertEqual(Event.objects.count(), events)  # the same seed writes the same log


class AnalyticsApiTest(TestCase):
    def setUp(self):
        tiny()

    def test_endpoints_answer_with_scope_and_figures(self):
        filters = json.dumps([{"type": "attribute", "key": "resource", "values": ["anna"]}])
        for path, key in (("overview", "case_duration"), ("timeline", "points"), ("activities", "activities"),
                          ("resources", "handoffs"), ("variants", "rows"), ("decisions", "decisions"),
                          ("queues", "lanes"), ("compare", "figures")):
            with self.subTest(path=path):
                r = self.client.get(f"/api/analytics/{path}", {"process": "P", "filters": filters})
                self.assertEqual(r.status_code, 200, r.content)
                body = r.json()
                self.assertIn(key, body)
                self.assertEqual((body["scope"]["cases"], body["scope"]["filters"][0]["kept"]), (2, 2))
        r = self.client.get("/api/analytics/compare", {"process": "P", "a": '{"type":"attribute","key":"resource","values":"anna"}',
                                                         "b": '{"type":"attribute","key":"resource","values":"bob"}'})
        self.assertEqual((r.json()["a"]["cases"], r.json()["b"]["cases"]), (2, 1))
        r = self.client.get("/api/analytics/overview", {"since": (AT + timedelta(minutes=15)).isoformat()})
        self.assertEqual(r.json()["cases"], 2)  # the cases that started since then

    def test_bad_filters_and_windows_are_422(self):
        for params in ({"filters": "nope"}, {"filters": '[{"type": "variants"}]'}, {"a": "[{]"},
                       {"since": "2026-02-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"}):
            with self.subTest(params=params):
                r = self.client.get("/api/analytics/compare", params)
                self.assertEqual(r.status_code, 422)
                self.assertIn("detail", r.json())

    def test_overlay_per_node_of_one_workflow(self):
        r = self.client.get("/api/analytics/overlay", {"workflow_id": "w"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body["nodes"]), {"Register", "Approve", "Pay", "Reject", "Scan"})
        self.assertEqual(body["nodes"]["Register"]["frequency"], 3)
        self.assertEqual(self.client.get("/api/analytics/overlay", {"workflow_id": "other"}).json()["nodes"], {})

    @override_settings(API_TOKEN="secret", N8N_PUBLIC_URL="http://localhost:5678")
    def test_overlay_takes_the_token_or_an_n8n_session_that_may_open_the_workflow(self):
        self.assertEqual(self.client.get("/api/analytics/overlay", {"workflow_id": "w"}).status_code, 401)
        self.assertEqual(self.client.get("/api/analytics/overview").status_code, 401)
        ok = self.client.get("/api/analytics/overlay", {"workflow_id": "w"}, HTTP_AUTHORIZATION="Bearer secret")
        self.assertEqual(ok.status_code, 200)
        self.client.cookies["n8n-auth"] = "session"
        with patch("bpmn8n.api.evaluations.request_json") as n8n:
            r = self.client.get("/api/analytics/overlay", {"workflow_id": "w"}, HTTP_ORIGIN="http://localhost:5678")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/rest/workflows/w", n8n.call_args.args[1])
        self.assertEqual((r["Access-Control-Allow-Origin"], r["Access-Control-Allow-Credentials"]), ("http://localhost:5678", "true"))
        preflight = self.client.options("/api/analytics/overlay", HTTP_ORIGIN="http://localhost:5678")
        self.assertEqual((preflight.status_code, preflight["Access-Control-Allow-Origin"]), (204, "http://localhost:5678"))


class InsightsPageTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("viewer")
        self.client.force_login(self.user)

    def test_needs_a_login_and_says_what_to_do_without_data(self):
        self.client.logout()
        self.assertEqual(self.client.get("/insights/").status_code, 302)
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get("/insights/"), "/workflows/", fetch_redirect_response=False)  # a workflow's tab

    def test_renders_every_section_on_a_seeded_log(self):
        generate("Claim Handling", 80, seed=2)
        r = self.client.get("/insights/", {"process": "Claim Handling"})
        self.assertEqual(r.status_code, 200)
        for text in ("Claim Handling", "Cases in progress", "Closed cases by duration", "Bottlenecks", "assessors",
                     "Where the cycle time goes", "Variants", "Handoffs", "Route Claim", "Compare two groups"):
            self.assertContains(r, text)
        self.assertContains(r, 'class="plot"', count=3)  # two timelines and the histogram

    def test_filters_are_added_by_form_and_removed_by_link(self):
        tiny()
        r = self.client.get("/insights/", {"process": "P", "add": "attribute", "key": "resource", "value": "anna",
                                           "mode": "include", "into": "filters"})
        self.assertEqual(r.status_code, 302)
        page = self.client.get(r.url)
        self.assertContains(page, "With resource anna")
        self.assertContains(page, "2 cases of P kept by 1 filter, of 3")
        remove = page.context["chips"][0]["remove"]
        self.assertNotIn("filters", remove)
        self.assertContains(self.client.get("/insights/" + remove), "3 cases of P")

    def test_a_cohort_is_compared_with_the_filtered_cases(self):
        tiny()
        r = self.client.get("/insights/", {"process": "P", "add": "follower", "from": "Register", "to": "Pay",
                                           "within": "5", "within_unit": "min", "into": "vs"})
        page = self.client.get(r.url)
        self.assertContains(page, "B · Register ⇢ Pay within 5.0 min")
        self.assertEqual(page.context["vs_cases"], 1)
        self.assertContains(page, "case duration mean")

    def test_unreadable_filters_are_reported_not_fatal(self):
        tiny()
        r = self.client.get("/insights/", {"process": "P", "add": "variants", "by": "top", "n": "0"}, follow=True)
        self.assertContains(r, "The filter was not added")
        r = self.client.get("/insights/", {"process": "P", "filters": "[oops"})
        self.assertContains(r, "Filters ignored")
        self.assertContains(r, "3 cases of P")
