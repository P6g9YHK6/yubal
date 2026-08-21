"""Library auto-add scan service.

Scans the user's YouTube Music library (saved playlists, saved albums,
followed artists) and auto-creates subscriptions for anything not already
subscribed. Runs at the top of each sync cycle when enabled via the
YUBAL_AUTO_ADD_* settings.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from yubal.client import YTMusicProtocol
from yubal.models.ytmusic import LibraryAlbum

from yubal_api.api.exceptions import SubscriptionConflictError
from yubal_api.services.subscription_service import SubscriptionService
from yubal_api.settings import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class LibrarySyncService:
    """Scans the user's YouTube Music library and auto-adds new subscriptions.

    Requires authenticated cookies — library endpoints have no
    unauthenticated form. When cookies aren't configured, the scan is
    skipped entirely (logged, not raised) so a missing cookies.txt never
    breaks the regular sync cycle.
    """

    client: YTMusicProtocol
    subscription_service: SubscriptionService
    settings: Settings
    cookies_path: Path | None = None

    def scan_and_add(self) -> int:
        """Scan every enabled library section and add new subscriptions.

        Returns:
            Number of new subscriptions created.
        """
        if not self._any_enabled():
            return 0

        if self.cookies_path is None:
            logger.warning(
                "Auto-add is enabled but no cookies are configured; skipping "
                "library scan (library access requires authentication)"
            )
            return 0

        added = 0
        if self.settings.auto_add_playlists:
            added += self._add_new_items(
                self._fetch("playlists", self.client.get_library_playlists),
                lambda p: f"https://music.youtube.com/playlist?list={p.playlist_id}",
                "playlist",
            )
        if self.settings.auto_add_albums:
            added += self._add_new_items(
                self._fetch("albums", self.client.get_library_albums),
                self._album_url,
                "album",
            )
        if self.settings.auto_add_artists:
            added += self._add_new_items(
                self._fetch(
                    "followed artists", self.client.get_library_followed_artists
                ),
                lambda a: f"https://music.youtube.com/channel/{a.browse_id}",
                "artist",
            )
        return added

    def _any_enabled(self) -> bool:
        return (
            self.settings.auto_add_playlists
            or self.settings.auto_add_albums
            or self.settings.auto_add_artists
        )

    def _fetch(self, label: str, fetch_fn: Callable[[], list[T]]) -> list[T]:
        """Fetch one library section, logging (not raising) on failure."""
        try:
            return fetch_fn()
        except Exception:
            logger.exception("Failed to fetch library %s for auto-add", label)
            return []

    def _album_url(self, album: LibraryAlbum) -> str:
        """Build a subscription URL for a library album.

        Prefers the audio playlist form (matches what a manually-added
        album subscription's URL looks like); falls back to a browse URL
        for the rare album with no playlistId.
        """
        if album.playlist_id:
            return f"https://music.youtube.com/playlist?list={album.playlist_id}"
        return f"https://music.youtube.com/browse/{album.browse_id}"

    def _add_new_items(
        self,
        items: Iterable[T],
        url_builder: Callable[[T], str],
        label: str,
    ) -> int:
        """Add subscriptions for any items not already subscribed.

        Reuses SubscriptionService.create()'s own duplicate-URL check —
        every item is submitted and SubscriptionConflictError (already
        subscribed) is caught and swallowed, rather than diffing against
        existing subscriptions separately.
        """
        added = 0
        for item in items:
            url = url_builder(item)
            try:
                self.subscription_service.create(url)
                added += 1
            except SubscriptionConflictError:
                continue  # Already subscribed — nothing to do
            except Exception:
                logger.exception("Failed to auto-add %s from library: %s", label, url)
        return added
