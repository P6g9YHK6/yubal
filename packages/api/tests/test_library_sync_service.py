"""Tests for LibrarySyncService."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from yubal.models.ytmusic import LibraryAlbum, LibraryArtist, LibraryPlaylist
from yubal_api.api.exceptions import SubscriptionConflictError
from yubal_api.services.library_sync_service import LibrarySyncService


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock YTMusicClient."""
    client = MagicMock()
    client.get_library_playlists.return_value = []
    client.get_library_albums.return_value = []
    client.get_library_followed_artists.return_value = []
    return client


@pytest.fixture
def mock_subscription_service() -> MagicMock:
    """Create a mock SubscriptionService."""
    return MagicMock()


@pytest.fixture
def settings() -> MagicMock:
    """Create mock settings with all auto-add flags off by default."""
    settings = MagicMock()
    settings.auto_add_playlists = False
    settings.auto_add_albums = False
    settings.auto_add_artists = False
    return settings


@pytest.fixture
def service(
    mock_client: MagicMock,
    mock_subscription_service: MagicMock,
    settings: MagicMock,
) -> LibrarySyncService:
    return LibrarySyncService(
        client=mock_client,
        subscription_service=mock_subscription_service,
        settings=settings,
        cookies_path=Path("/tmp/cookies.txt"),
    )


class TestScanAndAdd:
    def test_no_flags_enabled_is_a_no_op(
        self, service: LibrarySyncService, mock_client: MagicMock
    ) -> None:
        added = service.scan_and_add()

        assert added == 0
        mock_client.get_library_playlists.assert_not_called()
        mock_client.get_library_albums.assert_not_called()
        mock_client.get_library_followed_artists.assert_not_called()

    def test_no_cookies_skips_scan_entirely(
        self,
        mock_client: MagicMock,
        mock_subscription_service: MagicMock,
        settings: MagicMock,
    ) -> None:
        settings.auto_add_playlists = True
        service = LibrarySyncService(
            client=mock_client,
            subscription_service=mock_subscription_service,
            settings=settings,
            cookies_path=None,
        )

        added = service.scan_and_add()

        assert added == 0
        mock_client.get_library_playlists.assert_not_called()

    def test_auto_add_playlists_creates_subscriptions(
        self,
        service: LibrarySyncService,
        mock_client: MagicMock,
        mock_subscription_service: MagicMock,
        settings: MagicMock,
    ) -> None:
        settings.auto_add_playlists = True
        mock_client.get_library_playlists.return_value = [
            LibraryPlaylist(playlistId="PLone", title="One"),
            LibraryPlaylist(playlistId="PLtwo", title="Two"),
        ]

        added = service.scan_and_add()

        assert added == 2
        called_urls = [
            c.args[0] for c in mock_subscription_service.create.call_args_list
        ]
        assert "https://music.youtube.com/playlist?list=PLone" in called_urls
        assert "https://music.youtube.com/playlist?list=PLtwo" in called_urls

    def test_auto_add_albums_prefers_playlist_id_url(
        self,
        service: LibrarySyncService,
        mock_client: MagicMock,
        mock_subscription_service: MagicMock,
        settings: MagicMock,
    ) -> None:
        settings.auto_add_albums = True
        mock_client.get_library_albums.return_value = [
            LibraryAlbum(browseId="MPREb_1", playlistId="OLAK5uy_1", title="Album"),
            LibraryAlbum(browseId="MPREb_2", title="No Playlist Album"),
        ]

        added = service.scan_and_add()

        assert added == 2
        called_urls = [
            c.args[0] for c in mock_subscription_service.create.call_args_list
        ]
        assert "https://music.youtube.com/playlist?list=OLAK5uy_1" in called_urls
        assert "https://music.youtube.com/browse/MPREb_2" in called_urls

    def test_auto_add_artists_uses_channel_url(
        self,
        service: LibrarySyncService,
        mock_client: MagicMock,
        mock_subscription_service: MagicMock,
        settings: MagicMock,
    ) -> None:
        settings.auto_add_artists = True
        mock_client.get_library_followed_artists.return_value = [
            LibraryArtist(browseId="UCabc", artist="Oasis"),
        ]

        added = service.scan_and_add()

        assert added == 1
        mock_subscription_service.create.assert_called_once_with(
            "https://music.youtube.com/channel/UCabc"
        )

    def test_already_subscribed_item_is_skipped_silently(
        self,
        service: LibrarySyncService,
        mock_client: MagicMock,
        mock_subscription_service: MagicMock,
        settings: MagicMock,
    ) -> None:
        settings.auto_add_playlists = True
        mock_client.get_library_playlists.return_value = [
            LibraryPlaylist(playlistId="PLexisting", title="Existing"),
        ]
        mock_subscription_service.create.side_effect = SubscriptionConflictError(
            "already exists"
        )

        added = service.scan_and_add()

        assert added == 0

    def test_per_item_failure_does_not_abort_scan(
        self,
        service: LibrarySyncService,
        mock_client: MagicMock,
        mock_subscription_service: MagicMock,
        settings: MagicMock,
    ) -> None:
        settings.auto_add_playlists = True
        mock_client.get_library_playlists.return_value = [
            LibraryPlaylist(playlistId="PLbad", title="Bad"),
            LibraryPlaylist(playlistId="PLgood", title="Good"),
        ]
        mock_subscription_service.create.side_effect = [
            RuntimeError("boom"),
            None,
        ]

        added = service.scan_and_add()

        assert added == 1

    def test_fetch_failure_for_one_section_does_not_abort_others(
        self,
        service: LibrarySyncService,
        mock_client: MagicMock,
        mock_subscription_service: MagicMock,
        settings: MagicMock,
    ) -> None:
        settings.auto_add_playlists = True
        settings.auto_add_albums = True
        mock_client.get_library_playlists.side_effect = RuntimeError("boom")
        mock_client.get_library_albums.return_value = [
            LibraryAlbum(browseId="MPREb_1", playlistId="OLAK5uy_1", title="Album"),
        ]

        added = service.scan_and_add()

        assert added == 1
