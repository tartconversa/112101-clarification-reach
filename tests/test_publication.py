"""显式渠道发布事件：published_only 与 SLA 超时路径。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from clarification_facts.enums import CoverageBucket
from _helpers import WarehouseTestCase

PUB_HEADER = ["record_id", "correction_id", "channel_id",
              "published_at", "ingested_at"]
T1 = "2026-04-10T12:00:00Z"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PublicationTests(WarehouseTestCase):
    def _seed_correction(self, published_at: str) -> None:
        self.ingest("corrections", [
            "record_id", "correction_id", "content_id", "revision",
            "predecessor_id", "scope_ref", "status",
            "confirmed_at", "published_at", "ingested_at",
        ], [
            ("r1", "CR-1", "N-1", 1, "", "CLAIM-1", "published",
             published_at, published_at, T1),
        ])

    def test_published_only_within_sla(self) -> None:
        recent = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        self._seed_correction(recent)
        self.ingest("channel_publications", PUB_HEADER, [
            ("p1", "CR-1", "feed", recent, T1),
        ])
        # 无曝光/回执时没有 cohort 单元；用曝光制造单元
        self.ingest("exposures", [
            "record_id", "exposure_id", "content_id", "channel_id",
            "started_at", "ended_at", "cohort_ref", "estimated_views",
            "ingested_at",
        ], [
            ("e1", "E-1", "N-1", "feed",
             "2026-04-09T08:00:00Z", "2026-04-10T08:00:00Z", "G-F", 10, T1),
        ])
        self.build(sla_hours=72)
        row = self.coverage_map()[("CR-1", "feed", "G-F")]
        self.assertEqual(row["coverage_bucket"],
                         CoverageBucket.PUBLISHED_ONLY.value)
        self.assertEqual(row["report_bucket"],
                         CoverageBucket.PUBLISHED_ONLY.value)

    def test_sla_expiry_moves_published_only_to_unverifiable(self) -> None:
        old = _iso(datetime.now(timezone.utc) - timedelta(days=4))
        self._seed_correction(old)
        self.ingest("channel_publications", PUB_HEADER, [
            ("p1", "CR-1", "feed", old, T1),
        ])
        self.ingest("exposures", [
            "record_id", "exposure_id", "content_id", "channel_id",
            "started_at", "ended_at", "cohort_ref", "estimated_views",
            "ingested_at",
        ], [
            ("e1", "E-1", "N-1", "feed",
             "2026-04-09T08:00:00Z", "2026-04-10T08:00:00Z", "G-F", 10, T1),
        ])
        self.build(sla_hours=72)
        row = self.coverage_map()[("CR-1", "feed", "G-F")]
        self.assertEqual(row["report_bucket"],
                         CoverageBucket.UNVERIFIABLE.value)
        self.assertIn("SLA", row["reason"])

    def test_explicit_publication_distinct_from_non_published(self) -> None:
        # feed 有显式发布；push 完全没有发布事件 -> 两者原因不同
        recent = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        self._seed_correction(recent)
        self.ingest("channel_publications", PUB_HEADER, [
            ("p1", "CR-1", "feed", recent, T1),
        ])
        self.ingest("exposures", [
            "record_id", "exposure_id", "content_id", "channel_id",
            "started_at", "ended_at", "cohort_ref", "estimated_views",
            "ingested_at",
        ], [
            ("e1", "E-1", "N-1", "feed",
             "2026-04-09T08:00:00Z", "2026-04-10T08:00:00Z", "G-F", 10, T1),
            ("e2", "E-2", "N-1", "push",
             "2026-04-09T08:00:00Z", "2026-04-10T08:00:00Z", "G-P", 10, T1),
        ])
        self.build(sla_hours=72)
        cov = self.coverage_map()
        self.assertEqual(cov[("CR-1", "feed", "G-F")]["coverage_bucket"],
                         CoverageBucket.PUBLISHED_ONLY.value)
        self.assertEqual(cov[("CR-1", "push", "G-P")]["coverage_bucket"],
                         CoverageBucket.NOT_PUBLISHED.value)
