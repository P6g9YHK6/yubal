"""Tests for SyncService audio quality propagation."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from yubal import (
    AudioCodec,
    CancelToken,
    ContentKind,
    DownloadConfig,
    DownloadStatus,
    PlaylistDownloadConfig,
)
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

    @pytest.fixture(autouse=True)
    def _no_cover_download(self) -> Iterator[None]:
        with patch("yubal.services.artifacts.write_playlist_cover", return_value=None):
            yield

    def _run(
        self,
        tmp_path: Path,
        url: str,
        max_items: int | None = None,
        artist_m3u: bool = True,
    ) -> SyncResult:
        service = SyncService(
            base_path=tmp_path, audio_format="opus", artist_m3u=artist_m3u
        )
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
        assert result.content_info.kind == ContentKind.ARTIST
        assert result.content_info.track_count == 4  # 2 albums x 2 tracks each
        assert mock_downloader.download_playlist.call_count == 2
        called_urls = [
            c.args[0] for c in mock_downloader.download_playlist.call_args_list
        ]
        assert "https://music.youtube.com/playlist?list=OLAK5uy_1" in called_urls
        assert "https://music.youtube.com/playlist?list=OLAK5uy_2" in called_urls

    def test_max_items_caps_total_tracks_across_albums(self, tmp_path: Path) -> None:
        """max_items means the same thing here as everywhere else: a total
        track cap, spent across the discography rather than per album."""
        mock_client = MagicMock()
        mock_client.resolve_channel_id.return_value = "UCabc"
        mock_client.get_artist_albums.return_value = ArtistDiscography(
            name="Oasis",
            thumbnail_url=None,
            album_browse_ids=["MPREb_1", "MPREb_2", "MPREb_3"],
        )
        mock_client.get_album.side_effect = [
            _make_album("MPREb_1", "OLAK5uy_1"),
            _make_album("MPREb_2", "OLAK5uy_2"),
        ]

        mock_downloader = MagicMock()
        mock_downloader.download_playlist.return_value = iter([])
        mock_downloader.get_result.side_effect = [
            _make_download_result_object(2),  # first album: 2 tracks
            _make_download_result_object(1),  # second album: budget ran out
        ]

        with (
            patch(
                "yubal_api.services.sync_service.YTMusicClient",
                return_value=mock_client,
            ),
            patch(
                "yubal_api.services.sync_service.PlaylistDownloadService",
                return_value=mock_downloader,
            ) as mock_downloader_cls,
        ):
            result = self._run(
                tmp_path, "https://music.youtube.com/@Oasis", max_items=3
            )

        assert result.success is True
        assert result.content_info is not None
        assert result.content_info.track_count == 3
        # Third album never gets touched once the budget is spent
        assert mock_downloader.download_playlist.call_count == 2
        # Each album's downloader gets the *remaining* budget, not the total
        configs = [call.args[0] for call in mock_downloader_cls.call_args_list]
        assert configs[0].max_items == 3
        assert configs[1].max_items == 1

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

    def test_writes_artist_m3u_across_albums(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.resolve_channel_id.return_value = "UCabcdefgh12345678"
        mock_client.get_artist_albums.return_value = ArtistDiscography(
            name="Oasis", thumbnail_url=None, album_browse_ids=["MPREb_1", "MPREb_2"]
        )
        mock_client.get_album.side_effect = [
            _make_album("MPREb_1", "OLAK5uy_1"),
            _make_album("MPREb_2", "OLAK5uy_2"),
        ]

        track = _make_download_result().track
        song_1 = tmp_path / "Oasis" / "1994 - Definitely Maybe" / "01 - Rock.opus"
        song_2 = tmp_path / "Oasis" / "1995 - Morning Glory" / "01 - Hello.opus"
        result_1 = PlaylistDownloadResult(
            playlist_info=PlaylistInfo(playlist_id="pl1"),
            download_results=[
                DownloadResult(
                    track=track, status=DownloadStatus.SUCCESS, output_path=song_1
                ),
                _make_download_result(DownloadStatus.FAILED),
            ],
        )
        result_2 = PlaylistDownloadResult(
            playlist_info=PlaylistInfo(playlist_id="pl2"),
            download_results=[
                DownloadResult(
                    track=track, status=DownloadStatus.SKIPPED, output_path=song_2
                ),
            ],
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
            self._run(tmp_path, "https://music.youtube.com/@Oasis")

        m3u_files = list((tmp_path / "_Playlists").glob("*.m3u"))
        assert len(m3u_files) == 1
        assert m3u_files[0].name.startswith("Oasis [")
        content = m3u_files[0].read_text(encoding="utf-8")
        assert "#PLAYLIST:Oasis" in content
        assert "../Oasis/1994 - Definitely Maybe/01 - Rock.opus" in content
        assert "../Oasis/1995 - Morning Glory/01 - Hello.opus" in content

    def test_no_artist_m3u_without_tracks_on_disk(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.resolve_channel_id.return_value = "UCabc"
        mock_client.get_artist_albums.return_value = ArtistDiscography(
            name="Oasis", thumbnail_url=None, album_browse_ids=["MPREb_1"]
        )
        mock_client.get_album.return_value = _make_album("MPREb_1", "OLAK5uy_1")

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
            self._run(tmp_path, "https://music.youtube.com/@Oasis")

        assert not (tmp_path / "_Playlists").exists()

    def test_no_artist_m3u_when_disabled(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.resolve_channel_id.return_value = "UCabc"
        mock_client.get_artist_albums.return_value = ArtistDiscography(
            name="Oasis", thumbnail_url=None, album_browse_ids=["MPREb_1"]
        )
        mock_client.get_album.return_value = _make_album("MPREb_1", "OLAK5uy_1")

        song = tmp_path / "Oasis" / "1994 - Definitely Maybe" / "01 - Rock.opus"
        mock_downloader = MagicMock()
        mock_downloader.download_playlist.return_value = iter([])
        mock_downloader.get_result.return_value = PlaylistDownloadResult(
            playlist_info=PlaylistInfo(playlist_id="pl1"),
            download_results=[
                DownloadResult(
                    track=_make_download_result().track,
                    status=DownloadStatus.SUCCESS,
                    output_path=song,
                )
            ],
        )

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
            self._run(tmp_path, "https://music.youtube.com/@Oasis", artist_m3u=False)

        assert not (tmp_path / "_Playlists").exists()


class TestSyncServiceAlbumM3u:
    """Tests for album_m3u flowing into PlaylistDownloadConfig."""

    def _config_for(
        self, tmp_path: Path, album_m3u: bool = False
    ) -> PlaylistDownloadConfig:
        service = SyncService(
            base_path=tmp_path, audio_format="opus", album_m3u=album_m3u
        )
        with patch(
            "yubal_api.services.sync_service.create_playlist_downloader"
        ) as mock_create:
            mock_create.return_value = None
            try:
                service.run("https://example.com", None, CancelToken())
            except Exception:
                pass  # Expected to fail since downloader is None
            return mock_create.call_args[0][0]

    def test_albums_skipped_by_default(self, tmp_path: Path) -> None:
        assert self._config_for(tmp_path).skip_album_m3u is True

    def test_album_m3u_enabled(self, tmp_path: Path) -> None:
        assert self._config_for(tmp_path, album_m3u=True).skip_album_m3u is False
