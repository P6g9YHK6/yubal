"""Configuration for yubal."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class AudioCodec(StrEnum):
    """Supported audio output codecs."""

    OPUS = "opus"
    MP3 = "mp3"
    M4A = "m4a"


@dataclass(frozen=True)
class APIConfig:
    """YouTube Music API configuration.

    Attributes:
        search_limit: Maximum number of search results to return.
        ignore_spelling: Whether to ignore spelling in search queries.
    """

    search_limit: int = 1
    ignore_spelling: bool = True


@dataclass(frozen=True)
class DownloadConfig:
    """Download service configuration.

    Attributes:
        base_path: Base directory for downloaded files.
        codec: Audio codec for output files.
        quality: Audio quality (0 = best, 10 = worst). Only applies to lossy codecs.
        quiet: Suppress yt-dlp output.
        fetch_lyrics: Whether to fetch lyrics from lrclib.net.
        ytmusic_lyrics_fallback: When fetch_lyrics is enabled, fall back to
            YouTube Music's lyrics if lrclib.net has no match.
        ascii_filenames: Transliterate unicode to ASCII in filenames.
        sleep_interval: Minimum seconds yt-dlp waits before each download.
        max_sleep_interval: Upper bound of the random wait before each download.
        sleep_requests: Seconds yt-dlp waits between extraction requests.
    """

    base_path: Path
    codec: AudioCodec = AudioCodec.OPUS
    quality: int = 0
    quiet: bool = True
    fetch_lyrics: bool = True
    ytmusic_lyrics_fallback: bool = True
    ascii_filenames: bool = False
    download_ugc: bool = False
    sleep_interval: float = 0
    max_sleep_interval: float = 0
    sleep_requests: float = 0


@dataclass(frozen=True)
class PlaylistDownloadConfig:
    """Playlist download service configuration.

    Combines download settings with playlist-specific options.

    Attributes:
        download: Download configuration for tracks.
        generate_m3u: Whether to generate M3U playlist file.
        save_cover: Whether to save playlist cover image.
        skip_album_m3u: Skip M3U generation for album playlists.
        skip_playlist_m3u: Skip M3U generation for regular playlists.
        max_items: Maximum number of tracks to download.
        apply_replaygain: Whether to apply ReplayGain tags using rsgain.
        cache_path: Directory for extraction cache. None disables caching.
    """

    download: DownloadConfig
    generate_m3u: bool = True
    save_cover: bool = True
    skip_album_m3u: bool = True
    skip_playlist_m3u: bool = False
    max_items: int | None = None
    apply_replaygain: bool = True
    cache_path: Path | None = None
