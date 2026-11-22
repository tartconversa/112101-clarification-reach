"""CLI 冒烟测试：主流程命令可运行并产出预期内容。"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from clarification_facts.cli import main

ROOT = Path(__file__).resolve().parents[1]


class CliSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "warehouse.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *argv: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--db", str(self.db), *argv])
        return buf.getvalue()

    def test_full_pipeline_and_queries(self) -> None:
        self._run("init-db")
        self._run("--sources", str(ROOT / "sources"), "ingest")
        self._run("build")
        out = self._run("coverage")
        self.assertIn("confirmed", out)
        self.assertIn("unverifiable", out)
        out = self._run("lag")
        self.assertIn("滞后窗口曝光", out)
        out = self._run("batches")
        self.assertIn("摄入批次", out)

    def test_synthesize_and_reach(self) -> None:
        synth = self.tmp / "synthetic"
        self._run("synthesize", "--out-dir", str(synth))
        self.assertTrue((synth / "batch1_2026-04-10" / "corrections.csv").exists())
        self._run("init-db")
        self._run("--sources", str(ROOT / "sources"),
                  "--dir", str(synth), "ingest")
        self._run("build")
        out = self._run("reach", "--correction", "CR-101")
        self.assertIn("去重后受众: 11", out)

    def test_replay_creates_independent_db(self) -> None:
        synth = self.tmp / "synthetic"
        self._run("synthesize", "--out-dir", str(synth))
        self._run("init-db")
        self._run("--sources", str(ROOT / "sources"),
                  "--dir", str(synth), "ingest")
        self._run("build")
        replay_db = self.tmp / "replay.db"
        self._run("replay", "--known-at", "2026-04-10T23:59:59Z",
                  "--out-db", str(replay_db))
        self.assertTrue(replay_db.exists())
        out = self._run("coverage", "--correction", "CR-100")
        # 源库当前视图包含后续批次
        self.assertIn("not_applicable", out)


if __name__ == "__main__":
    unittest.main()
