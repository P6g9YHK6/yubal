"""Playlist artifacts service for generating M3U and cover files."""

import logging
from pathlib import Path
from typing import NamedTuple, Protocol

from yubal.lib.m3u import resolve_playlist_stem, write_m3u
from yubal.models.enums import ContentKind, DownloadStatus
from yubal.models.results import DownloadResult
from yubal.models.track import PlaylistInfo, TrackMetadata
from yubal.utils.cover import write_playlist_cover
from yubal.utils.filename import format_playlist_filename

logger = logging.getLogger(__name__)


class ArtifactPaths(NamedTuple):
    """Paths to generated playlist artifacts.

    NamedTuple so callers can destructure: ``m3u, cover = composer.compose(...)``
    """

    m3u: Path | None = None
    cover: Path | None = None


class PlaylistArtifactsProtocol(Protocol):
    """Protocol for playlist artifact generation services.

    Enables dependency injection and testing of artifact generation.
    """

    def compose(
        self,
        base_path: Path,
        playlist_info: PlaylistInfo,
        results: list[DownloadResult],
        *,
        generate_m3u: bool = True,
        save_cover: bool = True,
        skip_album_m3u: bool = True,
        skip_playlist_m3u: bool = False,
    ) -> ArtifactPaths:
        """Generate playlist artifacts (M3U file and cover image)."""
        ...


class PlaylistArtifactsService:
    """Service for generating playlist artifacts (M3U files and covers).

    Pipeline Overview:
    ==================
    1. compose() - Main entry point: orchestrates M3U and cover generation
                   based on configuration flags and content type
    2. _collect_successful_tracks_for_playlist() - Filters download results
                   to include only successful and skipped tracks (both should
                   appear in the final playlist)
    3. Delegates to utility functions (write_m3u, write_playlist_cover) for
       actual file generation

    This service is stateless and operates purely on the results of completed
    downloads. It does not perform any downloads or API calls itself.

    Example:
        >>> composer = PlaylistArtifactsService()
        >>> artifacts = composer.compose(
        ...     base_path=Path("./music"),
        ...     playlist_info=playlist_info,
        ...     results=download_results,
        ... )
        >>> artifacts.m3u   # Path or None
        >>> artifacts.cover  # Path or None
    """

    def __init__(self, *, ascii_filenames: bool = False) -> None:
        self._ascii_filenames = ascii_filenames

    # ============================================================================
    # PUBLIC API - Main entry point for playlist artifact generation
    # ============================================================================

    def compose(
        self,
        base_path: Path,
        playlist_info: PlaylistInfo,
        results: list[DownloadResult],
        *,
        generate_m3u: bool = True,
        save_cover: bool = True,
        skip_album_m3u: bool = True,
        skip_playlist_m3u: bool = False,
    ) -> ArtifactPaths:
        """Generate playlist artifacts (M3U file and cover image) from downloads.

        This is the main composition pipeline. It takes completed download results
        and generates two types of artifacts:
        1. M3U playlist file - Contains relative paths to all tracks
        2. Playlist cover image - Saved as a sidecar JPEG file

        Why skip album M3U: Albums already have an inherent folder structure
        (one folder per album with all tracks inside). An M3U file is redundant
        for albums but valuable for curated playlists where tracks come from
        different albums and folders.

        Why include skipped tracks: Both SUCCESS and SKIPPED tracks should appear
        in the playlist. SKIPPED means the file already existed and was not
        re-downloaded, but it's still part of the playlist.

        Args:
            base_path: Base directory for output files.
            playlist_info: Playlist metadata (title, cover URL, content type).
            results: Download results with output paths and status.
            generate_m3u: Whether to generate M3U playlist file.
            save_cover: Whether to save playlist cover image.
            skip_album_m3u: Skip M3U generation for album playlists
                (they already have their own folder structure).
            skip_playlist_m3u: Skip M3U generation for regular playlists.

        Returns:
            ArtifactPaths with m3u and cover paths. Either may be None if
            not generated, not configured, or if generation failed.
        """
        m3u_path: Path | None = None
        cover_path: Path | None = None

        playlist_name = self._display_name(playlist_info, results)

        # Generate M3U playlist
        if generate_m3u:
            m3u_path = self._generate_m3u_if_needed(
                base_path=base_path,
                playlist_name=playlist_name,
                playlist_info=playlist_info,
                results=results,
                skip_album_m3u=skip_album_m3u,
                skip_playlist_m3u=skip_playlist_m3u,
            )

        # Save playlist cover, named like the M3U so players pair them
        if save_cover:
            stem = m3u_path.stem if m3u_path else None
            cover_path = self._save_cover_if_available(
                base_path=base_path,
                playlist_name=playlist_name,
                playlist_info=playlist_info,
                stem=stem,
            )

        self._remove_legacy_files(base_path, playlist_info, m3u_path, cover_path)

        return ArtifactPaths(m3u=m3u_path, cover=cover_path)

    # ============================================================================
    # NAMING - What players show as the playlist title
    # ============================================================================

    @staticmethod
    def _display_name(
        playlist_info: PlaylistInfo, results: list[DownloadResult]
    ) -> str:
        """Name the playlist the way it should read in a player.

        Albums get their artist in front so "Live" from two bands can be told
        apart, and a discography is marked so it doesn't look like a
        self-titled album.
        """
        title = playlist_info.title or "Untitled Playlist"
        if playlist_info.kind == ContentKind.ARTIST:
            return f"{title} (Discography)"
        if playlist_info.kind == ContentKind.ALBUM:
            artist = next(
                (
                    ", ".join(r.track.album_artists)
                    for r in results
                    if r.track and r.track.album_artists
                ),
                playlist_info.author,
            )
            if artist and not title.startswith(f"{artist} - "):
                return f"{artist} - {title}"
        return title

    def _remove_legacy_files(
        self,
        base_path: Path,
        playlist_info: PlaylistInfo,
        m3u_path: Path | None,
        cover_path: Path | None,
    ) -> None:
        """Drop files left under the old "<title> [<id>]" name.

        Older versions always added the ID suffix and used the bare album or
        artist name. Once this playlist has been written under its new name,
        the old file is a stale duplicate in the player.
        """
        legacy_stem = format_playlist_filename(
            playlist_info.title or "Untitled Playlist",
            playlist_info.playlist_id,
            ascii_filenames=self._ascii_filenames,
        )
        playlists_dir = base_path / "_Playlists"
        for new_path, suffix in ((m3u_path, ".m3u"), (cover_path, ".jpg")):
            if new_path is None:
                continue
            legacy = playlists_dir / f"{legacy_stem}{suffix}"
            if legacy != new_path and legacy.is_file():
                legacy.unlink()
                logger.info("Removed old playlist file: '%s'", legacy)

    # ============================================================================
    # M3U GENERATION - Create playlist file with track paths
    # ============================================================================

    def _generate_m3u_if_needed(
        self,
        base_path: Path,
        playlist_name: str,
        playlist_info: PlaylistInfo,
        results: list[DownloadResult],
        skip_album_m3u: bool,
        skip_playlist_m3u: bool = False,
    ) -> Path | None:
        """Generate M3U playlist file if appropriate for this content type.

        Why skip albums: Albums have an inherent folder structure with all tracks
        in a single directory. An M3U file is redundant for albums but valuable
        for curated playlists where tracks span multiple albums/folders.

        Args:
            base_path: Base directory for output files.
            playlist_name: Name to use for the M3U file.
            playlist_info: Playlist metadata (content type, etc.).
            results: Download results to include in M3U.
            skip_album_m3u: Whether to skip M3U generation for albums.
            skip_playlist_m3u: Whether to skip M3U generation for playlists.

        Returns:
            Path to generated M3U file, or None if skipped/failed/no tracks.
        """
        # Skip albums if configured (they have inherent folder structure)
        if skip_album_m3u and playlist_info.kind == ContentKind.ALBUM:
            logger.debug("Skipping M3U for album: %s", playlist_info.playlist_id)
            return None

        # Skip regular playlists if configured
        if skip_playlist_m3u and playlist_info.kind == ContentKind.PLAYLIST:
            logger.debug("Skipping M3U for playlist: %s", playlist_info.playlist_id)
            return None

        # Skip single tracks (M3U is for playlists, not individual tracks)
        if playlist_info.kind == ContentKind.TRACK:
            logger.debug("Skipping M3U for single track: %s", playlist_info.playlist_id)
            return None

        tracks = self._collect_successful_tracks_for_playlist(results)
        if not tracks:
            logger.debug("No tracks available for M3U generation")
            return None

        m3u_path = write_m3u(
            base_path,
            playlist_name,
            playlist_info.playlist_id,
            tracks,
            ascii_filenames=self._ascii_filenames,
        )
        logger.info(
            "Generated M3U playlist: '%s'",
            m3u_path,
            extra={"file_type": "m3u", "file_path": str(m3u_path)},
        )
        return m3u_path

    def _collect_successful_tracks_for_playlist(
        self, results: list[DownloadResult]
    ) -> list[tuple[TrackMetadata, Path]]:
        """Collect successful and skipped tracks for playlist inclusion.

        Why include both SUCCESS and SKIPPED: Both represent tracks that exist
        on disk and should appear in the final playlist. SUCCESS means we just
        downloaded it, SKIPPED means it already existed. Either way, it's part
        of the playlist.

        Why exclude FAILED: Failed downloads don't have output files, so they
        can't be included in the M3U playlist (no path to reference).

        Args:
            results: Download results to filter.

        Returns:
            List of (track_metadata, output_path) tuples for M3U generation.
            Only includes tracks with SUCCESS or SKIPPED status.
        """
        return [
            (result.track, result.output_path)
            for result in results
            if result.status in (DownloadStatus.SUCCESS, DownloadStatus.SKIPPED)
            and result.output_path
        ]

    # ============================================================================
    # COVER GENERATION - Save playlist cover image
    # ============================================================================

    def _save_cover_if_available(
        self,
        base_path: Path,
        playlist_name: str,
        playlist_info: PlaylistInfo,
        stem: str | None = None,
    ) -> Path | None:
        """Save playlist cover image as a sidecar JPEG file.

        Why sidecar files: Storing the cover as a separate file (e.g.,
        "playlist.jpg") allows music players and file browsers to display
        playlist artwork without parsing audio files.

        Args:
            base_path: Base directory for output files.
            playlist_name: Name to use for the cover file.
            playlist_info: Playlist metadata (cover URL, etc.).
            stem: Filename to use, normally the M3U's. Resolved like the M3U
                when no M3U was written.

        Returns:
            Path to saved cover file, or None if no cover URL or save failed.
        """
        if not playlist_info.cover_url:
            return None

        # Skip single tracks (cover saved with the track file, not separately)
        if playlist_info.kind == ContentKind.TRACK:
            logger.debug(
                "Skipping playlist cover for single track: %s",
                playlist_info.playlist_id,
            )
            return None

        if stem is None:
            stem = resolve_playlist_stem(
                base_path / "_Playlists",
                playlist_name,
                playlist_info.playlist_id,
                ascii_filenames=self._ascii_filenames,
            )
        cover_path = write_playlist_cover(
            base_path,
            playlist_name,
            playlist_info.playlist_id,
            playlist_info.cover_url,
            ascii_filenames=self._ascii_filenames,
            stem=stem,
        )
        if cover_path:
            logger.info(
                "Saved cover: '%s'",
                cover_path,
                extra={"file_type": "cover", "file_path": str(cover_path)},
            )
        return cover_path
