"""Tests for the Apprise error notification handler."""

import logging
import sys
from unittest.mock import MagicMock

import apprise
import pytest
from yubal_api.services.notify import NotifyHandler, setup_notifications


def _error_record() -> logging.LogRecord:
    try:
        raise ValueError("boom")
    except ValueError:
        return logging.LogRecord(
            "yubal.test",
            logging.ERROR,
            __file__,
            1,
            "sync failed",
            None,
            sys.exc_info(),
        )


class TestNotifyHandler:
    def test_sends_failure_with_traceback(self) -> None:
        notifier = MagicMock()
        handler = NotifyHandler(notifier)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.emit(_error_record())

        kwargs = notifier.notify.call_args.kwargs
        assert kwargs["title"] == "yubal: ERROR in yubal.test"
        assert kwargs["notify_type"] == apprise.NotifyType.FAILURE
        assert "sync failed" in kwargs["body"]
        assert "ValueError: boom" in kwargs["body"]

    def test_send_failure_does_not_raise(self) -> None:
        notifier = MagicMock()
        notifier.notify.side_effect = RuntimeError("down")
        NotifyHandler(notifier).emit(_error_record())

    def test_long_message_truncated(self) -> None:
        notifier = MagicMock()
        record = logging.LogRecord(
            "yubal", logging.ERROR, __file__, 1, "x" * 10000, None, None
        )
        NotifyHandler(notifier).emit(record)
        assert len(notifier.notify.call_args.kwargs["body"]) == 4000


class TestSetupNotifications:
    def test_invalid_urls_disable(self) -> None:
        assert setup_notifications("not a url", "ERROR", []) is None

    def test_only_sends_at_or_above_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = MagicMock()
        monkeypatch.setattr(apprise.Apprise, "notify", sent)
        logger = logging.getLogger("yubal.test.notify")
        logger.propagate = False
        listener = setup_notifications(
            "gotify://gotify.example.com/abcdefghijklmno", "ERROR", [logger]
        )
        assert listener is not None
        try:
            logger.warning("just a warning")
            logging.getLogger("apprise.test").addHandler(logger.handlers[0])
            logging.getLogger("apprise.test").error("apprise own failure")
            try:
                raise RuntimeError("bad")
            except RuntimeError:
                logger.exception("job crashed")
        finally:
            listener.stop()
            logging.getLogger("apprise.test").handlers.clear()
            logger.handlers.clear()

        sent.assert_called_once()
        body = sent.call_args.kwargs["body"]
        assert "job crashed" in body
        assert "RuntimeError: bad" in body
