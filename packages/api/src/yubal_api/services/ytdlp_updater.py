"""Keep yt-dlp up to date without rebuilding the image.

YouTube changes break yt-dlp often and fixes ship as new releases, usually
long before a new image would. New releases are unpacked into a folder in
the config volume that is put in front of the bundled copy on startup, then
the app restarts itself so the running process picks them up.
See overlay_env for how the folder gets in front.
"""

import asyncio
import hashlib
import importlib.metadata
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from yubal_api.services.job_executor import JobExecutor

logger = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
CHECK_INTERVAL = 6 * 3600
FIRST_CHECK_DELAY = 60
# How long a pending restart waits for the job queue to empty. A job cut off
# by the restart is resumed afterwards, so this only avoids needless restarts.
IDLE_WAIT = 3600
IDLE_POLL = 60

_REEXEC_MARKER = "YUBAL_YTDLP_OVERLAY_LOADED"
_VERSION_RE = re.compile(r"""__version__\s*=\s*['"]([^'"]+)['"]""")

# Set by __main__ when the app runs under a server it can restart
_restart_hook: Callable[[], None] | None = None


def set_restart_hook(hook: Callable[[], None]) -> None:
    """Register how to restart the app, which turns the updater on."""
    global _restart_hook
    _restart_hook = hook


def can_restart() -> bool:
    return _restart_hook is not None


def parse_version(version: str) -> tuple[int, ...]:
    """Turn "2026.11.23" or "2026.11.23.1" into a comparable tuple."""
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def bundled_version(overlay_dir: Path) -> str | None:
    """Version installed in the venv, ignoring the overlay."""
    overlay = os.path.abspath(overlay_dir)
    path = [p for p in sys.path if os.path.abspath(p or ".") != overlay]
    dist = next(iter(importlib.metadata.distributions(name="yt-dlp", path=path)), None)
    return dist.version if dist else None


def overlay_version(overlay_dir: Path) -> str | None:
    """Version unpacked in the overlay, read without importing it."""
    try:
        text = (overlay_dir / "yt_dlp" / "version.py").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def use_overlay(overlay_dir: Path) -> bool:
    """Whether the overlay holds a newer yt-dlp than the venv.

    An overlay that is not newer (the image got a newer pin since) is
    deleted, so the bundled copy takes over again.
    """
    version = overlay_version(overlay_dir)
    if version is None:
        return False
    bundled = bundled_version(overlay_dir)
    if bundled and parse_version(version) <= parse_version(bundled):
        shutil.rmtree(overlay_dir, ignore_errors=True)
        return False
    return True


def overlay_env(overlay_dir: Path) -> dict[str, str] | None:
    """Environment to re-exec with when the overlay is due but not loaded.

    yubal imports yt_dlp as soon as settings load, so the overlay can't just
    be put on sys.path at that point. Setting PYTHONPATH and starting over
    puts it in front for this process and the ones it spawns.

    Returns:
        The environment to exec with, or None when nothing has to change.
    """
    # The marker stops a loop should the path still not show up after the exec
    if os.environ.get(_REEXEC_MARKER) or not use_overlay(overlay_dir):
        return None
    overlay = os.path.abspath(overlay_dir)
    if overlay in (os.path.abspath(p or ".") for p in sys.path):
        return None
    env = dict(os.environ)
    env[_REEXEC_MARKER] = "1"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [overlay, env.get("PYTHONPATH")]))
    return env


def running_version() -> str:
    from yt_dlp.version import __version__

    return __version__


def describe_running(overlay_dir: Path) -> str:
    """Running version and where it came from, for the startup log."""
    import yt_dlp.version

    origin = (
        "downloaded update"
        if Path(yt_dlp.version.__file__).resolve().is_relative_to(overlay_dir.resolve())
        else "bundled"
    )
    auto = "auto-update on" if can_restart() else "auto-update off"
    return f"{running_version()} ({origin}, {auto})"


def fetch_latest() -> tuple[str, str, str]:
    """Latest release on PyPI as (version, wheel url, sha256)."""
    response = httpx.get(PYPI_URL, timeout=30, follow_redirects=True)
    response.raise_for_status()
    data = response.json()
    version = data["info"]["version"]
    for file in data["urls"]:
        if file["packagetype"] == "bdist_wheel" and file["filename"].endswith(
            "-py3-none-any.whl"
        ):
            return version, file["url"], file["digests"]["sha256"]
    raise ValueError(f"No universal wheel for yt-dlp {version}")


def _smoke_test(path: Path) -> str:
    """Import yt-dlp from path in a fresh interpreter and return its version."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import yt_dlp; print(yt_dlp.version.__version__)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return result.stdout.strip()


def install(version: str, url: str, sha256: str, overlay_dir: Path) -> None:
    """Download and unpack a release, replacing the overlay once it imports.

    Raises:
        ValueError: The download doesn't match its checksum or version.
    """
    overlay_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=overlay_dir.parent) as tmp:
        wheel = Path(tmp) / "yt_dlp.whl"
        with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
            r.raise_for_status()
            digest = hashlib.sha256()
            with wheel.open("wb") as f:
                for chunk in r.iter_bytes():
                    digest.update(chunk)
                    f.write(chunk)
        if digest.hexdigest() != sha256:
            raise ValueError(f"Checksum mismatch for yt-dlp {version}")

        staged = Path(tmp) / "staged"
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(staged)
        imported = _smoke_test(staged)
        if imported != version:
            raise ValueError(f"Downloaded yt-dlp reports {imported}, not {version}")

        old = Path(tmp) / "old"
        if overlay_dir.exists():
            overlay_dir.rename(old)
        staged.rename(overlay_dir)


class YtdlpUpdater:
    """Checks PyPI now and then and restarts the app onto new releases."""

    def __init__(self, overlay_dir: Path, job_executor: "JobExecutor") -> None:
        self._overlay_dir = overlay_dir
        self._job_executor = job_executor
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task is not None or _restart_hook is None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _sleep(self, seconds: float) -> bool:
        """Wait, returning True if the updater was stopped meanwhile."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def _run_loop(self) -> None:
        delay = FIRST_CHECK_DELAY
        while not await self._sleep(delay):
            delay = CHECK_INTERVAL
            if await self.check():
                await self._restart_when_idle()
                return

    async def check(self) -> bool:
        """Install a newer release if there is one.

        Returns:
            True when one was installed and the app should restart.
        """
        try:
            version, url, sha256 = await asyncio.to_thread(fetch_latest)
            current = running_version()
            if parse_version(version) <= parse_version(current):
                return False
            await asyncio.to_thread(install, version, url, sha256, self._overlay_dir)
        except Exception as e:
            logger.warning("yt-dlp update check failed: %s", e)
            return False
        logger.info("yt-dlp %s -> %s downloaded, restarting to apply", current, version)
        return True

    async def _restart_when_idle(self) -> None:
        waited = 0
        while self._job_executor.unfinished_job_urls() and waited < IDLE_WAIT:
            if await self._sleep(IDLE_POLL):
                return
            waited += IDLE_POLL
        if _restart_hook is not None:
            logger.info("Restarting to load the new yt-dlp")
            _restart_hook()
