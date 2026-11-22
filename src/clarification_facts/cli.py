"""命令行入口：

    python -m clarification_facts.cli init-db
    python -m clarification_facts.cli ingest [--dir DIR] [--note N]
    python -m clarification_facts.cli build [--sla-hours 72]
    python -m clarification_facts.cli synthesize [--dir DIR]
    python -m clarification_facts.cli coverage [--correction CR] [--channel CH]
    python -m clarification_facts.cli reach --correction CR
    python -m clarification_facts.cli lag [--content N]
    python -m clarification_facts.cli lineage --content N
    python -m clarification_facts.cli timeline --correction CR
    python -m clarification_facts.cli batches
    python -m clarification_facts.cli replay --known-at 2026-04-11T00:00:00Z
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import metrics
from .config import DEFAULT_SYNTH_DIR, WarehouseConfig
from .db import connect, init_schema, latest_build_id
from .etl import rebuild
from .ingestion import ingest_config_sources, ingest_file
from .synthetic import generate_synthetic


def _config(args: argparse.Namespace) -> WarehouseConfig:
    extra = tuple(Path(d) for d in (args.dir or []))
    return WarehouseConfig(
        db_path=Path(args.db),
        sources_dir=Path(args.sources),
        receipt_sla_hours=args.sla_hours,
        extra_source_dirs=extra,
    )


def _print_table(rows: list[object], columns: list[str]) -> None:
    if not rows:
        print("（无数据）")
        return
    widths = {c: len(c) for c in columns}
    text_rows = []
    for row in rows:
        mapping = dict(row) if not isinstance(row, dict) else row
        text_rows.append({c: "" if mapping.get(c) is None else str(mapping.get(c))
                          for c in columns})
        for c in columns:
            widths[c] = max(widths[c], len(text_rows[-1][c]))
    header = " | ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("-+-".join("-" * widths[c] for c in columns))
    for row in text_rows:
        print(" | ".join(row[c].ljust(widths[c]) for c in columns))


def cmd_init_db(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    init_schema(conn)
    conn.close()
    print(f"已初始化数据库: {args.db}")


def cmd_synthesize(args: argparse.Namespace) -> None:
    out = Path(args.out_dir or DEFAULT_SYNTH_DIR)
    path = generate_synthetic(out, seed=args.seed)
    print(f"合成数据已生成于: {path}")


def cmd_ingest(args: argparse.Namespace) -> None:
    config = _config(args)
    if args.file:
        kind = args.kind
        if not kind:
            raise SystemExit("使用 --file 时必须同时指定 --kind")
        conn = connect(config.db_path)
        init_schema(conn)
        try:
            results = [ingest_file(conn, kind, args.file,
                                   strict=config.strict, note=args.note)]
        finally:
            conn.close()
    else:
        results = ingest_config_sources(config, note=args.note)
    for r in results:
        print(
            f"[{r.source_kind}] {Path(r.source_file).name}: "
            f"seen={r.rows_seen} inserted={r.rows_inserted} "
            f"duplicated={r.rows_duplicated} rejected={r.rows_rejected} "
            f"batch={r.batch_id}"
        )


def cmd_build(args: argparse.Namespace) -> None:
    config = _config(args)
    conn = connect(config.db_path)
    try:
        result = rebuild(conn, receipt_sla_hours=config.receipt_sla_hours,
                         notes=args.note)
    finally:
        conn.close()
    print(
        f"构建完成 build={result.build_id} known_at={result.known_at} "
        f"coverage_units={result.coverage_rows} "
        f"exposure_alignments={result.alignment_rows} "
        f"snapshots={result.snapshot_rows}"
    )


def cmd_coverage(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = metrics.coverage_rows(
            conn, content_id=args.content, correction_id=args.correction,
            channel_id=args.channel, cohort_ref=args.cohort,
        )
        cols = ["correction_id", "channel_id", "cohort_ref", "report_bucket",
                "target_users", "confirmed_users", "lag_minutes",
                "first_published_at", "first_confirmed_at", "reason"]
        if args.json:
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        else:
            _print_table(rows, cols)
            print("\n四态汇总：")
            summary = metrics.coverage_summary(conn, correction_id=args.correction)
            _print_table(summary, ["report_bucket", "unit_count",
                                   "units_with_unknown_size",
                                   "known_target_users", "confirmed_users"])
    finally:
        conn.close()


def cmd_reach(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        reach = metrics.audience_reach(conn, args.correction)
        print(f"更正 {args.correction} 的受众覆盖（非相加口径，方法={reach.method}）：")
        print(f"  渠道-受众单元数: {reach.unit_count}")
        print(f"  规模未知单元: {reach.unknown_size_units}")
        print(f"  直接相加（上界，可能重复计数）: {reach.summed_targets}")
        print(f"  最大单元规模（去重下界）: {reach.max_unit_target}")
        if reach.deduplicated_targets is not None:
            print(f"  去重后受众: {reach.deduplicated_targets}")
        print(f"  结论: {reach.display}")
    finally:
        conn.close()


def cmd_lag(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = metrics.exposure_alignment(conn, content_id=args.content,
                                          channel_id=args.channel)
        cols = ["exposure_id", "content_id", "channel_id", "cohort_ref",
                "estimated_views", "correction_id",
                "correction_published_at", "first_confirmed_at",
                "lag_window_views", "post_coverage_views"]
        _print_table(rows, cols)
        totals = metrics.lag_totals(conn)
        print(
            f"\n合计: 总曝光={totals['total_views']} "
            f"滞后窗口曝光={totals['lag_window_views']} "
            f"确认到达后曝光={totals['post_coverage_views']} "
            f"无纠正覆盖的曝光条数={totals['exposures_without_correction']}"
        )
    finally:
        conn.close()


def cmd_lineage(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        rows = metrics.correction_lineage(conn, args.content)
        _print_table(rows, ["correction_id", "revision", "predecessor_id",
                            "scope_ref", "status", "confirmed_at",
                            "published_at", "confirmed_units", "total_units"])
    finally:
        conn.close()


def cmd_timeline(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        for event in metrics.correction_timeline(conn, args.correction):
            print(f"{event['at']}  {event['event']:<20} {event['detail']}")
    finally:
        conn.close()


def cmd_batches(args: argparse.Namespace) -> None:
    conn = connect(args.db)
    try:
        print("摄入批次：")
        _print_table(metrics.ingest_batches(conn),
                     ["batch_id", "source_kind", "source_file", "ingested_at",
                      "rows_seen", "rows_inserted", "rows_duplicated",
                      "rows_rejected"])
        print("\n构建批次：")
        _print_table(metrics.build_history(conn),
                     ["build_id", "built_at", "params_json", "notes"])
        print(f"\n当前最新构建: {latest_build_id(conn)}")
    finally:
        conn.close()


def cmd_replay(args: argparse.Namespace) -> None:
    """以历史摄入时点重算到独立数据库，演示迟到回执到达前后的口径变化。"""
    from .etl import rebuild_at
    result = rebuild_at(args.db, args.out_db, args.known_at,
                        receipt_sla_hours=args.sla_hours)
    print(f"历史重算完成: {args.out_db} build={result.build_id} "
          f"known_at={args.known_at} coverage_units={result.coverage_rows}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clarification_facts",
        description="公开澄清传播覆盖数据仓库（纯标准库）",
    )
    parser.add_argument("--db", default="data/warehouse.db", help="SQLite 路径")
    parser.add_argument("--sources", default="sources", help="基线来源目录")
    parser.add_argument("--sla-hours", type=int, default=72,
                        dest="sla_hours", help="无回执判定无法核实的 SLA 小时数")
    parser.add_argument("--dir", action="append",
                        help="附加来源目录（可多次，ingest 时使用）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db").set_defaults(func=cmd_init_db)

    p_syn = sub.add_parser("synthesize", help="生成合成场景数据")
    p_syn.add_argument("--out-dir", dest="out_dir")
    p_syn.add_argument("--seed", type=int, default=20260415)
    p_syn.set_defaults(func=cmd_synthesize)

    p_ing = sub.add_parser("ingest", help="摄入 CSV 到原始层")
    p_ing.add_argument("--file", help="摄入单个文件")
    p_ing.add_argument("--kind", choices=sorted(
        ["corrections", "channel_publications", "channel_receipts", "exposures",
         "audience_membership", "cohort_overlap"]))
    p_ing.add_argument("--note")
    p_ing.set_defaults(func=cmd_ingest)

    p_build = sub.add_parser("build", help="重建 DWD/DWS")
    p_build.add_argument("--note")
    p_build.set_defaults(func=cmd_build)

    p_cov = sub.add_parser("coverage", help="覆盖四态查询")
    p_cov.add_argument("--content")
    p_cov.add_argument("--correction")
    p_cov.add_argument("--channel")
    p_cov.add_argument("--cohort")
    p_cov.add_argument("--json", action="store_true")
    p_cov.set_defaults(func=cmd_coverage)

    p_reach = sub.add_parser("reach", help="非相加受众规模")
    p_reach.add_argument("--correction", required=True)
    p_reach.set_defaults(func=cmd_reach)

    p_lag = sub.add_parser("lag", help="滞后窗口曝光")
    p_lag.add_argument("--content")
    p_lag.add_argument("--channel")
    p_lag.set_defaults(func=cmd_lag)

    p_lin = sub.add_parser("lineage", help="内容的更正版本谱系")
    p_lin.add_argument("--content", required=True)
    p_lin.set_defaults(func=cmd_lineage)

    p_tl = sub.add_parser("timeline", help="单条更正的完整事件时间线")
    p_tl.add_argument("--correction", required=True)
    p_tl.set_defaults(func=cmd_timeline)

    sub.add_parser("batches", help="批次与构建审计").set_defaults(func=cmd_batches)

    p_rep = sub.add_parser("replay", help="按历史摄入时点重算（双时间轴）")
    p_rep.add_argument("--known-at", required=True, dest="known_at")
    p_rep.add_argument("--out-db", default="data/warehouse_replay.db",
                       dest="out_db")
    p_rep.set_defaults(func=cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
