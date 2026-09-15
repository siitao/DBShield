# -*- coding:utf-8 -*-
"""
慢查询聚合任务

从明细表聚合统计数据到统计表，保证数据一致性

优化说明：
- 聚合查询一次性获取统计信息，避免 N+1 查询
- p95 由单条查询全量拉取耗时明细后内存分组计算（旧实现按指纹逐条查询，
  指纹数为 N 即 N 次查询；聚合本就是全量重算，拉取数据量不变）
- 使用 bulk_create + ON DUPLICATE KEY UPDATE 批量 upsert（原子写入，
  消除 get→insert 唯一键竞争），批量失败自动降级逐行重试
- 聚合入口加防重叠锁：上一轮未结束则跳过本轮
"""
import logging
import math

from django.core.cache import cache
from django.db.models import Avg, Count, Max, Min, Sum

from .base import bulk_upsert

logger = logging.getLogger("default")

# 调度每 5 分钟触发一次聚合，但单轮全量重算可能超过间隔（任务 timeout=600s），
# 两轮并发会对同一批 (instance_id, sql_hash) 互撞 upsert，需要防重叠锁
AGGREGATE_LOCK_KEY = "slowquery_aggregate_lock"
# 与聚合任务 timeout 保持一致：进程被强杀后锁自动过期，最多多跳过两轮
AGGREGATE_LOCK_TIMEOUT = 600

# 各统计表批量 upsert 的更新列（不含唯一键与 created_at，
# 含 updated_at 以便 ODKU 更新分支刷新时间戳）
MYSQL_SUMMARY_UPDATE_FIELDS = [
    "fingerprint",
    "sample_sql",
    "db_name",
    "total_execution_counts",
    "total_execution_times",
    "query_time_avg",
    "query_time_p95",
    "parse_total_row_counts",
    "return_total_row_counts",
    "parse_row_avg",
    "return_row_avg",
    "first_seen",
    "last_seen",
    "updated_at",
]
PGSQL_SUMMARY_UPDATE_FIELDS = [
    "fingerprint",
    "sample_sql",
    "db_name",
    "total_execution_counts",
    "total_execution_times",
    "query_time_avg",
    "query_time_p95",
    "rows_sum",
    "rows_avg",
    "shared_blks_hit",
    "shared_blks_read",
    "first_seen",
    "last_seen",
    "updated_at",
]
MONGO_SUMMARY_UPDATE_FIELDS = [
    "fingerprint",
    "sample_sql",
    "db_name",
    "collection_name",
    "operation_type",
    "total_execution_counts",
    "total_execution_times",
    "query_time_avg",
    "query_time_p95",
    "docs_examined_avg",
    "docs_returned_avg",
    "has_sort",
    "first_seen",
    "last_seen",
    "updated_at",
]
REDIS_SUMMARY_UPDATE_FIELDS = [
    "fingerprint",
    "sample_sql",
    "total_execution_counts",
    "total_execution_times",
    "query_time_avg",
    "query_time_p95",
    "first_seen",
    "last_seen",
    "updated_at",
]


def _calculate_percentile(values, percentile=95):
    """
    计算百分位数（近似值）

    Args:
        values: 排序后的数值列表
        percentile: 百分位数（0-100）

    Returns:
        百分位数对应的值
    """
    if not values:
        return 0
    n = len(values)
    k = (n - 1) * percentile / 100
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return values[int(k)]
    return values[int(f)] * (c - k) + values[int(c)] * (k - f)


def _calculate_p95_all(detail_model, time_field):
    """一次查询拉取全部明细耗时，内存按 (instance_id, sql_hash) 分组计算 p95。

    聚合是全量重算——所有指纹的 p95 都需要，拉取数据量与旧的逐指纹查询一致，
    查询次数由 O(指纹数) 降为 1。
    """
    times_by_pair = {}
    values_qs = detail_model.objects.exclude(sql_hash="").values_list(
        "instance_id", "sql_hash", time_field
    )
    for instance_id, sql_hash, value in values_qs:
        times_by_pair.setdefault((instance_id, sql_hash), []).append(value)
    return {
        pair: _calculate_percentile(sorted(values), 95)
        for pair, values in times_by_pair.items()
    }


def _aggregate_slowquery(
    *,
    detail_model,
    summary_model,
    annotations,
    build_summary,
    update_fields,
    label,
    time_field,
):
    """通用聚合流程：分组统计 → p95 → 构建 summary 对象 → 批量 upsert。"""
    try:
        stats = (
            detail_model.objects
            .values("instance_id", "sql_hash")
            .annotate(**annotations)
        )
        valid_stats = [s for s in stats if s["sql_hash"]]

        if not valid_stats:
            logger.info(f"{label}完成: 无数据")
            return 0

        p95_map = _calculate_p95_all(detail_model, time_field)

        summary_objects = [
            build_summary(stat, p95_map.get((stat["instance_id"], stat["sql_hash"]), 0))
            for stat in valid_stats
        ]

        persisted = bulk_upsert(
            summary_model,
            summary_objects,
            update_fields=update_fields,
            log_label=label,
        )
        logger.info(f"{label}完成: 处理 {persisted} 行")
        return persisted

    except Exception as e:
        logger.error(f"{label}失败: {e}", exc_info=True)
        return 0


def aggregate_mysql_slowquery():
    """聚合 MySQL 慢查询统计数据"""
    from sql.models import MySQLSlowQueryDetail, MySQLSlowQuerySummary

    def build_summary(stat, p95):
        return MySQLSlowQuerySummary(
            instance_id=stat["instance_id"],
            sql_hash=stat["sql_hash"],
            fingerprint=stat["fingerprint"] or "",
            sample_sql=stat["sample_sql"] or "",
            db_name=stat["db_name"],
            total_execution_counts=stat["total_count"],
            total_execution_times=round(stat["total_time"] or 0, 6),
            query_time_avg=round(stat["avg_time"] or 0, 6),
            query_time_p95=round(p95, 6),
            parse_total_row_counts=int(stat["total_rows_examined"] or 0),
            return_total_row_counts=int(stat["total_rows_sent"] or 0),
            parse_row_avg=round(stat["avg_rows_examined"] or 0, 2),
            return_row_avg=round(stat["avg_rows_sent"] or 0, 2),
            first_seen=stat["first_seen"],
            last_seen=stat["last_seen"],
        )

    return _aggregate_slowquery(
        detail_model=MySQLSlowQueryDetail,
        summary_model=MySQLSlowQuerySummary,
        annotations={
            "total_count": Count("id"),
            "total_time": Sum("query_time"),
            "avg_time": Avg("query_time"),
            "total_rows_sent": Sum("rows_sent"),
            "total_rows_examined": Sum("rows_examined"),
            "avg_rows_sent": Avg("rows_sent"),
            "avg_rows_examined": Avg("rows_examined"),
            "first_seen": Min("execution_start_time"),
            "last_seen": Max("execution_start_time"),
            "db_name": Max("db_name"),
            # 直接在聚合中获取 fingerprint（取最新的一条）
            "fingerprint": Max("sql_text"),
            "sample_sql": Max("sql_text"),
        },
        build_summary=build_summary,
        update_fields=MYSQL_SUMMARY_UPDATE_FIELDS,
        label="MySQL聚合",
        time_field="query_time",
    )


def aggregate_pgsql_slowquery():
    """聚合 PgSQL 慢查询统计数据"""
    from sql.models import PgSQLSlowQueryDetail, PgSQLSlowQuerySummary

    def build_summary(stat, p95):
        fingerprint = stat["fingerprint"] or ""
        return PgSQLSlowQuerySummary(
            instance_id=stat["instance_id"],
            sql_hash=stat["sql_hash"],
            fingerprint=fingerprint,
            sample_sql=fingerprint,
            db_name=stat["db_name"],
            total_execution_counts=stat["total_count"],
            total_execution_times=round(stat["total_time"] or 0, 6),
            query_time_avg=round(stat["avg_time"] or 0, 6),
            query_time_p95=round(p95, 6),
            rows_sum=int(stat["total_rows_sent"] or 0),
            rows_avg=round(stat["avg_rows_sent"] or 0, 2),
            shared_blks_hit=int(stat["total_blks_hit"] or 0),
            shared_blks_read=int(stat["total_blks_read"] or 0),
            first_seen=stat["first_seen"],
            last_seen=stat["last_seen"],
        )

    return _aggregate_slowquery(
        detail_model=PgSQLSlowQueryDetail,
        summary_model=PgSQLSlowQuerySummary,
        annotations={
            "total_count": Count("id"),
            "total_time": Sum("query_time"),
            "avg_time": Avg("query_time"),
            "total_rows_sent": Sum("rows_sent"),
            "avg_rows_sent": Avg("rows_sent"),
            "total_blks_hit": Sum("shared_blks_hit"),
            "total_blks_read": Sum("shared_blks_read"),
            "first_seen": Min("execution_start_time"),
            "last_seen": Max("execution_start_time"),
            "db_name": Max("db_name"),
            # 直接在聚合中获取 fingerprint
            "fingerprint": Max("sql_text"),
        },
        build_summary=build_summary,
        update_fields=PGSQL_SUMMARY_UPDATE_FIELDS,
        label="PgSQL聚合",
        time_field="query_time",
    )


def aggregate_mongo_slowquery():
    """聚合 MongoDB 慢查询统计数据"""
    from sql.models import MongoSlowQueryDetail, MongoSlowQuerySummary

    def build_summary(stat, p95):
        fingerprint = stat["fingerprint"] or ""
        return MongoSlowQuerySummary(
            instance_id=stat["instance_id"],
            sql_hash=stat["sql_hash"],
            fingerprint=fingerprint,
            sample_sql=fingerprint,
            db_name=stat["db_name"],
            collection_name=stat["collection_name"],
            operation_type=stat["operation_type"],
            total_execution_counts=stat["total_count"],
            total_execution_times=round(stat["total_time"] or 0, 2),
            query_time_avg=round(stat["avg_time"] or 0, 2),
            query_time_p95=round(p95, 2),
            docs_examined_avg=round(stat["avg_docs_examined"] or 0, 2),
            docs_returned_avg=round(stat["avg_docs_returned"] or 0, 2),
            has_sort=bool(stat["has_sort"]),
            first_seen=stat["first_seen"],
            last_seen=stat["last_seen"],
        )

    return _aggregate_slowquery(
        detail_model=MongoSlowQueryDetail,
        summary_model=MongoSlowQuerySummary,
        annotations={
            "total_count": Count("id"),
            "total_time": Sum("duration"),
            "avg_time": Avg("duration"),
            "avg_docs_examined": Avg("docs_examined"),
            "avg_docs_returned": Avg("docs_returned"),
            "has_sort": Max("has_sort"),
            "first_seen": Min("execution_start_time"),
            "last_seen": Max("execution_start_time"),
            "db_name": Max("db_name"),
            "collection_name": Max("collection_name"),
            "operation_type": Max("operation_type"),
            # 直接在聚合中获取 fingerprint
            "fingerprint": Max("command_text"),
        },
        build_summary=build_summary,
        update_fields=MONGO_SUMMARY_UPDATE_FIELDS,
        label="MongoDB聚合",
        time_field="duration",
    )


def aggregate_redis_slowquery():
    """聚合 Redis 慢查询统计数据"""
    from sql.models import RedisSlowQueryDetail, RedisSlowQuerySummary

    def build_summary(stat, p95):
        fingerprint = stat["fingerprint"] or ""
        return RedisSlowQuerySummary(
            instance_id=stat["instance_id"],
            sql_hash=stat["sql_hash"],
            fingerprint=fingerprint,
            sample_sql=fingerprint,
            total_execution_counts=stat["total_count"],
            total_execution_times=round(stat["total_time"] or 0, 2),
            query_time_avg=round(stat["avg_time"] or 0, 2),
            query_time_p95=round(p95, 2),
            first_seen=stat["first_seen"],
            last_seen=stat["last_seen"],
        )

    return _aggregate_slowquery(
        detail_model=RedisSlowQueryDetail,
        summary_model=RedisSlowQuerySummary,
        annotations={
            "total_count": Count("id"),
            "total_time": Sum("duration"),
            "avg_time": Avg("duration"),
            "first_seen": Min("execution_start_time"),
            "last_seen": Max("execution_start_time"),
            # 直接在聚合中获取 fingerprint
            "fingerprint": Max("command_text"),
        },
        build_summary=build_summary,
        update_fields=REDIS_SUMMARY_UPDATE_FIELDS,
        label="Redis聚合",
        time_field="duration",
    )


def aggregate_all_slowquery():
    """聚合所有数据库类型的慢查询统计

    聚合是全量重算、幂等：上一轮未结束时直接跳过本轮（明细采集不受影响，
    本轮数据由下一轮补齐）。Redis 不可用时 cache.add 降级返回 False，
    聚合同样暂停，恢复后自动继续。跳过时返回 None。
    """
    if not cache.add(AGGREGATE_LOCK_KEY, 1, AGGREGATE_LOCK_TIMEOUT):
        logger.info("上一轮慢查询聚合仍在运行，跳过本轮")
        return None

    try:
        logger.info("开始聚合慢查询统计数据...")

        results = {
            "mysql": aggregate_mysql_slowquery(),
            "pgsql": aggregate_pgsql_slowquery(),
            "mongo": aggregate_mongo_slowquery(),
            "redis": aggregate_redis_slowquery(),
        }

        logger.info(f"聚合完成: {results}")
        return results
    finally:
        cache.delete(AGGREGATE_LOCK_KEY)
