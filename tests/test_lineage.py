"""更正谱系与端到端时间线。"""

from __future__ import annotations

from clarification_facts.metrics import correction_lineage, correction_timeline
from _helpers import (
    CORR_HEADER, RECEIPT_HEADER, WarehouseTestCase,
)

T1 = "2026-04-10T12:00:00Z"
T2 = "2026-04-11T12:00:00Z"
T3 = "2026-04-13T12:00:00Z"


class LineageTests(WarehouseTestCase):
    def _seed_chain(self) -> None:
        self.ingest("corrections", CORR_HEADER, [
            ("r1", "CR-100", "N-1", 1, "", "CLAIM-1", "partial",
             "2026-04-10T08:30:00Z", "2026-04-10T09:00:00Z", T1),
        ])
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc1", "RR-1", "CR-100", "home", "2026-04-10T09:20:00Z",
             "confirmed", "G-H", T1),
        ])
        self.ingest("corrections", CORR_HEADER, [
            ("r1b", "CR-100", "N-1", 1, "", "CLAIM-1", "amended",
             "2026-04-10T08:30:00Z", "2026-04-10T09:00:00Z", T2),
            ("r2", "CR-101", "N-1", 2, "CR-100", "CLAIM-1", "published",
             "2026-04-11T10:00:00Z", "2026-04-11T12:00:00Z", T2),
        ])
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc2", "RR-3", "CR-101", "home", "2026-04-11T12:20:00Z",
             "confirmed", "G-H", T2),
        ])
        self.ingest("corrections", CORR_HEADER, [
            ("r3", "CR-102", "N-1", 3, "CR-101", "CLAIM-1", "withdrawn",
             "2026-04-13T09:00:00Z", "2026-04-13T10:00:00Z", T3),
        ])

    def test_lineage_orders_revisions_and_links_predecessors(self) -> None:
        self._seed_chain()
        self.build()
        lineage = correction_lineage(self.conn, "N-1")
        ids = [r["correction_id"] for r in lineage]
        self.assertEqual(ids, ["CR-100", "CR-101", "CR-102"])
        self.assertEqual(lineage[0]["status"], "amended")
        self.assertEqual(lineage[1]["predecessor_id"], "CR-100")
        self.assertEqual(lineage[2]["status"], "withdrawn")

    def test_scd2_keeps_full_status_history(self) -> None:
        self._seed_chain()
        self.build()
        history = self.conn.execute(
            "SELECT status FROM dim_correction WHERE correction_id = 'CR-100' "
            "ORDER BY effective_from"
        ).fetchall()
        self.assertEqual([r["status"] for r in history], ["partial", "amended"])

    def test_timeline_merges_all_event_types_in_order(self) -> None:
        self._seed_chain()
        self.build()
        events = correction_timeline(self.conn, "CR-100")
        kinds = [e["event"] for e in events]
        self.assertIn("confirmed", kinds)
        self.assertIn("channel_publication", kinds)
        self.assertIn("receipt", kinds)
        # 时间单调不减
        stamps = [str(e["at"]) for e in events]
        self.assertEqual(stamps, sorted(stamps))
        # 回执事件标注了摄入时间，可解释迟到
        receipt_events = [e for e in events if e["event"] == "receipt"]
        self.assertTrue(all("摄入于" in str(e["detail"]) for e in receipt_events))
