"""Tests for removing tracks no playlist lists anymore."""

from pathlib import Path

from yubal.utils.cleanup import remove_unreferenced_tracks


def _track(base: Path, rel: str) -> Path:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    return path


def _m3u(base: Path, name: str, *tracks: str) -> None:
    playlists = base / "_Playlists"
    playlists.mkdir(exist_ok=True)
    lines = ["#EXTM3U", "#YUBAL-ID:PLx", *(f"../{t}" for t in tracks)]
    (playlists / f"{name}.m3u").write_text("\n".join(lines) + "\n")


def _library(tmp_path: Path, count: int = 10) -> list[str]:
    tracks = [f"Artist/2020 - Album/{i:02} - Song.opus" for i in range(count)]
    for rel in tracks:
        _track(tmp_path, rel)
    return tracks


class TestRemoveUnreferencedTracks:
    def test_keeps_everything_listed(self, tmp_path: Path) -> None:
        tracks = _library(tmp_path)
        _m3u(tmp_path, "All", *tracks)

        result = remove_unreferenced_tracks(tmp_path)

        assert result.unreferenced == []
        assert all((tmp_path / t).exists() for t in tracks)

    def test_deletes_dropped_track_with_sidecars(self, tmp_path: Path) -> None:
        tracks = _library(tmp_path)
        _m3u(tmp_path, "All", *tracks[1:])
        dropped = tmp_path / tracks[0]
        dropped.with_suffix(".lrc").write_text("[00:01]la")
        dropped.with_suffix(".nfo").write_text("<musicvideo/>")

        result = remove_unreferenced_tracks(tmp_path)

        assert result.deleted
        assert result.unreferenced == [dropped]
        assert not dropped.exists()
        assert not dropped.with_suffix(".lrc").exists()
        assert not dropped.with_suffix(".nfo").exists()
        assert (tmp_path / tracks[1]).exists()

    def test_track_in_any_playlist_is_kept(self, tmp_path: Path) -> None:
        tracks = _library(tmp_path)
        _m3u(tmp_path, "One", *tracks[:5])
        _m3u(tmp_path, "Two", *tracks[5:])

        assert remove_unreferenced_tracks(tmp_path).unreferenced == []

    def test_removes_emptied_folders(self, tmp_path: Path) -> None:
        tracks = _library(tmp_path)
        _track(tmp_path, "Gone/2001 - Old/01 - Old.opus")
        (tmp_path / "Gone/2001 - Old/cover.jpg").write_bytes(b"jpg")
        (tmp_path / "Gone/artist.nfo").write_text("<artist/>")
        _m3u(tmp_path, "All", *tracks)

        remove_unreferenced_tracks(tmp_path)

        assert not (tmp_path / "Gone").exists()
        assert (tmp_path / "Artist/2020 - Album").is_dir()

    def test_dry_run_deletes_nothing(self, tmp_path: Path) -> None:
        tracks = _library(tmp_path)
        _m3u(tmp_path, "All", *tracks[1:])

        result = remove_unreferenced_tracks(tmp_path, dry_run=True)

        assert not result.deleted
        assert len(result.unreferenced) == 1
        assert (tmp_path / tracks[0]).exists()

    def test_refuses_to_delete_too_much(self, tmp_path: Path) -> None:
        tracks = _library(tmp_path)
        _m3u(tmp_path, "Few", *tracks[:5])

        result = remove_unreferenced_tracks(tmp_path, max_ratio=0.2)

        assert not result.deleted
        assert all((tmp_path / t).exists() for t in tracks)

    def test_no_m3u_deletes_nothing(self, tmp_path: Path) -> None:
        tracks = _library(tmp_path)

        result = remove_unreferenced_tracks(tmp_path, max_ratio=1.0)

        assert not result.deleted
        assert all((tmp_path / t).exists() for t in tracks)
