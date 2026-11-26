"""Background scheduler for periodic subscription syncing."""

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from croniter import croniter
from yubal import remove_unreferenced_tracks

from yubal_api.api.exceptions import SubscriptionNotFoundError
from yubal_api.db.subscription import Subscription
from yubal_api.domain.enums import JobSource
from yubal_api.services.job_executor import JobExecutor
from yubal_api.services.job_priority import subscription_priority
from yubal_api.services.library_sync_service import LibrarySyncService
from yubal_api.services.subscription_service import SubscriptionService
from yubal_api.settings import Settings

logger = logging.getLogger(__name__)


class Scheduler:
    """Background scheduler that syncs enabled subscriptions periodically."""

    def __init__(
        self,
        subscription_service: SubscriptionService,
        job_executor: JobExecutor,
        settings: Settings,
        library_sync_service: LibrarySyncService | None = None,
    ) -> None:
        """Initialize scheduler.

        Args:
            subscription_service: Service for subscription CRUD.
            job_executor: Service that creates and runs sync jobs.
            settings: Application settings (cron schedule, auto-add flags).
            library_sync_service: Optional service that scans the user's YT
                Music library and auto-adds new subscriptions before each
                full sync sweep. None disables the library scan step
                entirely (e.g. in tests that don't need it).
        """
        self._subscription_service = subscription_service
        self._job_executor = job_executor
        self._settings = settings
        self._library_sync_service = library_sync_service
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._next_run_at: datetime | None = None

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._task is not None and not self._task.done()

    @property
    def enabled(self) -> bool:
        """Check if scheduler is enabled (from settings)."""
        return self._settings.scheduler_enabled

    @property
    def cron_expression(self) -> str:
        """Get cron expression (from settings)."""
        return self._settings.scheduler_cron

    @property
    def next_run_at(self) -> datetime | None:
        """Get next scheduled run time."""
        return self._next_run_at

    def _get_next_run_time(self) -> datetime:
        """Calculate next run time using croniter in configured timezone."""
        tz = self._settings.timezone
        cron = croniter(self._settings.scheduler_cron, datetime.now(tz))
        next_time = cron.get_next(datetime)
        # Convert to UTC for storage/comparison
        if next_time.tzinfo is None:
            next_time = next_time.replace(tzinfo=tz)
        return next_time.astimezone(UTC)

    def start(self) -> None:
        """Start the scheduler background task."""
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler background task."""
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._next_run_at = None
        logger.info("Scheduler stopped")

    async def _run_loop(self) -> None:
        """Main scheduler loop."""
        while not self._stop_event.is_set():
            next_run = self._get_next_run_time()
            self._next_run_at = next_run if self._settings.scheduler_enabled else None
            wait_seconds = (next_run - datetime.now(UTC)).total_seconds()

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=max(0, wait_seconds),
                )
                break  # Stop event was set
            except TimeoutError:
                pass  # Timeout expired, time to sync

            if self._settings.scheduler_enabled:
                await self._sync_all_enabled()

    def _create_jobs_for_subscriptions(
        self,
        subscriptions: list[Subscription],
        source: JobSource = JobSource.SCHEDULER,
    ) -> list[str]:
        """Create sync jobs for given subscriptions, in the order given.

        Stops at the first one the queue has no room for: the rest keep their
        last sync time, so they rank higher on the next sweep.
        """
        job_ids: list[str] = []
        for index, subscription in enumerate(subscriptions):
            try:
                job = self._job_executor.create_and_start_job(
                    subscription.url,
                    subscription.max_items,
                    source,
                    subscription.id,
                    first_sync=subscription.last_synced_at is None,
                    change_rate=subscription.change_rate,
                    waiting_since=subscription.last_synced_at,
                )
                if job is None:
                    left = len(subscriptions) - index
                    logger.warning(
                        "Queue full (YUBAL_MAX_JOBS=%d): %d subscription(s) left "
                        "for the next sweep, starting with %s",
                        self._settings.max_jobs,
                        left,
                        subscription.name,
                    )
                    break

                job_ids.append(job.id)
                self._subscription_service.update(
                    subscription.id,
                    {"last_synced_at": datetime.now(UTC)},
                )
                logger.info(
                    "Created sync job %s for %s",
                    job.id[:8],
                    subscription.name,
                )
            except Exception:
                logger.exception(
                    "Failed to create job for %s",
                    subscription.name,
                )
        return job_ids

    @staticmethod
    def _by_priority(subscriptions: list[Subscription]) -> list[Subscription]:
        """Order a sweep the way the queue will run it, see job_priority."""
        now = datetime.now(UTC)
        return sorted(
            subscriptions,
            key=lambda s: subscription_priority(s, now),
            reverse=True,
        )

    def _without_queued(self, subscriptions: list[Subscription]) -> list[Subscription]:
        """Drop subscriptions that already have a job queued or running.

        A full sweep can take longer than the time between two of them, and
        syncing the same thing twice only wastes the rate limit.
        """
        queued = self._job_executor.unfinished_job_urls()
        remaining = [s for s in subscriptions if s.url not in queued]
        skipped = len(subscriptions) - len(remaining)
        if skipped:
            logger.info("Skipped %d subscription(s) already in the queue", skipped)
        return remaining

    def _run_library_scan(self) -> None:
        """Scan the library for new items to auto-add, if configured.

        Runs before the enabled-subscriptions sweep so anything newly
        auto-added (created with enabled=True) is synced in the same
        cycle. Never raises — a scan failure shouldn't block the regular
        sync sweep that follows it.
        """
        if self._library_sync_service is None:
            return
        try:
            self._library_sync_service.scan_and_add()
        except Exception:
            logger.exception("Library auto-add scan failed")

    def _run_auto_delete(self) -> None:
        """Delete tracks the last sweep dropped from every playlist, if enabled.

        Runs before the new sweep, so every M3U already reflects the previous
        one. Skipped while jobs are still queued (a half-done sweep leaves
        M3Us that don't agree yet) and when an M3U kind is turned off, as the
        files only that kind lists would look unreferenced. Never raises.
        """
        settings = self._settings
        if not settings.auto_delete:
            return
        if not (settings.playlist_m3u and settings.album_m3u and settings.artist_m3u):
            logger.warning(
                "Auto-delete skipped: it needs YUBAL_PLAYLIST_M3U, YUBAL_ALBUM_M3U "
                "and YUBAL_ARTIST_M3U on to know which tracks are still wanted"
            )
            return
        if self._job_executor.unfinished_job_urls():
            logger.info("Auto-delete skipped: the previous sync is still running")
            return
        try:
            result = remove_unreferenced_tracks(settings.data)
        except Exception:
            logger.exception("Auto-delete failed")
            return
        if result.deleted:
            logger.info(
                "Auto-delete removed %d track(s) no playlist has anymore",
                len(result.unreferenced),
            )

    async def _sync_all_enabled(self) -> list[str]:
        """Sync all enabled subscriptions (async wrapper)."""
        await asyncio.to_thread(self._run_auto_delete)
        self._run_library_scan()
        return self._create_jobs_for_subscriptions(
            self._by_priority(
                self._without_queued(self._subscription_service.list(enabled=True))
            )
        )

    def sync_subscription(self, subscription_id: UUID) -> str | None:
        """Create sync job for a single subscription. Returns job_id or None.

        Deliberately does not run the library scan — resyncing one
        subscription shouldn't trigger a whole-library scan.
        """
        try:
            subscription = self._subscription_service.get(subscription_id)
        except SubscriptionNotFoundError:
            return None

        job_ids = self._create_jobs_for_subscriptions([subscription], JobSource.MANUAL)
        return job_ids[0] if job_ids else None

    def sync_all(self) -> list[str]:
        """Create sync jobs for all enabled subscriptions. Returns job_ids."""
        self._run_library_scan()
        return self._create_jobs_for_subscriptions(
            self._by_priority(
                self._without_queued(self._subscription_service.list(enabled=True))
            )
        )
