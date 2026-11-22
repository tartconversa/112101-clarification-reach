"""原始层摄入：幂等、不可变载荷、双时间轴。"""

from __future__ import annotations

import csv

from clarification_facts.ingestion import ingest_file
from clarification_facts.validation import ValidationError
from _helpers import (
    CORR_HEADER, RECEIPT_HEADER, WarehouseTestCase, write_csv,
)


class IngestionTests(WarehouseTestCase):
    def _corr_file(self, rows: list[tuple[object, ...]], name: str = "c.csv"):
        return write_csv(self.tmp / name, CORR_HEADER, rows)

    def test_idempotent_same_payload(self) -> None:
        row = ("r1", "CR-1", "N-1", 1, "", "CLAIM-1", "published",
               "2026-04-10T08:00:00Z", "2026-04-10T09:00:00Z",
               "2026-04-10T12:00:00Z")
        path = self._corr_file([row])
        first = ingest_file(self.conn, "corrections", path)
        second = ingest_file(self.conn, "corrections", path)
        self.assertEqual(first.rows_inserted, 1)
        self.assertEqual(second.rows_inserted, 0)
        self.assertEqual(second.rows_duplicated, 1)
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM raw_correction WHERE record_id = 'r1'"
        ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_payload_change_rejected_in_strict_mode(self) -> None:
        path1 = self._corr_file([
            ("r1", "CR-1", "N-1", 1, "", "CLAIM-1", "published",
             "2026-04-10T08:00:00Z", "2026-04-10T09:00:00Z",
             "2026-04-10T12:00:00Z")])
        ingest_file(self.conn, "corrections", path1)
        path2 = self._corr_file([
            ("r1", "CR-1", "N-1", 1, "", "CLAIM-1", "withdrawn",
             "2026-04-10T08:00:00Z", "2026-04-10T09:00:00Z",
             "2026-04-10T12:00:00Z")], name="c2.csv")
        with self.assertRaises(ValidationError):
            ingest_file(self.conn, "corrections", path2, strict=True)
        # 原始行未被改写
        state = self.conn.execute(
            "SELECT status FROM raw_correction WHERE record_id = 'r1'"
        ).fetchone()["status"]
        self.assertEqual(state, "published")

    def test_duplicate_receipt_rows_both_kept(self) -> None:
        rows = [
            ("rc1", "RR-1", "CR-1", "home", "2026-04-10T09:20:00Z",
             "confirmed", "G-1", "2026-04-10T12:00:00Z"),
            ("rc2", "RR-1", "CR-1", "home", "2026-04-10T09:25:00Z",
             "confirmed", "G-1", "2026-04-11T12:00:00Z"),
        ]
        path = write_csv(self.tmp / "r.csv", RECEIPT_HEADER, rows)
        result = ingest_file(self.conn, "channel_receipts", path)
        self.assertEqual(result.rows_inserted, 2)

    def test_bad_timestamp_rejected_and_rolled_back(self) -> None:
        rows = [
            ("rc1", "RR-1", "CR-1", "home", "2026-04-10 09:20:00",
             "confirmed", "G-1", "2026-04-10T12:00:00Z"),
        ]
        path = write_csv(self.tmp / "bad.csv", RECEIPT_HEADER, rows)
        with self.assertRaises(ValidationError):
            ingest_file(self.conn, "channel_receipts", path)
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM raw_channel_receipt"
        ).fetchone()["n"]
        self.assertEqual(n, 0)
        n_batch = self.conn.execute(
            "SELECT COUNT(*) AS n FROM meta_ingest_batch"
        ).fetchone()["n"]
        self.assertEqual(n_batch, 0)

    def test_ingested_at_distinct_from_event_time(self) -> None:
        rows = [
            ("rc1", "RR-1", "CR-1", "home", "2026-04-10T09:20:00Z",
             "confirmed", "G-1", "2026-04-15T12:00:00Z"),
        ]
        path = write_csv(self.tmp / "r.csv", RECEIPT_HEADER, rows)
        ingest_file(self.conn, "channel_receipts", path)
        row = self.conn.execute(
            "SELECT received_at, ingested_at FROM raw_channel_receipt"
        ).fetchone()
        self.assertEqual(row["received_at"], "2026-04-10T09:20:00Z")
        self.assertEqual(row["ingested_at"], "2026-04-15T12:00:00Z")

    def test_unknown_enum_rejected(self) -> None:
        rows = [
            ("r1", "CR-1", "N-1", 1, "", "CLAIM-1", "exploded",
             "2026-04-10T08:00:00Z", "2026-04-10T09:00:00Z",
             "2026-04-10T12:00:00Z")]
        path = self._corr_file(rows)
        with self.assertRaises(ValidationError):
            ingest_file(self.conn, "corrections", path)

    def test_missing_column_rejected(self) -> None:
        path = self.tmp / "x.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerow(["record_id", "correction_id"])
        with self.assertRaises(ValidationError):
            ingest_file(self.conn, "corrections", path)
