"""Job store with thread-safe operations, optionally saved to the database."""

import logging
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from yubal import AudioCodec, PhaseStats

from yubal_api.db.job_repository import JobRepository
from yubal_api.domain.enums import JobSource, JobStatus
from yubal_api.domain.job import ContentInfo, Job
from yubal_api.domain.types import Clock, IdGenerator
from yubal_api.services.job_event_bus import JobEventBus

logger = logging.getLogger(__name__)


class JobStore:
    """Job store with capacity limit and FIFO queue semantics.

    Thread-Safety:
        All public methods are thread-safe using a single lock. Operations are
        synchronous since memory is the source of truth while running.

    Persistence:
        With a repository, jobs are written through to the database and read
        back by `restore` after a restart. A failed write is logged and the job
        carries on, losing the queue on a restart beats failing a download.

    Responsibilities:
        - Job persistence (CRUD operations)
        - Queue management (FIFO ordering, capacity limits)

    Non-Responsibilities:
        - State machine validation (caller's responsibility)
        - Cancellation signaling (handled by CancelToken in JobExecutor)
        - Timeout enforcement (handled by asyncio.timeout in JobExecutor)

    Capacity:
        When at MAX_JOBS, completed jobs are pruned to make room for new ones.
        If all jobs are active/queued, job creation returns None.
    """

    MAX_JOBS = 200

    def __init__(
        self,
        clock: Clock,
        id_generator: IdGenerator,
        event_bus: JobEventBus,
        repository: JobRepository | None = None,
    ) -> None:
        """Initialize the job store.

        Args:
            clock: Function returning current datetime (enables testing).
            id_generator: Function generating unique job IDs.
            event_bus: Event bus for emitting job state change events.
            repository: Optional repository to save jobs across restarts.
        """
        self._clock = clock
        self._id_generator = id_generator
        self._event_bus = event_bus
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._active_job_id: str | None = None
        self._repository = repository
        self._positions: dict[str, int] = {}
        self._next_position = 0

    # -------------------------------------------------------------------------
    # Public API: Persistence
    # -------------------------------------------------------------------------

    def restore(self) -> int:
        """Load saved jobs back into the store. Call once on startup.

        Queue order and finished jobs are kept as they were. A job that was
        running when the app stopped goes back to pending in its old position,
        so it starts first and skips whatever it already downloaded.

        Returns:
            Number of jobs waiting to run.
        """
        if self._repository is None:
            return 0

        with self._locked():
            for record in self._repository.list():
                try:
                    job = Job.model_validate_json(record.data)
                except ValidationError:
                    logger.warning("Dropping unreadable saved job %s", record.id[:8])
                    self._repository.delete(record.id)
                    continue

                if not job.status.is_finished and job.status != JobStatus.PENDING:
                    job.status = JobStatus.PENDING
                    job.progress = 0.0
                    job.started_at = None
                    self._repository.save(
                        job.id, record.position, job.model_dump_json()
                    )

                self._jobs[job.id] = job
                self._positions[job.id] = record.position
                self._next_position = record.position + 1

            return sum(not job.status.is_finished for job in self._jobs.values())

    def stop_persisting(self) -> None:
        """Stop writing to the database. Call first thing on shutdown.

        Shutting down cancels the running job, and that must not be saved as
        its status or it wouldn't resume on the next start.
        """
        with self._locked():
            self._repository = None

    # -------------------------------------------------------------------------
    # Public API: Job lifecycle
    # -------------------------------------------------------------------------

    def create(
        self,
        url: str,
        audio_format: AudioCodec = AudioCodec.OPUS,
        max_items: int | None = None,
        source: JobSource = JobSource.MANUAL,
        subscription_id: UUID | None = None,
    ) -> tuple[Job, bool] | None:
        """Create a new job.

        When at capacity, completed jobs are pruned to make room. If all jobs
        are active or queued, returns None.

        Args:
            url: The URL to download content from.
            audio_format: Audio codec for transcoding.
            max_items: Maximum number of items to download (None for all).
            source: Source of the job (manual API call or scheduler).
            subscription_id: Optional subscription that triggered this job.

        Returns:
            Tuple of (job, should_start_immediately), or None if queue is full.
        """
        with self._locked():
            if not self._prune_to_capacity():
                return None  # Queue full, all jobs active/queued

            should_start = self._active_job_id is None
            job = Job(
                id=self._id_generator(),
                url=url,
                audio_format=audio_format,
                max_items=max_items,
                subscription_id=subscription_id,
                source=source,
            )
            self._jobs[job.id] = job
            self._positions[job.id] = self._next_position
            self._next_position += 1
            self._persist(job)

            if should_start:
                self._active_job_id = job.id

            self._event_bus.emit_created(job)
            return job, should_start

    def get(self, job_id: str) -> Job | None:
        """Get a job by ID.

        Args:
            job_id: The job identifier.

        Returns:
            The job if found, None otherwise.
        """
        with self._locked():
            return self._jobs.get(job_id)

    def get_all(self) -> list[Job]:
        """Get all jobs in FIFO order (oldest first).

        Returns:
            List of all jobs ordered by creation time.
        """
        with self._locked():
            return list(self._jobs.values())

    def unfinished_urls(self) -> set[str]:
        """Get the URLs of all jobs that are queued or running.

        Returns:
            Set of URLs with a job that hasn't finished.
        """
        with self._locked():
            return {
                job.url for job in self._jobs.values() if not job.status.is_finished
            }

    def delete(self, job_id: str) -> bool:
        """Delete a finished job.

        Cannot delete jobs that are still running.

        Args:
            job_id: The job identifier.

        Returns:
            True if deleted, False if job doesn't exist or is still running.
        """
        with self._locked():
            if not (job := self._jobs.get(job_id)):
                return False

            if not job.status.is_finished:
                return False

            self._remove(job_id)
            self._event_bus.emit_deleted(job_id)
            logger.debug("Job removed: %s", job_id[:8])
            return True

    def clear_finished(self) -> int:
        """Remove all completed, failed, and cancelled jobs.

        Returns:
            Number of jobs removed.
        """
        with self._locked():
            finished_ids = [job.id for job in self._iter_finished()]
            for job_id in finished_ids:
                self._remove(job_id)
            count = len(finished_ids)
            if count > 0:
                self._event_bus.emit_cleared(count)
            return count

    # -------------------------------------------------------------------------
    # Public API: Job state transitions
    # -------------------------------------------------------------------------

    def transition(
        self,
        job_id: str,
        status: JobStatus,
        *,
        progress: float | None = None,
        content_info: ContentInfo | None = None,
        download_stats: PhaseStats | None = None,
        started_at: datetime | None = None,
    ) -> Job | None:
        """Update job status and related fields atomically.

        Args:
            job_id: The job identifier.
            status: New job status.
            progress: Optional progress value (0.0 to 1.0).
            content_info: Optional content metadata.
            download_stats: Optional download statistics.
            started_at: Optional job start timestamp.

        Returns:
            The updated job, or None if not found.
        """
        with self._locked():
            if not (job := self._jobs.get(job_id)):
                return None

            if job.status.is_finished:
                return job

            # Progress alone isn't saved: it comes for every track and a resumed
            # job starts over anyway
            should_persist = (
                status != job.status
                or content_info is not None
                or download_stats is not None
                or started_at is not None
            )

            self._apply_updates(
                job,
                status=status,
                progress=progress,
                content_info=content_info,
                download_stats=download_stats,
                started_at=started_at,
            )
            if should_persist:
                self._persist(job)
            self._event_bus.emit_updated(job)
            return job

    def cancel(self, job_id: str) -> bool:
        """Mark a job as cancelled.

        Does NOT clear the active job marker immediately. The job continues
        running until the executor's cleanup completes and calls
        `release_active`. This prevents new jobs from starting before the
        cancelled job has fully stopped.

        Cancellation signaling is handled by CancelToken in JobExecutor.

        Args:
            job_id: The job identifier.

        Returns:
            True if cancelled, False if job doesn't exist or is already finished.
        """
        with self._locked():
            if not (job := self._jobs.get(job_id)):
                return False

            if job.status.is_finished:
                return False

            job.status = JobStatus.CANCELLED
            job.completed_at = self._clock()
            self._persist(job)
            self._event_bus.emit_updated(job)
            return True

    # -------------------------------------------------------------------------
    # Public API: Queue management
    # -------------------------------------------------------------------------

    def pop_next_pending(self) -> Job | None:
        """Activate and return the next pending job.

        Uses FIFO ordering (insertion order via OrderedDict).

        Returns:
            The next pending job, or None if queue is empty.
        """
        with self._locked():
            for job in self._jobs.values():
                if job.status == JobStatus.PENDING and job.id != self._active_job_id:
                    self._active_job_id = job.id
                    return job
            return None

    def release_active(self, job_id: str) -> bool:
        """Release the active job slot after execution ends.

        Called by the executor after cleanup is complete, allowing the
        next pending job to start.

        Args:
            job_id: ID of the job that finished executing.

        Returns:
            True if released, False if the job was not the active job.
        """
        with self._locked():
            if self._active_job_id == job_id:
                self._active_job_id = None
                return True

            active_display = self._active_job_id[:8] if self._active_job_id else "None"
            logger.warning(
                "Attempted to release job %s but active job is %s",
                job_id[:8],
                active_display,
            )
            return False

    # -------------------------------------------------------------------------
    # Private: Lock management
    # -------------------------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Context manager for thread-safe operations."""
        with self._lock:
            yield

    # -------------------------------------------------------------------------
    # Private: Job collection operations (require lock held)
    # -------------------------------------------------------------------------

    def _remove(self, job_id: str) -> bool:
        """Remove a job from the store.

        Note:
            Must be called with lock held.

        Args:
            job_id: The job identifier.

        Returns:
            True if removed, False if job didn't exist.
        """
        try:
            del self._jobs[job_id]
        except KeyError:
            return False

        self._positions.pop(job_id, None)
        if self._repository is not None:
            try:
                self._repository.delete(job_id)
            except SQLAlchemyError:
                logger.exception("Could not delete saved job %s", job_id[:8])
        return True

    def _persist(self, job: Job) -> None:
        """Save a job to the database, if one is configured.

        Note:
            Must be called with lock held.

        Args:
            job: The job to save.
        """
        if self._repository is None:
            return
        try:
            self._repository.save(
                job.id, self._positions[job.id], job.model_dump_json()
            )
        except SQLAlchemyError:
            logger.exception("Could not save job %s", job.id[:8])

    def _iter_finished(self) -> Iterator[Job]:
        """Iterate over finished jobs.

        Note:
            Must be called with lock held.

        Yields:
            Jobs with a finished status (completed, failed, cancelled).
        """
        return (job for job in self._jobs.values() if job.status.is_finished)

    def _prune_to_capacity(self) -> bool:
        """Remove finished jobs until under capacity.

        Note:
            Must be called with lock held.

        Returns:
            True if capacity is available, False if all jobs are active/queued.
        """
        while len(self._jobs) >= self.MAX_JOBS:
            if oldest := next(self._iter_finished(), None):
                self._remove(oldest.id)
            else:
                return False
        return True

    # -------------------------------------------------------------------------
    # Private: Job state management (require lock held)
    # -------------------------------------------------------------------------

    def _apply_updates(
        self,
        job: Job,
        *,
        status: JobStatus | None = None,
        progress: float | None = None,
        content_info: ContentInfo | None = None,
        download_stats: PhaseStats | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        """Apply field updates to a job.

        Automatically sets completed_at and clears active job when status
        becomes finished.

        Note:
            Must be called with lock held.

        Args:
            job: The job to update.
            status: New status value.
            progress: New progress value.
            content_info: New content info.
            download_stats: New download statistics.
            started_at: New start timestamp.
            completed_at: New completion timestamp.
        """
        if status is not None:
            job.status = status
        if progress is not None:
            job.progress = progress
        if content_info is not None:
            job.content_info = content_info
        if download_stats is not None:
            job.download_stats = download_stats
        if started_at is not None:
            job.started_at = started_at
        if completed_at is not None:
            job.completed_at = completed_at

        # Auto-set completion timestamp when status becomes finished
        # Note: Do NOT clear _active_job_id here - the executor is responsible
        # for calling release_active() after cleanup completes
        if job.status.is_finished:
            job.completed_at = job.completed_at or self._clock()
