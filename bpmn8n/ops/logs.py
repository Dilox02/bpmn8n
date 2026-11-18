"""JSON logs (LOG_FORMAT=json): one object per line, which log collectors (Loki, Elastic, CloudWatch) read
without a parsing rule. Standard library only."""
import json
import logging
from datetime import UTC, datetime

STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """time, level, logger, message, the exception if any, and the fields a call passed as `extra`
    (log.info("claimed", extra={"case_id": ...}))."""

    def format(self, record):
        entry = {"time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
                 "level": record.levelname, "logger": record.name, "message": record.getMessage()}
        entry.update((k, v) for k, v in vars(record).items() if k not in STANDARD)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)
