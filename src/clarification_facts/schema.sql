-- 公开澄清传播数据仓库 DDL（SQLite）。
--
-- 设计约定：
--   1. raw_ 表为仅追加原始层，record_id 全局唯一；同一记录重新导入时，
--      载荷一致则跳过，载荷变化则拒绝（strict 模式），保证原始证据不可变。
--   2. 每条 raw 记录带 batch_id 与 ingested_at（系统摄入时间），
--      与记录自身的事件时间（published_at / received_at / started_at）相互独立，
--      构成双时间轴（事件时间 + 摄入时间），支持"以历史某时点已知信息重算"。
--   3. dim_ 表为 SCD2 缓慢变化维，fact_ 表为不可变事件事实，
--      每次构建全量重算并以 build_id 留痕，可随时复现任一批次的口径。
--   4. 时间统一为 UTC ISO 字符串（YYYY-MM-DDTHH:MM:SSZ），字典序可比。

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 元数据：摄入批次与构建批次
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS meta_ingest_batch (
    batch_id        TEXT PRIMARY KEY,
    source_kind     TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    file_sha256     TEXT NOT NULL,
    ingested_at     TEXT NOT NULL,
    rows_seen       INTEGER NOT NULL,
    rows_inserted   INTEGER NOT NULL,
    rows_duplicated INTEGER NOT NULL,
    rows_rejected   INTEGER NOT NULL,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS meta_build (
    build_id        TEXT PRIMARY KEY,
    built_at        TEXT NOT NULL,
    source_batch_ids TEXT NOT NULL,   -- 构建时纳入的摄入批次，JSON 数组
    params_json     TEXT NOT NULL,    -- 构建参数（SLA 等），保证口径可复现
    notes           TEXT
);

-- ---------------------------------------------------------------------------
-- 原始层（仅追加，不可变）
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS raw_correction (
    record_id        TEXT PRIMARY KEY,
    correction_id    TEXT NOT NULL,
    content_id       TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    predecessor_id   TEXT,            -- 上一版本 correction_id，构成修订链
    scope_ref        TEXT NOT NULL,   -- 被纠正的原说法标识
    status           TEXT NOT NULL,   -- published|partial|amended|withdrawn
    confirmed_at     TEXT,            -- 核验团队确认时间（事件时间）
    published_at     TEXT NOT NULL,   -- 记录中的发布时间（事件时间）
    payload_json     TEXT NOT NULL,   -- 原始行完整快照
    batch_id         TEXT NOT NULL,
    ingested_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_channel_publication (
    record_id        TEXT PRIMARY KEY,
    correction_id    TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    published_at     TEXT NOT NULL,   -- 渠道发布事件时间（事件时间）
    payload_json     TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    ingested_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_channel_receipt (
    record_id        TEXT PRIMARY KEY,
    receipt_id       TEXT NOT NULL,
    correction_id    TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    receipt_state    TEXT NOT NULL,   -- confirmed|partial|pending|failed|expired
    cohort_ref       TEXT NOT NULL,
    received_at      TEXT NOT NULL,   -- 回执到达平台时间（事件时间，可能迟到）
    payload_json     TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    ingested_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_exposure (
    record_id        TEXT PRIMARY KEY,
    exposure_id      TEXT NOT NULL,
    content_id       TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    cohort_ref       TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    ended_at         TEXT NOT NULL,
    estimated_views  INTEGER NOT NULL,
    payload_json     TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    ingested_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_audience_membership (
    record_id        TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    cohort_ref       TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    valid_from       TEXT NOT NULL,
    valid_to         TEXT,            -- NULL 表示当前仍有效
    payload_json     TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    ingested_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_cohort_overlap (
    record_id        TEXT PRIMARY KEY,
    cohort_ref_a     TEXT NOT NULL,
    cohort_ref_b     TEXT NOT NULL,
    channel_a        TEXT NOT NULL,
    channel_b        TEXT NOT NULL,
    overlap_users    INTEGER NOT NULL,  -- 两渠道-受众单元之间的去重重叠人数
    payload_json     TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    ingested_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- DWD：SCD2 维度
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dim_content (
    content_key      INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id       TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL,
    UNIQUE(content_id)
);

-- 更正版本 SCD2：同一 correction_id 的状态随批次变化（如 published -> amended）
CREATE TABLE IF NOT EXISTS dim_correction (
    correction_key   INTEGER PRIMARY KEY AUTOINCREMENT,
    correction_id    TEXT NOT NULL,
    content_id       TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    predecessor_id   TEXT,
    scope_ref        TEXT NOT NULL,
    status           TEXT NOT NULL,
    confirmed_at     TEXT,
    published_at     TEXT NOT NULL,
    effective_from   TEXT NOT NULL,   -- 该版本状态生效的摄入时间
    effective_to     TEXT,            -- NULL 为当前行
    is_current       INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    build_id         TEXT NOT NULL,
    source_record_id TEXT NOT NULL,   -- 对应 raw 记录，保证同批次多行也唯一
    UNIQUE(correction_id, effective_from, source_record_id)
);

CREATE TABLE IF NOT EXISTS dim_channel (
    channel_key      INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id       TEXT NOT NULL UNIQUE,
    first_seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_cohort (
    cohort_key       INTEGER PRIMARY KEY AUTOINCREMENT,
    cohort_ref       TEXT NOT NULL UNIQUE,
    first_seen_at    TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- DWD：事件事实（每次构建全量重算）
-- ---------------------------------------------------------------------------

-- 渠道发布事件：仅证明"更正被发布到渠道"，不证明受众已收到
CREATE TABLE IF NOT EXISTS fact_publication (
    publication_key  INTEGER PRIMARY KEY AUTOINCREMENT,
    correction_id    TEXT NOT NULL,
    content_id       TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    published_at     TEXT NOT NULL,
    build_id         TEXT NOT NULL,
    UNIQUE(correction_id, channel_id)
);

-- 到达回执事实：同键回执可能重复/迟到，保留全部原始记录并标记当前采用值
CREATE TABLE IF NOT EXISTS fact_delivery_receipt (
    receipt_key      INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id        TEXT NOT NULL,   -- 原始记录键：同批次重复回执也可区分
    receipt_id       TEXT NOT NULL,
    correction_id    TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    cohort_ref       TEXT NOT NULL,
    receipt_state    TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    ingested_at      TEXT NOT NULL,
    is_latest        INTEGER NOT NULL CHECK (is_latest IN (0, 1)),
    build_id         TEXT NOT NULL,
    UNIQUE(record_id)
);

-- 曝光事实（原内容触达），不可变
CREATE TABLE IF NOT EXISTS fact_exposure (
    exposure_key     INTEGER PRIMARY KEY AUTOINCREMENT,
    exposure_id      TEXT NOT NULL UNIQUE,
    content_id       TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    cohort_ref       TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    ended_at         TEXT NOT NULL,
    estimated_views  INTEGER NOT NULL,
    build_id         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_audience_membership (
    membership_key   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          TEXT NOT NULL,
    cohort_ref       TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    valid_from       TEXT NOT NULL,
    valid_to         TEXT,
    build_id         TEXT NOT NULL,
    UNIQUE(user_id, cohort_ref, channel_id, valid_from)
);

CREATE TABLE IF NOT EXISTS fact_cohort_overlap (
    overlap_key      INTEGER PRIMARY KEY AUTOINCREMENT,
    cohort_ref_a     TEXT NOT NULL,
    cohort_ref_b     TEXT NOT NULL,
    channel_a        TEXT NOT NULL,
    channel_b        TEXT NOT NULL,
    overlap_users    INTEGER NOT NULL,
    build_id         TEXT NOT NULL,
    UNIQUE(cohort_ref_a, cohort_ref_b, channel_a, channel_b)
);

-- ---------------------------------------------------------------------------
-- DWS：覆盖四态（渠道 × 受众 × 更正版本）
-- 行级口径单元；target_users 为 NULL 表示没有受众名册，只能给事件级证据。
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dws_coverage (
    coverage_key         INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id             TEXT NOT NULL,
    correction_id        TEXT NOT NULL,
    revision             INTEGER NOT NULL,
    content_id           TEXT NOT NULL,
    scope_ref            TEXT NOT NULL,
    channel_id           TEXT NOT NULL,
    cohort_ref           TEXT NOT NULL,
    coverage_bucket      TEXT NOT NULL,  -- 内部五态，报表用 report_bucket
    report_bucket        TEXT NOT NULL,  -- 对外四态
    target_users         INTEGER,         -- NULL = 无名册，规模未知
    confirmed_users      INTEGER,         -- NULL = 无用户级证据
    first_published_at   TEXT,
    first_confirmed_at   TEXT,
    latest_receipt_state TEXT,
    lag_minutes          INTEGER,         -- 发布 -> 首次确认到达的滞后分钟数
    reason               TEXT NOT NULL,
    UNIQUE(build_id, correction_id, channel_id, cohort_ref)
);

-- 原曝光与纠正生效的对齐：滞后期间曝光单独标记，不被新推荐统计抹掉
CREATE TABLE IF NOT EXISTS dws_exposure_correction (
    alignment_key        INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id             TEXT NOT NULL,
    exposure_id          TEXT NOT NULL,
    content_id           TEXT NOT NULL,
    channel_id           TEXT NOT NULL,
    cohort_ref           TEXT NOT NULL,
    exposure_started_at  TEXT NOT NULL,
    exposure_ended_at    TEXT NOT NULL,
    estimated_views      INTEGER NOT NULL,
    correction_id        TEXT,            -- 适用的更正（NULL = 该曝光尚无更正）
    correction_published_at TEXT,
    first_confirmed_at   TEXT,
    lag_window_views     INTEGER NOT NULL,-- 发布前/到达前滞后窗口内的曝光量
    post_coverage_views  INTEGER NOT NULL,-- 确认到达之后的曝光量
    UNIQUE(build_id, exposure_id)
);

-- 构建批次留痕的覆盖快照（每次构建追加，旧快照永不覆盖，支持口径回溯）
CREATE TABLE IF NOT EXISTS dws_coverage_snapshot (
    snapshot_key         INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id             TEXT NOT NULL,
    taken_at             TEXT NOT NULL,
    correction_id        TEXT NOT NULL,
    channel_id           TEXT NOT NULL,
    cohort_ref           TEXT NOT NULL,
    report_bucket        TEXT NOT NULL,
    target_users         INTEGER,
    confirmed_users      INTEGER,
    lag_minutes          INTEGER
);

-- ---------------------------------------------------------------------------
-- 索引
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_raw_corr_content ON raw_correction(content_id);
CREATE INDEX IF NOT EXISTS idx_raw_corr_cid ON raw_correction(correction_id);
CREATE INDEX IF NOT EXISTS idx_raw_pub_corr ON raw_channel_publication(correction_id);
CREATE INDEX IF NOT EXISTS idx_raw_rc_corr ON raw_channel_receipt(correction_id);
CREATE INDEX IF NOT EXISTS idx_raw_rc_chan_cohort ON raw_channel_receipt(channel_id, cohort_ref);
CREATE INDEX IF NOT EXISTS idx_raw_exp_content ON raw_exposure(content_id);
CREATE INDEX IF NOT EXISTS idx_dim_corr_current ON dim_correction(is_current);
CREATE INDEX IF NOT EXISTS idx_dws_cov_build ON dws_coverage(build_id);
CREATE INDEX IF NOT EXISTS idx_dws_cov_corr ON dws_coverage(correction_id);
CREATE INDEX IF NOT EXISTS idx_dws_ec_build ON dws_exposure_correction(build_id);
