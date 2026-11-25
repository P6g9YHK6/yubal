"""Tests for the Apprise error notification handler."""

import logging
import sys
from unittest.mock import MagicMock

import apprise
import pytest
from yubal_api.services.notify import (
    Notifications,
    NotifyHandler,
    setup_notifications,
)


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


class TestDigest:
    def _setup(
        self, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> tuple[MagicMock, logging.Logger, Notifications]:
        sent = MagicMock()
        monkeypatch.setattr(apprise.Apprise, "notify", sent)
        logger = logging.getLogger(name)
        logger.propagate = False
        notifications = setup_notifications(
            "gotify://gotify.example.com/abcdefghijklmno",
            "ERROR",
            [logger],
            digest_hours=24,
        )
        assert notifications is not None
        return sent, logger, notifications

    def test_groups_records_into_one_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent, logger, notifications = self._setup(monkeypatch, "yubal.test.digest")
        try:
            for job in ("aaaa", "bbbb", "cccc", "dddd"):
                logger.error("Job %s failed: %s", job, "cookies expired")
            try:
                raise RuntimeError("bad")
            except RuntimeError:
                logger.exception("Sync failed")
            sent.assert_not_called()
        finally:
            notifications.stop()
            logger.handlers.clear()

        sent.assert_called_once()
        kwargs = sent.call_args.kwargs
        assert kwargs["title"].startswith("yubal: 5 problems since ")
        assert kwargs["notify_type"] == apprise.NotifyType.FAILURE
        body = kwargs["body"]
        assert "4x ERROR in yubal.test.digest" in body
        assert "Job aaaa failed: cookies expired" in body
        assert "Job cccc failed" in body
        assert "Job dddd failed" not in body
        assert "(and 1 more)" in body
        assert "1x ERROR in yubal.test.digest\n  Sync failed" in body
        assert "Traceback" not in body

    def test_nothing_sent_without_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent, logger, notifications = self._setup(monkeypatch, "yubal.test.quiet")
        notifications.stop()
        logger.handlers.clear()
        sent.assert_not_called()

    def test_per_track_errors_not_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent, logger, notifications = self._setup(monkeypatch, "yubal.test.track")
        track_logger = logging.getLogger("yubal.services.download_service")
        track_logger.addHandler(logger.handlers[0])
        try:
            track_logger.error("Track '%s' failed: %s", "Song", "unavailable")
        finally:
            notifications.stop()
            track_logger.removeHandler(logger.handlers[0])
            logger.handlers.clear()
        sent.assert_not_called()
