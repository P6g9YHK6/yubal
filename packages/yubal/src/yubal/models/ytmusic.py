"""Models for parsing ytmusicapi responses.

These are internal models used to parse and validate responses from
the YouTube Music API. They may change if the API changes.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Album",
    "AlbumRef",
    "AlbumTrack",
    "Artist",
    "ArtistDiscography",
    "Playlist",
    "PlaylistTrack",
    "SearchResult",
    "Thumbnail",
]


class YTMusicModel(BaseModel):
    """Base model for ytmusicapi responses."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class Thumbnail(YTMusicModel):
    """Video/album thumbnail."""

    url: str
    width: int
    height: int


class Artist(YTMusicModel):
    """Artist reference."""

    name: str
    id: str | None = None


class AlbumRef(YTMusicModel):
    """Album reference (in playlist/search results)."""

    id: str | None = None
    name: str


class PlaylistTrack(YTMusicModel):
    """Track in a playlist."""

    video_id: str = Field(alias="videoId")
    video_type: str | None = Field(default=None, alias="videoType")
    title: str
    artists: list[Artist] = Field(default_factory=list)
    album: AlbumRef | None = None
    thumbnails: list[Thumbnail] = Field(default_factory=list)
    duration_seconds: int


class Playlist(YTMusicModel):
    """Playlist response from get_playlist()."""

    title: str | None = None
    thumbnails: list[Thumbnail] = Field(default_factory=list)
    tracks: list[PlaylistTrack]
    unavailable_tracks_raw: list[dict[str, Any]] = Field(
        default_factory=list, alias="unavailable_tracks"
    )
    author: Artist | None = None  # Channel/creator name
    year: str | None = None

    @property
    def unavailable_count(self) -> int:
        """Number of unavailable tracks in the playlist."""
        return len(self.unavailable_tracks_raw)


class AlbumTrack(YTMusicModel):
    """Track in an album."""

    video_id: str = Field(alias="videoId")
    title: str
    artists: list[Artist] = Field(default_factory=list)
    track_number: int = Field(alias="trackNumber")
    duration_seconds: int


class Album(YTMusicModel):
    """Album response from get_album()."""

    title: str
    artists: list[Artist]
    year: str | None = None
    thumbnails: list[Thumbnail]
    tracks: list[AlbumTrack]
    audio_playlist_id: str | None = Field(default=None, alias="audioPlaylistId")


class ArtistDiscography(YTMusicModel):
    """An artist's full discography, assembled from get_artist()/get_artist_albums().

    Unlike other models in this module, this isn't validated directly from a
    single raw API response — it's built by YTMusicClient.get_artist_albums()
    from one or more combined ytmusicapi calls (the artist page plus any
    paginated album/single sections).
    """

    name: str
    thumbnail_url: str | None = None
    album_browse_ids: list[str] = Field(default_factory=list)


class SearchResult(YTMusicModel):
    """Song search result."""

    video_id: str = Field(alias="videoId")
    video_type: str | None = Field(default=None, alias="videoType")
    title: str
    artists: list[Artist] = Field(default_factory=list)
    album: AlbumRef | None = None
