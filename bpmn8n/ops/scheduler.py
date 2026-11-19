"""The scheduler: which jobs are due, and the loop that runs them. Shared by `python -m bpmn8n schedule` (from the
host, through the web container) and `manage.py run_scheduler` (the `scheduler` compose service). The clock, the
sleep and the runner are arguments: the tests move time by hand."""
import time
from dataclasses import dataclass
from pathlib import Path

DAY = 86400


@dataclass(frozen=True)
class Job:
    name: str  # a management command
    every: int  # seconds
    args: tuple = ()


def jobs(root, prune_days=""):
    """What runs, from the commands this checkout has: the deadlines every minute; the daily digest when the
    people area ships one; a daily prune when PRUNE_DAYS is set."""
    commands = {p.stem for p in (Path(root) / "bpmn8n" / "management" / "commands").glob("*.py")}
    found = [Job("run_deadlines", 60)]
    if "send_digest" in commands:
        found.append(Job("send_digest", DAY))
    if str(prune_days).isdigit() and int(prune_days) > 0:
        found.append(Job("prune", DAY, ("--events-older-than", str(prune_days), "--taskruns-older-than", str(prune_days))))
    return found


def tick(todo, last, now, run):
    """Runs the jobs whose time has come (the first tick runs all of them); one failing never stops the others."""
    for job in todo:
        if job.name not in last or now - last[job.name] >= job.every:
            last[job.name] = now
            try:
                run(job)
            except Exception as e:  # tried again at its next time
                print(f"✗ {job.name}: {type(e).__name__}: {e}", flush=True)


def loop(todo, run, *, clock=time.monotonic, sleep=time.sleep, step=10, ticks=None):
    """Ticks every `step` seconds, for ever (or `ticks` times)."""
    last, n = {}, 0
    while ticks is None or n < ticks:
        tick(todo, last, clock(), run)
        n += 1
        if ticks is None or n < ticks:
            sleep(step)
