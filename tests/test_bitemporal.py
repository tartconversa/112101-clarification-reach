"""双时间轴：按历史摄入时点重算，迟到数据不回写改变历史口径。"""

from __future__ import annotations

from pathlib import Path

from clarification_facts.db import connect
from clarification_facts.etl import rebuild_at
from clarification_facts.enums import CoverageBucket
from clarification_facts.metrics import coverage_rows
from _helpers import (
    CORR_HEADER, EXPOSURE_HEADER, RECEIPT_HEADER, WarehouseTestCase,
)

T1 = "2026-04-10T12:00:00Z"
T2 = "2026-04-11T12:00:00Z"


class BitemporalTests(WarehouseTestCase):
    def _seed(self) -> None:
        self.ingest("corrections", CORR_HEADER, [
            ("r1", "CR-1", "N-1", 1, "", "CLAIM-1", "published",
             "2026-04-10T08:30:00Z", "2026-04-10T09:00:00Z", T1),
        ])
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc1", "RR-1", "CR-1", "search", "2026-04-10T10:05:00Z",
             "partial", "G-S", T1),
        ])
        self.ingest("exposures", EXPOSURE_HEADER, [
            ("e1", "E-1", "N-1", "search",
             "2026-04-09T08:00:00Z", "2026-04-10T09:00:00Z", "G-S", 100, T1),
        ])

    def test_replay_excludes_later_ingestions(self) -> None:
        self._seed()
        self.build()
        # T2 才迟到的 confirmed 回执
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc2", "RR-1", "CR-1", "search", "2026-04-11T08:00:00Z",
             "confirmed", "G-S", T2),
        ])
        self.build()

        replay_path = Path(self.db_path).with_name("replay.db")
        result = rebuild_at(self.db_path, replay_path, T1)
        self.assertEqual(result.known_at, T1)

        replay_conn = connect(replay_path)
        try:
            rows = coverage_rows(replay_conn)
            self.assertEqual(len(rows), 1)
            # T1 时点只知道 partial -> 无法核实
            self.assertEqual(rows[0]["report_bucket"],
                             CoverageBucket.UNVERIFIABLE.value)
            # 迟到回执在历史库中不存在
            n = replay_conn.execute(
                "SELECT COUNT(*) AS n FROM raw_channel_receipt WHERE ingested_at > ?",
                (T1,),
            ).fetchone()["n"]
            self.assertEqual(n, 0)
        finally:
            replay_conn.close()

    def test_current_build_reflects_late_data(self) -> None:
        self._seed()
        self.ingest("channel_receipts", RECEIPT_HEADER, [
            ("rc2", "RR-1", "CR-1", "search", "2026-04-11T08:00:00Z",
             "confirmed", "G-S", T2),
        ])
        self.build()
        row = self.coverage_map()[("CR-1", "search", "G-S")]
        self.assertEqual(row["report_bucket"], CoverageBucket.CONFIRMED.value)
        self.assertEqual(row["lag_minutes"], 1380)  # 09:00 -> 次日 08:00

    def test_replay_does_not_mutate_source_db(self) -> None:
        self._seed()
        self.build()
        before = self.conn.execute(
            "SELECT COUNT(*) AS n FROM meta_build"
        ).fetchone()["n"]
        replay_path = Path(self.db_path).with_name("replay2.db")
        rebuild_at(self.db_path, replay_path, T1)
        after = self.conn.execute(
            "SELECT COUNT(*) AS n FROM meta_build"
        ).fetchone()["n"]
        self.assertEqual(before, after)

    def test_rebuild_is_deterministic(self) -> None:
        self._seed()
        b1 = self.build()
        rows1 = [dict(r) for r in coverage_rows(self.conn, build_id=b1)]
        b2 = self.build()
        rows2 = [dict(r) for r in coverage_rows(self.conn, build_id=b2)]
        for r in rows1 + rows2:
            r.pop("build_id", None)
            r.pop("coverage_key", None)  # 自增主键，每次重建必然变化
        self.assertEqual(rows1, rows2)
