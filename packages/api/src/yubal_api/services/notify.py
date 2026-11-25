"""Push error logs to notification services through Apprise."""

import logging
import queue
import sys
from logging.handlers import QueueHandler, QueueListener
from typing import override

import apprise

MAX_MESSAGE_LENGTH = 4000


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
            message = self.format(record)
            if len(message) > MAX_MESSAGE_LENGTH:
                message = message[: MAX_MESSAGE_LENGTH - 3] + "..."
            notify_type = (
                apprise.NotifyType.FAILURE
                if record.levelno >= logging.ERROR
                else apprise.NotifyType.WARNING
            )
            self._notifier.notify(
                title=f"yubal: {record.levelname} in {record.name}",
                body=message,
                notify_type=notify_type,
            )
        except Exception as e:
            # Print instead of logging so a broken service can't loop back here
            print(f"Notification failed: {e}", file=sys.stderr)


def _not_from_apprise(record: logging.LogRecord) -> bool:
    # Apprise logs its own send failures, forwarding those would loop
    return not record.name.startswith("apprise")


def setup_notifications(
    urls: str, level: str, loggers: list[logging.Logger]
) -> QueueListener | None:
    """Attach a queued notification handler to the given loggers.

    Returns the running listener (stop it on shutdown to flush pending
    messages), or None when none of the URLs could be parsed.
    """
    notifier = apprise.Apprise()
    notifier.add(urls)
    if not len(notifier):
        print(f"No valid notification URLs in: {urls!r}", file=sys.stderr)
        return None

    handler = NotifyHandler(notifier)
    handler.setFormatter(logging.Formatter("%(message)s"))

    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(-1)
    queue_handler = QueueHandler(log_queue)
    queue_handler.setLevel(level)
    queue_handler.addFilter(_not_from_apprise)
    for target in loggers:
        target.addHandler(queue_handler)

    listener = QueueListener(log_queue, handler)
    listener.start()
    return listener
