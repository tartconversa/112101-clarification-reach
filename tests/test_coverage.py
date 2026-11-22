"""覆盖四态、迟到回执与 SCD2 修订/撤回口径。"""

from __future__ import annotations

from clarification_facts.enums import CoverageBucket
from clarification_facts.metrics import coverage_summary
from _helpers import (
    CORR_HEADER, RECEIPT_HEADER, WarehouseTestCase,
)

T1 = "2026-04-10T12:00:00Z"
T2 = "2026-04-11T12:00:00Z"
T3 = "2026-04-13T12:00:00Z"


def _corr(rid, cid, rev, pred, status, confirmed, published, stamp):
    return (rid, cid, "N-1", rev, pred, "CLAIM-1", status,
            confirmed, published, stamp)


class CoverageBucketTests(WarehouseTestCase):
    def _seed_cr100_v1(self) -> None:
        self.ingest("corrections", CORR_HEADER, [
            _corr("r1", "CR-100", 1, "", "partial",
                  "2026-04-10T08:30:00Z", "2026-04-10T09:00:00Z", T1),
        ])

    def test_confirmed_vs_unverifiable_partial(self) -> None:
        self._seed_cr100_v1()
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc1", "RR-1", "CR-100", "home", "2026-04-10T09:20:00Z",
             "confirmed", "G-H", T1),
            ("rc2", "RR-2", "CR-100", "search", "2026-04-10T10:05:00Z",
             "partial", "G-S", T1),
        ])
        self.build()
        cov = self.coverage_map()
        self.assertEqual(cov[("CR-100", "home", "G-H")]["report_bucket"],
                         CoverageBucket.CONFIRMED.value)
        self.assertEqual(cov[("CR-100", "search", "G-S")]["report_bucket"],
                         CoverageBucket.UNVERIFIABLE.value)
        self.assertIsNone(cov[("CR-100", "search", "G-S")]["confirmed_users"])

    def test_late_receipt_flips_unverifiable_to_confirmed(self) -> None:
        self._seed_cr100_v1()
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc1", "RR-2", "CR-100", "search", "2026-04-10T10:05:00Z",
             "partial", "G-S", T1),
        ])
        build1 = self.build()
        self.assertEqual(
            self.coverage_map(build1)[("CR-100", "search", "G-S")]["report_bucket"],
            CoverageBucket.UNVERIFIABLE.value,
        )
        # 迟到的同键回执：partial -> confirmed
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc2", "RR-2", "CR-100", "search", "2026-04-11T08:00:00Z",
             "confirmed", "G-S", T2),
        ])
        build2 = self.build()
        row = self.coverage_map(build2)[("CR-100", "search", "G-S")]
        self.assertEqual(row["report_bucket"], CoverageBucket.CONFIRMED.value)
        self.assertEqual(row["first_confirmed_at"], "2026-04-11T08:00:00Z")
        # 旧构建的快照口径保持不变（历史口径不被后续重建覆盖）
        from clarification_facts.metrics import coverage_snapshot
        old = {
            (r["correction_id"], r["channel_id"], r["cohort_ref"]): r
            for r in coverage_snapshot(self.conn, build1)
        }
        self.assertEqual(old[("CR-100", "search", "G-S")]["report_bucket"],
                         CoverageBucket.UNVERIFIABLE.value)
        snapshots = self.conn.execute(
            "SELECT DISTINCT build_id FROM dws_coverage_snapshot"
        ).fetchall()
        self.assertEqual(len(snapshots), 2)

    def test_amendment_moves_old_version_to_not_applicable(self) -> None:
        self._seed_cr100_v1()
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc1", "RR-1", "CR-100", "home", "2026-04-10T09:20:00Z",
             "confirmed", "G-H", T1),
        ])
        self.build()
        # 下一版修订到达：v1 变 amended，v2 发布
        self.ingest("corrections", CORR_HEADER, [
            _corr("r1b", "CR-100", 1, "", "amended",
                  "2026-04-10T08:30:00Z", "2026-04-10T09:00:00Z", T2),
            _corr("r2", "CR-101", 2, "CR-100", "published",
                  "2026-04-11T10:00:00Z", "2026-04-11T12:00:00Z", T2),
        ])
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc2", "RR-3", "CR-101", "home", "2026-04-11T12:20:00Z",
             "confirmed", "G-H", T2),
        ])
        self.build()
        cov = self.coverage_map()
        self.assertEqual(cov[("CR-100", "home", "G-H")]["report_bucket"],
                         CoverageBucket.NOT_APPLICABLE.value)
        self.assertEqual(cov[("CR-101", "home", "G-H")]["report_bucket"],
                         CoverageBucket.CONFIRMED.value)

    def test_withdrawn_correction_not_applicable_but_evidence_kept(self) -> None:
        self.ingest("corrections", CORR_HEADER, [
            _corr("r3", "CR-102", 3, "CR-101", "withdrawn",
                  "2026-04-13T09:00:00Z", "2026-04-13T10:00:00Z", T3),
        ])
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc1", "RR-6", "CR-102", "home", "2026-04-13T10:20:00Z",
             "confirmed", "G-H", T3),
            ("rc2", "RR-7", "CR-102", "feed", "2026-04-13T11:00:00Z",
             "failed", "G-F", T3),
        ])
        self.build()
        cov = self.coverage_map()
        for unit in (("CR-102", "home", "G-H"), ("CR-102", "feed", "G-F")):
            self.assertEqual(cov[unit]["report_bucket"],
                             CoverageBucket.NOT_APPLICABLE.value)
        # 回执事实仍然保留
        receipts = self.conn.execute(
            "SELECT COUNT(*) AS n FROM fact_delivery_receipt WHERE correction_id='CR-102'"
        ).fetchone()["n"]
        self.assertEqual(receipts, 2)

    def test_no_receipt_is_unverifiable_never_zero(self) -> None:
        self._seed_cr100_v1()
        self.build()
        cov = self.coverage_map()
        # 没有任何渠道回执 -> 没有可解释单元时，至少更正本身不产生虚构渠道；
        # 加入一条曝光制造"原内容触达但无纠正动作"的缺口单元
        self.ingest("exposures", [
            "record_id", "exposure_id", "content_id", "channel_id",
            "started_at", "ended_at", "cohort_ref", "estimated_views",
            "ingested_at",
        ], [
            ("e1", "E-1", "N-1", "push", "2026-04-09T08:00:00Z",
             "2026-04-10T08:00:00Z", "G-P", 100, T1),
        ])
        self.build()
        row = self.coverage_map()[("CR-100", "push", "G-P")]
        # 内部 not_published 对外并入 unverifiable
        self.assertEqual(row["report_bucket"],
                         CoverageBucket.UNVERIFIABLE.value)
        self.assertIn(row["coverage_bucket"],
                      (CoverageBucket.NOT_PUBLISHED.value,
                       CoverageBucket.UNVERIFIABLE.value))

    def test_summary_partitions_all_units(self) -> None:
        self._seed_cr100_v1()
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc1", "RR-1", "CR-100", "home", "2026-04-10T09:20:00Z",
             "confirmed", "G-H", T1),
            ("rc2", "RR-2", "CR-100", "search", "2026-04-10T10:05:00Z",
             "pending", "G-S", T1),
        ])
        self.build()
        summary = {r["report_bucket"]: r["unit_count"]
                   for r in coverage_summary(self.conn)}
        self.assertEqual(summary.get("confirmed"), 1)
        self.assertEqual(summary.get("unverifiable"), 1)
        self.assertEqual(sum(summary.values()), 2)
