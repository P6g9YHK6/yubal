"""Tests for the job queue surviving restarts."""

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError
from yubal import PhaseStats
from yubal_api.db.job_repository import JobRepository
from yubal_api.domain.enums import JobStatus
from yubal_api.domain.job import ContentInfo
from yubal_api.services.job_event_bus import JobEventBus
from yubal_api.services.job_executor import JobExecutor
from yubal_api.services.job_store import JobStore
from yubal_api.services.sync_service import SyncResult


@pytest.fixture
def new_store(clock: Any, id_generator: Any, job_repository: JobRepository) -> Any:
    """Build a store on the shared database, like the app does on each start."""

    def build() -> JobStore:
        return JobStore(
            clock=clock,
            id_generator=id_generator,
            event_bus=JobEventBus(),
            repository=job_repository,
        )

    return build


def _create(store: JobStore, url: str) -> str:
    result = store.create(url)
    assert result is not None
    return result[0].id


class TestRestore:
    """Tests for what comes back after a restart."""

    def test_keeps_queue_order(self, new_store: Any) -> None:
        store = new_store()
        ids = [_create(store, f"https://example.com/{i}") for i in range(5)]

        restored = new_store()
        assert restored.restore() == 5
        assert [job.id for job in restored.get_all()] == ids

    def test_keeps_job_fields(self, new_store: Any) -> None:
        store = new_store()
        result = store.create("https://example.com/a", max_items=9999)
        assert result is not None

        restored = new_store()
        restored.restore()
        job = restored.get(result[0].id)
        assert job is not None
        assert job.url == "https://example.com/a"
        assert job.max_items == 9999

    def test_running_job_goes_back_to_pending_and_runs_first(
        self, new_store: Any, clock: Any
    ) -> None:
        store = new_store()
        first = _create(store, "https://example.com/1")
        second = _create(store, "https://example.com/2")
        store.transition(first, JobStatus.DOWNLOADING, started_at=clock())
        store.transition(first, JobStatus.DOWNLOADING, progress=50.0)

        restored = new_store()
        restored.restore()
        job = restored.get(first)
        assert job is not None
        assert job.status == JobStatus.PENDING
        assert job.progress == 0.0
        assert job.started_at is None
        next_job = restored.pop_next_pending()
        assert next_job is not None
        assert next_job.id == first
        assert restored.get(second).status == JobStatus.PENDING

    def test_interrupted_job_keeps_content_info(self, new_store: Any) -> None:
        store = new_store()
        job_id = _create(store, "https://example.com/1")
        info = ContentInfo(title="Album", artist="Artist")
        store.transition(job_id, JobStatus.DOWNLOADING, content_info=info)

        restored = new_store()
        restored.restore()
        assert restored.get(job_id).content_info == info

    def test_finished_jobs_keep_their_status(self, new_store: Any) -> None:
        store = new_store()
        done = _create(store, "https://example.com/1")
        failed = _create(store, "https://example.com/2")
        cancelled = _create(store, "https://example.com/3")
        stats = PhaseStats(success=3)
        store.transition(done, JobStatus.COMPLETED, download_stats=stats)
        store.transition(failed, JobStatus.FAILED)
        store.cancel(cancelled)

        restored = new_store()
        assert restored.restore() == 0
        job = restored.get(done)
        assert job.status == JobStatus.COMPLETED
        assert job.completed_at is not None
        assert job.download_stats == stats
        assert restored.get(failed).status == JobStatus.FAILED
        assert restored.get(cancelled).status == JobStatus.CANCELLED
        assert restored.pop_next_pending() is None

    def test_deleted_jobs_stay_gone(self, new_store: Any) -> None:
        store = new_store()
        deleted = _create(store, "https://example.com/1")
        cleared = _create(store, "https://example.com/2")
        kept = _create(store, "https://example.com/3")
        store.transition(deleted, JobStatus.FAILED)
        store.delete(deleted)
        store.transition(cleared, JobStatus.COMPLETED)
        store.clear_finished()

        restored = new_store()
        restored.restore()
        assert [job.id for job in restored.get_all()] == [kept]

    def test_new_jobs_queue_behind_restored_ones(self, new_store: Any) -> None:
        store = new_store()
        old = _create(store, "https://example.com/1")

        restored = new_store()
        restored.restore()
        new = _create(restored, "https://example.com/2")

        again = new_store()
        again.restore()
        assert [job.id for job in again.get_all()] == [old, new]

    def test_unreadable_row_is_dropped(
        self, new_store: Any, job_repository: JobRepository
    ) -> None:
        store = new_store()
        good = _create(store, "https://example.com/1")
        job_repository.save("broken", 99, "not json")

        restored = new_store()
        assert restored.restore() == 1
        assert [job.id for job in restored.get_all()] == [good]
        assert [record.id for record in job_repository.list()] == [good]

    def test_without_repository_nothing_is_restored(
        self, clock: Any, id_generator: Any
    ) -> None:
        store = JobStore(clock, id_generator, JobEventBus())
        _create(store, "https://example.com/1")
        assert store.restore() == 0


class TestUnfinishedUrls:
    """Tests for telling which URLs are already in the queue."""

    def test_includes_pending_and_running_but_not_finished(
        self, new_store: Any
    ) -> None:
        store = new_store()
        running = _create(store, "https://example.com/running")
        _create(store, "https://example.com/pending")
        done = _create(store, "https://example.com/done")
        store.transition(running, JobStatus.DOWNLOADING)
        store.transition(done, JobStatus.COMPLETED)

        assert store.unfinished_urls() == {
            "https://example.com/running",
            "https://example.com/pending",
        }


class TestWrites:
    """Tests for when the store writes to the database."""

    def test_progress_alone_is_not_written(
        self, new_store: Any, job_repository: JobRepository
    ) -> None:
        store = new_store()
        job_id = _create(store, "https://example.com/1")
        store.transition(job_id, JobStatus.DOWNLOADING)
        saved = job_repository.list()[0].data

        store.transition(job_id, JobStatus.DOWNLOADING, progress=50.0)

        assert job_repository.list()[0].data == saved

    def test_failed_write_does_not_break_the_job(
        self, new_store: Any, job_repository: JobRepository
    ) -> None:
        store = new_store()
        job_id = _create(store, "https://example.com/1")
        job_repository.save = MagicMock(  # type: ignore[method-assign]
            side_effect=OperationalError("save", {}, Exception("locked"))
        )

        job = store.transition(job_id, JobStatus.COMPLETED)

        assert job is not None
        assert job.status == JobStatus.COMPLETED

    def test_stop_persisting_freezes_the_saved_queue(self, new_store: Any) -> None:
        store = new_store()
        job_id = _create(store, "https://example.com/1")
        store.transition(job_id, JobStatus.DOWNLOADING)

        store.stop_persisting()
        store.cancel(job_id)

        restored = new_store()
        restored.restore()
        assert restored.get(job_id).status == JobStatus.PENDING


@pytest.mark.enable_socket
class TestExecutorRestart:
    """Tests for the executor running a restored queue."""

    @pytest.mark.asyncio
    async def test_resume_runs_whole_queue_in_order(
        self, new_store: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = new_store()
        ids = [_create(store, f"https://example.com/{i}") for i in range(3)]
        ran: list[str] = []

        def fake_run(_self: Any, url: str, *_args: Any) -> SyncResult:
            ran.append(url)
            return SyncResult(success=True)

        monkeypatch.setattr("yubal_api.services.job_executor.SyncService.run", fake_run)

        restored = new_store()
        restored.restore()
        executor = JobExecutor(job_store=restored, base_path=tmp_path)
        executor.resume_queue()
        while executor._background_tasks:
            await asyncio.sleep(0.01)

        assert ran == [f"https://example.com/{i}" for i in range(3)]
        assert all(restored.get(i).status == JobStatus.COMPLETED for i in ids)

    @pytest.mark.asyncio
    async def test_shutdown_leaves_queue_to_resume(
        self, new_store: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A job cut off by shutdown should resume, and the next must not start."""
        store = new_store()
        first = _create(store, "https://example.com/1")
        second = _create(store, "https://example.com/2")
        store = new_store()
        store.restore()
        ran: list[str] = []

        def cut_off_run(_self: Any, url: str, *_args: Any) -> SyncResult:
            ran.append(url)
            store.stop_persisting()
            executor.cancel_all_jobs()
            raise RuntimeError("download interrupted")

        monkeypatch.setattr(
            "yubal_api.services.job_executor.SyncService.run", cut_off_run
        )

        executor = JobExecutor(job_store=store, base_path=tmp_path)
        executor.resume_queue()
        while executor._background_tasks:
            await asyncio.sleep(0.01)

        assert ran == ["https://example.com/1"]
        restored = new_store()
        assert restored.restore() == 2
        assert restored.get(first).status == JobStatus.PENDING
        assert restored.get(second).status == JobStatus.PENDING
