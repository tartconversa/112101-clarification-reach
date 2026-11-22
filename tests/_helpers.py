"""测试夹具：临时仓库与分阶段摄入辅助。"""

from __future__ import annotations

import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from clarification_facts.db import connect, init_schema
from clarification_facts.etl import rebuild
from clarification_facts.ingestion import ingest_file


def write_csv(path: Path, header: list[str], rows: list[tuple[object, ...]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)
    return path


CORR_HEADER = [
    "record_id", "correction_id", "content_id", "revision", "predecessor_id",
    "scope_ref", "status", "confirmed_at", "published_at", "ingested_at",
]
RECEIPT_HEADER = [
    "record_id", "receipt_id", "correction_id", "channel_id",
    "received_at", "receipt_state", "cohort_ref", "ingested_at",
]
EXPOSURE_HEADER = [
    "record_id", "exposure_id", "content_id", "channel_id",
    "started_at", "ended_at", "cohort_ref", "estimated_views", "ingested_at",
]
MEMBER_HEADER = [
    "record_id", "user_id", "cohort_ref", "channel_id",
    "valid_from", "valid_to", "ingested_at",
]
OVERLAP_HEADER = [
    "record_id", "cohort_ref_a", "cohort_ref_b", "channel_a",
    "channel_b", "overlap_users", "ingested_at",
]


class WarehouseTestCase(unittest.TestCase):
    """每个用例独立临时目录与数据库。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "warehouse.db"
        self.conn: sqlite3.Connection = connect(self.db_path)
        init_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def ingest(self, kind: str, header: list[str],
               rows: list[tuple[object, ...]], *,
               filename: str | None = None) -> None:
        name = filename or f"{kind}.csv"
        path = write_csv(self.tmp / name, header, rows)
        result = ingest_file(self.conn, kind, path)
        # ingest_file 自身已提交；显式提交避免连接残留的隐式事务影响后续 rebuild
        self.conn.commit()
        return result

    def build(self, *, sla_hours: int = 72) -> str:
        result = rebuild(self.conn, receipt_sla_hours=sla_hours)
        return result.build_id

    def coverage_map(self, build_id: str | None = None
                     ) -> dict[tuple[str, str, str], sqlite3.Row]:
        from clarification_facts.metrics import coverage_rows
        return {
            (r["correction_id"], r["channel_id"], r["cohort_ref"]): r
            for r in coverage_rows(self.conn, build_id=build_id)
        }
