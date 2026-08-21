"""Subscription business logic service."""

import logging
from datetime import UTC, datetime
from uuid import UUID

from yubal import (
    ArtistNotFoundError,
    AuthenticationRequiredError,
    ChannelParseError,
    PlaylistNotFoundError,
    PlaylistParseError,
    UnsupportedPlaylistError,
    UpstreamAPIError,
    is_artist_url,
)

from yubal_api.api.exceptions import (
    MetadataFetchError,
    SubscriptionConflictError,
    SubscriptionNotFoundError,
)
from yubal_api.db.subscription import Subscription, SubscriptionFields, SubscriptionType
from yubal_api.services.playlist_info_service import PlaylistInfoService
from yubal_api.services.protocols import SubscriptionRepository

# Known exceptions from metadata fetching that should propagate to the API's
# exception handlers rather than being wrapped as MetadataFetchError.
_KNOWN_METADATA_ERRORS = (
    PlaylistNotFoundError,
    ArtistNotFoundError,
    AuthenticationRequiredError,
    PlaylistParseError,
    ChannelParseError,
    UnsupportedPlaylistError,
    UpstreamAPIError,
)

logger = logging.getLogger(__name__)


class SubscriptionService:
    """Use-case layer for subscription operations.

    Encapsulates business logic for subscription CRUD, keeping route handlers
    thin (HTTP concerns only). PlaylistInfoService is injected as a concrete
    type (single implementation) — extract to a protocol if a second
    implementation is ever needed.
    """

    def __init__(
        self,
        repository: SubscriptionRepository,
        playlist_info: PlaylistInfoService,
    ) -> None:
        self._repository = repository
        self._playlist_info = playlist_info

    def list(
        self,
        *,
        enabled: bool | None = None,
        type: SubscriptionType | None = None,
    ) -> list[Subscription]:
        return self._repository.list(enabled=enabled, type=type)

    def get(self, subscription_id: UUID) -> Subscription:
        sub = self._repository.get(subscription_id)
        if sub is None:
            raise SubscriptionNotFoundError(subscription_id)
        return sub

    def create(self, url: str, max_items: int | None = None) -> Subscription:
        if is_artist_url(url):
            return self._create_artist(url, max_items)
        return self._create_playlist(url, max_items)

    def _create_playlist(self, url: str, max_items: int | None) -> Subscription:
        existing = self._repository.get_by_url(url)
        if existing is not None:
            raise SubscriptionConflictError(
                f"Subscription with URL already exists: {existing.id}",
                subscription_id=existing.id,
            )

        try:
            metadata = self._playlist_info.get_playlist_metadata(url)
        except _KNOWN_METADATA_ERRORS:
            raise  # Known exceptions — propagate to exception handlers
        except Exception as e:
            logger.warning("Unexpected error fetching metadata for %s: %s", url, e)
            raise MetadataFetchError(str(e), upstream_error=type(e).__name__) from e

        subscription = Subscription(
            type=SubscriptionType.PLAYLIST,
            url=url,
            name=metadata.title,
            thumbnail_url=metadata.thumbnail_url,
            enabled=True,
            max_items=max_items,
            created_at=datetime.now(UTC),
        )
        return self._repository.create(subscription)

    def _create_artist(self, url: str, max_items: int | None) -> Subscription:
        try:
            metadata = self._playlist_info.get_artist_metadata(url)
        except _KNOWN_METADATA_ERRORS:
            raise  # Known exceptions — propagate to exception handlers
        except Exception as e:
            logger.warning(
                "Unexpected error fetching artist metadata for %s: %s", url, e
            )
            raise MetadataFetchError(str(e), upstream_error=type(e).__name__) from e

        # Canonicalize to the resolved /channel/UC... form so scheduled
        # syncs never need to re-resolve a handle URL.
        canonical_url = f"https://music.youtube.com/channel/{metadata.channel_id}"

        existing = self._repository.get_by_url(canonical_url)
        if existing is not None:
            raise SubscriptionConflictError(
                f"Subscription with URL already exists: {existing.id}",
                subscription_id=existing.id,
            )

        subscription = Subscription(
            type=SubscriptionType.ARTIST,
            url=canonical_url,
            name=metadata.name,
            thumbnail_url=metadata.thumbnail_url,
            enabled=True,
            max_items=max_items,
            created_at=datetime.now(UTC),
        )
        return self._repository.create(subscription)

    def update(self, subscription_id: UUID, fields: SubscriptionFields) -> Subscription:
        if not fields:
            return self.get(subscription_id)
        sub = self._repository.update(subscription_id, fields)
        if sub is None:
            raise SubscriptionNotFoundError(subscription_id)
        return sub

    def count(
        self,
        *,
        enabled: bool | None = None,
        type: SubscriptionType | None = None,
    ) -> int:
        return self._repository.count(enabled=enabled, type=type)

    def delete(self, subscription_id: UUID) -> None:
        if not self._repository.delete(subscription_id):
            raise SubscriptionNotFoundError(subscription_id)
