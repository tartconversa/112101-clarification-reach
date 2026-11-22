"""SQLite 连接管理与 schema 初始化。"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

from . import __name__ as _pkg_name


def _schema_text() -> str:
    # 与包内 schema.sql 一起分发；resources.files 在 3.9+ 可用
    return resources.files(_pkg_name).joinpath("schema.sql").read_text(encoding="utf-8")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开仓库数据库并启用外键与 WAL。

    调用方负责关闭连接。数据库文件与父目录按需创建。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # autocommit 隔离级别：事务边界完全由代码中的显式 BEGIN/commit 控制，
    # 避免 Python sqlite3 隐式事务与显式 BEGIN 在重建流程中相互干扰。
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等创建全部表、视图与索引。"""
    conn.executescript(_schema_text())
    conn.commit()


def latest_build_id(conn: sqlite3.Connection) -> str | None:
    """最近一次成功构建的 build_id（无构建时返回 None）。

    同秒内可能连续多次构建，built_at 不足以排序，按插入顺序 rowid 取最新。
    """
    row = conn.execute(
        "SELECT build_id FROM meta_build ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return row["build_id"] if row else None


def all_build_ids(conn: sqlite3.Connection) -> list[str]:
    return [
        row["build_id"]
        for row in conn.execute(
            "SELECT build_id FROM meta_build ORDER BY rowid"
        ).fetchall()
    ]
