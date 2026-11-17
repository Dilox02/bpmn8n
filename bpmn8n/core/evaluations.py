"""Local evaluation samples, isolated by project, workflow and n8n execution."""
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=120)
    value: float = Field(strict=True)
    unit: str = Field("", max_length=40)
    direction: Literal["higher", "lower"] = "higher"
    threshold: float | None = None

    @property
    def passed(self):
        if self.threshold is None:
            return None
        return self.value >= self.threshold if self.direction == "higher" else self.value <= self.threshold


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str
    workflow_id: str
    execution_id: str
    node_id: str = Field(min_length=1, max_length=120)
    node_name: str = Field(min_length=1, max_length=200)
    sample_id: str = Field(min_length=1, max_length=120)
    label: str = Field("", max_length=200)
    metrics: list[Metric] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_names(self):
        if len({m.name for m in self.metrics}) != len(self.metrics):
            raise ValueError("Metric names must be unique within a sample")
        return self


class EvaluationStore:
    """One atomic file per node invocation/item: parallel writers never share a file.

    Retrying the same invocation replaces its sample; loop iterations get separate sample IDs.
    Raw workflow items and credentials are deliberately never persisted here.
    """

    def __init__(self, root=None):
        self.root = Path(root or settings.BASE_DIR / "projects").resolve()

    @staticmethod
    def identifier(value):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", value):
            raise ValueError("Invalid project, workflow or execution identifier")
        return value

    def folder(self, project):
        path = self.root / self.identifier(project)
        if path.is_symlink() or not (path / "workflow.json").is_file():
            raise ValueError("Project must contain workflow.json")
        return path

    def run_folder(self, project, workflow_id, execution_id=None):
        folder = self.folder(project) / "run" / self.identifier(workflow_id)
        if execution_id is not None:
            folder /= self.identifier(execution_id)
        if not folder.resolve().is_relative_to(self.folder(project).resolve()):
            raise ValueError("Run directory escapes project folder")
        return folder

    def write(self, sample: Sample):
        folder = self.run_folder(sample.project, sample.workflow_id, sample.execution_id) / "metrics"
        if not folder.resolve().is_relative_to(self.folder(sample.project).resolve()):
            raise ValueError("Metrics directory escapes project folder")
        folder.mkdir(parents=True, exist_ok=True)
        identity = json.dumps([sample.node_id, sample.sample_id])
        target = folder / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
        if target.is_symlink():
            raise ValueError("Metrics file cannot be a symlink")
        record = dict(schema_version=1, **sample.model_dump(), recorded_at=datetime.now(timezone.utc).isoformat())
        record["metrics"] = [dict(**m.model_dump(), passed=m.passed) for m in sample.metrics]
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as stream:
                temporary = stream.name
                json.dump(record, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            self.index(db, record)
        return {"path": str(target.relative_to(self.root.parent)), "sample_id": sample.sample_id}

    @contextmanager
    def database(self):
        """Rebuildable SQLite index; normal reads never reopen the exported sample files."""
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.root / ".evaluations.sqlite3", timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, project TEXT, workflow TEXT, execution TEXT,
                    node TEXT, stamp TEXT, label TEXT, payload TEXT);
                CREATE INDEX IF NOT EXISTS records_run ON records(project,workflow,execution,stamp,id);
                CREATE TABLE IF NOT EXISTS measurements (
                    record TEXT REFERENCES records(id) ON DELETE CASCADE, name TEXT, value REAL,
                    unit TEXT, direction TEXT, threshold REAL, passed INTEGER);
                CREATE INDEX IF NOT EXISTS measurements_record ON measurements(record);
                CREATE TABLE IF NOT EXISTS runs (
                    project TEXT, workflow TEXT, execution TEXT, updated_at TEXT, label TEXT,
                    revision INTEGER, sample_count INTEGER, failed_checks INTEGER, checked_count INTEGER,
                    PRIMARY KEY(project,workflow,execution));
                CREATE INDEX IF NOT EXISTS runs_workflow ON runs(workflow,updated_at DESC);
                CREATE TABLE IF NOT EXISTS catalog (imported INTEGER);
            """)
            if not db.execute("SELECT 1 FROM catalog").fetchone():
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("SELECT 1 FROM catalog").fetchone():
                    for path in self.root.glob("*/run/*/*/metrics/*.json"):
                        project, _, workflow, execution, _, _ = path.relative_to(self.root).parts
                        try:
                            self.run_folder(project, workflow, execution)
                            if path.is_symlink() or not path.resolve().is_relative_to(self.folder(project).resolve()):
                                continue
                            record = json.loads(path.read_text(encoding="utf-8"))
                            sample = Sample.model_validate({k: v for k, v in record.items()
                                if k not in ("schema_version", "recorded_at", "metrics")} | {
                                "metrics": [{k: v for k, v in m.items() if k != "passed"} for m in record["metrics"]]})
                            if (sample.project, sample.workflow_id, sample.execution_id) != (project, workflow, execution):
                                continue
                            datetime.fromisoformat(record["recorded_at"])
                            record["metrics"] = [dict(**m.model_dump(), passed=m.passed) for m in sample.metrics]
                            self.index(db, record)
                        except (ValueError, KeyError, TypeError, OSError):
                            continue
                    db.execute("INSERT INTO catalog VALUES (1)")
                db.commit()
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def index(db, record):
        identity = json.dumps([record[k] for k in ("project", "workflow_id", "execution_id", "node_id", "sample_id")])
        key = hashlib.sha256(identity.encode()).hexdigest()
        previous = db.execute("SELECT count(*),coalesce(sum(passed=0),0),count(passed) FROM measurements WHERE record=?", (key,)).fetchone()
        exists = db.execute("SELECT 1 FROM records WHERE id=?", (key,)).fetchone()
        db.execute("DELETE FROM measurements WHERE record=?", (key,))
        db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?,?,?)", (
            key, record["project"], record["workflow_id"], record["execution_id"], record["node_name"],
            record["recorded_at"], record.get("label", ""), json.dumps(record, ensure_ascii=False, allow_nan=False)))
        db.executemany("INSERT INTO measurements VALUES (?,?,?,?,?,?,?)", [
            (key, m["name"], m["value"], m["unit"], m["direction"], m["threshold"], m["passed"]) for m in record["metrics"]])
        failed = sum(m["passed"] is False for m in record["metrics"]) - previous[1]
        checked = sum(m["passed"] is not None for m in record["metrics"]) - previous[2]
        db.execute("""INSERT INTO runs VALUES (?,?,?,?,?,1,?,?,?)
            ON CONFLICT(project,workflow,execution) DO UPDATE SET
            updated_at=max(runs.updated_at,excluded.updated_at),
            label=CASE WHEN excluded.updated_at>=runs.updated_at AND excluded.label!='' THEN excluded.label ELSE runs.label END,
            revision=runs.revision+1, sample_count=runs.sample_count+excluded.sample_count,
            failed_checks=runs.failed_checks+excluded.failed_checks, checked_count=runs.checked_count+excluded.checked_count""",
            (record["project"], record["workflow_id"], record["execution_id"], record["recorded_at"], record.get("label", ""),
             int(not exists), failed, checked))

    def summary(self, row):
        data = dict(row)
        data["workflow_id"] = data.pop("workflow")
        data["execution_id"] = data.pop("execution")
        data["status"] = "failed" if data.pop("failed_checks") else "passed" if data.pop("checked_count") else "unscored"
        data.pop("checked_count", None)
        data["path"] = str(self.run_folder(data["project"], data["workflow_id"], data["execution_id"]).relative_to(self.root.parent))
        return data

    def runs(self, workflow_id, limit=50):
        self.identifier(workflow_id)
        with self.database() as db:
            return [self.summary(r) for r in db.execute("SELECT * FROM runs WHERE workflow=? ORDER BY updated_at DESC,execution DESC LIMIT ?", (workflow_id, limit))]

    def version(self, workflow_id, execution_id=None):
        with self.database() as db:
            if execution_id is None:
                return [tuple(r) for r in db.execute("SELECT project,execution,revision FROM runs WHERE workflow=? ORDER BY project,execution", (workflow_id,))]
            return [tuple(r) for r in db.execute("SELECT project,execution,revision FROM runs WHERE workflow=? AND execution=?", (workflow_id, execution_id))]

    def metadata(self, project, workflow_id, execution_id):
        self.run_folder(project, workflow_id, execution_id)
        with self.database() as db:
            row = db.execute("SELECT * FROM runs WHERE project=? AND workflow=? AND execution=?", (project, workflow_id, execution_id)).fetchone()
            return self.summary(row) if row else None

    def read(self, project, workflow_id, execution_id, limit=100, offset=0, points=150):
        metadata = self.metadata(project, workflow_id, execution_id)
        if not metadata:
            return dict(project=project, workflow_id=workflow_id, execution_id=execution_id,
                        samples=[], sample_count=0, metrics=[], warnings=[], updated_at="", status="unscored")
        args = (project, workflow_id, execution_id)
        where = "r.project=? AND r.workflow=? AND r.execution=?"
        with self.database() as db:
            metrics = []
            grouped = db.execute(f"""SELECT r.node,m.name,m.unit,m.direction,m.threshold,
                count(*) AS count,avg(m.value) AS mean,min(m.value) AS minimum,max(m.value) AS maximum,
                coalesce(sum(m.passed),0) AS passed_count,count(m.passed) AS checked_count
                FROM records r JOIN measurements m ON m.record=r.id WHERE {where}
                GROUP BY r.node,m.name,m.unit,m.direction,m.threshold""", args).fetchall()
            for row in grouped:
                metric = dict(row)
                filters = "r.node=? AND m.name=? AND m.unit=? AND m.direction=? AND m.threshold IS ?"
                selected = (*args, *(metric[k] for k in ("node", "name", "unit", "direction", "threshold")))
                last = db.execute(f"SELECT m.value FROM records r JOIN measurements m ON m.record=r.id WHERE {where} AND {filters} ORDER BY r.stamp DESC,r.id DESC LIMIT 1", selected).fetchone()[0]
                series = db.execute(f"""WITH ordered AS (
                    SELECT r.stamp,m.value,row_number() OVER(ORDER BY r.stamp,r.id)-1 AS n
                    FROM records r JOIN measurements m ON m.record=r.id WHERE {where} AND {filters})
                    SELECT max(stamp) AS at,avg(value) AS value,min(value) AS minimum,max(value) AS maximum,count(*) AS count
                    FROM ordered GROUP BY CAST(n*?/? AS INTEGER) ORDER BY min(n)""", (*selected, points, metric["count"])).fetchall()
                metric["node_name"] = metric.pop("node")
                metric.update(last=last, series=[dict(p) for p in series])
                metrics.append(metric)
            records = db.execute("SELECT payload FROM records WHERE project=? AND workflow=? AND execution=? ORDER BY stamp DESC,id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return dict(**metadata, metrics=metrics, samples=[json.loads(r[0]) for r in reversed(records)],
                    offset=offset, limit=limit, has_more=offset+len(records)<metadata["sample_count"], warnings=[])

    def export(self, project, workflow_id, execution_id):
        self.run_folder(project, workflow_id, execution_id)
        with self.database() as db:
            yield '{"samples":['
            separator = ""
            for row in db.execute("SELECT payload FROM records WHERE project=? AND workflow=? AND execution=? ORDER BY stamp,id", (project, workflow_id, execution_id)):
                yield separator + row[0]
                separator = ","
            yield "]}"
