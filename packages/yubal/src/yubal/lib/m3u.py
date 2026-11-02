"""M3U playlist file generation utilities.

M3U files are written with UTF-8 encoding, which is the modern standard
supported by most media players.
"""

import logging
from os.path import relpath
from pathlib import Path

from yubal.models.track import TrackMetadata
from yubal.utils.filename import format_playlist_filename, format_playlist_name

logger = logging.getLogger(__name__)

# Comment line marking which playlist an M3U belongs to, so a later sync can
# tell its own file from a different playlist that happens to share the name.
# Players treat it like any other comment.
PLAYLIST_ID_TAG = "#YUBAL-ID:"

# YouTube Music gives every album release a playlist ID with this prefix.
ALBUM_PLAYLIST_ID_PREFIX = "OLAK5uy_"


def is_album_playlist_id(playlist_id: str) -> bool:
    """Return whether a playlist ID belongs to an album release."""
    return playlist_id.startswith(ALBUM_PLAYLIST_ID_PREFIX)


def read_m3u_playlist_id(m3u_path: Path) -> str | None:
    """Return the playlist ID tagged in an M3U file, or None if untagged."""
    try:
        with m3u_path.open(encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(5), f, strict=False):
                if line.startswith(PLAYLIST_ID_TAG):
                    return line[len(PLAYLIST_ID_TAG) :].strip()
    except OSError:
        return None
    return None


def _relative_track_path(file_path: Path, m3u_dir: Path) -> str:
    """Return a track's path relative to the M3U's directory."""
    # Note: pathlib.Path.relative_to() doesn't support going up with '..'
    # so we use os.path.relpath which handles this correctly
    try:
        return relpath(file_path, m3u_dir)
    except ValueError:
        # Fall back to absolute if on different drives (Windows)
        return str(file_path)


def _has_same_tracks(m3u_path: Path, track_paths: list[Path]) -> bool:
    """Return whether an M3U already lists exactly these tracks, in any order."""
    if not track_paths:
        return False
    try:
        lines = m3u_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    existing = {line for line in lines if line and not line.startswith("#")}
    return existing == {_relative_track_path(p, m3u_path.parent) for p in track_paths}


def resolve_playlist_stem(
    playlists_dir: Path,
    playlist_name: str,
    playlist_id: str,
    *,
    ascii_filenames: bool = False,
    track_paths: list[Path] | None = None,
) -> str:
    """Pick the filename (without extension) for a playlist's M3U and cover.

    Players like Jellyfin show the filename rather than ``#PLAYLIST:``, so the
    plain name is used whenever it is free or already ours. The ID suffix is
    only added when a different playlist owns the plain name. YouTube Music can
    publish one album under several IDs, so an album owning the name with the
    same tracks counts as the same album rather than a clash.
    """
    stem = format_playlist_name(playlist_name, ascii_filenames=ascii_filenames)
    existing = playlists_dir / f"{stem}.m3u"
    if not existing.exists():
        return stem
    owner_id = read_m3u_playlist_id(existing)
    if owner_id == playlist_id:
        return stem
    if (
        owner_id
        and is_album_playlist_id(owner_id)
        and is_album_playlist_id(playlist_id)
        and _has_same_tracks(existing, track_paths or [])
    ):
        return stem
    return format_playlist_filename(
        playlist_name, playlist_id, ascii_filenames=ascii_filenames
    )


def generate_m3u(
    tracks: list[tuple[TrackMetadata, Path]],
    m3u_path: Path,
    playlist_name: str,
    playlist_id: str | None = None,
) -> str:
    """Generate M3U playlist content with paths relative to the M3U file location.

    Creates an extended M3U format file with track duration and title information.
    Emits a ``#PLAYLIST:`` directive so players (Navidrome, VLC, Plex, MPD, etc.)
    display the playlist by its name rather than by the on-disk filename, which
    may carry a collision-avoiding ID suffix.

    Args:
        tracks: List of tuples containing (TrackMetadata, file_path) for each track.
        m3u_path: Path where the M3U file will be written (used for relative paths).
        playlist_name: Human-readable playlist name (used for ``#PLAYLIST:``).
        playlist_id: Playlist ID written as a ``#YUBAL-ID:`` comment, if given.

    Returns:
        M3U file content as a string.

    Example:
        >>> from pathlib import Path
        >>> tracks = [(track_meta, Path("/music/Artist/2024 - Album/01 - Song.opus"))]
        >>> m3u_path = Path("/music/Playlists/My Playlist.m3u")
        >>> content = generate_m3u(tracks, m3u_path, "My Playlist")
        >>> print(content)
        #EXTM3U
        #PLAYLIST:My Playlist
        #EXTINF:-1,Artist One; Artist Two - Song Title
        ../Artist/2024 - Album/01 - Song.opus
    """
    lines = ["#EXTM3U", f"#PLAYLIST:{playlist_name}"]
    if playlist_id:
        lines.append(f"{PLAYLIST_ID_TAG}{playlist_id}")

    for track, file_path in tracks:
        # Get duration from track metadata, use -1 if unknown
        duration = track.duration_seconds if track.duration_seconds is not None else -1

        # Format: Artist - Title
        display_title = f"{track.artist} - {track.title}"

        # EXTINF line: #EXTINF:duration,display title
        lines.append(f"#EXTINF:{duration},{display_title}")

        # Relative path from M3U file location to track file
        lines.append(_relative_track_path(file_path, m3u_path.parent))

    # Ensure trailing newline
    return "\n".join(lines) + "\n"


def write_m3u(
    base_path: Path,
    playlist_name: str,
    playlist_id: str,
    tracks: list[tuple[TrackMetadata, Path]],
    *,
    ascii_filenames: bool = False,
) -> Path:
    """Write an M3U playlist file to the Playlists folder.

    Creates the Playlists directory if it doesn't exist.
    Sanitizes the playlist name for safe filesystem usage.
    Appends a truncated playlist ID only when another playlist already
    owns the plain name.

    Args:
        base_path: Base directory for downloads (e.g., /music or ./data).
        playlist_name: Name of the playlist (will be sanitized for filename).
        playlist_id: Unique playlist ID (tagged in the file, last 8 chars
            appended to the filename on a name clash).
        tracks: List of tuples containing (TrackMetadata, file_path) for each track.
        ascii_filenames: If True, transliterate unicode to ASCII in filenames.

    Returns:
        Path to the written M3U file.

    Example:
        >>> from pathlib import Path
        >>> tracks = [(track_meta, Path("/music/Artist/2024 - Album/01 - Song.opus"))]
        >>> m3u_path = write_m3u(Path("/music"), "My Favorites", "PLxyz123abc", tracks)
        >>> print(m3u_path)
        /music/_Playlists/My Favorites.m3u
    """
    # Create _Playlists directory
    playlists_dir = base_path / "_Playlists"
    playlists_dir.mkdir(parents=True, exist_ok=True)

    filename = resolve_playlist_stem(
        playlists_dir,
        playlist_name,
        playlist_id,
        ascii_filenames=ascii_filenames,
        track_paths=[path for _, path in tracks],
    )
    m3u_path = playlists_dir / f"{filename}.m3u"

    # Generate and write content
    content = generate_m3u(tracks, m3u_path, playlist_name, playlist_id)
    m3u_path.write_text(content, encoding="utf-8")

    return m3u_path
