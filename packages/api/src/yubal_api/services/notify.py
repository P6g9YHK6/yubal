"""Push error logs to notification services through Apprise."""

import logging
import queue
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener
from typing import override

import apprise

MAX_MESSAGE_LENGTH = 4000

# Loggers that report single tracks. The job executor sends one summary per
# job instead, so a bad album doesn't turn into dozens of notifications.
PER_TRACK_LOGGERS = ("yubal.services.download_service",)

# Examples kept per kind of error in a digest, the rest are only counted
DIGEST_EXAMPLES = 3
EXAMPLE_LENGTH = 600
# Distinct kinds of error tracked per digest, so the buffer stays bounded
MAX_DIGEST_GROUPS = 100

# Record attribute holding the unformatted message, see _TemplateQueueHandler
TEMPLATE_ATTR = "yubal_msg_template"
_TRACEBACK_START = "\nTraceback (most recent call last):"


def _notify_type(levelno: int) -> apprise.NotifyType:
    if levelno >= logging.ERROR:
        return apprise.NotifyType.FAILURE
    return apprise.NotifyType.WARNING


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


class NotifyHandler(logging.Handler):
    """Sends each log record to every configured Apprise URL.

    Does blocking network calls, so it should run behind a QueueListener
    (see setup_notifications) rather than be attached to a logger directly.
    """

    def __init__(self, notifier: apprise.Apprise) -> None:
        super().__init__()
        self._notifier = notifier

    @override
    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._notifier.notify(
                title=f"yubal: {record.levelname} in {record.name}",
                body=_truncate(self.format(record), MAX_MESSAGE_LENGTH),
                notify_type=_notify_type(record.levelno),
            )
        except Exception as e:
            # Print instead of logging so a broken service can't loop back here
            print(f"Notification failed: {e}", file=sys.stderr)


@dataclass
class _Group:
    levelno: int
    levelname: str
    logger: str
    count: int = 0
    examples: list[str] = field(default_factory=list)


class DigestHandler(logging.Handler):
    """Collects log records and sends them as one message per interval.

    Records from the same log call are grouped, so a hundred failed jobs
    read as one line with a count and a few examples. Nothing is sent for an
    interval without records. close() sends whatever is still pending.
    """

    def __init__(self, notifier: apprise.Apprise, interval: float) -> None:
        super().__init__()
        self._notifier = notifier
        self._interval = interval
        self._groups: dict[tuple[str, str], _Group] = {}
        self._dropped = 0
        self._since = datetime.now()
        self._buffer_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="notify-digest", daemon=True
        )
        self._thread.start()

    @override
    def emit(self, record: logging.LogRecord) -> None:
        # Group by the call site's format string, not the rendered message
        key = (record.name, getattr(record, TEMPLATE_ATTR, str(record.msg)))
        with self._buffer_lock:
            group = self._groups.get(key)
            if group is None:
                if len(self._groups) >= MAX_DIGEST_GROUPS:
                    self._dropped += 1
                    return
                group = self._groups[key] = _Group(
                    record.levelno, record.levelname, record.name
                )
            group.count += 1
            if len(group.examples) < DIGEST_EXAMPLES:
                message = record.getMessage().split(_TRACEBACK_START)[0]
                group.examples.append(_truncate(message, EXAMPLE_LENGTH))

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.flush()

    @override
    def flush(self) -> None:
        with self._buffer_lock:
            groups, self._groups = self._groups, {}
            dropped, self._dropped = self._dropped, 0
            since, self._since = self._since, datetime.now()
        if not groups and not dropped:
            return

        total = sum(g.count for g in groups.values()) + dropped
        ordered = sorted(groups.values(), key=lambda g: (-g.levelno, -g.count))
        sections = []
        for group in ordered:
            lines = [f"{group.count}x {group.levelname} in {group.logger}"]
            lines.extend(
                "  " + example.replace("\n", "\n  ") for example in group.examples
            )
            if group.count > len(group.examples):
                lines.append(f"  (and {group.count - len(group.examples)} more)")
            sections.append("\n".join(lines))
        if dropped:
            sections.append(f"{dropped} more of other kinds, see the logs")

        try:
            self._notifier.notify(
                title=f"yubal: {total} problems since {since:%b %d %H:%M}",
                body=_truncate("\n\n".join(sections), MAX_MESSAGE_LENGTH),
                notify_type=_notify_type(max(g.levelno for g in ordered)),
            )
        except Exception as e:
            print(f"Notification failed: {e}", file=sys.stderr)

    @override
    def close(self) -> None:
        self._stop.set()
        self._thread.join()
        self.flush()
        super().close()


def _should_notify(record: logging.LogRecord) -> bool:
    # Apprise logs its own send failures, forwarding those would loop
    if record.name.startswith("apprise"):
        return False
    return not record.name.startswith(PER_TRACK_LOGGERS)


class _TemplateQueueHandler(QueueHandler):
    """QueueHandler that keeps the format string the digest groups by.

    prepare() swaps record.msg for the rendered text before queueing it.
    """

    @override
    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        template = str(record.msg)
        prepared = super().prepare(record)
        setattr(prepared, TEMPLATE_ATTR, template)
        return prepared


class Notifications:
    """Running notification pipeline, stop() it on shutdown."""

    def __init__(self, listener: QueueListener, handler: logging.Handler) -> None:
        self._listener = listener
        self._handler = handler

    def stop(self) -> None:
        """Send what is still queued or waiting for the digest."""
        self._listener.stop()
        self._handler.close()


def setup_notifications(
    urls: str,
    level: str,
    loggers: list[logging.Logger],
    digest_hours: float = 0,
) -> Notifications | None:
    """Attach a queued notification handler to the given loggers.

    With digest_hours above 0, records are collected and sent as one message
    per period instead of one message each. Returns None when none of the
    URLs could be parsed.
    """
    notifier = apprise.Apprise()
    notifier.add(urls)
    if not len(notifier):
        print(f"No valid notification URLs in: {urls!r}", file=sys.stderr)
        return None

    handler: logging.Handler
    if digest_hours > 0:
        handler = DigestHandler(notifier, digest_hours * 3600)
    else:
        handler = NotifyHandler(notifier)
        handler.setFormatter(logging.Formatter("%(message)s"))

    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(-1)
    queue_handler = _TemplateQueueHandler(log_queue)
    queue_handler.setLevel(level)
    queue_handler.addFilter(_should_notify)
    for target in loggers:
        target.addHandler(queue_handler)

    listener = QueueListener(log_queue, handler)
    listener.start()
    return Notifications(listener, handler)
