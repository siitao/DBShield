# -*- coding:utf-8 -*-
"""
慢查询采集任务（基于 django_q）

使用方式：
1. 手动触发：collect_slowquery_task(instance_id, collect_type='all')
2. 定时调度：在 Django Admin 中配置 Schedule

任务列表：
- collect_all_slowquery_task: 采集所有实例的慢查询
- aggregate_slowquery_task: 聚合慢查询统计
- cleanup_mysql_slow_log_task: 清理 MySQL 服务器上的 slow_log 表
- cleanup_slowquery_data_task: 清理本地数据库中的慢查询数据（保留策略）

配置项（通过 SysConfig 页面管理）：
- slow_query_retention_days: 慢查询数据保留天数（默认30天）
- slow_query_cleanup_batch_size: 每批删除数量（默认5000）
- slow_query_cleanup_batch_sleep: 每批间隔秒数（默认1）
"""
import logging
from datetime import datetime, timedelta

from django_q.tasks import async_task, schedule
from django_q.models import Schedule

logger = logging.getLogger("default")


# ---------- 配置读取 ----------

def _get_config(key, default_value):
    """从 SysConfig 读取配置"""
    from common.config import SysConfig
    return SysConfig().get(key, default_value)


def get_retention_days():
    """获取慢查询数据保留天数"""
    return int(_get_config("slow_query_retention_days", 30))


def get_cleanup_batch_size():
    """获取每批删除数量"""
    return int(_get_config("slow_query_cleanup_batch_size", 5000))


def get_cleanup_batch_sleep():
    """获取每批间隔秒数"""
    return float(_get_config("slow_query_cleanup_batch_sleep", 1))


def collect_slowquery_task(instance_id: int, collect_type: str = "all"):
    """
    采集指定实例的慢查询数据

    Args:
        instance_id: 实例ID
        collect_type: 采集类型，可选值：'all', 'summary', 'detail'
    """
    from sql.models import Instance
    from sql.collectors import (
        MySQLSlowQueryCollector,
        PgSQLSlowQueryCollector,
        MongoSlowQueryCollector,
        RedisSlowQueryCollector,
    )

    try:
        instance = Instance.objects.get(id=instance_id)
    except Instance.DoesNotExist:
        logger.error(f"实例 {instance_id} 不存在")
        return

    # 采集时间范围（最近1小时）
    end_time = datetime.now()
    start_time = end_time - timedelta(hours=1)

    # 选择采集器
    collector_map = {
        "mysql": MySQLSlowQueryCollector,
        "pgsql": PgSQLSlowQueryCollector,
        "mongo": MongoSlowQueryCollector,
        "redis": RedisSlowQueryCollector,
    }

    collector_class = collector_map.get(instance.db_type)
    if not collector_class:
        logger.warning(f"[{instance.instance_name}] 不支持的数据库类型: {instance.db_type}")
        return

    collector = collector_class(instance)

    try:
        # 采集明细数据（统计由聚合任务负责）
        if collect_type in ("all", "detail"):
            collector.collect_detail(start_time, end_time)

        # 如果明确指定采集统计，也执行（兼容旧调用）
        if collect_type == "summary":
            collector.collect_summary(start_time, end_time)

        logger.info(f"[{instance.instance_name}] 慢查询采集完成")

    except Exception as e:
        logger.error(f"[{instance.instance_name}] 慢查询采集失败: {e}", exc_info=True)


def collect_all_slowquery_task():
    """
    调度所有实例的慢查询采集

    此任务应该由定时调度器定期调用（建议每5分钟一次）
    """
    from sql.models import Instance

    instances = Instance.objects.all()

    logger.info(f"开始调度慢查询采集，共 {instances.count()} 个实例")

    for instance in instances:
        try:
            # 使用 async_task 异步执行，避免阻塞
            # 显式 timeout：async_task 不继承调度任务的 600s，默认走集群 60s，
            # 慢实例采集/入库在 60s 内完不成会被 TimeoutException 打断
            async_task(
                "sql.collectors.tasks.collect_slowquery_task",
                instance.id,
                "all",
                group=f"slowquery_{instance.db_type}",
                timeout=600,
            )
        except Exception as e:
            logger.error(f"[{instance.instance_name}] 调度采集任务失败: {e}")


# ---------- MySQL 服务器 slow_log 清理 ----------


def cleanup_mysql_slow_log_task():
    """
    清理 MySQL 服务器上的 slow_log 表

    注意：这是清理 MySQL 服务器上的 slow_log 表，不是本地数据库
    分批删除避免长事务锁表

    此任务应该每天执行一次
    """
    from sql.models import Instance
    from sql.engines import get_engine

    instances = Instance.objects.filter(db_type="mysql")

    for instance in instances:
        try:
            engine = get_engine(instance=instance)

            # 分批删除，避免长事务
            batch_size = 10000
            total_deleted = 0

            while True:
                # 删除一批数据（SQL 必须走 sql= 关键字：execute 第一位置参数是 db_name）
                result = engine.execute(
                    db_name="mysql",
                    sql=f"""
                    DELETE FROM mysql.slow_log
                    WHERE start_time < DATE_SUB(NOW(), INTERVAL 7 DAY)
                    LIMIT {batch_size}
                    """,
                )

                # 检查删除的行数（ResultSet 无 rowcount 属性，实际行数为 affected_rows）
                deleted = result.affected_rows if result else 0
                total_deleted += deleted

                # 如果删除的行数少于批次大小，说明已经删除完毕
                if deleted < batch_size:
                    break

                # 等待一下，避免过度占用资源
                import time
                time.sleep(get_cleanup_batch_sleep())

            if total_deleted > 0:
                logger.info(f"[{instance.instance_name}] 清理 MySQL slow_log 完成，删除 {total_deleted} 条")
            else:
                logger.debug(f"[{instance.instance_name}] 无需清理 MySQL slow_log")

        except Exception as e:
            logger.error(f"[{instance.instance_name}] 清理 MySQL slow_log 失败: {e}")


# ---------- 本地慢查询数据保留策略 ----------


def _batch_delete(model, days=None, batch_size=None):
    """
    分批删除指定模型中过期的数据

    Args:
        model: Django 模型类
        days: 保留天数（默认从配置读取）
        batch_size: 每批删除数量（默认从配置读取）

    Returns:
        删除的总行数
    """
    import time
    from django.utils import timezone

    if days is None:
        days = get_retention_days()
    if batch_size is None:
        batch_size = get_cleanup_batch_size()

    cutoff = timezone.now() - timedelta(days=days)
    total_deleted = 0
    batch_sleep = get_cleanup_batch_sleep()

    while True:
        # 获取一批要删除的 ID
        ids = list(
            model.objects.filter(created_at__lt=cutoff)
            .values_list("id", flat=True)[:batch_size]
        )

        if not ids:
            break

        # 删除这批数据
        deleted, _ = model.objects.filter(id__in=ids).delete()
        total_deleted += deleted

        # 等待一下，避免过度占用资源
        time.sleep(batch_sleep)

        # 如果删除的行数少于批次大小，说明已经删除完毕
        if deleted < batch_size:
            break

    return total_deleted


def cleanup_slowquery_data_task(days: int = None):
    """
    清理本地数据库中的慢查询数据

    根据数据保留策略清理过期的明细数据。
    统计数据保留更长时间（由聚合任务自动更新）。

    Args:
        days: 保留天数，默认从配置读取

    此任务应该每天执行一次
    """
    from sql.models import (
        MySQLSlowQueryDetail,
        PgSQLSlowQueryDetail,
        MongoSlowQueryDetail,
        RedisSlowQueryDetail,
    )

    if days is None:
        days = get_retention_days()

    logger.info(f"开始清理慢查询数据（保留 {days} 天）...")

    results = {}

    # 清理 MySQL 明细
    try:
        deleted = _batch_delete(MySQLSlowQueryDetail, days)
        results["mysql_detail"] = deleted
        if deleted > 0:
            logger.info(f"MySQL 明细清理完成: 删除 {deleted} 条")
    except Exception as e:
        logger.error(f"MySQL 明细清理失败: {e}")
        results["mysql_detail"] = 0

    # 清理 PgSQL 明细
    try:
        deleted = _batch_delete(PgSQLSlowQueryDetail, days)
        results["pgsql_detail"] = deleted
        if deleted > 0:
            logger.info(f"PgSQL 明细清理完成: 删除 {deleted} 条")
    except Exception as e:
        logger.error(f"PgSQL 明细清理失败: {e}")
        results["pgsql_detail"] = 0

    # 清理 MongoDB 明细
    try:
        deleted = _batch_delete(MongoSlowQueryDetail, days)
        results["mongo_detail"] = deleted
        if deleted > 0:
            logger.info(f"MongoDB 明细清理完成: 删除 {deleted} 条")
    except Exception as e:
        logger.error(f"MongoDB 明细清理失败: {e}")
        results["mongo_detail"] = 0

    # 清理 Redis 明细
    try:
        deleted = _batch_delete(RedisSlowQueryDetail, days)
        results["redis_detail"] = deleted
        if deleted > 0:
            logger.info(f"Redis 明细清理完成: 删除 {deleted} 条")
    except Exception as e:
        logger.error(f"Redis 明细清理失败: {e}")
        results["redis_detail"] = 0

    # 清理游标表中的过期记录
    try:
        from sql.models import SlowQueryCursor
        deleted = _batch_delete(SlowQueryCursor, days * 2)  # 游标保留更长时间
        results["cursor"] = deleted
        if deleted > 0:
            logger.info(f"游标清理完成: 删除 {deleted} 条")
    except Exception as e:
        logger.error(f"游标清理失败: {e}")
        results["cursor"] = 0

    total = sum(results.values())
    logger.info(f"慢查询数据清理完成: 共删除 {total} 条")

    return results


# ---------- 定时任务调度 ----------


def add_slowquery_collect_schedule():
    """
    添加慢查询采集定时任务

    调用此函数会创建以下定时任务：
    1. 每5分钟采集所有实例的慢查询明细
    2. 每5分钟聚合统计数据
    3. 每天凌晨2点清理 MySQL 慢日志
    4. 每天凌晨3点清理本地慢查询数据
    """
    # 每5分钟采集慢查询明细
    schedule(
        "sql.collectors.tasks.collect_all_slowquery_task",
        name="慢查询采集-每5分钟",
        schedule_type="I",  # 每N分钟
        minutes=5,
        repeats=-1,  # 无限重复
        timeout=600,  # 有界超时：避免无限任务长期占满 worker 导致诊断/采集排队
    )
    logger.info("添加慢查询采集定时任务（每5分钟）")

    # 每5分钟聚合统计数据
    schedule(
        "sql.collectors.tasks.aggregate_slowquery_task",
        name="慢查询聚合-每5分钟",
        schedule_type="I",  # 每N分钟
        minutes=5,
        repeats=-1,
        timeout=600,  # 有界超时：避免无限任务长期占满 worker 导致诊断/采集排队
    )
    logger.info("添加慢查询聚合定时任务（每5分钟）")

    # 每10分钟清理孤儿 stale 诊断任务（无人轮询时任务会永久卡 pending/running）
    schedule(
        "sql_api.api_slowquery_v2.cleanup_stale_diagnosis_tasks",
        name="慢查询诊断任务清理-每10分钟",
        schedule_type="I",  # 每N分钟
        minutes=10,
        repeats=-1,
        timeout=120,
    )
    logger.info("添加慢查询诊断任务清理定时任务（每10分钟）")

    # 每天凌晨2点清理 MySQL 慢日志
    schedule(
        "sql.collectors.tasks.cleanup_mysql_slow_log_task",
        name="MySQL慢日志清理-每天",
        schedule_type="D",  # 每天
        repeats=-1,
        timeout=600,  # 有界超时：避免无限任务长期占满 worker 导致诊断/采集排队
    )
    logger.info("添加MySQL慢日志清理定时任务（每天）")

    # 每天凌晨3点清理本地慢查询数据
    schedule(
        "sql.collectors.tasks.cleanup_slowquery_data_task",
        name="慢查询数据清理-每天",
        schedule_type="D",  # 每天
        repeats=-1,
        timeout=600,  # 有界超时：避免无限任务长期占满 worker 导致诊断/采集排队
    )
    logger.info("添加慢查询数据清理定时任务（每天）")


def aggregate_slowquery_task():
    """
    聚合慢查询统计数据

    从明细表聚合统计数据到统计表，建议每5分钟执行一次
    """
    from sql.collectors.aggregator import aggregate_all_slowquery
    aggregate_all_slowquery()


def del_slowquery_schedules():
    """删除慢查询相关的定时任务"""
    schedule_names = [
        "慢查询采集-每5分钟",
        "慢查询聚合-每5分钟",
        "慢查询诊断任务清理-每10分钟",
        "MySQL慢日志清理-每天",
        "慢查询数据清理-每天",
    ]
    for name in schedule_names:
        try:
            Schedule.objects.get(name=name).delete()
            logger.info(f"删除定时任务: {name}")
        except Schedule.DoesNotExist:
            pass
