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

定时调度：add_slowquery_collect_schedule 由数据迁移在 migrate 时自动执行
（幂等，可重复调用），无需人工在 Django Admin 创建调度。

配置项（通过 SysConfig 页面管理）：
- slow_query_retention_days: 慢查询数据保留天数（默认30天）
- slow_query_cleanup_batch_size: 每批删除数量（默认5000）
- slow_query_cleanup_batch_sleep: 每批间隔秒数（默认1）
"""
import logging
from datetime import datetime, timedelta

from django_q.tasks import async_task
from django_q.models import Schedule

logger = logging.getLogger("default")

# 支持慢查采集的 db_type（与 collect_slowquery_task 内的 collector_map 保持一致，
# 新增采集器时两处同步修改）
SUPPORTED_COLLECTOR_DB_TYPES = ("mysql", "pgsql", "mongo", "redis")


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

    # 只投递有采集器的实例：Oracle/MSSQL 等入队后只会在任务里打 warning，
    # 每 5 分钟一轮纯属无效队列消息
    instances = Instance.objects.filter(db_type__in=SUPPORTED_COLLECTOR_DB_TYPES)

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

    只能整体 TRUNCATE，不能 DELETE：
    mysql.slow_log 是 CSV 引擎的日志表，不支持加锁，
    DELETE（即使带 LIMIT）会报 1556 "You can't use locks with log tables"。
    TRUNCATE 需要账号对 mysql.slow_log 有 DROP 权限。

    明细由采集任务先行落库到本地（每5分钟），本地保留时长由
    cleanup_slowquery_data_task 的保留策略负责，因此清空服务端日志表不会丢数据。

    此任务应该每天执行一次
    """
    from sql.models import Instance
    from sql.engines import get_engine

    instances = Instance.objects.filter(db_type="mysql")

    for instance in instances:
        try:
            engine = get_engine(instance=instance)

            # SQL 必须走 sql= 关键字：execute 第一位置参数是 db_name
            result = engine.execute(
                db_name="mysql",
                sql="TRUNCATE TABLE mysql.slow_log",
            )

            # execute 内部吞掉异常只回填 error，必须显式检查
            if result.error:
                logger.error(
                    f"[{instance.instance_name}] 清理 MySQL slow_log 失败: {result.error}"
                    f"（TRUNCATE 需要账号对 mysql.slow_log 具备 DROP 权限）"
                )
            else:
                logger.info(
                    f"[{instance.instance_name}] 清理 MySQL slow_log 完成，"
                    f"清空 {result.affected_rows} 条"
                )

        except Exception as e:
            logger.error(f"[{instance.instance_name}] 清理 MySQL slow_log 失败: {e}")


# ---------- 本地慢查询数据保留策略 ----------


def _batch_delete(model, days=None, batch_size=None, time_field="created_at"):
    """
    分批删除指定模型中过期的数据

    Args:
        model: Django 模型类
        days: 保留天数（默认从配置读取）
        batch_size: 每批删除数量（默认从配置读取）
        time_field: 过期判断的时间字段。明细表用 created_at（入库时间）；
            统计表用 last_seen（指纹最后出现时间）——持续活跃的统计行
            created_at 很旧但仍在被聚合任务更新，按 created_at 清会误删活跃统计。

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
            model.objects.filter(**{f"{time_field}__lt": cutoff})
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

    根据数据保留策略清理过期数据：
    - 明细表按 created_at（入库时间）判断过期
    - 统计表按 last_seen（指纹最后出现时间）判断过期，
      持续活跃的 SQL 统计不会被误删；指纹停止出现超过保留期后，
      其统计行随明细一起清理

    Args:
        days: 保留天数，默认从配置读取

    此任务每天由定时调度执行一次
    """
    from sql.models import (
        MySQLSlowQueryDetail,
        PgSQLSlowQueryDetail,
        MongoSlowQueryDetail,
        RedisSlowQueryDetail,
        MySQLSlowQuerySummary,
        PgSQLSlowQuerySummary,
        MongoSlowQuerySummary,
        RedisSlowQuerySummary,
    )

    if days is None:
        days = get_retention_days()

    logger.info(f"开始清理慢查询数据（保留 {days} 天）...")

    # (结果键, 模型, 过期时间字段, 日志名)
    targets = [
        ("mysql_detail", MySQLSlowQueryDetail, "created_at", "MySQL 明细"),
        ("pgsql_detail", PgSQLSlowQueryDetail, "created_at", "PgSQL 明细"),
        ("mongo_detail", MongoSlowQueryDetail, "created_at", "MongoDB 明细"),
        ("redis_detail", RedisSlowQueryDetail, "created_at", "Redis 明细"),
        ("mysql_summary", MySQLSlowQuerySummary, "last_seen", "MySQL 统计"),
        ("pgsql_summary", PgSQLSlowQuerySummary, "last_seen", "PgSQL 统计"),
        ("mongo_summary", MongoSlowQuerySummary, "last_seen", "MongoDB 统计"),
        ("redis_summary", RedisSlowQuerySummary, "last_seen", "Redis 统计"),
    ]

    results = {}
    for key, model, time_field, label in targets:
        try:
            deleted = _batch_delete(model, days, time_field=time_field)
            results[key] = deleted
            if deleted > 0:
                logger.info(f"{label}清理完成: 删除 {deleted} 条")
        except Exception as e:
            logger.error(f"{label}清理失败: {e}")
            results[key] = 0

    # 清理孤儿游标（实例已删除的游标）。
    # 注意不能按时间清理游标：活跃游标被删会让下一轮采集回退起点、重复采集成批数据；
    # 且 SlowQueryCursor 只有 updated_at 字段，按 created_at 过滤本身就会 FieldError
    try:
        from django.db.models import Q

        from sql.models import Instance, SlowQueryCursor

        instance_ids = list(Instance.objects.values_list("id", flat=True))
        deleted, _ = SlowQueryCursor.objects.exclude(
            Q(instance_id__in=instance_ids)
        ).delete()
        results["cursor"] = deleted
        if deleted > 0:
            logger.info(f"孤儿游标清理完成: 删除 {deleted} 条")
    except Exception as e:
        logger.error(f"游标清理失败: {e}")
        results["cursor"] = 0

    total = sum(results.values())
    logger.info(f"慢查询数据清理完成: 共删除 {total} 条")

    # 附带清理 AI 用量流水（同为每日一次的过期数据清理；默认保留 180 天）
    try:
        from sql.utils.ai_tasks import cleanup_ai_usage_log_task

        results["ai_usage"] = cleanup_ai_usage_log_task()
    except Exception as e:
        logger.error(f"AI 用量流水清理失败: {e}")
        results["ai_usage"] = 0

    return results


# ---------- 定时任务调度 ----------


def _daily_run_at(hour: int) -> datetime:
    """返回下一个本地时间的 hour:00（USE_TZ=False，朴素本地时间）"""
    now = datetime.now()
    run_at = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if run_at <= now:
        run_at += timedelta(days=1)
    return run_at


def _normalize_field(key: str, value):
    """Schedule 的 args/kwargs 是 TextField，调度器触发时用 literal_eval 解析。

    比较时把存储形态的字符串还原成对象，避免 dict 入参与 str 存储值
    永远不相等、导致每次 migrate 都误判有变化而重写。
    """
    if key in ("args", "kwargs") and isinstance(value, str):
        try:
            import ast

            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value
    return value


def _upsert_schedule(name: str, func: str, **fields) -> Schedule:
    """
    幂等注册调度：同名调度已存在则同步定义字段，不存在则创建。

    更新已有调度时不回拨 next_run：next_run 是运行态（随每次执行推进），
    不能按注册值覆盖，否则每次发版 migrate 都会把执行时间拉回注册时刻，
    分钟级任务会立即触发一轮。
    """
    defaults = {"func": func, **fields}
    obj, created = Schedule.objects.get_or_create(name=name, defaults=defaults)
    if created:
        return obj

    changed = False
    for key, value in defaults.items():
        if key == "next_run":
            continue
        if _normalize_field(key, getattr(obj, key)) != _normalize_field(key, value):
            setattr(obj, key, value)
            changed = True
    if changed:
        obj.save()
    return obj


def add_slowquery_collect_schedule():
    """
    幂等注册慢查询相关定时任务（可重复调用）

    由数据迁移在 migrate 时自动执行，也可在 shell 手工调用。
    调度生效前提：qcluster 进程在运行，且 Q_CLUSTER_SYNC 为 false。

    注册内容：
    1. 每5分钟采集所有实例的慢查询明细
    2. 每5分钟聚合统计数据
    3. 每10分钟清理 stale 诊断任务（无人轮询时任务会永久卡 pending/running）
    4. 每天凌晨2点清理 MySQL 服务器 slow_log 表
    5. 每天凌晨3点清理本地慢查询数据（保留策略，slow_query_retention_days）
    """
    # timeout 经 kwargs 透传给 async_task（Schedule 模型没有 timeout 字段，
    # 调度器触发时把 kwargs 原样投给 async_task 生效）；
    # 不传则走集群默认 60s，采集/清理类任务必然超时
    _upsert_schedule(
        "慢查询采集-每5分钟",
        "sql.collectors.tasks.collect_all_slowquery_task",
        schedule_type=Schedule.MINUTES,
        minutes=5,
        repeats=-1,  # 无限重复
        kwargs={"timeout": 600},
        next_run=datetime.now(),
    )

    _upsert_schedule(
        "慢查询聚合-每5分钟",
        "sql.collectors.tasks.aggregate_slowquery_task",
        schedule_type=Schedule.MINUTES,
        minutes=5,
        repeats=-1,
        kwargs={"timeout": 600},
        next_run=datetime.now(),
    )

    _upsert_schedule(
        "慢查询诊断任务清理-每10分钟",
        "sql.services.diagnosis.cleanup_stale_diagnosis_tasks",
        schedule_type=Schedule.MINUTES,
        minutes=10,
        repeats=-1,
        kwargs={"timeout": 120},
        next_run=datetime.now(),
    )

    _upsert_schedule(
        "MySQL慢日志清理-每天",
        "sql.collectors.tasks.cleanup_mysql_slow_log_task",
        schedule_type=Schedule.DAILY,
        repeats=-1,
        kwargs={"timeout": 600},
        next_run=_daily_run_at(2),
    )

    _upsert_schedule(
        "慢查询数据清理-每天",
        "sql.collectors.tasks.cleanup_slowquery_data_task",
        schedule_type=Schedule.DAILY,
        repeats=-1,
        kwargs={"timeout": 600},
        next_run=_daily_run_at(3),
    )
    logger.info("慢查询定时任务注册完成（采集/聚合每5分钟，诊断清理每10分钟，MySQL慢日志清理每日2点，数据清理每日3点）")


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
