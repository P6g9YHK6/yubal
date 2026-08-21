"""Tests for SyncService audio quality propagation."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from yubal import AudioCodec, CancelToken, DownloadConfig, DownloadStatus
from yubal.models.results import DownloadResult, PlaylistDownloadResult
from yubal.models.track import PlaylistInfo, TrackMetadata
from yubal.models.ytmusic import Album, ArtistDiscography
from yubal_api.services.sync_service import SyncResult, SyncService


class TestSyncServiceAudioQuality:
    """Tests for audio_quality flowing from SyncService to DownloadConfig."""

    def test_quality_passed_to_download_config(self, tmp_path: Path) -> None:
        """audio_quality should be forwarded to DownloadConfig.quality."""
        service = SyncService(
            base_path=tmp_path,
            audio_format="opus",
            audio_quality=7,
        )

        with patch(
            "yubal_api.services.sync_service.create_playlist_downloader"
        ) as mock_create:
            mock_create.return_value = None  # We only care about the config

            try:
                service.run(
                    "https://example.com", None, __import__("yubal").CancelToken()
                )
            except Exception:
                pass  # Expected to fail since downloader is None

            config = mock_create.call_args[0][0]
            assert isinstance(config.download, DownloadConfig)
            assert config.download.quality == 7

    def test_quality_defaults_to_zero(self, tmp_path: Path) -> None:
        """audio_quality should default to 0 (best) when not specified."""
        service = SyncService(base_path=tmp_path, audio_format="opus")
        assert service.audio_quality == 0

    def test_quality_with_different_codecs(self, tmp_path: Path) -> None:
        """audio_quality should propagate regardless of codec."""
        for codec in ["opus", "mp3", "m4a"]:
            service = SyncService(
                base_path=tmp_path,
                audio_format=codec,
                audio_quality=3,
            )

            with patch(
                "yubal_api.services.sync_service.create_playlist_downloader"
            ) as mock_create:
                mock_create.return_value = None

                try:
                    service.run(
                        "https://example.com",
                        None,
                        __import__("yubal").CancelToken(),
                    )
                except Exception:
                    pass

                config = mock_create.call_args[0][0]
                assert config.download.quality == 3
                assert config.download.codec == AudioCodec(codec)


def _make_album(browse_id: str, playlist_id: str, title: str = "Album") -> Album:
    return Album(
        title=title,
        artists=[],
        thumbnails=[],
        tracks=[],
        audioPlaylistId=playlist_id,
    )


def _make_download_result(
    status: DownloadStatus = DownloadStatus.SUCCESS,
) -> DownloadResult:
    track = TrackMetadata(
        title="Test Track",
        artists=["Test Artist"],
        album="Test Album",
        album_artists=["Test Artist"],
    )
    return DownloadResult(track=track, status=status)


def _make_download_result_object(success_count: int) -> PlaylistDownloadResult:
    return PlaylistDownloadResult(
        playlist_info=PlaylistInfo(playlist_id="pl1"),
        download_results=[_make_download_result() for _ in range(success_count)],
    )


class TestArtistSyncWorkflow:
    """Tests for the artist/channel URL fan-out workflow."""

    def _run(
        self, tmp_path: Path, url: str, max_items: int | None = None
    ) -> SyncResult:
        service = SyncService(base_path=tmp_path, audio_format="opus")
        return service.run(url, None, CancelToken(), max_items=max_items)

    def test_downloads_each_album_in_discography(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.resolve_channel_id.return_value = "UCabc"
        mock_client.get_artist_albums.return_value = ArtistDiscography(
            name="Oasis",
            thumbnail_url="https://example.com/artist.jpg",
            album_browse_ids=["MPREb_1", "MPREb_2"],
        )
        mock_client.get_album.side_effect = [
            _make_album("MPREb_1", "OLAK5uy_1"),
            _make_album("MPREb_2", "OLAK5uy_2"),
        ]

        mock_downloader = MagicMock()
        mock_downloader.download_playlist.return_value = iter([])
        mock_downloader.get_result.return_value = _make_download_result_object(2)

        with (
            patch(
                "yubal_api.services.sync_service.YTMusicClient",
                return_value=mock_client,
            ),
            patch(
                "yubal_api.services.sync_service.PlaylistDownloadService",
                return_value=mock_downloader,
            ),
        ):
            result = self._run(tmp_path, "https://music.youtube.com/@Oasis")

        assert result.success is True
        assert result.content_info is not None
        assert result.content_info.title == "Oasis"
        assert result.content_info.track_count == 4  # 2 albums x 2 tracks each
        assert mock_downloader.download_playlist.call_count == 2
        called_urls = [
            c.args[0] for c in mock_downloader.download_playlist.call_args_list
        ]
        assert "https://music.youtube.com/playlist?list=OLAK5uy_1" in called_urls
        assert "https://music.youtube.com/playlist?list=OLAK5uy_2" in called_urls

    def test_max_items_limits_number_of_albums(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.resolve_channel_id.return_value = "UCabc"
        mock_client.get_artist_albums.return_value = ArtistDiscography(
            name="Oasis",
            thumbnail_url=None,
            album_browse_ids=["MPREb_1", "MPREb_2", "MPREb_3"],
        )
        mock_client.get_album.side_effect = [_make_album("MPREb_1", "OLAK5uy_1")]

        mock_downloader = MagicMock()
        mock_downloader.download_playlist.return_value = iter([])
        mock_downloader.get_result.return_value = _make_download_result_object(1)

        with (
            patch(
                "yubal_api.services.sync_service.YTMusicClient",
                return_value=mock_client,
            ),
            patch(
                "yubal_api.services.sync_service.PlaylistDownloadService",
                return_value=mock_downloader,
            ),
        ):
            result = self._run(
                tmp_path, "https://music.youtube.com/@Oasis", max_items=1
            )

        assert result.success is True
        assert mock_downloader.download_playlist.call_count == 1

    def test_no_albums_found_returns_failure(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.resolve_channel_id.return_value = "UCabc"
        mock_client.get_artist_albums.return_value = ArtistDiscography(
            name="Empty Artist", thumbnail_url=None, album_browse_ids=[]
        )

        with patch(
            "yubal_api.services.sync_service.YTMusicClient",
            return_value=mock_client,
        ):
            result = self._run(tmp_path, "https://music.youtube.com/@Empty")

        assert result.success is False
        assert result.error == "No albums found for artist"

    def test_skips_album_with_no_playlist_id(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.resolve_channel_id.return_value = "UCabc"
        mock_client.get_artist_albums.return_value = ArtistDiscography(
            name="Oasis", thumbnail_url=None, album_browse_ids=["MPREb_1"]
        )
        album_without_playlist = Album(
            title="No Playlist Album", artists=[], thumbnails=[], tracks=[]
        )
        mock_client.get_album.return_value = album_without_playlist

        mock_downloader = MagicMock()

        with (
            patch(
                "yubal_api.services.sync_service.YTMusicClient",
                return_value=mock_client,
            ),
            patch(
                "yubal_api.services.sync_service.PlaylistDownloadService",
                return_value=mock_downloader,
            ),
        ):
            result = self._run(tmp_path, "https://music.youtube.com/@Oasis")

        mock_downloader.download_playlist.assert_not_called()
        assert result.success is True
        assert result.content_info is not None
        assert result.content_info.track_count == 0

    def test_aggregates_download_stats_across_albums(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.resolve_channel_id.return_value = "UCabc"
        mock_client.get_artist_albums.return_value = ArtistDiscography(
            name="Oasis", thumbnail_url=None, album_browse_ids=["MPREb_1", "MPREb_2"]
        )
        mock_client.get_album.side_effect = [
            _make_album("MPREb_1", "OLAK5uy_1"),
            _make_album("MPREb_2", "OLAK5uy_2"),
        ]

        result_1 = PlaylistDownloadResult(
            playlist_info=PlaylistInfo(playlist_id="pl1"),
            download_results=[
                _make_download_result(DownloadStatus.SUCCESS),
                _make_download_result(DownloadStatus.FAILED),
            ],
        )
        result_2 = PlaylistDownloadResult(
            playlist_info=PlaylistInfo(playlist_id="pl2"),
            download_results=[_make_download_result(DownloadStatus.SUCCESS)],
        )

        mock_downloader = MagicMock()
        mock_downloader.download_playlist.return_value = iter([])
        mock_downloader.get_result.side_effect = [result_1, result_2]

        with (
            patch(
                "yubal_api.services.sync_service.YTMusicClient",
                return_value=mock_client,
            ),
            patch(
                "yubal_api.services.sync_service.PlaylistDownloadService",
                return_value=mock_downloader,
            ),
        ):
            result = self._run(tmp_path, "https://music.youtube.com/@Oasis")

        assert result.download_stats is not None
        assert result.download_stats.success == 2
        assert result.download_stats.failed == 1
