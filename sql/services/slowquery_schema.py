# -*- coding:utf-8 -*-
"""慢查询本地表结构的幂等引导（建表 + 补索引）

慢查相关模型均为 managed=False，migrate 本不为它们建表。历史上依赖手工执行
两份 DDL：
- src/init_sql/slow_query/*.sql：建表（基础索引写在建表语句内）；
- src/init_sql/slow_query/add_indexes.sql：复合索引补充（MySQL 无
  CREATE INDEX IF NOT EXISTS，重复执行会报错）。

本模块把两份 DDL 统一解析成带存在性检查的操作计划：缺表才建表、缺索引才补建，
供数据迁移（migrate 自动完成首次部署的结构准备）与测试 conftest 复用。
"""
import logging
import re
from pathlib import Path

logger = logging.getLogger("default")

# DDL 目录：sql/services/ -> sql/ -> 项目根 -> src/init_sql/slow_query
DDL_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "init_sql" / "slow_query"

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?(?P<table>\w+)[`\"']?",
    re.IGNORECASE,
)
_CREATE_INDEX_RE = re.compile(
    r"CREATE\s+INDEX\s+[`\"']?(?P<index>\w+)[`\"']?\s+ON\s+[`\"']?(?P<table>\w+)[`\"']?",
    re.IGNORECASE,
)


def _split_statements(sql_text: str):
    """按语句切分 DDL 文件内容（均为简单 DDL：无存储过程/字符串内分号）"""
    stripped = re.sub(r"--[^\n]*", "", sql_text)
    return [s.strip() for s in stripped.split(";") if s.strip()]


def plan_slowquery_schema():
    """解析 DDL 目录，返回有序操作计划。

    Returns:
        list[tuple[str, str, str, str]]: (kind, name, table, sql)。
        kind 为 "table" 或 "index"；name 为表名/索引名；table 为操作目标表。
    """
    plan = []
    table_files = sorted(f for f in DDL_DIR.glob("*.sql") if f.name != "add_indexes.sql")
    index_file = DDL_DIR / "add_indexes.sql"

    # 建表在前、索引在后：索引依赖表存在
    for ddl_file in [*table_files, *([index_file] if index_file.exists() else [])]:
        for statement in _split_statements(ddl_file.read_text(encoding="utf-8")):
            table_match = _CREATE_TABLE_RE.search(statement)
            if table_match:
                table = table_match.group("table")
                plan.append(("table", table, table, statement))
                continue
            index_match = _CREATE_INDEX_RE.search(statement)
            if index_match:
                plan.append(
                    ("index", index_match.group("index"), index_match.group("table"), statement)
                )
    return plan


def ensure_slowquery_schema(connection=None):
    """幂等创建慢查询相关表与缺失的索引。

    Args:
        connection: Django 数据库连接；默认 default 库，迁移中传 schema_editor.connection。

    Returns:
        dict: {"tables_created": [表名...], "indexes_created": [索引名...]}，
        结构已存在时对应列表为空。
    """
    if connection is None:
        from django.db import connection as default_connection

        connection = default_connection

    plan = plan_slowquery_schema()
    existing_tables = set(connection.introspection.table_names())

    tables_created = []
    for kind, name, _, statement in plan:
        if kind != "table" or name in existing_tables:
            continue
        with connection.cursor() as cursor:
            cursor.execute(statement)
        tables_created.append(name)
        existing_tables.add(name)
        logger.info(f"慢查询表结构引导: 创建表 {name}")

    indexes_created = []
    constraints_cache = {}
    for kind, name, table, statement in plan:
        if kind != "index" or table not in existing_tables:
            continue
        if table not in constraints_cache:
            with connection.cursor() as cursor:
                constraints_cache[table] = set(
                    connection.introspection.get_constraints(cursor, table)
                )
        if name in constraints_cache[table]:
            continue
        with connection.cursor() as cursor:
            cursor.execute(statement)
        indexes_created.append(name)
        logger.info(f"慢查询表结构引导: 创建索引 {name}（{table}）")

    return {"tables_created": tables_created, "indexes_created": indexes_created}
