"""仓库配置：默认路径、SLA 与口径参数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES_DIR = PROJECT_ROOT / "sources"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "warehouse.db"
DEFAULT_SYNTH_DIR = PROJECT_ROOT / "data" / "synthetic"

# 基线三表（随项目提供的合成来源资料）
BASELINE_SOURCE_FILES = {
    "corrections": "corrections.csv",
    "channel_receipts": "channel_receipts.csv",
    "exposures": "exposures.csv",
}

# 扩展表（持续导入的新事件与受众资料，缺失时跳过）
EXTENDED_SOURCE_FILES = {
    "channel_publications": "channel_publications.csv",
    "audience_membership": "audience_membership.csv",
    "cohort_overlap": "cohort_overlap.csv",
}

ALL_SOURCE_KINDS = (
    "corrections",
    "channel_publications",
    "channel_receipts",
    "exposures",
    "audience_membership",
    "cohort_overlap",
)


@dataclass(frozen=True)
class WarehouseConfig:
    """仓库运行参数。

    receipt_sla_hours: 发布后多少小时仍无任何回执，则该渠道-受众单元计为
    ``unverifiable``（缺失回执视为无法核实，而非零到达）。
    """

    db_path: Path = DEFAULT_DB_PATH
    sources_dir: Path = DEFAULT_SOURCES_DIR
    receipt_sla_hours: int = 72
    strict: bool = True  # 同 record_id 载荷变化时报错而非忽略
    extra_source_dirs: tuple[Path, ...] = field(default_factory=tuple)

    def candidate_files(self) -> dict[str, list[Path]]:
        """返回每类来源的全部候选文件（基线目录 + 扩展目录，递归扫描）。

        扩展目录支持嵌套批次目录（如 ``batch1_*/corrections.csv``），
        按完整路径排序以保证批次摄入顺序确定。
        """
        result: dict[str, list[Path]] = {}
        known = {**BASELINE_SOURCE_FILES, **EXTENDED_SOURCE_FILES}
        baseline_files = {
            kind: [self.sources_dir / fname]
            for kind, fname in BASELINE_SOURCE_FILES.items()
            if (self.sources_dir / fname).exists()
        }
        for kind, filename in known.items():
            extra_paths = []
            for directory in self.extra_source_dirs:
                if not directory.exists():
                    continue
                extra_paths.extend(sorted(directory.rglob(filename)))
            # 去重：扩展目录若与基线目录重叠，不重复摄入同一文件
            baseline = baseline_files.get(kind, [])
            seen = {p.resolve() for p in baseline}
            unique_extra = []
            for p in extra_paths:
                resolved = p.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    unique_extra.append(p)
            result[kind] = baseline + unique_extra
        return result
