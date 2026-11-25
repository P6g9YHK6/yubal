"""Cleanup utilities for partial downloads, temporary files and dropped tracks."""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def cleanup_part_files(directory: Path) -> int:
    """Remove .part files from a directory tree.

    yt-dlp creates .part files during downloads. If a download is interrupted,
    these partial files should be cleaned up to avoid leaving incomplete data.

    Args:
        directory: Base directory to search for .part files recursively.

    Returns:
        Number of .part files removed.
    """
    cleaned = 0

    try:
        for part_file in directory.rglob("*.part"):
            try:
                part_file.unlink(missing_ok=True)
                cleaned += 1
            except OSError:
                pass  # Best effort cleanup
    except OSError:
        pass  # Directory might not exist

    return cleaned


AUDIO_EXTENSIONS = frozenset({".opus", ".mp3", ".m4a"})

# Files next to a track named after it (lyrics, media server metadata)
_SIDECAR_EXTENSIONS = (".lrc", ".nfo")
# Folder art and metadata that don't keep a folder alive once its tracks are gone
_FOLDER_LEFTOVER_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".nfo"})


@dataclass(frozen=True)
class CleanupResult:
    """What remove_unreferenced_tracks found and did."""

    unreferenced: list[Path]
    total_audio: int
    deleted: bool


def _referenced_tracks(playlists_dir: Path) -> set[Path]:
    """Collect every track path listed in an M3U under playlists_dir."""
    referenced: set[Path] = set()
    for m3u in playlists_dir.glob("*.m3u"):
        for line in m3u.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                referenced.add(Path(os.path.normpath(playlists_dir / line)))
    return referenced


def remove_unreferenced_tracks(
    base_path: Path, *, max_ratio: float = 0.2, dry_run: bool = False
) -> CleanupResult:
    """Delete audio files no M3U in _Playlists points to anymore.

    Every playlist, album and artist subscription writes an M3U with its
    current tracks, so a file none of them list was dropped upstream.
    Lyrics next to it go too, and folders left empty are removed.

    Nothing is deleted when more than max_ratio of the library would go, a
    wrong M3U folder or a half-written sync shouldn't be able to wipe it.

    Args:
        base_path: Library root (the folder holding _Playlists).
        max_ratio: Largest share of audio files a single run may delete.
        dry_run: Only report what would be deleted.

    Returns:
        CleanupResult with the unreferenced files and whether they were deleted.
    """
    playlists_dir = base_path / "_Playlists"
    referenced = _referenced_tracks(playlists_dir)

    audio = [
        path
        for path in base_path.rglob("*")
        if path.suffix.lower() in AUDIO_EXTENSIONS
        and playlists_dir not in path.parents
        and path.is_file()
    ]
    unreferenced = sorted(
        path for path in audio if Path(os.path.normpath(path)) not in referenced
    )

    if not unreferenced or dry_run:
        return CleanupResult(unreferenced, len(audio), deleted=False)
    if not referenced or len(unreferenced) > len(audio) * max_ratio:
        logger.error(
            "Auto-delete stopped: %d of %d tracks are in no playlist, more than "
            "the %d%% limit. Nothing was deleted, check the _Playlists folder",
            len(unreferenced),
            len(audio),
            round(max_ratio * 100),
        )
        return CleanupResult(unreferenced, len(audio), deleted=False)

    folders: set[Path] = set()
    for path in unreferenced:
        try:
            path.unlink()
        except OSError as e:
            logger.warning("Could not delete '%s': %s", path, e)
            continue
        for ext in _SIDECAR_EXTENSIONS:
            Path(f"{path.parent / path.stem}{ext}").unlink(missing_ok=True)
        folders.add(path.parent)
        logger.info("Deleted track in no playlist: '%s'", path.relative_to(base_path))

    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        _remove_if_empty(folder, base_path)

    return CleanupResult(unreferenced, len(audio), deleted=True)


def _remove_if_empty(folder: Path, base_path: Path) -> None:
    """Remove folder (and then its parents) once only art and metadata are left."""
    while folder != base_path and base_path in folder.parents:
        try:
            entries = list(folder.iterdir())
        except OSError:
            return
        if any(
            entry.is_dir() or entry.suffix.lower() not in _FOLDER_LEFTOVER_EXTENSIONS
            for entry in entries
        ):
            return
        for entry in entries:
            entry.unlink(missing_ok=True)
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent
