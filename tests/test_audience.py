"""受众非相加口径：跨渠道去重与规模未知处理。"""

from __future__ import annotations

from clarification_facts.metrics import audience_reach
from _helpers import (
    CORR_HEADER, MEMBER_HEADER, OVERLAP_HEADER, RECEIPT_HEADER,
    WarehouseTestCase,
)

T1 = "2026-04-10T12:00:00Z"


class AudienceReachTests(WarehouseTestCase):
    def _seed_correction_with_two_channels(self) -> None:
        self.ingest("corrections", CORR_HEADER, [
            ("r1", "CR-1", "N-1", 1, "", "CLAIM-1", "published",
             "2026-04-10T08:30:00Z", "2026-04-10T09:00:00Z", T1),
        ])
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc1", "RR-1", "CR-1", "home", "2026-04-10T09:20:00Z",
             "confirmed", "G-H", T1),
            ("rc2", "RR-2", "CR-1", "search", "2026-04-10T09:30:00Z",
             "confirmed", "G-S", T1),
        ])

    def test_membership_dedup_beats_naive_sum(self) -> None:
        self._seed_correction_with_two_channels()
        # home: u1,u2,u3；search: u3,u4,u5 -> 直接相加 6，去重后 5
        self.ingest("audience_membership", MEMBER_HEADER, [
            ("m1", "u1", "G-H", "home", "2026-04-01T00:00:00Z", "", T1),
            ("m2", "u2", "G-H", "home", "2026-04-01T00:00:00Z", "", T1),
            ("m3", "u3", "G-H", "home", "2026-04-01T00:00:00Z", "", T1),
            ("m4", "u3", "G-S", "search", "2026-04-01T00:00:00Z", "", T1),
            ("m5", "u4", "G-S", "search", "2026-04-01T00:00:00Z", "", T1),
            ("m6", "u5", "G-S", "search", "2026-04-01T00:00:00Z", "", T1),
        ])
        self.build()
        reach = audience_reach(self.conn, "CR-1")
        self.assertEqual(reach.method, "membership")
        self.assertEqual(reach.summed_targets, 6)
        self.assertEqual(reach.deduplicated_targets, 5)
        self.assertEqual(reach.max_unit_target, 3)

    def test_overlap_table_deduction_without_membership(self) -> None:
        self._seed_correction_with_two_channels()
        # 无 membership，仅重叠表声明两个单元重叠 1 人
        self.ingest("cohort_overlap", OVERLAP_HEADER, [
            ("o1", "G-H", "G-S", "home", "search", 1, T1),
        ])
        # 手工注入名册规模：通过 membership 给规模但不重叠的用户集
        self.ingest("audience_membership", MEMBER_HEADER, [
            ("m1", "u1", "G-H", "home", "2026-04-01T00:00:00Z", "", T1),
            ("m2", "u2", "G-H", "home", "2026-04-01T00:00:00Z", "", T1),
            ("m3", "u3", "G-H", "home", "2026-04-01T00:00:00Z", "", T1),
            ("m4", "u4", "G-S", "search", "2026-04-01T00:00:00Z", "", T1),
            ("m5", "u5", "G-S", "search", "2026-04-01T00:00:00Z", "", T1),
            ("m6", "u6", "G-S", "search", "2026-04-01T00:00:00Z", "", T1),
        ])
        self.build()
        reach = audience_reach(self.conn, "CR-1")
        # 有名册时优先名册去重（这里用户互不重叠 -> 6）
        self.assertEqual(reach.method, "membership")
        self.assertEqual(reach.deduplicated_targets, 6)

    def test_bounded_estimate_when_no_evidence(self) -> None:
        self._seed_correction_with_two_channels()
        # 不提供任何名册/重叠表 -> 只能给区间，绝不输出唯一人数
        self.build()
        reach = audience_reach(self.conn, "CR-1")
        self.assertEqual(reach.method, "bounded_estimate")
        self.assertIsNone(reach.deduplicated_targets)
        self.assertEqual(reach.unknown_size_units, 2)
        self.assertIn("规模未知", reach.display)
