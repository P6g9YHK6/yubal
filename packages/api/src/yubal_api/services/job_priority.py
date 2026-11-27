"""Which queued job should run next.

Jobs added by hand go first, then subscriptions that were never synced,
then whatever most likely has new songs waiting: how many new tracks a
subscription brought per sync (learned, see next_rate) times the hours since
its last sync. That score keeps growing while a job waits, and a job pending
for more than STARVED_AFTER is moved up with the new ones, so nothing waits
forever behind a stream of busier subscriptions. Within the new tier the same
score still decides, so Liked Music doesn't fall back to plain FIFO behind a
backlog of artists.
"""

from datetime import UTC, datetime, timedelta
from enum import IntEnum
from typing import NamedTuple

from yubal import YubalError, is_artist_url, parse_playlist_id
from yubal.lib.m3u import (
    LIKED_MUSIC_ID,
    PERSONAL_MIX_ID_PREFIX,
    QUICK_PICKS_ID,
    is_album_playlist_id,
)

from yubal_api.db.subscription import Subscription
from yubal_api.domain.enums import JobSource
from yubal_api.domain.job import Job

STARVED_AFTER = timedelta(hours=72)
# Weight of the latest sync in the moving average of new tracks per sync
RATE_WEIGHT = 0.3


class Kind(IntEnum):
    PERSONAL = 0  # Liked Music, Quick picks and the personal mixes
    PLAYLIST = 1
    ARTIST = 2
    ALBUM = 3


# New tracks per sync assumed before anything is measured, and the lowest
# rate a kind is ever given so even a quiet subscription comes up in time
_SEED_RATE = {
    Kind.PERSONAL: 5.0,
    Kind.PLAYLIST: 1.0,
    Kind.ARTIST: 0.5,
    Kind.ALBUM: 0.05,
}
_RATE_FLOOR = {
    Kind.PERSONAL: 2.0,
    Kind.PLAYLIST: 0.1,
    Kind.ARTIST: 0.1,
    Kind.ALBUM: 0.01,
}


class Tier(IntEnum):
    SCORED = 1
    NEW = 2  # never synced, or starved
    MANUAL = 3


class Priority(NamedTuple):
    tier: Tier
    score: float


def kind_of(url: str) -> Kind:
    if is_artist_url(url):
        return Kind.ARTIST
    try:
        playlist_id = parse_playlist_id(url)
    except YubalError:
        return Kind.PLAYLIST
    if playlist_id in (LIKED_MUSIC_ID, QUICK_PICKS_ID) or playlist_id.startswith(
        PERSONAL_MIX_ID_PREFIX
    ):
        return Kind.PERSONAL
    if is_album_playlist_id(playlist_id):
        return Kind.ALBUM
    return Kind.PLAYLIST


def next_rate(old: float | None, new_tracks: int) -> float:
    """Fold the tracks one sync brought into the running average."""
    if old is None:
        return float(new_tracks)
    return (1 - RATE_WEIGHT) * old + RATE_WEIGHT * new_tracks


def _as_utc(moment: datetime) -> datetime:
    # SQLite hands datetimes back without a timezone, they are stored in UTC
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _score(url: str, rate: float | None, since: datetime, now: datetime) -> float:
    kind = kind_of(url)
    rate = max(_SEED_RATE[kind] if rate is None else rate, _RATE_FLOOR[kind])
    hours = (_as_utc(now) - _as_utc(since)).total_seconds() / 3600
    return rate * max(hours, 0.0)


def job_priority(job: Job, now: datetime) -> Priority:
    if job.source == JobSource.MANUAL:
        return Priority(Tier.MANUAL, 0.0)
    since = job.waiting_since or job.created_at
    score = _score(job.url, job.change_rate, since, now)
    if job.first_sync or _as_utc(now) - _as_utc(job.created_at) > STARVED_AFTER:
        return Priority(Tier.NEW, score)
    return Priority(Tier.SCORED, score)


def subscription_priority(subscription: Subscription, now: datetime) -> Priority:
    since = subscription.last_synced_at or subscription.created_at
    score = _score(subscription.url, subscription.change_rate, since, now)
    if subscription.last_synced_at is None:
        return Priority(Tier.NEW, score)
    return Priority(Tier.SCORED, score)
