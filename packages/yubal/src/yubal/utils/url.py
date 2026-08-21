"""URL parsing utilities."""

import re
from urllib.parse import urlparse

from yubal.exceptions import PlaylistParseError

PLAYLIST_ID_PATTERN = re.compile(r"list=([A-Za-z0-9_-]+)")
VIDEO_ID_PATTERN = re.compile(r"v=([A-Za-z0-9_-]+)")

# Path-based video ID patterns (youtu.be, shorts, live, embed)
_PATH_VIDEO_ID_PATTERN = re.compile(r"^/(?:shorts|live|embed|e|v|vi)/([A-Za-z0-9_-]+)")

# Channel ID and handle path patterns (artist/channel pages)
_CHANNEL_ID_PATH_PATTERN = re.compile(r"^/channel/([A-Za-z0-9_-]+)")
_HANDLE_PATH_PATTERN = re.compile(r"^/@([A-Za-z0-9_.-]+)")

# Recognized YouTube hostnames for path-based video ID extraction
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

# Hostnames that serve channel/artist pages (no dedicated -nocookie embeds
# for channels, so this is a narrower set than _YOUTUBE_HOSTS)
_CHANNEL_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
}

# Maximum URL length to prevent potential abuse (standard browser limit)
MAX_URL_LENGTH = 2048


def _parse_video_id_from_path(url: str) -> str | None:
    """Extract video ID from path-based YouTube URLs.

    Handles youtu.be short URLs and path-based formats like /shorts/, /live/,
    /embed/, /e/, /v/, /vi/ on YouTube domains.

    Args:
        url: Full URL to parse.

    Returns:
        Video ID string, or None if not a recognized path-based URL.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or ""

    # youtu.be/VIDEO_ID
    if host == "youtu.be" and len(path) > 1:
        # Path is /VIDEO_ID — strip leading slash
        video_id = path.split("/")[1]
        if re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
            return video_id
        return None

    # /shorts/ID, /live/ID, /embed/ID, /e/ID, /v/ID, /vi/ID on YouTube hosts
    if host in _YOUTUBE_HOSTS:
        if match := _PATH_VIDEO_ID_PATTERN.match(path):
            return match.group(1)

    return None


def parse_playlist_id(url: str) -> str:
    """Extract playlist ID from YouTube Music URL.

    Args:
        url: Full YouTube Music playlist URL.

    Returns:
        The playlist ID string.

    Raises:
        PlaylistParseError: If playlist ID cannot be extracted or URL is too long.
    """
    if not url or len(url) > MAX_URL_LENGTH:
        raise PlaylistParseError(f"Could not extract playlist ID from: {url}")
    if match := PLAYLIST_ID_PATTERN.search(url):
        return match.group(1)
    raise PlaylistParseError(f"Could not extract playlist ID from: {url}")


def parse_video_id(url: str) -> str | None:
    """Extract video ID from a YouTube URL.

    Supports standard watch URLs (v= parameter), youtu.be short URLs,
    and path-based formats (/shorts/, /live/, /embed/, /e/, /v/, /vi/).

    Args:
        url: YouTube, YouTube Music, or youtu.be URL.

    Returns:
        The video ID string, or None if not found or URL is too long.
    """
    # Validate URL length
    if not url or len(url) > MAX_URL_LENGTH:
        return None

    # Extract video ID from v= parameter
    if match := VIDEO_ID_PATTERN.search(url):
        return match.group(1)

    # Extract video ID from path-based URLs (youtu.be, shorts, live, embed)
    return _parse_video_id_from_path(url)


def parse_channel_id(url: str) -> str | None:
    """Extract a channel ID from a YouTube Music artist/channel URL.

    Only handles the direct `/channel/UC...` form, which requires no
    network access. Handle URLs (`/@name`) cannot be resolved here since
    resolving them requires a network lookup — use
    `YTMusicClient.resolve_channel_id()` for those.

    Args:
        url: YouTube or YouTube Music URL.

    Returns:
        The channel ID string, or None if the URL isn't a `/channel/` URL.
    """
    if not url or len(url) > MAX_URL_LENGTH:
        return None

    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or ""

    if host not in _CHANNEL_HOSTS:
        return None
    if match := _CHANNEL_ID_PATH_PATTERN.match(path):
        return match.group(1)
    return None


def is_handle_url(url: str) -> bool:
    """Check if URL is a YouTube Music handle URL (e.g. `/@name`).

    Args:
        url: YouTube or YouTube Music URL.

    Returns:
        True if the URL is a handle-shaped channel URL, False otherwise.
    """
    if not url or len(url) > MAX_URL_LENGTH:
        return False

    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or ""

    if host not in _CHANNEL_HOSTS:
        return False
    return bool(_HANDLE_PATH_PATTERN.match(path))


def is_artist_url(url: str) -> bool:
    """Check if URL points to an artist/channel page (either form).

    Args:
        url: YouTube or YouTube Music URL.

    Returns:
        True if the URL is a `/channel/UC...` or `/@handle` URL.
    """
    return parse_channel_id(url) is not None or is_handle_url(url)


def is_single_track_url(url: str) -> bool:
    """Check if URL is a single track (not a playlist).

    Args:
        url: YouTube or YouTube Music URL.

    Returns:
        True if the URL is a single track URL, False otherwise.
    """
    return parse_video_id(url) is not None


def is_supported_url(url: str) -> bool:
    """Check if URL is supported by yubal (playlist, album, or single track).

    Args:
        url: YouTube or YouTube Music URL.

    Returns:
        True if the URL can be processed by yubal, False otherwise.
    """
    if not url or len(url) > MAX_URL_LENGTH:
        return False

    url = url.strip()

    # Playlist URL (has list= parameter)
    if PLAYLIST_ID_PATTERN.search(url):
        return True
    # Single track URL (has v= parameter without list=)
    if VIDEO_ID_PATTERN.search(url):
        return True
    # Path-based video URL (youtu.be, shorts, live, embed)
    if _parse_video_id_from_path(url):
        return True
    # Browse URL (album pages on music.youtube.com)
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or ""
    if "/browse/" in path and host == "music.youtube.com":
        return True
    # Artist/channel URL (channel ID or handle)
    if is_artist_url(url):
        return True
    return False
