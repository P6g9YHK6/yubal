"""Tests for how the queue picks the next job."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from yubal_api.domain.enums import JobSource
from yubal_api.domain.job import Job
from yubal_api.services.job_priority import (
    Kind,
    Tier,
    job_priority,
    kind_of,
    next_rate,
    subscription_priority,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
PLAYLIST = "https://music.youtube.com/playlist?list="


def _job(url: str, **fields: Any) -> Job:
    fields.setdefault("source", JobSource.SCHEDULER)
    fields.setdefault("subscription_id", uuid4())
    fields.setdefault("created_at", NOW - timedelta(hours=1))
    return Job(id="job", url=url, **fields)


class TestKind:
    @pytest.mark.parametrize(
        ("url", "kind"),
        [
            (f"{PLAYLIST}LM", Kind.PERSONAL),
            (f"{PLAYLIST}QUICKPICKS", Kind.PERSONAL),
            (f"{PLAYLIST}RDTMAK5uy_n_5IN6hzAOwdCnM8D8rzrs3vDl12UcZpA", Kind.PERSONAL),
            (f"{PLAYLIST}OLAK5uy_lee2ks2YoD_CjqL8JHyEFM3zWz_MXW1LY", Kind.ALBUM),
            (f"{PLAYLIST}PL1oFNVnEV7fXuoHdy7WZLYqOB_ofNZLm_", Kind.PLAYLIST),
            ("https://music.youtube.com/channel/UC8yu6IvCtH_4AuVSmyLuw1Q", Kind.ARTIST),
            ("https://example.com/not-a-playlist", Kind.PLAYLIST),
        ],
    )
    def test_kind(self, url: str, kind: Kind) -> None:
        assert kind_of(url) == kind


class TestNextRate:
    def test_first_measure_is_taken_as_is(self) -> None:
        assert next_rate(None, 4) == 4

    def test_moving_average(self) -> None:
        assert next_rate(10, 0) == pytest.approx(7)
        assert next_rate(0, 10) == pytest.approx(3)


class TestJobPriority:
    def test_tiers(self) -> None:
        manual = _job(f"{PLAYLIST}OLAK5uy_x", source=JobSource.MANUAL)
        new = _job(f"{PLAYLIST}OLAK5uy_x", first_sync=True)
        starved = _job(f"{PLAYLIST}OLAK5uy_x", created_at=NOW - timedelta(hours=73))
        scored = _job(f"{PLAYLIST}LM", waiting_since=NOW - timedelta(days=30))

        assert job_priority(manual, NOW).tier == Tier.MANUAL
        assert job_priority(new, NOW).tier == Tier.NEW
        assert job_priority(starved, NOW).tier == Tier.NEW
        assert job_priority(scored, NOW).tier == Tier.SCORED
        assert (
            job_priority(manual, NOW)
            > job_priority(new, NOW)
            > job_priority(scored, NOW)
        )

    def test_liked_music_beats_an_album(self) -> None:
        day = NOW - timedelta(hours=24)
        liked = _job(f"{PLAYLIST}LM", waiting_since=day)
        album = _job(f"{PLAYLIST}OLAK5uy_x", waiting_since=day)

        assert job_priority(liked, NOW) > job_priority(album, NOW)

    def test_learned_rate_beats_kind(self) -> None:
        day = NOW - timedelta(hours=24)
        busy = _job(f"{PLAYLIST}PLbusy", waiting_since=day, change_rate=20)
        quiet = _job(f"{PLAYLIST}PLquiet", waiting_since=day, change_rate=0)

        assert job_priority(busy, NOW).score == pytest.approx(480)
        # A playlist that never changes still counts at its kind's floor
        assert job_priority(quiet, NOW).score == pytest.approx(2.4)

    def test_score_grows_while_waiting(self) -> None:
        job = _job(f"{PLAYLIST}PLx", waiting_since=NOW - timedelta(hours=24))

        later = NOW + timedelta(hours=24)
        assert job_priority(job, later).score == 2 * job_priority(job, NOW).score

    def test_naive_times_are_utc(self) -> None:
        job = _job(f"{PLAYLIST}PLx", waiting_since=datetime(2026, 11, 26, 10, 0))

        assert job_priority(job, NOW).score == pytest.approx(2)


class TestSubscriptionPriority:
    def test_never_synced_first(self) -> None:
        never = MagicMock(url=f"{PLAYLIST}PLx", last_synced_at=None, change_rate=None)
        synced = MagicMock(
            url=f"{PLAYLIST}LM",
            last_synced_at=NOW - timedelta(days=30),
            change_rate=None,
        )

        assert subscription_priority(never, NOW) > subscription_priority(synced, NOW)
