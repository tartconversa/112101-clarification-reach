"""端到端演示：从零构建仓库并展示治理查询。

运行（在项目根目录）：

    python -I examples/end_to_end_demo.py

脚本使用独立临时目录，不影响 sources/ 与 data/。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# 允许未安装包时直接从源码树运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clarification_facts import metrics
from clarification_facts.db import connect, init_schema
from clarification_facts.etl import rebuild, rebuild_at
from clarification_facts.ingestion import ingest_config_sources
from clarification_facts.config import WarehouseConfig
from clarification_facts.synthetic import generate_synthetic


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        synth_dir = generate_synthetic(root / "synthetic")
        config = WarehouseConfig(
            db_path=root / "warehouse.db",
            sources_dir=Path("sources"),
            extra_source_dirs=(synth_dir,),
        )
        ingest_config_sources(config)
        conn = connect(config.db_path)
        init_schema(conn)
        result = rebuild(conn, receipt_sla_hours=72)
        print(f"构建 {result.build_id}: 覆盖单元 {result.coverage_rows} 个，"
              f"曝光对齐 {result.alignment_rows} 条\n")

        print("== 覆盖四态汇总（CR-101）==")
        for row in metrics.coverage_summary(conn, correction_id="CR-101"):
            print(f"  {row['report_bucket']:<16} 单元={row['unit_count']} "
                  f"未知规模单元={row['units_with_unknown_size']} "
                  f"确认人数={row['confirmed_users']}")

        print("\n== 受众非相加口径（CR-101）==")
        reach = metrics.audience_reach(conn, "CR-101")
        print(f"  {reach.display}（直接相加={reach.summed_targets}）")

        print("\n== 滞后曝光全局合计 ==")
        totals = metrics.lag_totals(conn)
        print(f"  总曝光={totals['total_views']} "
              f"滞后窗口={totals['lag_window_views']} "
              f"确认后={totals['post_coverage_views']} "
              f"无纠正覆盖条数={totals['exposures_without_correction']}")

        print("\n== 历史重放（2026-04-10 已知信息）==")
        replay_db = root / "replay.db"
        rebuild_at(config.db_path, replay_db, "2026-04-10T23:59:59Z")
        replay_conn = connect(replay_db)
        for row in metrics.coverage_rows(replay_conn, correction_id="CR-100"):
            print(f"  {row['channel_id']}/{row['cohort_ref']}: "
                  f"{row['report_bucket']} —— {row['reason']}")
        replay_conn.close()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
