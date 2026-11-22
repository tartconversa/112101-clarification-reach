"""滞后窗口曝光：历史曝光不被新推荐统计抹掉。"""

from __future__ import annotations

from clarification_facts.metrics import exposure_alignment, lag_totals
from _helpers import (
    CORR_HEADER, EXPOSURE_HEADER, RECEIPT_HEADER, WarehouseTestCase,
)

T1 = "2026-04-10T12:00:00Z"


class LagWindowTests(WarehouseTestCase):
    def _seed(self) -> None:
        self.ingest("corrections", CORR_HEADER, [
            ("r1", "CR-1", "N-1", 1, "", "CLAIM-1", "published",
             "2026-04-10T08:30:00Z", "2026-04-10T09:00:00Z", T1),
        ])
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc1", "RR-1", "CR-1", "home", "2026-04-10T10:00:00Z",
             "confirmed", "G-H", T1),
        ])

    def test_exposure_before_confirmation_fully_in_lag_window(self) -> None:
        self._seed()
        self.ingest("exposures", EXPOSURE_HEADER, [
            ("e1", "E-1", "N-1", "home",
             "2026-04-09T08:00:00Z", "2026-04-10T09:00:00Z", "G-H", 100, T1),
        ])
        self.build()
        rows = exposure_alignment(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lag_window_views"], 100)
        self.assertEqual(rows[0]["post_coverage_views"], 0)

    def test_exposure_split_around_confirmation(self) -> None:
        self._seed()
        # 09:00 -> 11:00 共 120 分钟；10:00 确认 -> 前后各 60 分钟
        self.ingest("exposures", EXPOSURE_HEADER, [
            ("e1", "E-1", "N-1", "home",
             "2026-04-10T09:00:00Z", "2026-04-10T11:00:00Z", "G-H", 100, T1),
        ])
        self.build()
        row = exposure_alignment(self.conn)[0]
        self.assertEqual(row["lag_window_views"], 50)
        self.assertEqual(row["post_coverage_views"], 50)
        self.assertEqual(
            row["lag_window_views"] + row["post_coverage_views"],
            row["estimated_views"],
        )

    def test_uncorrected_exposure_counted_as_lag_not_zero(self) -> None:
        self._seed()
        self.ingest("exposures", EXPOSURE_HEADER, [
            ("e1", "E-1", "N-1", "home",
             "2026-04-09T08:00:00Z", "2026-04-10T09:00:00Z", "G-H", 100, T1),
            # push 渠道没有任何更正/回执
            ("e2", "E-2", "N-1", "push",
             "2026-04-09T08:00:00Z", "2026-04-10T09:00:00Z", "G-P", 40, T1),
        ])
        self.build()
        totals = lag_totals(self.conn)
        self.assertEqual(totals["total_views"], 140)
        self.assertEqual(totals["lag_window_views"], 140)
        self.assertEqual(totals["exposures_without_correction"], 1)
        uncorrected = [r for r in exposure_alignment(self.conn)
                       if r["correction_id"] is None]
        self.assertEqual(uncorrected[0]["estimated_views"], 40)

    def test_totals_are_stable_across_rebuilds(self) -> None:
        self._seed()
        self.ingest("exposures", EXPOSURE_HEADER, [
            ("e1", "E-1", "N-1", "home",
             "2026-04-10T09:00:00Z", "2026-04-10T11:00:00Z", "G-H", 100, T1),
        ])
        self.build()
        first = dict(lag_totals(self.conn))
        self.build()
        second = dict(lag_totals(self.conn))
        self.assertEqual(first, second)
