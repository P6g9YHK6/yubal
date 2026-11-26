"""Tests for JobExecutor timeout enforcement."""

import logging
import time
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from yubal import AudioCodec, PhaseStats
from yubal_api.domain.enums import JobSource, JobStatus, ProgressStep
from yubal_api.domain.job import ContentInfo, Job
from yubal_api.services.job_executor import JobExecutor
from yubal_api.services.sync_service import SyncResult, SyncService


class FakeJobStore:
    """Minimal fake implementing the JobExecutionStore protocol."""

    def __init__(self) -> None:
        self.transitions: list[tuple[str, JobStatus]] = []
        self.released: list[str] = []
        self._pending: list[Job] = []

    def create(
        self,
        url: str,
        audio_format: AudioCodec = AudioCodec.OPUS,
        max_items: int | None = None,
        source: JobSource = JobSource.MANUAL,
        subscription_id: UUID | None = None,
        **_hints: Any,
    ) -> tuple[Job, bool] | None:
        job = Job(id="test-job", url=url, audio_format=audio_format)
        return job, True

    def get(self, job_id: str) -> Job | None:
        return None

    def transition(self, job_id: str, status: JobStatus, **kwargs: Any) -> Job:
        self.transitions.append((job_id, status))
        return Job(id=job_id, url="", audio_format=AudioCodec.OPUS, status=status)

    def unfinished_urls(self) -> set[str]:
        return set()

    def pop_next_pending(self) -> Job | None:
        return self._pending.pop(0) if self._pending else None

    def release_active(self, job_id: str) -> bool:
        self.released.append(job_id)
        return True


@pytest.mark.enable_socket
class TestExecutorTimeout:
    """Tests for timeout enforcement in _run_job."""

    @pytest.fixture
    def store(self) -> FakeJobStore:
        return FakeJobStore()

    @pytest.fixture
    def executor(self, store: FakeJobStore, tmp_path: Any) -> JobExecutor:
        return JobExecutor(job_store=store, base_path=tmp_path, job_timeout=0.1)

    @pytest.mark.asyncio
    async def test_timeout_triggers_cancellation_and_fails_job(
        self,
        executor: JobExecutor,
        store: FakeJobStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Timeout should mark job FAILED and trigger cancel token."""

        def blocking_run(*_args: Any, **_kwargs: Any) -> SyncResult:
            time.sleep(0.5)
            return SyncResult(success=True)

        monkeypatch.setattr(
            "yubal_api.services.job_executor.SyncService.run",
            blocking_run,
        )

        await executor._run_job("test-job", "https://example.com")

        # Should have transitioned to FETCHING_INFO then FAILED
        statuses = [s for _, s in store.transitions]
        assert JobStatus.FETCHING_INFO in statuses
        assert statuses[-1] == JobStatus.FAILED

        # Cancel token cleaned up in finally
        assert "test-job" not in executor._cancel_tokens

        # Active slot should have been released
        assert "test-job" in store.released

    @pytest.mark.asyncio
    async def test_normal_completion_within_timeout(
        self,
        executor: JobExecutor,
        store: FakeJobStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Job finishing before timeout should complete normally."""

        def fast_run(*_args: Any, **_kwargs: Any) -> SyncResult:
            return SyncResult(success=True)

        monkeypatch.setattr(
            "yubal_api.services.job_executor.SyncService.run",
            fast_run,
        )

        await executor._run_job("test-job", "https://example.com")

        statuses = [s for _, s in store.transitions]
        assert JobStatus.COMPLETED in statuses
        assert JobStatus.FAILED not in statuses
        assert "test-job" in store.released

    @pytest.mark.asyncio
    async def test_progress_pushes_back_timeout(
        self,
        store: FakeJobStore,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A job outliving the timeout should survive while it keeps progressing."""
        executor = JobExecutor(job_store=store, base_path=tmp_path, job_timeout=0.5)

        def progressing_run(
            _self: Any, _url: str, on_progress: Any, *_args: Any
        ) -> SyncResult:
            for _ in range(10):
                time.sleep(0.1)
                on_progress(ProgressStep.DOWNLOADING, "", 50.0, None)
            return SyncResult(success=True)

        monkeypatch.setattr(
            "yubal_api.services.job_executor.SyncService.run",
            progressing_run,
        )

        await executor._run_job("test-job", "https://example.com")

        statuses = [s for _, s in store.transitions]
        assert statuses[-1] == JobStatus.COMPLETED
        assert JobStatus.FAILED not in statuses

    @pytest.mark.asyncio
    async def test_stall_after_progress_still_times_out(
        self,
        executor: JobExecutor,
        store: FakeJobStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A job that stops progressing should be killed despite earlier progress."""

        def stalling_run(
            _self: Any, _url: str, on_progress: Any, *_args: Any
        ) -> SyncResult:
            on_progress(ProgressStep.DOWNLOADING, "", 50.0, None)
            time.sleep(0.5)
            on_progress(ProgressStep.DOWNLOADING, "", 60.0, None)
            return SyncResult(success=True)

        monkeypatch.setattr(
            "yubal_api.services.job_executor.SyncService.run",
            stalling_run,
        )

        await executor._run_job("test-job", "https://example.com")

        statuses = [s for _, s in store.transitions]
        assert statuses[-1] == JobStatus.FAILED
        assert JobStatus.COMPLETED not in statuses


@pytest.mark.enable_socket
class TestExecutorAudioQuality:
    """Tests for audio_quality propagation through JobExecutor to SyncService."""

    @pytest.fixture
    def store(self) -> FakeJobStore:
        return FakeJobStore()

    @pytest.mark.asyncio
    async def test_audio_quality_passed_to_sync_service(
        self,
        store: FakeJobStore,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """audio_quality should be forwarded to SyncService."""
        executor = JobExecutor(job_store=store, base_path=tmp_path, audio_quality=5)

        captured_quality: list[int] = []

        original_init = SyncService.__init__

        def spy_init(self: Any, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            captured_quality.append(self.audio_quality)

        monkeypatch.setattr(
            "yubal_api.services.job_executor.SyncService.__init__",
            spy_init,
        )
        monkeypatch.setattr(
            "yubal_api.services.job_executor.SyncService.run",
            lambda *a, **kw: SyncResult(success=True),
        )

        await executor._run_job("test-job", "https://example.com")

        assert captured_quality == [5]

    @pytest.mark.asyncio
    async def test_audio_quality_defaults_to_zero(
        self,
        store: FakeJobStore,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """audio_quality should default to 0 (best) when not specified."""
        executor = JobExecutor(job_store=store, base_path=tmp_path)

        captured_quality: list[int] = []

        original_init = SyncService.__init__

        def spy_init(self: Any, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            captured_quality.append(self.audio_quality)

        monkeypatch.setattr(
            "yubal_api.services.job_executor.SyncService.__init__",
            spy_init,
        )
        monkeypatch.setattr(
            "yubal_api.services.job_executor.SyncService.run",
            lambda *a, **kw: SyncResult(success=True),
        )

        await executor._run_job("test-job", "https://example.com")

        assert captured_quality == [0]


class TestFailedTracksSummary:
    def _result(self, failed: int) -> SyncResult:
        return SyncResult(
            success=True,
            content_info=ContentInfo(title="Some Album", artist="Some Band"),
            download_stats=PhaseStats(success=30 - failed, failed=failed),
            failed_tracks=[f"Some Band - Song {i}: unavailable" for i in range(failed)],
        )

    def test_one_error_lists_the_tracks(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, "yubal_api.services.job_executor"):
            JobExecutor._log_failed_tracks("https://example.com", self._result(2))

        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert message.startswith("Some Album: 2 of 30 tracks failed\n")
        assert "- Some Band - Song 1: unavailable" in message

    def test_long_list_is_cut(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, "yubal_api.services.job_executor"):
            JobExecutor._log_failed_tracks("https://example.com", self._result(25))

        message = caplog.records[0].getMessage()
        assert "- Some Band - Song 19: unavailable" in message
        assert "Song 20" not in message
        assert message.endswith("- and 5 more")


class TestLearnsChangeRate:
    def _executor(self, tmp_path: Any, old_rate: float | None) -> tuple[Any, Any]:
        store = MagicMock()
        store.get.return_value = Job(id="j", url="https://x", change_rate=old_rate)
        subscriptions = MagicMock()
        executor = JobExecutor(
            job_store=store, base_path=tmp_path, subscription_service=subscriptions
        )
        return executor, subscriptions

    def test_stores_new_rate_and_metadata(self, tmp_path: Any) -> None:
        executor, subscriptions = self._executor(tmp_path, 10)
        sub_id = uuid4()
        result = SyncResult(
            success=True,
            content_info=ContentInfo(title="Liked Music", artist="Me"),
            download_stats=PhaseStats(success=0),
        )

        executor._update_subscription("j", sub_id, result)

        subscriptions.update.assert_called_once_with(
            sub_id,
            {"name": "Liked Music", "thumbnail_url": None, "change_rate": 7.0},
        )

    def test_first_measure(self, tmp_path: Any) -> None:
        executor, subscriptions = self._executor(tmp_path, None)

        executor._update_subscription(
            "j", uuid4(), SyncResult(success=True, download_stats=PhaseStats(success=4))
        )

        assert subscriptions.update.call_args.args[1] == {"change_rate": 4.0}
