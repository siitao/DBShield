# -*- coding:utf-8 -*-
"""
慢查询聚合任务

从明细表聚合统计数据到统计表，保证数据一致性

优化说明：
- 使用聚合查询一次性获取统计信息，避免 N+1 查询
- 使用批量计算 p95 替代逐条查询
- 使用 bulk_create + ON DUPLICATE KEY UPDATE 批量 upsert（原子写入，
  消除 get→insert 唯一键竞争），批量失败自动降级逐行重试
- 聚合入口加防重叠锁：上一轮未结束则跳过本轮
"""
import logging
import math
from datetime import datetime

from django.core.cache import cache
from django.db import models
from django.db.models import Avg, Count, Max, Min, Sum, Subquery, OuterRef, F, Q
from django.db.models.functions import Coalesce

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


def _batch_calculate_p95(detail_model, instance_id, sql_hash, time_field="query_time"):
    """
    批量计算单个 sql_hash 的 p95 值

    Args:
        detail_model: 明细模型类
        instance_id: 实例ID
        sql_hash: SQL指纹哈希
        time_field: 耗时字段名

    Returns:
        p95 耗时值
    """
    times = list(
        detail_model.objects.filter(
            instance_id=instance_id,
            sql_hash=sql_hash
        ).values_list(time_field, flat=True).order_by(time_field)
    )
    return _calculate_percentile(times, 95)


def aggregate_mysql_slowquery():
    """聚合 MySQL 慢查询统计数据（优化版：消除 N+1 查询）"""
    from sql.models import MySQLSlowQueryDetail, MySQLSlowQuerySummary

    try:
        # 按 sql_hash 聚合，一次性获取所有统计信息
        stats = (
            MySQLSlowQueryDetail.objects
            .values("instance_id", "sql_hash")
            .annotate(
                total_count=Count("id"),
                total_time=Sum("query_time"),
                avg_time=Avg("query_time"),
                max_time=Max("query_time"),
                min_time=Min("query_time"),
                total_rows_sent=Sum("rows_sent"),
                total_rows_examined=Sum("rows_examined"),
                avg_rows_sent=Avg("rows_sent"),
                avg_rows_examined=Avg("rows_examined"),
                first_seen=Min("execution_start_time"),
                last_seen=Max("execution_start_time"),
                db_name=Max("db_name"),
                # 直接在聚合中获取 fingerprint（取最新的一条）
                fingerprint=Max("sql_text"),
                sample_sql=Max("sql_text"),
            )
        )

        # 收集所有需要更新的 instance_id 和 sql_hash 组合
        valid_stats = []
        for stat in stats:
            if stat["sql_hash"]:
                valid_stats.append(stat)

        if not valid_stats:
            logger.info("MySQL聚合完成: 无数据")
            return 0

        # 批量计算 p95（按 instance_id 和 sql_hash 分组）
        p95_cache = {}
        for stat in valid_stats:
            instance_id = stat["instance_id"]
            sql_hash = stat["sql_hash"]
            cache_key = (instance_id, sql_hash)

            if cache_key not in p95_cache:
                p95_cache[cache_key] = _batch_calculate_p95(
                    MySQLSlowQueryDetail, instance_id, sql_hash
                )

        # 准备批量更新的数据
        summary_objects = []
        for stat in valid_stats:
            instance_id = stat["instance_id"]
            sql_hash = stat["sql_hash"]
            cache_key = (instance_id, sql_hash)

            summary_objects.append(MySQLSlowQuerySummary(
                instance_id=instance_id,
                sql_hash=sql_hash,
                fingerprint=stat["fingerprint"] or "",
                sample_sql=stat["sample_sql"] or "",
                db_name=stat["db_name"],
                total_execution_counts=stat["total_count"],
                total_execution_times=round(stat["total_time"] or 0, 6),
                query_time_avg=round(stat["avg_time"] or 0, 6),
                query_time_p95=round(p95_cache.get(cache_key, 0), 6),
                parse_total_row_counts=int(stat["total_rows_examined"] or 0),
                return_total_row_counts=int(stat["total_rows_sent"] or 0),
                parse_row_avg=round(stat["avg_rows_examined"] or 0, 2),
                return_row_avg=round(stat["avg_rows_sent"] or 0, 2),
                first_seen=stat["first_seen"],
                last_seen=stat["last_seen"],
            ))

        # 批量 upsert（INSERT ... ON DUPLICATE KEY UPDATE），
        # 批量失败自动降级逐行重试（见 base.bulk_upsert）
        persisted = bulk_upsert(
            MySQLSlowQuerySummary,
            summary_objects,
            update_fields=MYSQL_SUMMARY_UPDATE_FIELDS,
            log_label="MySQL聚合",
        )
        logger.info(f"MySQL聚合完成: 处理 {persisted} 行")
        return persisted

    except Exception as e:
        logger.error(f"MySQL聚合失败: {e}", exc_info=True)
        return 0


def aggregate_pgsql_slowquery():
    """聚合 PgSQL 慢查询统计数据（优化版：消除 N+1 查询）"""
    from sql.models import PgSQLSlowQueryDetail, PgSQLSlowQuerySummary

    try:
        # 按 sql_hash 聚合，一次性获取所有统计信息
        stats = (
            PgSQLSlowQueryDetail.objects
            .values("instance_id", "sql_hash")
            .annotate(
                total_count=Count("id"),
                total_time=Sum("query_time"),
                avg_time=Avg("query_time"),
                max_time=Max("query_time"),
                total_rows_sent=Sum("rows_sent"),
                avg_rows_sent=Avg("rows_sent"),
                total_blks_hit=Sum("shared_blks_hit"),
                total_blks_read=Sum("shared_blks_read"),
                first_seen=Min("execution_start_time"),
                last_seen=Max("execution_start_time"),
                db_name=Max("db_name"),
                # 直接在聚合中获取 fingerprint
                fingerprint=Max("sql_text"),
            )
        )

        # 过滤无效数据
        valid_stats = [stat for stat in stats if stat["sql_hash"]]

        if not valid_stats:
            logger.info("PgSQL聚合完成: 无数据")
            return 0

        # 批量计算 p95
        p95_cache = {}
        for stat in valid_stats:
            instance_id = stat["instance_id"]
            sql_hash = stat["sql_hash"]
            cache_key = (instance_id, sql_hash)

            if cache_key not in p95_cache:
                p95_cache[cache_key] = _batch_calculate_p95(
                    PgSQLSlowQueryDetail, instance_id, sql_hash
                )

        # 构建统计对象，批量 upsert（批量失败自动降级逐行重试）
        summary_objects = []
        for stat in valid_stats:
            instance_id = stat["instance_id"]
            sql_hash = stat["sql_hash"]
            cache_key = (instance_id, sql_hash)
            fingerprint = stat["fingerprint"] or ""

            summary_objects.append(PgSQLSlowQuerySummary(
                instance_id=instance_id,
                sql_hash=sql_hash,
                fingerprint=fingerprint,
                sample_sql=fingerprint,
                db_name=stat["db_name"],
                total_execution_counts=stat["total_count"],
                total_execution_times=round(stat["total_time"] or 0, 6),
                query_time_avg=round(stat["avg_time"] or 0, 6),
                query_time_p95=round(p95_cache.get(cache_key, 0), 6),
                rows_sum=int(stat["total_rows_sent"] or 0),
                rows_avg=round(stat["avg_rows_sent"] or 0, 2),
                shared_blks_hit=int(stat["total_blks_hit"] or 0),
                shared_blks_read=int(stat["total_blks_read"] or 0),
                first_seen=stat["first_seen"],
                last_seen=stat["last_seen"],
            ))

        persisted = bulk_upsert(
            PgSQLSlowQuerySummary,
            summary_objects,
            update_fields=PGSQL_SUMMARY_UPDATE_FIELDS,
            log_label="PgSQL聚合",
        )
        logger.info(f"PgSQL聚合完成: 处理 {persisted} 行")
        return persisted

    except Exception as e:
        logger.error(f"PgSQL聚合失败: {e}", exc_info=True)
        return 0


def aggregate_mongo_slowquery():
    """聚合 MongoDB 慢查询统计数据（优化版：消除 N+1 查询）"""
    from sql.models import MongoSlowQueryDetail, MongoSlowQuerySummary

    try:
        # 按 sql_hash 聚合，一次性获取所有统计信息
        stats = (
            MongoSlowQueryDetail.objects
            .values("instance_id", "sql_hash")
            .annotate(
                total_count=Count("id"),
                total_time=Sum("duration"),
                avg_time=Avg("duration"),
                max_time=Max("duration"),
                avg_docs_examined=Avg("docs_examined"),
                avg_docs_returned=Avg("docs_returned"),
                has_sort=Max("has_sort"),
                first_seen=Min("execution_start_time"),
                last_seen=Max("execution_start_time"),
                db_name=Max("db_name"),
                collection_name=Max("collection_name"),
                operation_type=Max("operation_type"),
                # 直接在聚合中获取 fingerprint
                fingerprint=Max("command_text"),
            )
        )

        # 过滤无效数据
        valid_stats = [stat for stat in stats if stat["sql_hash"]]

        if not valid_stats:
            logger.info("MongoDB聚合完成: 无数据")
            return 0

        # 批量计算 p95
        p95_cache = {}
        for stat in valid_stats:
            instance_id = stat["instance_id"]
            sql_hash = stat["sql_hash"]
            cache_key = (instance_id, sql_hash)

            if cache_key not in p95_cache:
                p95_cache[cache_key] = _batch_calculate_p95(
                    MongoSlowQueryDetail, instance_id, sql_hash, time_field="duration"
                )

        # 构建统计对象，批量 upsert（批量失败自动降级逐行重试）
        summary_objects = []
        for stat in valid_stats:
            instance_id = stat["instance_id"]
            sql_hash = stat["sql_hash"]
            cache_key = (instance_id, sql_hash)
            fingerprint = stat["fingerprint"] or ""

            summary_objects.append(MongoSlowQuerySummary(
                instance_id=instance_id,
                sql_hash=sql_hash,
                fingerprint=fingerprint,
                sample_sql=fingerprint,
                db_name=stat["db_name"],
                collection_name=stat["collection_name"],
                operation_type=stat["operation_type"],
                total_execution_counts=stat["total_count"],
                total_execution_times=round(stat["total_time"] or 0, 2),
                query_time_avg=round(stat["avg_time"] or 0, 2),
                query_time_p95=round(p95_cache.get(cache_key, 0), 2),
                docs_examined_avg=round(stat["avg_docs_examined"] or 0, 2),
                docs_returned_avg=round(stat["avg_docs_returned"] or 0, 2),
                has_sort=bool(stat["has_sort"]),
                first_seen=stat["first_seen"],
                last_seen=stat["last_seen"],
            ))

        persisted = bulk_upsert(
            MongoSlowQuerySummary,
            summary_objects,
            update_fields=MONGO_SUMMARY_UPDATE_FIELDS,
            log_label="MongoDB聚合",
        )
        logger.info(f"MongoDB聚合完成: 处理 {persisted} 行")
        return persisted

    except Exception as e:
        logger.error(f"MongoDB聚合失败: {e}", exc_info=True)
        return 0


def aggregate_redis_slowquery():
    """聚合 Redis 慢查询统计数据（优化版：消除 N+1 查询）"""
    from sql.models import RedisSlowQueryDetail, RedisSlowQuerySummary

    try:
        # 按 sql_hash 聚合，一次性获取所有统计信息
        stats = (
            RedisSlowQueryDetail.objects
            .values("instance_id", "sql_hash")
            .annotate(
                total_count=Count("id"),
                total_time=Sum("duration"),
                avg_time=Avg("duration"),
                max_time=Max("duration"),
                first_seen=Min("execution_start_time"),
                last_seen=Max("execution_start_time"),
                # 直接在聚合中获取 fingerprint
                fingerprint=Max("command_text"),
            )
        )

        # 过滤无效数据
        valid_stats = [stat for stat in stats if stat["sql_hash"]]

        if not valid_stats:
            logger.info("Redis聚合完成: 无数据")
            return 0

        # 批量计算 p95
        p95_cache = {}
        for stat in valid_stats:
            instance_id = stat["instance_id"]
            sql_hash = stat["sql_hash"]
            cache_key = (instance_id, sql_hash)

            if cache_key not in p95_cache:
                p95_cache[cache_key] = _batch_calculate_p95(
                    RedisSlowQueryDetail, instance_id, sql_hash, time_field="duration"
                )

        # 构建统计对象，批量 upsert（批量失败自动降级逐行重试）
        summary_objects = []
        for stat in valid_stats:
            instance_id = stat["instance_id"]
            sql_hash = stat["sql_hash"]
            cache_key = (instance_id, sql_hash)
            fingerprint = stat["fingerprint"] or ""

            summary_objects.append(RedisSlowQuerySummary(
                instance_id=instance_id,
                sql_hash=sql_hash,
                fingerprint=fingerprint,
                sample_sql=fingerprint,
                total_execution_counts=stat["total_count"],
                total_execution_times=round(stat["total_time"] or 0, 2),
                query_time_avg=round(stat["avg_time"] or 0, 2),
                query_time_p95=round(p95_cache.get(cache_key, 0), 2),
                first_seen=stat["first_seen"],
                last_seen=stat["last_seen"],
            ))

        persisted = bulk_upsert(
            RedisSlowQuerySummary,
            summary_objects,
            update_fields=REDIS_SUMMARY_UPDATE_FIELDS,
            log_label="Redis聚合",
        )
        logger.info(f"Redis聚合完成: 处理 {persisted} 行")
        return persisted

    except Exception as e:
        logger.error(f"Redis聚合失败: {e}", exc_info=True)
        return 0


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
