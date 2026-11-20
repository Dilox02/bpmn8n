"""B9 backup/restore/schedule/version, the scheduler, the status dots, G10's `prune` and G11's one version."""
import gzip
import json
import re
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from .. import __version__, cli
from ..management.commands import run_scheduler
from ..models import Event, TaskRun, UserTask
from ..ops import health, scheduler
from .test_cli import Runner, stack

ROOT = Path(__file__).resolve().parents[2]


class CliTest(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_pull_runs_workflows_pull_in_the_web_container(self):
        runner = Runner()
        self.assertEqual(cli.main(["pull", "projects/acme"], stack(self.tmp, runner)), 0)
        self.assertEqual(runner.calls, ["docker compose exec -T web python manage.py workflows pull projects/acme"])

    def test_backup_dumps_both_databases_and_copies_the_projects(self):
        runner, lines = Runner(), []
        s = stack(self.tmp, runner, lines=lines)
        s.now = lambda: 1_800_000_000
        self.assertEqual(cli.main(["backup"], s), 0)
        folder = next((Path(self.tmp) / "backups").iterdir())
        self.assertRegex(folder.name, r"^\d{8}-\d{6}$")
        self.assertEqual(len(runner.ran("pg_dump -U bpmn8n --clean --if-exists bpmn8n >")), 1)
        self.assertEqual(len(runner.ran("pg_dump -U bpmn8n --clean --if-exists n8n >")), 1)
        self.assertEqual(len(runner.ran(f"cp postgres:/tmp/n8n.sql {folder}/n8n.sql")), 1)
        self.assertEqual(len(runner.ran(f"cp web:/app/projects/. {folder}/projects/")), 1)
        self.assertIn(f"backups/{folder.name}/", lines[-1])

    def make_backup(self):
        folder = Path(self.tmp) / "backups" / "x"
        (folder / "projects").mkdir(parents=True)
        for name in cli.DATABASES:
            (folder / f"{name}.sql").write_text("-- dump")
        return "backups/x"

    def test_restore_asks_first_and_changes_nothing_on_no(self):
        runner = Runner()
        with mock.patch("builtins.input", return_value="no") as ask:
            with mock.patch("sys.stderr", StringIO()):
                self.assertEqual(cli.main(["restore", self.make_backup()], stack(self.tmp, runner)), 1)
        self.assertIn("replaces both databases", ask.call_args.args[0])
        self.assertEqual(runner.calls, [])

    def test_restore_loads_the_dumps_and_the_projects_back(self):
        runner = Runner()
        with mock.patch("builtins.input", return_value="yes"):
            self.assertEqual(cli.main(["restore", self.make_backup()], stack(self.tmp, runner)), 0)
        self.assertEqual(runner.calls[0], "docker compose stop web n8n n8n-worker scheduler")
        self.assertEqual(len(runner.ran("psql -U bpmn8n -d bpmn8n")), 1)
        self.assertEqual(len(runner.ran("psql -U bpmn8n -d n8n")), 1)
        self.assertEqual(len(runner.ran("web:/app/projects/")), 1)
        self.assertTrue(runner.calls[-1].startswith("docker compose up -d web n8n"))

    def test_restore_needs_a_backup_folder(self):
        with mock.patch("sys.stderr", StringIO()):
            self.assertEqual(cli.main(["restore", "--yes", "backups/none"], stack(self.tmp)), 1)

    def test_schedule_runs_the_deadlines_every_minute_on_the_stack_clock(self):
        runner, clock = Runner(), iter([0, 10, 60, 61])
        s = stack(self.tmp, runner)
        s.now = lambda: next(clock)
        cli.main(["schedule", "--ticks", "4"], s)
        self.assertEqual(runner.ran("manage.py run_deadlines"), ["docker compose exec -T web python manage.py run_deadlines"] * 2)  # at 0 and 60

    def test_version(self):
        lines = []
        cli.main(["version"], stack(self.tmp, lines=lines))
        self.assertEqual(lines, [__version__])


class SchedulerTest(SimpleTestCase):
    def test_jobs_run_at_their_times_and_a_failure_does_not_stop_the_others(self):
        todo, ran, last = [scheduler.Job("a", 60), scheduler.Job("b", 100)], [], {}

        def run(job):
            ran.append(job.name)
            if job.name == "a":
                raise RuntimeError("boom")
        for now in (0, 30, 60, 100):
            scheduler.tick(todo, last, now, run)
        self.assertEqual(ran, ["a", "b", "a", "b"])

    def test_loop_sleeps_between_ticks(self):
        slept, ran = [], []
        scheduler.loop([scheduler.Job("a", 60)], ran.append, clock=lambda: len(slept) * 10, sleep=slept.append, ticks=7)
        self.assertEqual((len(ran), slept), (2, [10] * 6))  # at 0 and 60

    def test_jobs_follow_the_checkout_and_prune_days(self):
        root = Path(tempfile.mkdtemp())
        self.assertEqual([j.name for j in scheduler.jobs(root)], ["run_deadlines"])
        commands = root / "bpmn8n" / "management" / "commands"
        commands.mkdir(parents=True)
        (commands / "send_digests.py").write_text("")
        found = scheduler.jobs(root, "90")
        self.assertEqual([j.name for j in found], ["run_deadlines", "send_digests", "prune"])
        self.assertEqual(found[2].args, ("--events-older-than", "90", "--taskruns-older-than", "90"))
        self.assertEqual([j.name for j in scheduler.jobs(root, "0")], ["run_deadlines", "send_digests"])


class SchedulerCommandTest(TestCase):
    def test_the_command_runs_the_deadlines_through_call_command(self):
        with mock.patch.object(run_scheduler, "call_command") as call:
            call_command("run_scheduler", ticks=1, stdout=StringIO())
        # the first tick runs every job that is due: the deadlines, and the daily digest (people's notifications)
        self.assertEqual([c.args[0] for c in call.call_args_list], ["run_deadlines", "reconcile", "send_digests"])

    def test_a_second_scheduler_waits_on_the_lock(self):
        cursor = mock.MagicMock()
        cursor.__enter__.return_value.fetchone.return_value = (False,)
        with mock.patch.object(run_scheduler, "connection", vendor="postgresql", cursor=lambda: cursor):
            self.assertFalse(run_scheduler.locked())
            cursor.__enter__.return_value.fetchone.return_value = (True,)
            self.assertTrue(run_scheduler.locked())
        self.assertIn("pg_try_advisory_lock", cursor.__enter__.return_value.execute.call_args.args[0])


class StatusDotsTest(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.checks = mock.patch.object(health, "checks", return_value={
            "redis": None, "n8n": lambda: None, "laya": lambda: None, "extractor": mock.Mock(side_effect=OSError("x")),
            "search": None})
        self.checks.start()
        self.addCleanup(self.checks.stop)

    def test_dots_summarise_the_services_and_are_cached(self):
        self.assertEqual(health.dots(), [{"name": "n8n", "state": "up"}, {"name": "models", "state": "down"},
                                         {"name": "redis", "state": "off"}, {"name": "search", "state": "off"}])
        health.checks.reset_mock()
        health.dots()
        health.checks.assert_not_called()  # the second page render reads the cache

    def test_the_top_bar_has_a_labelled_dot_per_service_and_links_staff_to_the_status_page(self):
        User = get_user_model()
        self.client.force_login(User.objects.create_user("ann"))
        html = self.client.get("/inbox/").content.decode()
        self.assertEqual(re.findall(r'<span class="sr-only">(\w+: [\w ]+)</span>', html),
                         ["n8n: up", "models: down", "redis: not configured", "search: not configured"])
        self.assertNotIn('href="/status/"', html)
        self.client.force_login(User.objects.create_user("boss", is_staff=True))
        self.assertIn('href="/status/"', self.client.get("/inbox/").content.decode())

    def test_the_status_page_is_for_staff_and_lists_checks_and_hints(self):
        User = get_user_model()
        self.client.force_login(User.objects.create_user("ann"))
        self.assertEqual(self.client.get("/status/").status_code, 302)
        self.client.force_login(User.objects.create_user("boss", is_staff=True))
        html = self.client.get("/status/").content.decode()
        self.assertIn("database", html)
        self.assertIn("Hints", html)


class WaitingCountTest(TestCase):
    """What the strip in n8n's editor reads beside Inbox."""

    @override_settings(API_TOKEN="secret", N8N_PUBLIC_URL="http://localhost:5678")
    def test_the_count_takes_the_token_or_an_n8n_session_and_counts_workable_tasks(self):
        UserTask.objects.create(activity="A", title="a", resume_url="x", due=timezone.now() - timedelta(days=1))
        UserTask.objects.create(activity="B", title="b", resume_url="x")
        UserTask.objects.create(activity="C", title="c", resume_url="x", status=UserTask.SUSPENDED)
        UserTask.objects.create(activity="D", title="d", resume_url="x", status=UserTask.COMPLETED)
        self.assertEqual(self.client.get("/api/executions/w/waiting").status_code, 401)
        self.client.cookies["n8n-auth"] = "session"
        with mock.patch("bpmn8n.api.evaluations.request_json"):
            r = self.client.get("/api/executions/w/waiting", HTTP_ORIGIN="http://localhost:5678")
        self.assertEqual(r.json(), {"waiting": 2, "overdue": 1})
        self.assertEqual(r["Access-Control-Allow-Origin"], "http://localhost:5678")


class PruneTest(TestCase):
    def setUp(self):
        self.now = timezone.now()
        archive = Path(tempfile.mkdtemp())
        patch = mock.patch("bpmn8n.management.commands.prune.settings", BASE_DIR=archive)
        patch.start()
        self.addCleanup(patch.stop)
        self.archive = archive / "archive"

    def event(self, case, days):
        at = self.now - timedelta(days=days)
        return Event.objects.create(case_id=case, activity="Pay", process="P", endpoint="pay", started=at, ended=at,
                                    duration_ms=1, status="complete", resource="system")

    def run_prune(self, *args):
        out = StringIO()
        call_command("prune", *args, stdout=out)
        return out.getvalue()

    def test_old_events_are_archived_then_deleted_and_the_rest_stays(self):
        self.event("w:1", 400)
        self.event("w:2", 10)
        self.assertIn("1 events archived", self.run_prune("--events-older-than", "365"))
        self.assertEqual(list(Event.objects.values_list("case_id", flat=True)), ["w:2"])
        (path,) = self.archive.glob("events-*.csv.gz")
        rows = gzip.open(path, "rt").read().splitlines()
        self.assertEqual(rows[0], "case_id,activity,endpoint,start_timestamp,end_timestamp,status,resource")
        self.assertTrue(rows[1].startswith("w:1,Pay,pay,"))
        self.assertEqual(len(rows), 2)

    def test_the_archive_reads_back_with_import_eventlog(self):
        self.event("w:1", 400)
        self.run_prune("--events-older-than", "365")
        (path,) = self.archive.glob("events-*.csv.gz")
        plain = path.with_suffix("")
        plain.write_bytes(gzip.open(path).read())
        call_command("import_eventlog", str(plain), stdout=StringIO())
        self.assertEqual(Event.objects.filter(case_id="w:1").count(), 1)

    def test_a_case_that_still_waits_keeps_its_rows(self):
        self.event("w:wait", 500)
        self.event("w:done", 500)
        UserTask.objects.create(activity="Approve", title="t", case_id="w:wait", resume_url="x")
        TaskRun.objects.create(task="t", case_id="w:wait", node_id="n", run_index=0, item_index=0, started=self.now)
        TaskRun.objects.filter(case_id="w:wait").update(created=self.now - timedelta(days=500))
        self.run_prune("--events-older-than", "30", "--taskruns-older-than", "30")
        self.assertEqual(list(Event.objects.values_list("case_id", flat=True)), ["w:wait"])
        self.assertEqual(TaskRun.objects.count(), 1)

    def test_old_task_runs_are_deleted_and_dry_run_changes_nothing(self):
        for i, days in enumerate((100, 1)):
            TaskRun.objects.create(task="t", case_id=f"w:{i}", node_id="n", run_index=0, item_index=0, started=self.now)
            TaskRun.objects.filter(case_id=f"w:{i}").update(created=self.now - timedelta(days=days))
        self.event("w:0", 100)
        said = self.run_prune("--events-older-than", "30", "--taskruns-older-than", "30", "--dry-run")
        self.assertIn("1 task runs would be deleted", said)
        self.assertIn("1 events would be archived", said)
        self.assertEqual((TaskRun.objects.count(), Event.objects.count(), self.archive.exists()), (2, 1, False))
        self.run_prune("--taskruns-older-than", "30")
        self.assertEqual(list(TaskRun.objects.values_list("case_id", flat=True)), ["w:1"])
        self.assertEqual(Event.objects.count(), 1)  # only what was asked


class ReleaseTest(SimpleTestCase):
    def test_one_version_in_three_places(self):
        pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
        nodes = json.loads((ROOT / "n8n-nodes-bpmn8n" / "package.json").read_text())["version"]
        self.assertEqual((pyproject, nodes), (__version__, __version__))
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_the_changelog_has_this_version(self):
        self.assertIn(f"## {__version__}", (ROOT / "CHANGELOG.md").read_text())
