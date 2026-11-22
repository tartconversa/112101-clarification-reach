# 公开澄清传播数据仓库

面向内容核验团队与分发运营团队的 Python 数据仓库，回答治理问题：**一条更正只是被发布，还是确实覆盖了原内容曾触达的群体？更正到达滞后期间的曝光如何计入？**

项目仅使用 Python 标准库与 SQLite，无需网络或第三方包。所有资料为虚构样例，不对应真实个人或平台。

## 核心口径

### 覆盖四态（渠道 × 受众群 × 更正版本）

| 状态 | 含义 | 判定依据 |
| --- | --- | --- |
| `confirmed` 已确认到达 | 存在覆盖该单元的 confirmed 回执 | 回执证据，不是发布回执 |
| `published_only` 仅已发布 | 有渠道发布事件但尚无任何回执，且未超 SLA | 发布≠到达 |
| `unverifiable` 无法核实 | 回执为 partial/pending/failed/expired，或超 SLA 仍无回执 | **缺失回执不等于零曝光** |
| `not_applicable` 不再适用 | 更正版本已 amended（被修订替代）或 withdrawn（撤回） | 证据保留，停止追踪到达 |

内部还有 `not_published`（更正已确认但该渠道无发布事件），对外报表并入 `unverifiable`。

### 两条不可违反的纪律

1. **受众不直接相加**：不同渠道的 cohort 可能包含同一用户。有名册时按 `user_id` 去重；有重叠表时扣除显式重叠；都没有时只给区间（最大单元规模 ~ 各单元求和）并标注"规模未知"，绝不输出唯一人数。
2. **历史曝光不被抹掉**：原曝光按"确认到达时间"切分为滞后窗口曝光（`lag_window_views`）与确认后曝光（`post_coverage_views`）。无任何更正覆盖的曝光整条计入滞后窗口，新的推荐统计不能抵消它。

## 架构分层

```
CSV 来源（sources/ 基线 + 任意扩展批次目录）
  │  仅追加摄入，record_id 幂等，载荷不可变
  ▼
raw_*          原始层：完整载荷快照 + batch_id + ingested_at（摄入时间轴）
  │  全量可重算重建（每次构建独立 build_id，参数留痕）
  ▼
dim_/fact_     DWD：dim_correction 为 SCD2（状态修订史），事实表不可变
  │
  ▼
dws_*          DWS：覆盖四态、曝光-纠正对齐、受众去重；快照只追加不覆盖
```

**双时间轴**：每行同时保留业务事件时间（`published_at`/`received_at`/`started_at`）与系统摄入时间（`ingested_at`）。`replay --known-at` 可在独立数据库副本上用历史某时点已知的信息重算，迟到、重复回执不会回写改变历史口径。

## 安装

```bash
python -m pip install -e .
```

也可直接设 `PYTHONPATH=src` 运行；所有命令支持 `python -I` 隔离模式。

## 常用命令

```bash
# 1. 初始化与基线摄入
python -m clarification_facts.cli --db data/warehouse.db init-db
python -m clarification_facts.cli --db data/warehouse.db ingest
python -m clarification_facts.cli --db data/warehouse.db build

# 2. 生成并导入扩展合成场景（分批次，含迟到/重复回执、修订、撤回）
python -m clarification_facts.cli synthesize
python -m clarification_facts.cli --db data/warehouse.db --dir data/synthetic ingest
python -m clarification_facts.cli --db data/warehouse.db build

# 3. 治理查询
python -m clarification_facts.cli --db data/warehouse.db coverage                 # 四态明细+汇总
python -m clarification_facts.cli --db data/warehouse.db coverage --correction CR-101
python -m clarification_facts.cli --db data/warehouse.db reach --correction CR-101  # 非相加受众
python -m clarification_facts.cli --db data/warehouse.db lag --content N-100        # 滞后窗口曝光
python -m clarification_facts.cli --db data/warehouse.db lineage --content N-100    # 更正版本谱系
python -m clarification_facts.cli --db data/warehouse.db timeline --correction CR-100
python -m clarification_facts.cli --db data/warehouse.db batches                    # 摄入/构建审计

# 4. 历史时点重算（双时间轴）
python -m clarification_facts.cli --db data/warehouse.db replay \
    --known-at 2026-04-10T23:59:59Z --out-db data/replay1.db
```

## 来源文件契约

- UTF-8 CSV，时间为带时区 ISO 8601，入库时规整为 UTC `...Z`。
- `record_id` 在同类型来源内全局唯一，是摄入幂等键。
- 可选 `ingested_at` 列显式声明摄入时间，用于构造迟到数据与分批场景；缺省取导入时刻。
- 同 `record_id` 重新导入：载荷一致计 duplicated 跳过；载荷变化在 strict 模式下拒绝并整体回滚（原始证据不可静默改写）。

五类来源：`corrections`、`channel_publications`（显式渠道发布）、`channel_receipts`、`exposures`、`audience_membership`（可选）、`cohort_overlap`（可选）。扩展目录支持嵌套批次目录，递归扫描。

发布与到达严格分离：`channel_publications` 只证明更正在渠道入口生效；到达必须由 `channel_receipts` 的 confirmed 回执证明。没有显式发布记录时，发布事实可由回执反推（兼容基线资料），但显式记录优先。

## 测试

```bash
python -m unittest discover -s tests -v
```

## 目录

- `sources/` 随项目提供的基线合成资料（不可被导入流程修改）。
- `src/clarification_facts/` 仓库包：`schema.sql`、摄入、ETL、查询、CLI、合成生成器。
- `tests/` 单元测试（摄入幂等/不可变、四态、SCD2、受众去重、滞后曝光、双时间轴、谱系）。
- `data/` 运行产物（SQLite 库、合成批次），不入库。
- `docs/` 口径与架构说明。
