# -*- coding:utf-8 -*-
"""AI 慢查诊断：上下文采集与任务执行（自 api_slowquery_v2 下沉，供线程池/调度复用）"""

import datetime as _dt
import json
import logging
import re

from django.db import transaction
from django.db.models import Avg, Count, Max
from django.db.models.functions import TruncDate

from sql.engines import get_engine
from sql.models import (
    MongoSlowQueryDetail,
    MongoSlowQuerySummary,
    MySQLSlowQueryDetail,
    MySQLSlowQuerySummary,
    PgSQLSlowQueryDetail,
    PgSQLSlowQuerySummary,
    RedisSlowQueryDetail,
    RedisSlowQuerySummary,
)
from sql.utils.sql_utils import mask_sql_literals, sanitize_explain_sql

logger = logging.getLogger("default")


# —— stale 任务回收 ——

DIAGNOSIS_STALE_RUNNING_MINUTES = 5
DIAGNOSIS_STALE_PENDING_MINUTES = 12

def _mark_stale_task_failed(task):
    """将长时间停留在 pending/running 的任务标记为 failed。

    场景：web 进程线程池异常、或任务执行被环境卡住（如 AI 无响应）。
    判定后前端轮询会收到 failed 并停止，避免无限请求。
    pending（排队中）与 running（执行中）阈值不同，见常量注释。
    """
    if task.status in ("pending", "running") and task.created_at:
        stale_minutes = (
            DIAGNOSIS_STALE_RUNNING_MINUTES
            if task.status == "running"
            else DIAGNOSIS_STALE_PENDING_MINUTES
        )
        elapsed = _dt.datetime.now() - task.created_at
        if elapsed.total_seconds() > stale_minutes * 60:
            task.status = "failed"
            task.error = (
                f"诊断任务超时（{stale_minutes} 分钟内未完成），"
                "请稍后重新触发诊断"
            )
            task.finished_at = _dt.datetime.now()
            task.save(update_fields=["status", "error", "finished_at"])
            return True
    return False


def cleanup_stale_diagnosis_tasks():
    """后台清理孤儿 stale 诊断任务（L8）。

    轮询路径的 _mark_stale_task_failed 只在用户轮询时兜底；若用户从不轮询
    （如 web 进程重启遗留的 running 任务），任务会永久卡在 pending/running。
    由 django-q 定时任务（每 10 分钟）调用，统一清理。
    """
    from sql.models import AIDiagnosisTask

    marked = 0
    for task in (
        AIDiagnosisTask.objects
        .filter(status__in=["pending", "running"])
        .iterator()
    ):
        if _mark_stale_task_failed(task):
            marked += 1
    if marked:
        logger.info(f"诊断 stale 任务清理完成，标记 failed: {marked} 个")

# —— 深度诊断范围与模型映射 ——

DEEP_DIAGNOSIS_DB_TYPES = {"mysql", "pgsql"}

# Summary 模型映射（用于上下文采集）
_SUMMARY_MODEL_MAP = {
    "mysql": MySQLSlowQuerySummary,
    "pgsql": PgSQLSlowQuerySummary,
    "mongo": MongoSlowQuerySummary,
    "redis": RedisSlowQuerySummary,
}

# Detail 模型映射
_DETAIL_MODEL_MAP = {
    "mysql": MySQLSlowQueryDetail,
    "pgsql": PgSQLSlowQueryDetail,
    "mongo": MongoSlowQueryDetail,
    "redis": RedisSlowQueryDetail,
}

# ---------- 上下文采集 ----------


# 诊断上下文统一时间单位：一律换算为毫秒，避免误导 AI 严重度判断
_DIAG_TIME_UNIT = {
    "mysql": 1000,   # 秒 -> 毫秒
    "pgsql": 1000,   # 秒 -> 毫秒
    "mongo": 1,      # 已是毫秒
    "redis": 0.001,  # 微秒 -> 毫秒
}

_DIAG_TIME_FIELDS = {"query_time_p95", "query_time_avg", "total_execution_times"}


def _is_aliyun_rds(instance):
    """判断实例是否为已启用的阿里云 RDS"""
    from sql.models import AliyunRdsConfig

    return AliyunRdsConfig.objects.filter(instance=instance, is_enable=True).exists()


def _aliyun_stats_row(row):
    """将阿里云 DescribeSlowLogs 统计行转换为诊断用的 stats dict。

    阿里云 MySQL 统计时间字段单位为秒，统一换算为毫秒（与本地表一致）。
    """
    time_unit = 1000  # 秒 -> 毫秒
    stats = {}
    for field, src in [
        ("query_time_p95", "QueryTimePct95"),
        ("query_time_avg", "QueryTimeAvg"),
        ("total_execution_counts", "MySQLTotalExecutionCounts"),
        ("total_execution_times", "MySQLTotalExecutionTimes"),
        ("parse_total_row_counts", "ParseTotalRowCounts"),
        ("return_total_row_counts", "ReturnTotalRowCounts"),
        ("parse_row_avg", "ParseRowAvg"),
        ("return_row_avg", "ReturnRowAvg"),
    ]:
        val = row.get(src)
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if field in _DIAG_TIME_FIELDS:
            stats[field] = round(val * time_unit, 3)
        else:
            stats[field] = val
    stats["sample_sql"] = str(row.get("SQLText", "") or "")
    stats["fingerprint"] = stats["sample_sql"]
    stats["db_name"] = str(row.get("DBName", "") or "")
    return stats


def _aliyun_mongo_stats_row(row):
    """将阿里云 MongoDB 统计行转换为诊断用的 stats dict。

    阿里云 MongoDB 时间字段已是毫秒（无需换算）；文档级扫描/返回指标映射为
    行级扫描/返回数（复用 _apply_stat_severity 的扫描/返回比规则），并把累计值
    归一为 per-exec 平均（与本地 summary 表口径一致，见 M8）；SQLId 是
    命令文本 md5，SQLText 为 profiler JSON，从中提取顶层 op 作为操作类型。
    """
    stats = {}
    for field, src in [
        ("query_time_p95", "QueryTimePct95"),
        ("query_time_avg", "QueryTimeAvg"),
        ("total_execution_counts", "TotalExecutionCounts"),
        ("total_execution_times", "TotalExecutionTimes"),
    ]:
        val = row.get(src)
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        stats[field] = val
    # 阿里云 DescribeSlowLogs 的 DocsExamined/ReturnRowCounts 是窗口期累计值，
    # 而本地 summary 表存的是 per-exec 平均值——统一为 per-exec 平均（M8），
    # 避免同一扫描/返回比严重度规则被喂进两种语义的数值
    total_exec = stats.get("total_execution_counts") or 0
    for field, src in [
        ("parse_total_row_counts", "DocsExamined"),
        ("return_total_row_counts", "ReturnRowCounts"),
    ]:
        val = row.get(src)
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if total_exec and total_exec > 0:
            val = val / total_exec
        stats[field] = val
    stats["collection_name"] = str(row.get("TableName", "") or "")
    stats["operation_type"] = _extract_mongo_op(row.get("SQLText", "") or "")
    stats["has_sort"] = False
    # 与自建 mongo 采集（json.dumps(...)[:2000]）保持一致：命令 JSON 可能极大
    # （如几百个 uid 的 $in 管道），截断避免把 token 预算耗在样本文本上
    stats["sample_sql"] = str(row.get("SQLText", "") or "")[:2000]
    stats["fingerprint"] = stats["sample_sql"]
    stats["db_name"] = str(row.get("DBName", "") or "")
    # QueryTimePct95 阿里云未计算（恒为 0），用 avg 兜底避免严重度规则误判"极快"
    if not stats.get("query_time_p95"):
        stats["query_time_p95"] = stats.get("query_time_avg", 0)
    return stats


def _extract_mongo_op(sql_text):
    """从 MongoDB profiler 命令 JSON 中提取操作类型（顶层 op 字段）。"""
    try:
        parsed = json.loads(sql_text)
        if isinstance(parsed, dict):
            return parsed.get("op", "") or ""
    except (TypeError, ValueError):
        pass
    return ""


def _collect_aliyun_stats(instance, db_type, sql_hash, db_name=""):
    """阿里云 RDS：从云侧慢日志统计接口取指标（本地无 summary 表数据）。

    前端统计页对阿里云 RDS 走 DescribeSlowLogs 实时拉取，本地 summary 表没有
    对应记录，诊断采集必须走同一数据源。MySQL 时间字段秒 -> 毫秒；MongoDB
    时间字段本就是毫秒，SQLId 为命令文本 md5（见 aliyun_mongo.py）。
    """
    if db_type not in ("mysql", "mongo"):
        return {}
    try:
        if db_type == "mongo":
            # get_engine 仅对 mysql 特判阿里云，mongo 会拿到本地 MongoEngine
            from sql.engines.cloud.aliyun_mongo import AliyunMongoEngine

            engine = AliyunMongoEngine(instance=instance)
        else:
            from sql.engines import get_engine

            engine = get_engine(instance=instance)
        end_time = _dt.datetime.now().strftime("%Y-%m-%d")
        # 阿里云 MongoDB 的 DescribeSlowLogs 只接受 ≤7 天查询窗口（实测 8 天即
        # 报 InvalidParam）；MySQL 的 DescribeSlowLogs 可用 30 天
        window_days = 7 if db_type == "mongo" else 30
        start_time = (
            _dt.datetime.now() - _dt.timedelta(days=window_days)
        ).strftime("%Y-%m-%d")
        page_size = 100
        # DescribeSlowLogs 按 SQLHASH 去重分页，慢 SQL 统计通常几十~几百条，
        # 最多拉 10 页（1000 条）足够，避免极端情况下多次往返云端接口
        for page in range(1, 11):
            result = engine.slowquery_review(
                start_time, end_time, db_name, page_size, (page - 1) * page_size
            )
            rows = result.get("rows", []) or []
            for row in rows:
                if str(row.get("SQLId", "")) == str(sql_hash):
                    if db_type == "mongo":
                        return _aliyun_mongo_stats_row(row)
                    return _aliyun_stats_row(row)
            if page * page_size >= int(result.get("total", 0) or 0):
                break
    except Exception as e:
        logger.error(f"采集阿里云慢查统计失败: {e}")
    return {}


def _collect_stats(instance, db_type, sql_hash, db_name=""):
    """采集慢查统计指标（时间字段统一换算为毫秒）"""
    # 阿里云 RDS 无本地 summary 表，改走云侧慢日志统计接口
    if _is_aliyun_rds(instance):
        return _collect_aliyun_stats(instance, db_type, sql_hash, db_name)
    model = _SUMMARY_MODEL_MAP.get(db_type)
    if not model:
        return {}
    summary = model.objects.filter(
        instance_id=instance.id, sql_hash=sql_hash
    ).first()
    if not summary:
        return {}
    time_unit = _DIAG_TIME_UNIT.get(db_type, 1)
    stats = {}
    for field in [
        "query_time_p95", "query_time_avg", "total_execution_counts",
        "total_execution_times", "parse_total_row_counts",
        "return_total_row_counts", "parse_row_avg", "return_row_avg",
    ]:
        val = getattr(summary, field, None)
        if val is not None:
            if field in _DIAG_TIME_FIELDS:
                val = round(float(val) * time_unit, 3)
            stats[field] = val
    # PgSQL 模型无 parse/return 行数，用 rows_sum 映射总返回行数
    if db_type == "pgsql":
        rows_sum = getattr(summary, "rows_sum", None)
        if rows_sum is not None:
            stats["return_total_row_counts"] = rows_sum
    # MongoDB：把文档级扫描/返回指标映射为行级统计（供扫描/返回比与严重度规则兜底复用），
    # 并带上集合名/操作类型/是否排序等 MongoDB 特有上下文
    if db_type == "mongo":
        stats["parse_total_row_counts"] = getattr(summary, "docs_examined_avg", 0) or 0
        stats["return_total_row_counts"] = getattr(summary, "docs_returned_avg", 0) or 0
        stats["collection_name"] = getattr(summary, "collection_name", "") or ""
        stats["operation_type"] = getattr(summary, "operation_type", "") or ""
        stats["has_sort"] = bool(getattr(summary, "has_sort", False))
        # p95=0（聚合任务未跑/失败，collect_summary 置 0）时用 avg 兜底，
        # 避免严重度规则把缺失数据当"极快"误判
        if not stats.get("query_time_p95"):
            stats["query_time_p95"] = stats.get("query_time_avg", 0)
    stats["sample_sql"] = getattr(summary, "sample_sql", "") or getattr(summary, "fingerprint", "")
    stats["fingerprint"] = getattr(summary, "fingerprint", "")
    stats["db_name"] = getattr(summary, "db_name", "") or ""
    return stats


def _collect_trend(instance, db_type, sql_hash, days=14):
    """采集近期趋势（按天聚合，时间统一换算为毫秒）"""
    # 阿里云 RDS 无本地明细表，暂不提供趋势（报告按"无趋势"处理）
    if _is_aliyun_rds(instance):
        return "（阿里云 RDS 不提供本地趋势数据）"
    model = _DETAIL_MODEL_MAP.get(db_type)
    if not model:
        return ""
    end_dt = _dt.datetime.now()
    start_dt = end_dt - _dt.timedelta(days=days)
    time_field = "query_time" if db_type in ("mysql", "pgsql") else "duration"
    time_unit = _DIAG_TIME_UNIT.get(db_type, 1)
    qs = model.objects.filter(
        instance_id=instance.id,
        sql_hash=sql_hash,
        execution_start_time__gte=start_dt,
        execution_start_time__lte=end_dt,
    )
    trend = (
        qs.annotate(date=TruncDate("execution_start_time"))
        .values("date")
        .annotate(
            count=Count("id"),
            avg_time=Avg(time_field),
            max_time=Max(time_field),
        )
        .order_by("date")
    )
    if not trend:
        return f"近 {days} 天无趋势数据"
    lines = []
    for item in trend:
        d = item["date"].strftime("%Y-%m-%d") if item["date"] else "N/A"
        avg_ms = round((item["avg_time"] or 0) * time_unit, 1)
        max_ms = round((item["max_time"] or 0) * time_unit, 1)
        lines.append(
            f"  {d}: 执行{item['count']}次, 平均{avg_ms}ms, 最大{max_ms}ms"
        )
    summary_text = f"近 {days} 天趋势:\n" + "\n".join(lines)

    # 判断是否近期恶化（毫秒基准：平均耗时 3 倍以上且 > 500ms 视为恶化）
    items = list(trend)
    if len(items) >= 2:
        first_avg = (items[0]["avg_time"] or 0) * time_unit
        last_avg = (items[-1]["avg_time"] or 0) * time_unit
        if last_avg > first_avg * 3 and last_avg > 500:
            summary_text += (
                f"\n注意: 平均耗时从 {round(first_avg, 1)}ms 升至 "
                f"{round(last_avg, 1)}ms，趋势恶化"
            )
    return summary_text


def _extract_table_names(sql_text, db_type="mysql"):
    """从 SQL 文本中提取表名（简单正则，非完整解析）"""
    if not sql_text:
        return []
    # 去掉注释
    cleaned = re.sub(r"--.*$", "", sql_text, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    # 匹配 FROM / JOIN / UPDATE / INTO 后的表名，支持四种写法：
    #   `db`.`table`、db.table、`table`、table —— 取 . 分隔后的最后一段作为表名
    pattern = (
        r"(?:FROM|JOIN|UPDATE|INTO)\s+"
        r"((?:`[\w-]+`|[a-zA-Z_][\w-]*)(?:\s*\.\s*(?:`[\w-]+`|[a-zA-Z_][\w-]*))?)"
    )
    matches = re.findall(pattern, cleaned, re.IGNORECASE)
    # 去重，保留顺序
    seen = set()
    result = []
    for m in matches:
        table = m.replace("`", "").split(".")[-1].strip()
        if table.lower() not in seen:
            seen.add(table.lower())
            result.append(table)
    return result[:3]  # 最多取 3 张表，避免 prompt 过长


def _collect_mysql_ddl(engine, db_name, table):
    """MySQL: SHOW CREATE TABLE 返回完整建表语句"""
    result = engine.query(db_name, f"SHOW CREATE TABLE `{table}`")
    if not result.rows:
        return ""
    # rows[0] 格式: [table_name, create_statement]
    return str(result.rows[0][1]) if len(result.rows[0]) > 1 else str(result.rows[0][0])


def _collect_pgsql_ddl(engine, db_name, table):
    """PgSQL: 无 SHOW CREATE TABLE，用 information_schema 拼列定义 + pg_indexes 拼索引"""
    col_result = engine.query(
        db_name,
        "SELECT column_name, data_type, character_maximum_length, "
        "is_nullable, column_default "
        "FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND table_name = '{table}' "
        "ORDER BY ordinal_position",
    )
    if not col_result.rows:
        return ""

    col_defs = []
    for row in col_result.rows:
        name, dtype, char_len, nullable, default = (
            str(row[0]), str(row[1]), row[2], str(row[3]), row[4]
        )
        col_def = f'  "{name}" {dtype}'
        if char_len and dtype in ("character varying", "character"):
            col_def += f"({char_len})"
        if nullable.lower() == "no":
            col_def += " NOT NULL"
        if default is not None and str(default) != "":
            col_def += f" DEFAULT {default}"
        col_defs.append(col_def)

    lines = [f"CREATE TABLE {table} (", ",\n".join(col_defs), ");"]
    # 索引定义（pg_indexes.indexdef 是完整 CREATE INDEX 语句）
    try:
        idx_result = engine.query(
            db_name,
            "SELECT indexdef FROM pg_indexes "
            f"WHERE schemaname = 'public' AND tablename = '{table}' "
            "ORDER BY indexname",
        )
        for row in idx_result.rows or []:
            if row and row[0]:
                lines.append(f"{row[0]};")
    except Exception as e:
        logger.warning(f"获取表 {table} 索引失败: {e}")
    return "\n".join(lines)


def _extract_mongo_collection(command_text):
    """从 MongoDB profiler command（JSON 文本）中提取集合名。

    常见命令首字段即集合名：{"find": "orders", ...} / {"aggregate": "orders", ...} /
    {"update": "orders", ...} / {"delete": "orders", ...} / {"count": "orders", ...}。
    """
    if not command_text:
        return ""
    m = re.match(
        r'\{\s*"(find|aggregate|update|delete|count|distinct|remove|findAndModify)"'
        r'\s*:\s*"([^"]+)"',
        command_text.strip(),
    )
    return m.group(2) if m else ""


def _collect_mongo_indexes(instance, db_name, collection_name, command_text=""):
    """MongoDB：无 DDL，取集合索引清单（getIndexes）作为结构上下文。

    getIndexes 返回 {索引名: 定义}，等价于 MySQL SHOW CREATE TABLE 的索引部分。
    只读索引目录、开销低，不做全表扫描。
    """
    if not collection_name:
        collection_name = _extract_mongo_collection(command_text)
    if not collection_name:
        return "（未识别到集合名，无法获取索引信息）"
    try:
        from sql.engines import get_engine

        engine = get_engine(instance=instance)
        result = engine.query(
            db_name, f'db.getCollection("{collection_name}").getIndexes()'
        )
        lines = [f"-- 集合: {collection_name}（索引清单）"]
        if not result.rows:
            return "\n".join(lines) + "\n（无索引，仅 _id 或未获取到）"
        for row in result.rows:
            # 引擎返回的每行可能是单元素 list/tuple（内含 JSON 字符串），也直接是字符串
            raw = row[0] if isinstance(row, (list, tuple)) and row else row
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                lines.append(f"  {str(raw)[:300]}")
                continue
            if not isinstance(parsed, dict):
                lines.append(f"  {str(raw)[:300]}")
                continue
            for name, spec in parsed.items():
                spec = spec or {}
                if not isinstance(spec, dict):
                    lines.append(f"  {str(raw)[:300]}")
                    continue
                key = spec.get("key", {})
                # getIndexes 的 key 可能是 {字段: 方向} 或 [[字段, 方向], ...]（新版本）
                if isinstance(key, dict):
                    key_str = ", ".join(
                        f"{k}:{'desc' if v == -1 else v}" for k, v in key.items()
                    )
                elif isinstance(key, list):
                    key_str = ", ".join(
                        f"{pair[0]}:{'desc' if pair[1] == -1 else pair[1]}"
                        for pair in key
                        if isinstance(pair, (list, tuple)) and len(pair) >= 2
                    )
                else:
                    key_str = str(key)
                opts = [o for o in ("unique", "sparse") if spec.get(o)]
                suffix = f" [{', '.join(opts)}]" if opts else ""
                lines.append(f"  INDEX {name} ON ({key_str}){suffix}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"获取集合 {collection_name} 索引失败: {e}")
        return f"（获取集合 {collection_name} 索引失败: {e}）"


def _collect_table_schemas(instance, db_type, db_name, sql_text, collection_name=""):
    """采集相关表结构信息（mysql/pgsql 取 DDL，mongo 取集合索引清单）"""
    if db_type == "mongo":
        if _is_aliyun_rds(instance):
            # 阿里云 MongoDB 无直连查询，getIndexes 不可用，明确降级避免报错串进报告
            return "（阿里云 MongoDB 不支持直连查询，无法获取集合索引清单）"
        return _collect_mongo_indexes(instance, db_name, collection_name, sql_text)
    if db_type not in ("mysql", "pgsql"):
        return "（当前数据库类型不支持获取表结构 DDL）"
    table_names = _extract_table_names(sql_text, db_type)
    if not table_names:
        return "（未从 SQL 中识别到表名）"
    try:
        from sql.engines import get_engine
        engine = get_engine(instance=instance)
        schemas = []
        for table in table_names:
            try:
                if db_type == "mysql":
                    ddl = _collect_mysql_ddl(engine, db_name, table)
                else:
                    ddl = _collect_pgsql_ddl(engine, db_name, table)
                if ddl:
                    # 截断过长的 DDL（避免 prompt 溢出，收紧到 1200 字符）
                    if len(ddl) > 1200:
                        ddl = ddl[:1200] + "\n... (DDL 已截断)"
                    schemas.append(f"-- 表: {table}\n{ddl}")
                else:
                    schemas.append(f"-- 表: {table}（未获取到表结构）")
            except Exception as e:
                logger.warning(f"获取表 {table} DDL 失败: {e}")
                schemas.append(f"-- 表: {table}（DDL 获取失败: {e}）")
        return "\n\n".join(schemas) if schemas else "（未获取到表结构）"
    except Exception as e:
        logger.error(f"采集表结构失败: {e}")
        return f"（采集表结构失败: {e}）"


def _collect_mongo_plan_summary(instance, sql_hash):
    """MongoDB：取该指纹最近一条 profiler planSummary 作为执行计划摘要。

    不真发 EXPLAIN（慢库上对线上集合 explain 有成本与权限要求）；profiler 已在
    采集期记录了 planSummary，形如 "IXSCAN { status: 1 }, FETCH" / "COLLSCAN"。
    """
    detail = (
        MongoSlowQueryDetail.objects
        .filter(instance_id=instance.id, sql_hash=sql_hash)
        .exclude(plan_summary__isnull=True)
        .exclude(plan_summary="")
        .order_by("-execution_start_time")
        .first()
    )
    if not detail:
        return "（该指纹无 planSummary 记录）"
    return f"planSummary: {detail.plan_summary}"


def _collect_explain(instance, db_type, db_name, sql_text, sql_hash=""):
    """采集执行计划摘要"""
    if db_type == "mongo":
        return _collect_mongo_plan_summary(instance, sql_hash)
    if db_type not in DEEP_DIAGNOSIS_DB_TYPES:
        return "（当前数据库类型不支持 EXPLAIN，仅给通用建议）"
    if not sql_text:
        return "（无 SQL 文本，无法执行 EXPLAIN）"
    try:
        from sql.engines import get_engine
        engine = get_engine(instance=instance)
        # 安全红线（H4）：统一走 sanitize_explain_sql 公共闸门——
        # 截首句、剥注释、SELECT/WITH 白名单、拒 INTO OUTFILE/DUMPFILE
        clean_sql, reject_reason = sanitize_explain_sql(sql_text)
        if clean_sql is None:
            return f"（{reject_reason}）"
        first_sql = clean_sql
        # 阿里云/参数化模板 SQL 含占位符（MySQL '?'、PgSQL '%s'），EXPLAIN 无法直接执行，
        # 统一替换为字面量 1（EXPLAIN 只做计划不执行，类型不匹配时后续兜底返回失败提示）
        if db_type == "mysql":
            first_sql = first_sql.replace("?", "1")
        else:
            first_sql = re.sub(r"%s", "1", first_sql)
        # max_execution_time=30000：兜底病态优化器/超复杂查询的规划耗时
        # （MySQL set session max_execution_time=30000ms；PgSQL SET statement_timeout TO 30000ms）
        result = engine.query(
            db_name, f"EXPLAIN {first_sql}", max_execution_time=30000
        )
        # 引擎把异常吞进 ResultSet.error，直接看 rows 会把失败误报成"无结果"
        if result.error:
            return f"（EXPLAIN 执行失败：{result.error}）"
        if not result.rows:
            return "（EXPLAIN 无结果）"
        # 摘要：保留关键字段
        columns = result.column_list if hasattr(result, "column_list") else []
        col_lower = [str(c).lower() for c in columns]
        # MySQL EXPLAIN 关键字段: id, select_type, table, type, key, rows, Extra
        # PgSQL EXPLAIN 输出为文本
        if db_type == "mysql":
            key_indices = {}
            for key_field in ["id", "select_type", "table", "type", "key", "rows", "extra"]:
                for i, c in enumerate(col_lower):
                    if c == key_field or c.endswith(key_field):
                        key_indices[key_field] = i
                        break
            lines = []
            for row in result.rows[:10]:  # 最多 10 行
                parts = []
                for field_name, idx in key_indices.items():
                    if idx < len(row):
                        val = row[idx]
                        if val is not None and str(val) != "":
                            parts.append(f"{field_name}={val}")
                if parts:
                    lines.append(" | ".join(parts))
            return "\n".join(lines) if lines else "（EXPLAIN 摘要为空）"
        else:
            # PgSQL EXPLAIN 输出是 QUERY PLAN 文本
            lines = []
            for row in result.rows[:10]:
                for cell in row:
                    if cell:
                        lines.append(str(cell))
            return "\n".join(lines[:20]) if lines else "（EXPLAIN 摘要为空）"
    except Exception as e:
        logger.warning(f"采集执行计划失败: {e}")
        return f"（采集执行计划失败: {e}）"


# ---------- 异步诊断任务 ----------


def diagnose_slowquery_task(task_id):
    """执行 AI 慢查诊断任务（在 web 进程线程池中运行，不依赖 django-q）。

    通过 task_id 关联 AIDiagnosisTask 记录。
    状态机: pending → running → success | failed
    """
    import datetime as _dt_mod
    from sql.models import AIDiagnosisTask, AIDiagnosisReport
    from common.utils.ai_gateway import OpenaiClient, DIAGNOSIS_FALLBACK, record_ai_usage
    from common.config import SysConfig

    try:
        task = AIDiagnosisTask.objects.get(id=task_id)
    except AIDiagnosisTask.DoesNotExist:
        logger.error(f"诊断任务 {task_id} 不存在")
        return

    # 标记为运行中
    task.status = "running"
    task.save(update_fields=["status"])

    def _set_progress(progress):
        """上报阶段进度（collecting_stats/trend/ddl/explain/analyzing/saving），供前端展示"""
        try:
            AIDiagnosisTask.objects.filter(id=task_id).update(progress=progress)
        except Exception:
            pass

    try:
        instance = task.instance
        db_type = instance.db_type
        db_name = task.db_name
        sql_hash = task.sql_hash

        # 1. 采集上下文（统计/趋势/表结构/执行计划），每步完成后上报进度
        _set_progress("collecting")
        logger.info(f"[诊断 {task_id}] 开始采集上下文: instance={instance.instance_name}, db={db_name}, hash={sql_hash}")

        stats = _collect_stats(instance, db_type, sql_hash, db_name)
        sample_sql = stats.get("sample_sql", "") or stats.get("fingerprint", "")
        if not sample_sql:
            raise ValueError(
                "该慢查指纹在统计表中无样本记录（数据可能已过期清理），"
                "请从「慢查统计」页选择行进行诊断"
            )
        _set_progress("collecting_trend")
        trend_summary = _collect_trend(instance, db_type, sql_hash, days=14)
        _set_progress("collecting_ddl")
        # 非深度支持类型也尝试采集（会返回提示文本）
        table_schemas = _collect_table_schemas(
            instance, db_type, db_name, sample_sql,
            collection_name=stats.get("collection_name", ""),
        )
        _set_progress("collecting_explain")
        explain_text = _collect_explain(
            instance, db_type, db_name, sample_sql, sql_hash
        )

        # 2. 调用 AI 诊断
        _set_progress("analyzing")
        logger.info(f"[诊断 {task_id}] 调用 AI 诊断")
        # 场景化配置（不重试/限输出/关思考）；diagnose_slowquery_by_openai
        # 内部对这三项另有显式声明，双保险防裸 client 调用时退化
        client = OpenaiClient(scenario="slowquery_diagnosis")
        model_name = client.default_chat_model

        # 外发脱敏：prompt 只携带字面量脱敏后的 SQL（H1），
        # 真实业务数据（手机号/日期/ID 等字面量）不出内网
        prompt_sql = mask_sql_literals(sample_sql)

        result = client.diagnose_slowquery_by_openai(
            db_type=db_type,
            db_name=db_name,
            sample_sql=prompt_sql,
            stats=stats,
            trend_summary=trend_summary,
            table_schemas=table_schemas,
            explain_text=explain_text,
        )

        # 提取 token 使用量
        prompt_tokens = result.pop("_prompt_tokens", 0)
        completion_tokens = result.pop("_completion_tokens", 0)

        # AI 服务异常/解析失败返回了降级占位（DIAGNOSIS_FALLBACK）：
        # 不落 success 报告，直接判 failed 写错误信息——否则空报告会被
        # _get_cached_report 当 success 缓存 7 天，用户点重试仍拿旧空报告，
        # 永远无法重新触发 AI 诊断（H2）
        if result.get("_is_fallback"):
            task.status = "failed"
            task.error = "AI 诊断服务暂不可用，已降级跳过，请稍后重试"
            task.finished_at = _dt_mod.datetime.now()
            task.save(update_fields=["status", "error", "finished_at"])
            record_ai_usage(
                capability="slowquery_diagnosis",
                client=client,
                db_type=db_type,
                instance_name=instance.instance_name,
                db_name=db_name,
                user_name=task.user.username,
                status="failed",
                error=str(task.error),
            )
            logger.warning(f"[诊断 {task_id}] AI 服务不可用，任务标记 failed")
            return

        # 3. 落库报告
        _set_progress("saving")
        # 重跑同一任务时清理旧报告（AIDiagnosisReport.task 为 OneToOne 唯一），
        # 避免 force 重试/重复执行同一 task_id 时唯一键冲突。
        # delete+create 包 atomic：极端情况下 create 失败不会留下"无报告的 success 任务"
        with transaction.atomic():
            AIDiagnosisReport.objects.filter(task=task).delete()
            report = AIDiagnosisReport.objects.create(
                task=task,
                sql_hash=sql_hash,
                root_cause=result.get("root_cause", ""),
                severity=result.get("severity", "unknown"),
                bottleneck_type=result.get("bottleneck_type", "other"),
                evidence=result.get("evidence", []),
                suggestions=result.get("suggestions", []),
                confidence=result.get("confidence", 0.0),
                model=model_name,
            )

        # 更新任务状态
        task.status = "success"
        task.model = model_name
        task.prompt_tokens = prompt_tokens
        task.completion_tokens = completion_tokens
        task.finished_at = _dt_mod.datetime.now()
        task.save(update_fields=[
            "status", "model", "prompt_tokens",
            "completion_tokens", "finished_at",
        ])

        # 统一用量记账（AIDiagnosisTask 上的 token 字段保留，此处补全链路统一流水）
        record_ai_usage(
            capability="slowquery_diagnosis",
            client=client,
            db_type=db_type,
            instance_name=instance.instance_name,
            db_name=db_name,
            user_name=task.user.username,
        )

        logger.info(
            f"[诊断 {task_id}] 完成: severity={report.severity}, "
            f"bottleneck={report.bottleneck_type}, tokens={prompt_tokens}+{completion_tokens}"
        )

    except Exception as e:
        logger.error(f"[诊断 {task_id}] 失败: {e}", exc_info=True)
        task.status = "failed"
        task.error = str(e)[:1000]
        task.finished_at = _dt.datetime.now()
        task.save(update_fields=["status", "error", "finished_at"])


