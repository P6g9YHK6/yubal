"""Tests for the scheduler service."""

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from yubal_api.api.exceptions import SubscriptionNotFoundError
from yubal_api.services.scheduler import Scheduler


@pytest.fixture
def scheduler(mock_settings: MagicMock) -> Scheduler:
    """Create scheduler with mocked dependencies."""
    subscription_service = MagicMock()
    job_executor = MagicMock()
    return Scheduler(subscription_service, job_executor, mock_settings)


class TestGetNextRunTime:
    """Tests for _get_next_run_time calculation."""

    def test_calculates_next_run_in_utc(
        self, scheduler: Scheduler, mock_settings: MagicMock
    ) -> None:
        """Should calculate next run time and return in UTC."""
        mock_settings.scheduler_cron = "0 * * * *"  # Every hour
        mock_settings.timezone = ZoneInfo("UTC")

        next_run = scheduler._get_next_run_time()

        assert next_run.tzinfo == UTC
        assert next_run > datetime.now(UTC)
        # Should be at minute 0 (top of hour)
        assert next_run.minute == 0
        assert next_run.second == 0

    def test_respects_configured_timezone(
        self, scheduler: Scheduler, mock_settings: MagicMock
    ) -> None:
        """Should evaluate cron in configured timezone."""
        # Set timezone to UTC+1
        tz = ZoneInfo("Europe/Paris")
        mock_settings.timezone = tz
        mock_settings.scheduler_cron = "0 */6 * * *"  # Every 6 hours

        next_run = scheduler._get_next_run_time()

        # Result should be in UTC
        assert next_run.tzinfo == UTC

        # Convert to local timezone to verify cron was evaluated correctly
        next_run_local = next_run.astimezone(tz)
        # Should be at hour 0, 6, 12, or 18 in local time
        assert next_run_local.hour in (0, 6, 12, 18)
        assert next_run_local.minute == 0

    def test_next_run_is_in_future(
        self, scheduler: Scheduler, mock_settings: MagicMock
    ) -> None:
        """Should always return a future time."""
        mock_settings.scheduler_cron = "* * * * *"  # Every minute
        mock_settings.timezone = ZoneInfo("UTC")

        next_run = scheduler._get_next_run_time()

        assert next_run > datetime.now(UTC)

    def test_different_timezones_produce_different_utc_times(
        self, mock_settings: MagicMock
    ) -> None:
        """Same cron in different timezones should produce different UTC times."""
        subscription_service = MagicMock()
        job_executor = MagicMock()

        # Scheduler with UTC
        mock_settings.timezone = ZoneInfo("UTC")
        mock_settings.scheduler_cron = "0 12 * * *"  # Noon
        scheduler_utc = Scheduler(subscription_service, job_executor, mock_settings)
        next_utc = scheduler_utc._get_next_run_time()

        # Scheduler with Tokyo (UTC+9)
        mock_settings_tokyo = MagicMock()
        mock_settings_tokyo.scheduler_enabled = True
        mock_settings_tokyo.scheduler_cron = "0 12 * * *"  # Noon
        mock_settings_tokyo.timezone = ZoneInfo("Asia/Tokyo")
        scheduler_tokyo = Scheduler(
            subscription_service, job_executor, mock_settings_tokyo
        )
        next_tokyo = scheduler_tokyo._get_next_run_time()

        # Noon in Tokyo is 3am UTC, noon in UTC is 12pm UTC
        # They should differ by ~9 hours (depending on current time)
        diff = abs((next_utc - next_tokyo).total_seconds())
        # Allow for day wraparound - diff should be ~9h or ~15h (24-9)
        assert diff in range(8 * 3600, 10 * 3600) or diff in range(14 * 3600, 16 * 3600)


@pytest.mark.enable_socket
class TestLibraryScan:
    """Tests for the library auto-add scan wired into the sync cycle."""

    def _scheduler_with_library_sync(
        self, mock_settings: MagicMock
    ) -> tuple[Scheduler, MagicMock, MagicMock]:
        subscription_service = MagicMock()
        subscription_service.list.return_value = []
        job_executor = MagicMock()
        library_sync_service = MagicMock()
        scheduler = Scheduler(
            subscription_service,
            job_executor,
            mock_settings,
            library_sync_service=library_sync_service,
        )
        return scheduler, library_sync_service, subscription_service

    @pytest.mark.asyncio
    async def test_sync_all_enabled_runs_library_scan_first(
        self, mock_settings: MagicMock
    ) -> None:
        scheduler, library_sync_service, _ = self._scheduler_with_library_sync(
            mock_settings
        )

        await scheduler._sync_all_enabled()

        library_sync_service.scan_and_add.assert_called_once()

    def test_sync_all_runs_library_scan(self, mock_settings: MagicMock) -> None:
        scheduler, library_sync_service, _ = self._scheduler_with_library_sync(
            mock_settings
        )

        scheduler.sync_all()

        library_sync_service.scan_and_add.assert_called_once()

    def test_sync_subscription_does_not_run_library_scan(
        self, mock_settings: MagicMock
    ) -> None:
        scheduler, library_sync_service, subscription_service = (
            self._scheduler_with_library_sync(mock_settings)
        )
        subscription_service.get.side_effect = SubscriptionNotFoundError(uuid4())

        scheduler.sync_subscription(uuid4())

        library_sync_service.scan_and_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_all_enabled_survives_scan_failure(
        self, mock_settings: MagicMock
    ) -> None:
        """A scan failure must not block the regular sync sweep."""
        scheduler, library_sync_service, subscription_service = (
            self._scheduler_with_library_sync(mock_settings)
        )
        library_sync_service.scan_and_add.side_effect = RuntimeError("boom")

        job_ids = await scheduler._sync_all_enabled()

        assert job_ids == []
        subscription_service.list.assert_called_once_with(enabled=True)

    def test_no_library_sync_service_is_a_no_op(self, mock_settings: MagicMock) -> None:
        """Scheduler works fine with no library_sync_service configured."""
        subscription_service = MagicMock()
        subscription_service.list.return_value = []
        job_executor = MagicMock()
        scheduler = Scheduler(subscription_service, job_executor, mock_settings)

        job_ids = scheduler.sync_all()

        assert job_ids == []
