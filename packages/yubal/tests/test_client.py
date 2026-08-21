"""Tests for YTMusicClient."""

from unittest.mock import MagicMock, patch

import pytest
from ytmusicapi.auth.types import AuthType
from ytmusicapi.exceptions import YTMusicServerError
from yubal.client import YTMusicClient
from yubal.exceptions import (
    ArtistNotFoundError,
    AuthenticationRequiredError,
    ChannelParseError,
    TrackNotFoundError,
    UpstreamAPIError,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_ytmusic() -> MagicMock:
    """Create a mock YTMusic instance."""
    return MagicMock()


@pytest.fixture
def sample_album_data() -> dict:
    """Create sample album API response data."""
    return {
        "title": "Test Album",
        "artists": [{"name": "Test Artist", "id": "artist123"}],
        "year": "2024",
        "thumbnails": [
            {"url": "https://example.com/thumb.jpg", "width": 544, "height": 544}
        ],
        "tracks": [
            {
                "videoId": "video123",
                "title": "Test Song",
                "artists": [{"name": "Test Artist", "id": "artist123"}],
                "trackNumber": 1,
                "duration_seconds": 240,
            }
        ],
    }


# ============================================================================
# Album Caching Tests
# ============================================================================


class TestAlbumCaching:
    """Tests for album caching in YTMusicClient."""

    def test_caches_album_on_first_fetch(
        self, mock_ytmusic: MagicMock, sample_album_data: dict
    ) -> None:
        """Should cache album after first fetch."""
        mock_ytmusic.get_album.return_value = sample_album_data
        client = YTMusicClient(ytmusic=mock_ytmusic)

        assert client.get_album_cache_size() == 0

        client.get_album("album123")

        assert client.get_album_cache_size() == 1

    def test_returns_cached_album_on_second_fetch(
        self, mock_ytmusic: MagicMock, sample_album_data: dict
    ) -> None:
        """Should return cached album without API call on second fetch."""
        mock_ytmusic.get_album.return_value = sample_album_data
        client = YTMusicClient(ytmusic=mock_ytmusic)

        # First fetch
        album1 = client.get_album("album123")
        # Second fetch
        album2 = client.get_album("album123")

        assert album1.title == album2.title
        # API should only be called once
        assert mock_ytmusic.get_album.call_count == 1

    def test_different_albums_cached_separately(
        self, mock_ytmusic: MagicMock, sample_album_data: dict
    ) -> None:
        """Should cache different albums separately."""
        album_data_2 = {
            **sample_album_data,
            "title": "Another Album",
        }
        mock_ytmusic.get_album.side_effect = [sample_album_data, album_data_2]
        client = YTMusicClient(ytmusic=mock_ytmusic)

        album1 = client.get_album("album123")
        album2 = client.get_album("album456")

        assert album1.title == "Test Album"
        assert album2.title == "Another Album"
        assert client.get_album_cache_size() == 2
        assert mock_ytmusic.get_album.call_count == 2

    def test_clear_album_cache(
        self, mock_ytmusic: MagicMock, sample_album_data: dict
    ) -> None:
        """Should clear all cached albums."""
        mock_ytmusic.get_album.return_value = sample_album_data
        client = YTMusicClient(ytmusic=mock_ytmusic)

        client.get_album("album123")
        assert client.get_album_cache_size() == 1

        client.clear_album_cache()
        assert client.get_album_cache_size() == 0

    def test_refetches_after_cache_clear(
        self, mock_ytmusic: MagicMock, sample_album_data: dict
    ) -> None:
        """Should refetch album after cache is cleared."""
        mock_ytmusic.get_album.return_value = sample_album_data
        client = YTMusicClient(ytmusic=mock_ytmusic)

        client.get_album("album123")
        client.clear_album_cache()
        client.get_album("album123")

        # API should be called twice (once before clear, once after)
        assert mock_ytmusic.get_album.call_count == 2

    def test_cache_is_per_client_instance(
        self, mock_ytmusic: MagicMock, sample_album_data: dict
    ) -> None:
        """Cache should be isolated per client instance."""
        mock_ytmusic.get_album.return_value = sample_album_data
        client1 = YTMusicClient(ytmusic=mock_ytmusic)
        client2 = YTMusicClient(ytmusic=mock_ytmusic)

        client1.get_album("album123")

        assert client1.get_album_cache_size() == 1
        assert client2.get_album_cache_size() == 0


# ============================================================================
# get_track() Tests
# ============================================================================


class TestGetTrack:
    def test_returns_playlist_track_for_valid_video_id(self) -> None:
        mock_ytm = MagicMock()
        mock_ytm.get_watch_playlist.return_value = {
            "tracks": [
                {
                    "videoId": "Vgpv5PtWsn4",
                    "title": "A COLD PLAY",
                    "artists": [{"name": "The Kid LAROI", "id": "UC123"}],
                    "album": {"name": "A COLD PLAY", "id": "MPREb_123"},
                    "videoType": "MUSIC_VIDEO_TYPE_ATV",
                    "thumbnails": [
                        {
                            "url": "https://example.com/thumb.jpg",
                            "width": 120,
                            "height": 120,
                        }
                    ],
                    "duration_seconds": 180,
                }
            ]
        }
        client = YTMusicClient(ytmusic=mock_ytm)
        track = client.get_track("Vgpv5PtWsn4")
        assert track.video_id == "Vgpv5PtWsn4"
        assert track.title == "A COLD PLAY"
        mock_ytm.get_watch_playlist.assert_called_once_with("Vgpv5PtWsn4")

    def test_raises_for_empty_video_id(self) -> None:
        client = YTMusicClient(ytmusic=MagicMock())
        with pytest.raises(ValueError, match="video_id cannot be empty"):
            client.get_track("")

    def test_raises_track_not_found_for_empty_tracks(self) -> None:
        mock_ytm = MagicMock()
        mock_ytm.get_watch_playlist.return_value = {"tracks": []}
        client = YTMusicClient(ytmusic=mock_ytm)
        with pytest.raises(TrackNotFoundError, match="Track not found"):
            client.get_track("invalid123")

    def test_raises_track_not_found_for_none_tracks(self) -> None:
        mock_ytm = MagicMock()
        mock_ytm.get_watch_playlist.return_value = {"tracks": None}
        client = YTMusicClient(ytmusic=mock_ytm)
        with pytest.raises(TrackNotFoundError, match="Track not found"):
            client.get_track("invalid123")

    def test_raises_api_error_on_exception(self) -> None:
        from ytmusicapi.exceptions import YTMusicServerError

        mock_ytm = MagicMock()
        mock_ytm.get_watch_playlist.side_effect = YTMusicServerError("API failure")
        client = YTMusicClient(ytmusic=mock_ytm)
        with pytest.raises(UpstreamAPIError, match="Failed to fetch track"):
            client.get_track("abc123")

    def test_normalizes_watch_playlist_response_format(self) -> None:
        """Should normalize thumbnail->thumbnails and length->duration_seconds."""
        mock_ytm = MagicMock()
        mock_ytm.get_watch_playlist.return_value = {
            "tracks": [
                {
                    "videoId": "Vgpv5PtWsn4",
                    "title": "A COLD PLAY",
                    "artists": [{"name": "The Kid LAROI", "id": "UC123"}],
                    "album": {"name": "A COLD PLAY", "id": "MPREb_123"},
                    "videoType": "MUSIC_VIDEO_TYPE_ATV",
                    "thumbnail": [
                        {
                            "url": "https://example.com/thumb.jpg",
                            "width": 544,
                            "height": 544,
                        }
                    ],
                    "length": "3:00",
                }
            ]
        }
        client = YTMusicClient(ytmusic=mock_ytm)
        track = client.get_track("Vgpv5PtWsn4")
        assert track.video_id == "Vgpv5PtWsn4"
        assert track.duration_seconds == 180
        assert len(track.thumbnails) == 1
        assert track.thumbnails[0].url == "https://example.com/thumb.jpg"


class TestGetPlaylist:
    def test_normalizes_null_artists_to_empty_list(self) -> None:
        mock_ytm = MagicMock()
        mock_ytm.get_playlist.return_value = {
            "title": "Liked Music",
            "tracks": [
                {
                    "videoId": "abc123",
                    "videoType": "MUSIC_VIDEO_TYPE_ATV",
                    "title": "Track With Null Artists",
                    "artists": None,
                    "thumbnails": [
                        {
                            "url": "https://example.com/thumb.jpg",
                            "width": 120,
                            "height": 120,
                        }
                    ],
                    "duration_seconds": 180,
                }
            ],
        }

        client = YTMusicClient(ytmusic=mock_ytm)

        playlist = client.get_playlist("LM")

        assert len(playlist.tracks) == 1
        assert playlist.tracks[0].artists == []


class TestLikedMusic:
    """Tests for the Liked Music (LM) pseudo-playlist."""

    def test_unauthenticated_client_raises_auth_required(self) -> None:
        """Without auth, fetching LM should fail fast with a clear message."""
        mock_ytm = MagicMock()
        mock_ytm.auth_type = AuthType.UNAUTHORIZED
        client = YTMusicClient(ytmusic=mock_ytm)

        with pytest.raises(AuthenticationRequiredError, match="Liked Music"):
            client.get_playlist("LM")

        # The network call must not be attempted when auth is missing.
        mock_ytm.get_playlist.assert_not_called()

    def test_authenticated_client_fetches_liked_music(self) -> None:
        """With auth, LM is fetched via get_playlist (matching ytmusicapi's
        own get_liked_songs implementation)."""
        mock_ytm = MagicMock()
        mock_ytm.auth_type = AuthType.BROWSER
        mock_ytm.get_playlist.return_value = {
            "title": "Your Likes",
            "tracks": [
                {
                    "videoId": "abc123",
                    "videoType": "MUSIC_VIDEO_TYPE_ATV",
                    "title": "A Liked Song",
                    "artists": [{"name": "Some Artist", "id": "UC1"}],
                    "thumbnails": [
                        {
                            "url": "https://example.com/t.jpg",
                            "width": 120,
                            "height": 120,
                        }
                    ],
                    "duration_seconds": 200,
                }
            ],
        }
        client = YTMusicClient(ytmusic=mock_ytm)

        playlist = client.get_playlist("LM")

        mock_ytm.get_playlist.assert_called_once_with("LM", limit=None)
        assert playlist.title == "Your Likes"
        assert len(playlist.tracks) == 1
        assert playlist.tracks[0].video_id == "abc123"

    def test_defaults_title_when_missing(self) -> None:
        """If the LM response has no title, default to 'Liked Music'."""
        mock_ytm = MagicMock()
        mock_ytm.auth_type = AuthType.BROWSER
        mock_ytm.get_playlist.return_value = {"title": None, "tracks": []}
        client = YTMusicClient(ytmusic=mock_ytm)

        playlist = client.get_playlist("LM")

        assert playlist.title == "Liked Music"


class TestGetLyricsBrowseId:
    def test_returns_browse_id_when_present(self) -> None:
        mock_ytm = MagicMock()
        mock_ytm.get_watch_playlist.return_value = {"lyrics": "MPLYt_browse_id"}
        client = YTMusicClient(ytmusic=mock_ytm)
        assert client.get_lyrics_browse_id("V9PVRfjEBTI") == "MPLYt_browse_id"

    def test_returns_none_when_lyrics_key_missing(self) -> None:
        mock_ytm = MagicMock()
        mock_ytm.get_watch_playlist.return_value = {"tracks": []}
        client = YTMusicClient(ytmusic=mock_ytm)
        assert client.get_lyrics_browse_id("V9PVRfjEBTI") is None

    def test_returns_none_when_lyrics_key_is_none(self) -> None:
        mock_ytm = MagicMock()
        mock_ytm.get_watch_playlist.return_value = {"lyrics": None}
        client = YTMusicClient(ytmusic=mock_ytm)
        assert client.get_lyrics_browse_id("V9PVRfjEBTI") is None

    def test_swallows_api_error(self) -> None:
        mock_ytm = MagicMock()
        mock_ytm.get_watch_playlist.side_effect = YTMusicServerError("boom")
        client = YTMusicClient(ytmusic=mock_ytm)
        assert client.get_lyrics_browse_id("V9PVRfjEBTI") is None

    def test_empty_video_id_returns_none_without_call(self) -> None:
        mock_ytm = MagicMock()
        client = YTMusicClient(ytmusic=mock_ytm)
        assert client.get_lyrics_browse_id("") is None
        mock_ytm.get_watch_playlist.assert_not_called()


class TestGetLyrics:
    def test_returns_payload_dict(self) -> None:
        mock_ytm = MagicMock()
        payload = {
            "lyrics": "line one\nline two",
            "source": "Source: LyricFind",
            "hasTimestamps": False,
        }
        mock_ytm.get_lyrics.return_value = payload
        client = YTMusicClient(ytmusic=mock_ytm)
        assert client.get_lyrics("MPLYt_abc") == payload
        mock_ytm.get_lyrics.assert_called_once_with("MPLYt_abc", timestamps=True)

    def test_swallows_api_error(self) -> None:
        mock_ytm = MagicMock()
        mock_ytm.get_lyrics.side_effect = YTMusicServerError("nope")
        client = YTMusicClient(ytmusic=mock_ytm)
        assert client.get_lyrics("MPLYt_abc") is None

    def test_returns_none_for_non_dict(self) -> None:
        mock_ytm = MagicMock()
        mock_ytm.get_lyrics.return_value = None
        client = YTMusicClient(ytmusic=mock_ytm)
        assert client.get_lyrics("MPLYt_abc") is None

    def test_empty_browse_id_returns_none_without_call(self) -> None:
        mock_ytm = MagicMock()
        client = YTMusicClient(ytmusic=mock_ytm)
        assert client.get_lyrics("") is None
        mock_ytm.get_lyrics.assert_not_called()


# ============================================================================
# Artist / Channel Tests
# ============================================================================


@pytest.fixture
def sample_artist_data() -> dict:
    """Create sample get_artist() API response data."""
    return {
        "name": "Oasis",
        "thumbnails": [
            {"url": "https://example.com/artist.jpg", "width": 544, "height": 544}
        ],
        "albums": {
            "results": [
                {"title": "Familiar To Millions", "browseId": "MPREb_album1"},
            ],
            "browseId": "UCmMUZbaYdNH0bEd1PAlAqsA",
            "params": "albums_params",
        },
        "singles": {
            "results": [
                {"title": "Stand By Me", "browseId": "MPREb_single1"},
            ],
            "browseId": "UCmMUZbaYdNH0bEd1PAlAqsA",
            "params": "singles_params",
        },
    }


class TestResolveChannelId:
    """Tests for YTMusicClient.resolve_channel_id."""

    def test_returns_id_directly_for_channel_url(self) -> None:
        mock_ytm = MagicMock()
        client = YTMusicClient(ytmusic=mock_ytm)
        url = "https://music.youtube.com/channel/UC6pSrcsTD4kLFT9SQ2xNx3A"
        assert client.resolve_channel_id(url) == "UC6pSrcsTD4kLFT9SQ2xNx3A"

    def test_resolves_handle_via_yt_dlp(self) -> None:
        mock_ytm = MagicMock()
        client = YTMusicClient(ytmusic=mock_ytm)

        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_instance = mock_ydl.return_value.__enter__.return_value
            mock_instance.extract_info.return_value = {"channel_id": "UCresolved"}

            channel_id = client.resolve_channel_id(
                "https://music.youtube.com/@PinkGuy-l1q"
            )

        assert channel_id == "UCresolved"

    def test_raises_for_unresolvable_handle(self) -> None:
        mock_ytm = MagicMock()
        client = YTMusicClient(ytmusic=mock_ytm)

        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_instance = mock_ydl.return_value.__enter__.return_value
            mock_instance.extract_info.return_value = {}

            with pytest.raises(ChannelParseError):
                client.resolve_channel_id("https://music.youtube.com/@unknown")

    def test_wraps_yt_dlp_errors(self) -> None:
        mock_ytm = MagicMock()
        client = YTMusicClient(ytmusic=mock_ytm)

        with patch("yt_dlp.YoutubeDL") as mock_ydl:
            mock_instance = mock_ydl.return_value.__enter__.return_value
            mock_instance.extract_info.side_effect = Exception("network error")

            with pytest.raises(UpstreamAPIError):
                client.resolve_channel_id("https://music.youtube.com/@unknown")

    def test_raises_for_non_channel_url(self) -> None:
        mock_ytm = MagicMock()
        client = YTMusicClient(ytmusic=mock_ytm)

        with pytest.raises(ChannelParseError):
            client.resolve_channel_id("https://music.youtube.com/playlist?list=PL")


class TestGetArtistSummary:
    """Tests for YTMusicClient.get_artist_summary."""

    def test_returns_name_and_thumbnail(
        self, mock_ytmusic: MagicMock, sample_artist_data: dict
    ) -> None:
        mock_ytmusic.get_artist.return_value = sample_artist_data
        client = YTMusicClient(ytmusic=mock_ytmusic)

        name, thumbnail_url = client.get_artist_summary("UC123")

        assert name == "Oasis"
        assert thumbnail_url == "https://example.com/artist.jpg"
        mock_ytmusic.get_artist_albums.assert_not_called()

    def test_raises_for_missing_artist(self, mock_ytmusic: MagicMock) -> None:
        mock_ytmusic.get_artist.return_value = {}
        client = YTMusicClient(ytmusic=mock_ytmusic)

        with pytest.raises(ArtistNotFoundError):
            client.get_artist_summary("UC123")

    def test_wraps_api_error(self, mock_ytmusic: MagicMock) -> None:
        mock_ytmusic.get_artist.side_effect = YTMusicServerError("boom")
        client = YTMusicClient(ytmusic=mock_ytmusic)

        with pytest.raises(UpstreamAPIError):
            client.get_artist_summary("UC123")


class TestGetArtistAlbums:
    """Tests for YTMusicClient.get_artist_albums."""

    def test_paginates_albums_and_singles(
        self, mock_ytmusic: MagicMock, sample_artist_data: dict
    ) -> None:
        mock_ytmusic.get_artist.return_value = sample_artist_data

        def fake_get_artist_albums(
            channelId: str, params: str, limit: int | None = None, order: object = None
        ) -> list[dict]:
            if params == "albums_params":
                return [{"browseId": "MPREb_album1"}, {"browseId": "MPREb_album2"}]
            return [{"browseId": "MPREb_single1"}]

        mock_ytmusic.get_artist_albums.side_effect = fake_get_artist_albums
        client = YTMusicClient(ytmusic=mock_ytmusic)

        discography = client.get_artist_albums("UC123")

        assert discography.name == "Oasis"
        assert discography.thumbnail_url == "https://example.com/artist.jpg"
        assert discography.album_browse_ids == [
            "MPREb_album1",
            "MPREb_album2",
            "MPREb_single1",
        ]

    def test_falls_back_to_inline_results_without_params(
        self, mock_ytmusic: MagicMock
    ) -> None:
        mock_ytmusic.get_artist.return_value = {
            "name": "Small Artist",
            "thumbnails": [],
            "albums": {"results": [{"title": "Only Album", "browseId": "MPREb_x"}]},
        }
        client = YTMusicClient(ytmusic=mock_ytmusic)

        discography = client.get_artist_albums("UC123")

        assert discography.album_browse_ids == ["MPREb_x"]
        mock_ytmusic.get_artist_albums.assert_not_called()

    def test_dedupes_browse_ids_across_sections(self, mock_ytmusic: MagicMock) -> None:
        mock_ytmusic.get_artist.return_value = {
            "name": "Artist",
            "thumbnails": [],
            "albums": {"results": [{"title": "A", "browseId": "MPREb_dup"}]},
            "singles": {"results": [{"title": "A dup", "browseId": "MPREb_dup"}]},
        }
        client = YTMusicClient(ytmusic=mock_ytmusic)

        discography = client.get_artist_albums("UC123")

        assert discography.album_browse_ids == ["MPREb_dup"]

    def test_raises_for_missing_artist(self, mock_ytmusic: MagicMock) -> None:
        mock_ytmusic.get_artist.return_value = None
        client = YTMusicClient(ytmusic=mock_ytmusic)

        with pytest.raises(ArtistNotFoundError):
            client.get_artist_albums("UC123")
