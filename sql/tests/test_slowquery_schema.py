# -*- coding:utf-8 -*-
"""慢查询表结构引导：DDL 解析覆盖度与幂等性测试"""
import pytest
from django.db import connection

from sql.services.slowquery_schema import ensure_slowquery_schema, plan_slowquery_schema

EXPECTED_TABLES = {
    "mysql_slow_query_summary",
    "mysql_slow_query_detail",
    "pgsql_slow_query_summary",
    "pgsql_slow_query_detail",
    "mongo_slow_query_summary",
    "mongo_slow_query_detail",
    "redis_slow_query_summary",
    "redis_slow_query_detail",
    "slow_query_cursor",
}


def test_plan_covers_all_tables():
    plan = plan_slowquery_schema()
    tables = {name for kind, name, _, _ in plan if kind == "table"}
    assert tables == EXPECTED_TABLES


def test_plan_covers_composite_indexes():
    """add_indexes.sql 的复合索引全部进入计划，且目标表都在建表计划内"""
    plan = plan_slowquery_schema()
    tables = {name for kind, name, _, _ in plan if kind == "table"}
    index_items = [(name, table) for kind, name, table, _ in plan if kind == "index"]

    index_names = {name for name, _ in index_items}
    assert "idx_mysql_dtl_inst_hash_time" in index_names
    assert "idx_mysql_sum_inst_lastseen" in index_names
    assert "idx_redis_dtl_inst_hash_time" in index_names

    for _, table in index_items:
        assert table in tables


@pytest.mark.django_db
def test_ensure_is_noop_when_schema_present():
    """测试库的表/索引已由迁移 0009 建好，重复 ensure 零创建（幂等）"""
    created = ensure_slowquery_schema()
    assert created == {"tables_created": [], "indexes_created": []}


@pytest.mark.django_db
def test_required_indexes_present():
    """复合索引真实存在于库中（清理与查询依赖它们）"""
    with connection.cursor() as cursor:
        detail_constraints = connection.introspection.get_constraints(
            cursor, "mysql_slow_query_detail"
        )
        summary_constraints = connection.introspection.get_constraints(
            cursor, "mysql_slow_query_summary"
        )
    for index in ("idx_instance_time", "idx_sql_hash", "idx_mysql_dtl_inst_hash_time"):
        assert index in detail_constraints

    for index in ("idx_last_seen", "idx_mysql_sum_inst_lastseen"):
        assert index in summary_constraints
