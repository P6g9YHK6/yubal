"""Tests for the runtime yt-dlp updater."""

import hashlib
import os
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from yubal_api.services import ytdlp_updater
from yubal_api.services.ytdlp_updater import (
    YtdlpUpdater,
    install,
    overlay_env,
    parse_version,
    use_overlay,
)


def _write_overlay(overlay_dir: Path, version: str) -> None:
    package = overlay_dir / "yt_dlp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from yt_dlp import version\n")
    (package / "version.py").write_text(f"__version__ = '{version}'\n")


def _wheel_bytes(tmp_path: Path, version: str) -> bytes:
    wheel = tmp_path / "fake.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("yt_dlp/__init__.py", "from yt_dlp import version\n")
        zf.writestr("yt_dlp/version.py", f"__version__ = '{version}'\n")
    return wheel.read_bytes()


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make httpx.stream return the given bytes."""

    def _serve(data: bytes) -> None:
        @contextmanager
        def fake_stream(*_args: Any, **_kwargs: Any) -> Any:
            response = MagicMock()
            response.iter_bytes.return_value = [data[:100], data[100:]]
            yield response

        monkeypatch.setattr(ytdlp_updater.httpx, "stream", fake_stream)

    return _serve


class TestParseVersion:
    def test_hotfix_is_newer(self) -> None:
        assert parse_version("2026.11.23.1") > parse_version("2026.11.23")

    def test_dates_compare_as_numbers(self) -> None:
        assert parse_version("2026.12.1") > parse_version("2026.11.23")


class TestOverlay:
    def test_newer_overlay_is_used(self, tmp_path: Path) -> None:
        _write_overlay(tmp_path / "yt-dlp", "2099.1.1")
        assert use_overlay(tmp_path / "yt-dlp")

    def test_older_overlay_is_deleted(self, tmp_path: Path) -> None:
        _write_overlay(tmp_path / "yt-dlp", "2000.1.1")
        assert not use_overlay(tmp_path / "yt-dlp")
        assert not (tmp_path / "yt-dlp").exists()

    def test_no_overlay(self, tmp_path: Path) -> None:
        assert not use_overlay(tmp_path / "yt-dlp")
        assert overlay_env(tmp_path / "yt-dlp") is None

    def test_env_puts_overlay_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("YUBAL_YTDLP_OVERLAY_LOADED", raising=False)
        monkeypatch.setenv("PYTHONPATH", "/somewhere")
        _write_overlay(tmp_path / "yt-dlp", "2099.1.1")

        env = overlay_env(tmp_path / "yt-dlp")

        assert env is not None
        assert env["PYTHONPATH"] == os.pathsep.join(
            [str(tmp_path / "yt-dlp"), "/somewhere"]
        )

    def test_no_second_exec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("YUBAL_YTDLP_OVERLAY_LOADED", "1")
        _write_overlay(tmp_path / "yt-dlp", "2099.1.1")
        assert overlay_env(tmp_path / "yt-dlp") is None


class TestInstall:
    def test_installs_release(self, tmp_path: Path, serve: Any) -> None:
        data = _wheel_bytes(tmp_path, "2099.1.1")
        serve(data)
        overlay = tmp_path / "config" / "yt-dlp"
        _write_overlay(overlay, "2098.1.1")

        install("2099.1.1", "https://x", hashlib.sha256(data).hexdigest(), overlay)

        assert ytdlp_updater.overlay_version(overlay) == "2099.1.1"
        assert sorted(p.name for p in overlay.parent.iterdir()) == ["yt-dlp"]

    def test_bad_checksum_keeps_old(self, tmp_path: Path, serve: Any) -> None:
        serve(_wheel_bytes(tmp_path, "2099.1.1"))
        overlay = tmp_path / "config" / "yt-dlp"
        _write_overlay(overlay, "2098.1.1")

        with pytest.raises(ValueError, match="Checksum"):
            install("2099.1.1", "https://x", "0" * 64, overlay)

        assert ytdlp_updater.overlay_version(overlay) == "2098.1.1"

    def test_broken_release_keeps_old(self, tmp_path: Path, serve: Any) -> None:
        wheel = tmp_path / "broken.whl"
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr("yt_dlp/__init__.py", "raise ImportError('broken')\n")
        data = wheel.read_bytes()
        serve(data)
        overlay = tmp_path / "config" / "yt-dlp"
        _write_overlay(overlay, "2098.1.1")

        with pytest.raises(Exception, match="returned non-zero"):
            install("2099.1.1", "https://x", hashlib.sha256(data).hexdigest(), overlay)

        assert ytdlp_updater.overlay_version(overlay) == "2098.1.1"


@pytest.mark.enable_socket
class TestUpdater:
    @pytest.fixture
    def updater(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[YtdlpUpdater, MagicMock, MagicMock]:
        installed = MagicMock()
        monkeypatch.setattr(ytdlp_updater, "install", installed)
        monkeypatch.setattr(ytdlp_updater, "running_version", lambda: "2026.11.23")
        executor = MagicMock()
        executor.unfinished_job_urls.return_value = set()
        return YtdlpUpdater(tmp_path / "yt-dlp", executor), installed, executor

    @pytest.mark.asyncio
    async def test_installs_newer(
        self, updater: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ytdlp_updater, "fetch_latest", lambda: ("2026.12.1", "url", "sha")
        )
        service, installed, _ = updater

        assert await service.check()
        installed.assert_called_once_with(
            "2026.12.1", "url", "sha", service._overlay_dir
        )

    @pytest.mark.asyncio
    async def test_skips_same_version(
        self, updater: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ytdlp_updater, "fetch_latest", lambda: ("2026.11.23", "url", "sha")
        )
        service, installed, _ = updater

        assert not await service.check()
        installed.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_error_is_logged(
        self,
        updater: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def offline() -> None:
            raise OSError("no network")

        monkeypatch.setattr(ytdlp_updater, "fetch_latest", offline)
        service, _, _ = updater

        assert not await service.check()
        assert "yt-dlp update check failed: no network" in caplog.text

    @pytest.mark.asyncio
    async def test_restarts_once_queue_is_empty(
        self, updater: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hook = MagicMock()
        monkeypatch.setattr(ytdlp_updater, "_restart_hook", hook)
        monkeypatch.setattr(ytdlp_updater, "IDLE_POLL", 0.01)
        service, _, executor = updater
        executor.unfinished_job_urls.side_effect = [{"a"}, {"a"}, set()]

        await service._restart_when_idle()

        hook.assert_called_once()
        assert executor.unfinished_job_urls.call_count == 3

    @pytest.mark.asyncio
    async def test_restarts_anyway_after_waiting(
        self, updater: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hook = MagicMock()
        monkeypatch.setattr(ytdlp_updater, "_restart_hook", hook)
        monkeypatch.setattr(ytdlp_updater, "IDLE_POLL", 0.01)
        monkeypatch.setattr(ytdlp_updater, "IDLE_WAIT", 0.03)
        service, _, executor = updater
        executor.unfinished_job_urls.return_value = {"busy"}

        await service._restart_when_idle()

        hook.assert_called_once()
