"""
慢查询 API v2 - 支持 MySQL/PgSQL/MongoDB/Redis 统一采集架构

路由：
  POST /api/v1/slowquery/summary/          — 慢查统计
  POST /api/v1/slowquery/detail/           — 慢查明细
  GET  /api/v1/slowquery/trend/            — 慢查趋势
  POST /api/v1/slowquery/collect/          — 手动触发采集
  POST /api/v1/slowquery/diagnose/         — AI 慢查诊断（触发/查询）
  GET  /api/v1/slowquery/diagnose/<id>/    — 轮询诊断任务状态
  POST /api/v1/slowquery/diagnose/feedback/ — 诊断反馈
  POST /api/v1/slowquery/diagnose/workflow_draft/ — 生成工单草稿

优化说明：
- 提取公共查询构建器，消除重复代码
- 统一时间单位为毫秒（前端无需转换）
- 统一错误处理格式
"""
import datetime as _dt
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from common.utils.extend_json_encoder import encode_json as _encode
from django.db.models import Avg, Count, Max, QuerySet
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from sql.models import (
    Instance,
    MySQLSlowQuerySummary,
    MySQLSlowQueryDetail,
    PgSQLSlowQuerySummary,
    PgSQLSlowQueryDetail,
    MongoSlowQuerySummary,
    MongoSlowQueryDetail,
    RedisSlowQuerySummary,
    RedisSlowQueryDetail,
)
from sql.utils.resource_group import user_instances

logger = logging.getLogger("default")


# ---------- 时间单位常量 ----------
# 统一存储为毫秒，前端无需转换

TIME_UNIT_MS = {
    "mysql": 1000,      # MySQL 存储秒 -> 毫秒
    "pgsql": 1000,      # PgSQL 存储秒 -> 毫秒
    "mongo": 1,         # MongoDB 已经是毫秒
    "redis": 0.001,     # Redis 存储微秒 -> 毫秒
}


# ---------- 统一响应格式 ----------

def success_response(data=None, msg="success"):
    """成功响应"""
    return JsonResponse(_encode({
        "status": 0,
        "msg": msg,
        "data": data
    }))


def error_response(msg="操作失败", status=1):
    """错误响应"""
    return JsonResponse({
        "status": status,
        "msg": msg,
        "data": None
    })


def list_response(rows, total):
    """列表响应"""
    return JsonResponse(_encode({
        "status": 0,
        "msg": "success",
        "total": total,
        "rows": rows
    }))


# ---------- permissions ----------


class SlowQueryV2Permission:
    """慢查询权限检查"""

    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.menu_slowquery"))


# ---------- helpers ----------


def _get_and_check_instance(user, instance_name):
    """获取实例并做权限校验"""
    if not instance_name:
        raise Instance.DoesNotExist
    instance = Instance.objects.get(instance_name=instance_name)
    user_instances(user, db_type=[instance.db_type]).get(instance_name=instance_name)
    return instance


def _parse_date(date_str):
    """解析日期字符串"""
    if not date_str:
        return None
    try:
        return _dt.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        try:
            return _dt.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def _build_queryset(qs, start_dt=None, end_dt=None, db_name=None, search=None,
                    search_field="fingerprint", time_field="last_seen"):
    """
    构建通用查询条件

    Args:
        qs: QuerySet
        start_dt: 开始时间
        end_dt: 结束时间
        db_name: 数据库名
        search: 搜索关键词
        search_field: 搜索字段名
        time_field: 时间字段名
    """
    if start_dt:
        qs = qs.filter(**{f"{time_field}__gte": start_dt})
    if end_dt:
        qs = qs.filter(**{f"{time_field}__lte": end_dt})
    if db_name:
        qs = qs.filter(db_name=db_name)
    if search:
        qs = qs.filter(**{f"{search_field}__icontains": search})
    return qs


def _round_or_zero(value, decimals=2):
    """安全四舍五入，None 返回 0"""
    return round(value or 0, decimals)


def _int_or_zero(value):
    """安全转整数，None 返回 0"""
    return int(value or 0)


def _safe_int(value, default):
    """安全转整数，空/非数字入参返回默认值（避免非法入参触发 HTTP 500）"""
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _mask_sql_literals(sql_text):
    """对 SQL 中的字面量脱敏，供外发到外部 AI 时使用（H1）。

    慢查样本 SQL 含真实业务数据（user_id、手机号、日期等字面量），
    PRD §5.8/§12 要求 prompt 不携带脱敏后的业务数据行。此处把
    字符串字面量替换为 '?'、数字字面量替换为 0，保留 SQL 结构语义
    （与 pg_stat_statements 等指纹归一化同思路），仅影响 AI prompt，
    不影响 EXPLAIN 等需要真实值的场景。

    注意：`\b` 保证标识符内的数字（如 table_2024、idx_2）不被误伤。
    """
    if not sql_text:
        return sql_text
    # 字符串字面量（含转义序列），替换为 '?'
    masked = re.sub(r"'(?:[^'\\]|\\.)*'", "'?'", sql_text)
    # 数字字面量（整数/小数/科学计数法），替换为 0
    masked = re.sub(r"\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b", "0", masked)
    return masked


# ---------- 查询配置 ----------

# Summary 查询配置
SUMMARY_CONFIG = {
    "mysql": {
        "model": MySQLSlowQuerySummary,
        "fields": [
            "sql_hash", "fingerprint", "sample_sql", "db_name",
            "total_execution_counts", "total_execution_times",
            "query_time_avg", "query_time_p95",
            "parse_total_row_counts", "return_total_row_counts",
            "parse_row_avg", "return_row_avg",
            "first_seen", "last_seen",
        ],
        "field_map": {
            "sql_hash": "SQLId",
            "fingerprint": "SQLText",
            "last_seen": "CreateTime",
            "db_name": "DBName",
            "total_execution_counts": "MySQLTotalExecutionCounts",
            "total_execution_times": "MySQLTotalExecutionTimes",
            "query_time_avg": "QueryTimeAvg",
            "query_time_p95": "QueryTimePct95",
            "parse_total_row_counts": "ParseTotalRowCounts",
            "return_total_row_counts": "ReturnTotalRowCounts",
            "parse_row_avg": "ParseRowAvg",
            "return_row_avg": "ReturnRowAvg",
        },
        "round_fields": ["total_execution_times", "query_time_avg", "query_time_p95"],
        "int_fields": ["parse_row_avg", "return_row_avg"],
    },
    "pgsql": {
        "model": PgSQLSlowQuerySummary,
        "fields": [
            "sql_hash", "fingerprint", "sample_sql", "db_name", "user_name",
            "total_execution_counts", "total_execution_times",
            "query_time_avg", "query_time_p95",
            "rows_sum", "rows_avg",
            "shared_blks_hit", "shared_blks_read",
            "first_seen", "last_seen",
        ],
        "field_map": {
            "sql_hash": "SQLId",
            "fingerprint": "SQLText",
            "last_seen": "CreateTime",
            "db_name": "DBName",
            "total_execution_counts": "TotalExecutionCounts",
            "total_execution_times": "TotalExecutionTimes",
            "query_time_avg": "QueryTimeAvg",
            "query_time_p95": "QueryTimePct95",
            "rows_sum": "ReturnTotalRowCounts",
            "rows_avg": "ReturnRowAvg",
            "shared_blks_hit": "SharedBlksHit",
            "shared_blks_read": "SharedBlksRead",
        },
        "round_fields": ["total_execution_times", "query_time_avg", "query_time_p95", "rows_avg"],
        "int_fields": [],
    },
    "mongo": {
        "model": MongoSlowQuerySummary,
        "fields": [
            "sql_hash", "fingerprint", "sample_sql", "db_name",
            "collection_name", "operation_type",
            "total_execution_counts", "total_execution_times",
            "query_time_avg", "query_time_p95",
            "docs_examined_avg", "docs_returned_avg", "has_sort",
            "first_seen", "last_seen",
        ],
        "field_map": {
            "sql_hash": "SQLId",
            "fingerprint": "SQLText",
            "last_seen": "CreateTime",
            "db_name": "DBName",
            "collection_name": "CollectionName",
            "operation_type": "OperationType",
            "total_execution_counts": "TotalExecutionCounts",
            "total_execution_times": "TotalExecutionTimes",
            "query_time_avg": "QueryTimeAvg",
            "query_time_p95": "QueryTimePct95",
            "docs_examined_avg": "DocsExaminedAvg",
            "docs_returned_avg": "DocsReturnedAvg",
            "has_sort": "HasSort",
        },
        "round_fields": ["total_execution_times", "query_time_avg", "query_time_p95",
                         "docs_examined_avg", "docs_returned_avg"],
        "int_fields": [],
        "bool_fields": {"has_sort": {True: "是", False: "否"}},
    },
    "redis": {
        "model": RedisSlowQuerySummary,
        "fields": [
            "sql_hash", "fingerprint", "sample_sql",
            "total_execution_counts", "total_execution_times",
            "query_time_avg", "query_time_p95",
            "first_seen", "last_seen",
        ],
        "field_map": {
            "sql_hash": "SQLId",
            "fingerprint": "SQLText",
            "last_seen": "CreateTime",
            "total_execution_counts": "TotalExecutionCounts",
            "total_execution_times": "TotalExecutionTimes",
            "query_time_avg": "QueryTimeAvg",
            "query_time_p95": "DurationPct95",
        },
        "round_fields": ["total_execution_times", "query_time_avg", "query_time_p95"],
        "int_fields": [],
    },
}

# Detail 查询配置
DETAIL_CONFIG = {
    "mysql": {
        "model": MySQLSlowQueryDetail,
        "fields": [
            "sql_hash", "execution_start_time", "host_address", "db_name", "sql_text",
            "query_time", "lock_time", "rows_sent", "rows_examined",
        ],
        "field_map": {
            "sql_hash": "SQLId",
            "execution_start_time": "ExecutionStartTime",
            "host_address": "HostAddress",
            "db_name": "DBName",
            "sql_text": "SQLText",
            "query_time": "QueryTimes",
            "lock_time": "LockTimes",
            "rows_sent": "ReturnRowCounts",
            "rows_examined": "ParseRowCounts",
        },
        "round_fields": ["query_time", "lock_time"],
        "int_fields": [],
        "time_field": "execution_start_time",
    },
    "pgsql": {
        "model": PgSQLSlowQueryDetail,
        "fields": [
            "sql_hash", "execution_start_time", "host_address", "user_name", "db_name", "sql_text",
            "query_time", "rows_sent", "shared_blks_hit", "shared_blks_read",
        ],
        "field_map": {
            "sql_hash": "SQLId",
            "execution_start_time": "ExecutionStartTime",
            "host_address": "HostAddress",
            "db_name": "DBName",
            "sql_text": "SQLText",
            "query_time": "QueryTimes",
            "rows_sent": "ReturnRowCounts",
            "shared_blks_hit": "SharedBlksHit",
            "shared_blks_read": "SharedBlksRead",
        },
        "round_fields": ["query_time"],
        "int_fields": [],
        "time_field": "execution_start_time",
    },
    "mongo": {
        "model": MongoSlowQueryDetail,
        "fields": [
            "sql_hash", "execution_start_time", "operation_type", "host_address",
            "db_name", "collection_name", "command_text",
            "duration", "docs_examined", "docs_returned", "nreturned", "has_sort",
        ],
        "field_map": {
            "sql_hash": "SQLId",
            "execution_start_time": "执行时间",
            "operation_type": "操作类型",
            "host_address": "客户端地址",
            "db_name": "数据库",
            "collection_name": "集合",
            "command_text": "命令",
            "duration": "执行耗时(ms)",
            "docs_examined": "扫描文档数",
            "docs_returned": "返回文档数",
            "nreturned": "返回结果数",
            "has_sort": "包含排序",
        },
        "round_fields": ["duration"],
        "int_fields": [],
        "bool_fields": {"has_sort": {True: "是", False: "否"}},
        "time_field": "execution_start_time",
    },
    "redis": {
        "model": RedisSlowQueryDetail,
        "fields": [
            "sql_hash", "execution_start_time", "host_address", "command_text", "duration",
        ],
        "field_map": {
            "sql_hash": "SQLId",
            "execution_start_time": "ExecutionStartTime",
            "host_address": "HostName",
            "command_text": "SQLText",
            "duration": "Duration",
        },
        "round_fields": ["duration"],
        "int_fields": [],
        "time_field": "execution_start_time",
    },
}


def _format_rows(rows, config, db_type):
    """
    格式化查询结果行

    Args:
        rows: 原始数据行
        config: 配置信息
        db_type: 数据库类型
    """
    field_map = config["field_map"]
    round_fields = config.get("round_fields", [])
    int_fields = config.get("int_fields", [])
    bool_fields = config.get("bool_fields", {})
    time_unit = TIME_UNIT_MS.get(db_type, 1)

    # 需要进行时间单位转换的字段
    TIME_FIELDS = [
        "total_execution_times", "query_time_avg", "query_time_p95",
        "query_time", "lock_time", "duration",
    ]

    formatted = []
    for row in rows:
        new_row = {}
        for old_key, new_key in field_map.items():
            value = row.get(old_key)

            # 时间单位转换
            if old_key in round_fields and old_key in TIME_FIELDS:
                value = _round_or_zero(value * time_unit if time_unit != 1 else value, 2)
            elif old_key in round_fields:
                value = _round_or_zero(value, 2)

            # 整数转换
            if old_key in int_fields:
                value = _int_or_zero(value)

            # 布尔值转换
            if old_key in bool_fields:
                value = bool_fields[old_key].get(value, str(value))

            new_row[new_key] = value

        formatted.append(new_row)

    return formatted


# ---------- Summary (统计) ----------


class SlowQuerySummaryView(APIView):
    """慢查统计 - 支持 MySQL/PgSQL/MongoDB/Redis"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        instance_name = request.data.get("instance_name")
        start_time = request.data.get("StartTime") or "2010-01-01"
        end_time = request.data.get("EndTime") or _dt.datetime.now().strftime("%Y-%m-%d")
        db_name = request.data.get("db_name")
        search = request.data.get("search", "")
        limit = _safe_int(request.data.get("limit"), 50)
        offset = _safe_int(request.data.get("offset"), 0)

        try:
            instance = _get_and_check_instance(request.user, instance_name)
        except Instance.DoesNotExist:
            return error_response("你所在组未关联该实例")

        # 解析时间
        start_dt = _parse_date(start_time)
        _parsed_end = _parse_date(end_time)
        end_dt = _parsed_end + _dt.timedelta(days=1) if _parsed_end else None

        # 检查是否是阿里云 RDS 实例
        from sql.models import AliyunRdsConfig
        is_aliyun_rds = AliyunRdsConfig.objects.filter(instance=instance, is_enable=True).exists()

        try:
            # 根据数据库类型分发
            db_type = instance.db_type
            if is_aliyun_rds:
                result = self._query_aliyun(instance, db_type, start_time, end_time, db_name, limit, offset)
            elif db_type in SUMMARY_CONFIG:
                result = self._query_local(instance, db_type, start_dt, end_dt, db_name, search, limit, offset)
            else:
                return error_response(f"不支持的数据库类型: {db_type}")

            return list_response(result["rows"], result["total"])

        except Exception as e:
            # 只回显通用文案，原始异常进日志（L6：避免泄漏引擎/连接串细节）
            logger.error(f"获取慢查询统计失败: {e}", exc_info=True)
            return error_response("获取慢查询统计失败")

    def _query_aliyun(self, instance, db_type, start_time, end_time, db_name, limit, offset):
        """查询阿里云 RDS 慢查询统计"""
        if db_type == "mysql":
            from sql.engines import get_engine
            engine = get_engine(instance=instance)
            result = engine.slowquery_review(start_time, end_time, db_name, limit, offset)
            # 阿里云MySQL统计返回的时间字段单位是秒，需要乘以1000转为毫秒
            if "rows" in result:
                for row in result["rows"]:
                    # 阿里云返回的字段名
                    for time_field in ["QueryTimeAvg", "QueryTimePct95", "TotalExecutionTimes",
                                       "MySQLTotalExecutionTimes", "QueryTimes", "LockTimes"]:
                        if time_field in row and row[time_field] is not None:
                            row[time_field] = round(float(row[time_field]) * 1000, 2)
            return result
        elif db_type == "mongo":
            from sql.engines.cloud.aliyun_mongo import AliyunMongoEngine
            engine = AliyunMongoEngine(instance=instance)
            return engine.slowquery_review(start_time, end_time, db_name, limit, offset)
        elif db_type == "redis":
            from sql.engines.cloud.aliyun_redis import AliyunRedisEngine
            engine = AliyunRedisEngine(instance=instance)
            result = engine.slowquery_review(start_time, end_time, db_name, limit, offset)
            # 阿里云Redis统计返回的时间字段单位是微秒，需要除以1000转为毫秒
            if "rows" in result:
                for row in result["rows"]:
                    for time_field in ["TotalExecutionTimes", "ElapsedTimeAvg", "ElapsedTimePct95",
                                       "QueryTimeAvg", "QueryTimePct95", "DurationPct95"]:
                        if time_field in row and row[time_field] is not None:
                            row[time_field] = round(float(row[time_field]) / 1000, 2)
            return result
        else:
            raise ValueError(f"阿里云不支持的数据库类型: {db_type}")

    def _query_local(self, instance, db_type, start_dt, end_dt, db_name, search, limit, offset):
        """查询本地数据库慢查询"""
        config = SUMMARY_CONFIG[db_type]
        model = config["model"]

        # 构建查询
        qs = model.objects.filter(instance_id=instance.id)
        qs = _build_queryset(qs, start_dt, end_dt, db_name, search)

        # 统计总数
        total = qs.count()

        # 查询数据
        rows = list(
            qs.order_by("-total_execution_times")
            [offset:offset + limit]
            .values(*config["fields"])
        )

        # 格式化数据
        formatted_rows = _format_rows(rows, config, db_type)

        return {"total": total, "rows": formatted_rows}


# ---------- Detail (明细) ----------


class SlowQueryDetailView(APIView):
    """慢查明细 - 支持 MySQL/PgSQL/MongoDB/Redis"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        instance_name = request.data.get("instance_name")
        start_time = request.data.get("StartTime") or "2010-01-01"
        end_time = request.data.get("EndTime") or _dt.datetime.now().strftime("%Y-%m-%d")
        db_name = request.data.get("db_name")
        sql_id = request.data.get("SQLId")
        search = request.data.get("search", "")
        limit = _safe_int(request.data.get("limit"), 50)
        offset = _safe_int(request.data.get("offset"), 0)

        try:
            instance = _get_and_check_instance(request.user, instance_name)
        except Instance.DoesNotExist:
            return error_response("你所在组未关联该实例")

        # 解析时间
        start_dt = _parse_date(start_time)
        _parsed_end = _parse_date(end_time)
        end_dt = _parsed_end + _dt.timedelta(days=1) if _parsed_end else None

        # 检查是否是阿里云 RDS 实例
        from sql.models import AliyunRdsConfig
        is_aliyun_rds = AliyunRdsConfig.objects.filter(instance=instance, is_enable=True).exists()

        try:
            # 根据数据库类型分发
            db_type = instance.db_type
            if is_aliyun_rds:
                result = self._query_aliyun(instance, db_type, start_time, end_time, db_name, sql_id, limit, offset)
            elif db_type in DETAIL_CONFIG:
                result = self._query_local(instance, db_type, start_dt, end_dt, db_name, sql_id, search, limit, offset)
            else:
                return error_response(f"不支持的数据库类型: {db_type}")

            return list_response(result["rows"], result["total"])

        except Exception as e:
            logger.error(f"获取慢查询明细失败: {e}", exc_info=True)
            return error_response("获取慢查询明细失败")

    def _query_aliyun(self, instance, db_type, start_time, end_time, db_name, sql_id, limit, offset):
        """查询阿里云 RDS 慢查询明细"""
        if db_type == "mysql":
            from sql.engines import get_engine
            engine = get_engine(instance=instance)
            result = engine.slowquery_review_history(start_time, end_time, db_name, sql_id, limit, offset)
            # 格式化阿里云 MySQL 返回的字段
            # 注意：阿里云返回的时间单位是秒，需要乘以1000转为毫秒
            if "rows" in result:
                result["rows"] = [
                    {
                        "SQLId": row.get("SQLId") or row.get("SQLHASH") or row.get("SQLHash") or "",
                        "ExecutionStartTime": row.get("ExecutionStartTime"),
                        "HostAddress": row.get("HostAddress"),
                        "DBName": row.get("DBName"),
                        "SQLText": row.get("SQLText"),
                        "QueryTimes": round(float(row.get("QueryTimes", 0)) * 1000, 2),  # 秒 -> 毫秒
                        "LockTimes": round(float(row.get("LockTimes", 0)) * 1000, 2),  # 秒 -> 毫秒
                        "ParseRowCounts": row.get("ParseRowCounts"),
                        "ReturnRowCounts": row.get("ReturnRowCounts"),
                    }
                    for row in result["rows"]
                ]
            return result
        elif db_type == "mongo":
            from sql.engines.cloud.aliyun_mongo import AliyunMongoEngine
            engine = AliyunMongoEngine(instance=instance)
            result = engine.slowquery_review_history(start_time, end_time, db_name, sql_id, limit, offset)
            # 格式化阿里云 MongoDB 返回的字段
            if "rows" in result:
                result["rows"] = [
                    {
                        "SQLId": row.get("SQLId") or row.get("SQLHASH") or "",
                        "执行时间": row.get("ExecutionStartTime", ""),
                        "客户端地址": row.get("HostAddress", ""),
                        "数据库": row.get("DBName", ""),
                        "集合": row.get("TableName", ""),
                        "命令": row.get("SQLText", ""),
                        "执行耗时(ms)": round(float(row.get("QueryTimes", 0)), 2),
                        "扫描文档数": row.get("DocsExamined", 0),
                        "返回文档数": row.get("ReturnRowCounts", 0),
                    }
                    for row in result["rows"]
                ]
            return result
        elif db_type == "redis":
            from sql.engines.cloud.aliyun_redis import AliyunRedisEngine
            engine = AliyunRedisEngine(instance=instance)
            result = engine.slowquery_review_history(start_time, end_time, db_name, sql_id, limit, offset)
            # 格式化阿里云 Redis 返回的字段
            if "rows" in result:
                result["rows"] = [
                    {
                        "SQLId": row.get("SQLId") or row.get("SQLHASH") or "",
                        "ExecutionStartTime": row.get("ExecuteTime", ""),
                        "HostName": row.get("IPAddress", ""),
                        "SQLText": row.get("Command", ""),
                        "Duration": round(float(row.get("ElapsedTime", 0)) / 1000, 3),
                    }
                    for row in result["rows"]
                ]
            return result
        else:
            raise ValueError(f"阿里云不支持的数据库类型: {db_type}")

    def _query_local(self, instance, db_type, start_dt, end_dt, db_name, sql_id, search, limit, offset):
        """查询本地数据库慢查询明细"""
        config = DETAIL_CONFIG[db_type]
        model = config["model"]
        time_field = config.get("time_field", "execution_start_time")

        # 构建查询
        qs = model.objects.filter(instance_id=instance.id)
        qs = _build_queryset(qs, start_dt, end_dt, db_name, search,
                            search_field="sql_text" if db_type != "mongo" else "command_text",
                            time_field=time_field)

        # SQL ID 过滤
        if sql_id:
            qs = qs.filter(sql_hash=sql_id)

        # 统计总数
        total = qs.count()

        # 查询数据
        rows = list(
            qs.order_by(f"-{time_field}")
            [offset:offset + limit]
            .values(*config["fields"])
        )

        # 格式化数据
        formatted_rows = _format_rows(rows, config, db_type)

        return {"total": total, "rows": formatted_rows}


# ---------- Trend (趋势) ----------


class SlowQueryTrendView(APIView):
    """慢查趋势 - 支持所有数据库类型"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        instance_name = request.query_params.get("instance_name")
        sql_hash = request.query_params.get("sql_hash")
        days = _safe_int(request.query_params.get("days"), 7)

        try:
            instance = _get_and_check_instance(request.user, instance_name)
        except Instance.DoesNotExist:
            return error_response("你所在组未关联该实例")

        if not sql_hash:
            return error_response("缺少 sql_hash 参数")

        # 计算时间范围
        end_dt = _dt.datetime.now()
        start_dt = end_dt - _dt.timedelta(days=days)

        try:
            db_type = instance.db_type
            if db_type == "mysql":
                result = self._query_trend(MySQLSlowQueryDetail, instance, sql_hash, start_dt, end_dt, "query_time", db_type)
            elif db_type == "pgsql":
                result = self._query_trend(PgSQLSlowQueryDetail, instance, sql_hash, start_dt, end_dt, "query_time", db_type)
            elif db_type == "mongo":
                result = self._query_trend(MongoSlowQueryDetail, instance, sql_hash, start_dt, end_dt, "duration", db_type)
            elif db_type == "redis":
                result = self._query_trend(RedisSlowQueryDetail, instance, sql_hash, start_dt, end_dt, "duration", db_type)
            else:
                return error_response(f"不支持的数据库类型: {db_type}")

            return success_response(result)

        except Exception as e:
            logger.error(f"获取慢查询趋势失败: {e}", exc_info=True)
            return error_response("获取慢查询趋势失败")

    def _query_trend(self, model, instance, sql_hash, start_dt, end_dt, time_field, db_type):
        """查询趋势数据（时间统一换算为毫秒，与 summary/诊断口径一致）"""
        qs = model.objects.filter(
            instance_id=instance.id,
            sql_hash=sql_hash,
            execution_start_time__gte=start_dt,
            execution_start_time__lte=end_dt,
        )

        # 按日期聚合
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

        # 引擎原始单位 -> 毫秒（MySQL/PgSQL 秒、Mongo 毫秒、Redis 微秒）
        time_unit = TIME_UNIT_MS.get(db_type, 1)

        # 格式化结果
        rows = [
            {
                "date": item["date"].strftime("%Y-%m-%d"),
                "count": item["count"],
                "avg_time": round((item["avg_time"] or 0) * time_unit, 2),
                "max_time": round((item["max_time"] or 0) * time_unit, 2),
            }
            for item in trend
        ]

        return rows


# ---------- Collect (采集) ----------


class SlowQueryCollectView(APIView):
    """手动触发慢查询采集"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        instance_name = request.data.get("instance_name")
        collect_type = request.data.get("type", "all")

        if not instance_name:
            return error_response("缺少 instance_name 参数")

        try:
            instance = _get_and_check_instance(request.user, instance_name)
        except Instance.DoesNotExist:
            return error_response("你所在组未关联该实例")

        # 异步执行采集任务：走 django-q2 队列（与 collect_all_slowquery_task 同构），
        # 请求线程立即返回真实 task_id，前端可凭此轮询/追踪，而不是同步阻塞到采集完成
        try:
            from django_q.tasks import async_task

            task_id = async_task(
                "sql.collectors.tasks.collect_slowquery_task",
                instance.id,
                collect_type,
            )
            return success_response({"task_id": task_id}, "采集任务已提交")

        except Exception as e:
            logger.error(f"提交采集任务失败: {e}", exc_info=True)
            return error_response("提交采集任务失败")


# ---------- AI 诊断 (Diagnose) ----------


# 诊断报告缓存复用窗口（天）
DIAGNOSIS_CACHE_DAYS = 7

# 诊断任务 stale 判定阈值（分钟）：超过后自动判 failed，防止前端无限轮询。
# - running（执行中）：AI 单次 ≤60s + 采集余量，健康任务 ~2min 内收敛，5 分钟足够兜底，
#   且能在用户轮询放弃（~370s）前收到明确 failed。
# - pending（排队中）：线程池并发满时最长排队 4 并发+4 排队 × ~60s ≈ 8min，
#   放宽到 12 分钟避免排队中的任务被误判失败。
DIAGNOSIS_STALE_RUNNING_MINUTES = 5
DIAGNOSIS_STALE_PENDING_MINUTES = 12

# 诊断线程池：在 web 进程内用线程池直接执行诊断，不依赖 django-q 队列/worker。
# 这样即使后台任务队列积压（qcluster worker 卡死等）也不影响诊断出报告。
_DIAG_MAX_WORKERS = 4       # 同时最多 4 个诊断并发执行
_DIAG_MAX_PENDING = 4       # 允许排队的数量（超过直接拒绝，前端可稍后重试）
_DIAG_EXECUTOR = None
_DIAG_SLOTS = None
_DIAG_EXECUTOR_LOCK = threading.Lock()


def _get_diag_executor():
    """懒加载诊断线程池（模块级单例），并发上限 = workers + pending。"""
    global _DIAG_EXECUTOR, _DIAG_SLOTS
    if _DIAG_EXECUTOR is None:
        with _DIAG_EXECUTOR_LOCK:
            if _DIAG_EXECUTOR is None:
                _DIAG_SLOTS = threading.BoundedSemaphore(_DIAG_MAX_WORKERS + _DIAG_MAX_PENDING)
                _DIAG_EXECUTOR = ThreadPoolExecutor(max_workers=_DIAG_MAX_WORKERS)
    return _DIAG_EXECUTOR, _DIAG_SLOTS

# 深度诊断支持的数据库类型（有稳定 EXPLAIN）
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


def _check_diagnosis_permission(user):
    """检查用户是否有 AI 诊断权限"""
    if user.is_superuser:
        return True, ""
    if not user.has_perm("sql.use_ai_diagnosis"):
        return False, "无 AI 慢查诊断权限（需 sql.use_ai_diagnosis）"
    return True, ""


def _check_diagnosis_config():
    """检查 AI 诊断功能开关与配置"""
    from common.config import SysConfig
    from common.utils.openai import check_openai_config

    config = SysConfig()
    if not config.get("enable_ai_slowquery_diagnosis", False):
        return False, "AI 慢查诊断功能未开启"
    if not check_openai_config():
        return False, "AI 服务未配置（缺少 openai_api_key）"
    return True, ""


def _get_cached_report(instance, db_name, sql_hash, model_name=""):
    """查询 7 天内同指纹的已有诊断报告（缓存复用）"""
    from sql.models import AIDiagnosisTask, AIDiagnosisReport

    cutoff = _dt.datetime.now() - _dt.timedelta(days=DIAGNOSIS_CACHE_DAYS)
    task = (
        AIDiagnosisTask.objects
        .filter(
            instance=instance,
            db_name=db_name,
            sql_hash=sql_hash,
            status="success",
            created_at__gte=cutoff,
        )
        .order_by("-created_at")
        .first()
    )
    if not task:
        return None, None
    # 显式查询替代 hasattr(task, "report")，避免依赖反向 OneToOne 访问抛异常
    report = AIDiagnosisReport.objects.filter(task=task).first()
    if not report:
        return None, None
    # 模型不一致时不复用（避免不同模型结果混用）
    if model_name and report.model and report.model != model_name:
        return None, None
    return task, report


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


def _serialize_report(report):
    """序列化诊断报告为前端可用的 dict"""
    from common.utils.extend_json_encoder import encode_json as _enc
    return {
        "id": report.id,
        "task_id": report.task_id,
        "sql_hash": report.sql_hash,
        "root_cause": report.root_cause,
        "severity": report.severity,
        "bottleneck_type": report.bottleneck_type,
        "evidence": report.evidence,
        "suggestions": report.suggestions,
        "report_markdown": report.report_markdown,
        "confidence": report.confidence,
        "model": report.model,
        "created_at": report.created_at.strftime("%Y-%m-%d %H:%M:%S") if report.created_at else "",
    }


def _serialize_task(task):
    """序列化诊断任务为前端可用的 dict"""
    data = {
        "task_id": task.id,
        "status": task.status,
        "progress": task.progress,
        "sql_hash": task.sql_hash,
        "error": task.error,
        "created_at": task.created_at.strftime("%Y-%m-%d %H:%M:%S") if task.created_at else "",
    }
    if task.status == "success" and hasattr(task, "report"):
        data["report"] = _serialize_report(task.report)
    return data


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
        # 截取 SQL 的第一条语句（避免多条 SQL 导致 EXPLAIN 失败）
        first_sql = sql_text.strip().split(";")[0].strip()
        if not first_sql:
            return "（SQL 文本为空）"
        # 安全红线（H4）：slow_log 条目/库内值为不可信来源，只对 SELECT/WITH 语句
        # 做 EXPLAIN，拒绝任何写语句及 `/*!...*/` 版本注释、INTO OUTFILE/DUMPFILE
        # 等注入面。注释剥离后再校验语句类型（版本注释剥离后剩余为空同样被拒）。
        clean_sql = re.sub(
            r"(?:--[^\n]*|/\*.*?\*/)", "", first_sql, flags=re.DOTALL
        )
        if not re.match(r"^\s*(?:SELECT|WITH)\b", clean_sql, re.IGNORECASE):
            return "（仅支持 SELECT/WITH 语句的 EXPLAIN，已跳过该样本）"
        if re.search(r"\bINTO\s+(?:OUTFILE|DUMPFILE)\b", clean_sql, re.IGNORECASE):
            return "（样本包含 INTO OUTFILE/DUMPFILE，已拒绝执行 EXPLAIN）"
        # 用剥离注释后的语句执行：前导注释留在 EXPLAIN 后会被当作第二条语句导致失败
        first_sql = clean_sql.strip()
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
    from common.utils.openai import OpenaiClient, DIAGNOSIS_FALLBACK
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
        client = OpenaiClient()
        model_name = client.default_chat_model

        # 外发脱敏：prompt 只携带字面量脱敏后的 SQL（H1），
        # 真实业务数据（手机号/日期/ID 等字面量）不出内网
        prompt_sql = _mask_sql_literals(sample_sql)

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
            logger.warning(f"[诊断 {task_id}] AI 服务不可用，任务标记 failed")
            return

        # 3. 落库报告
        _set_progress("saving")
        # 重跑同一任务时清理旧报告（AIDiagnosisReport.task 为 OneToOne 唯一），
        # 避免 force 重试/重复执行同一 task_id 时唯一键冲突
        AIDiagnosisReport.objects.filter(task=task).delete()
        report = AIDiagnosisReport.objects.create(
            task=task,
            sql_hash=sql_hash,
            root_cause=result.get("root_cause", ""),
            severity=result.get("severity", "unknown"),
            bottleneck_type=result.get("bottleneck_type", "other"),
            evidence=result.get("evidence", []),
            suggestions=result.get("suggestions", []),
            report_markdown=result.get("report_markdown", ""),
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


# ---------- 诊断 API 视图 ----------


class SlowQueryDiagnosePermission:
    """AI 慢查诊断权限检查"""

    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated):
            return False
        # 查看（GET）复用慢查菜单权限
        if request.method == "GET":
            return u.is_superuser or u.has_perm("sql.menu_slowquery")
        # 诊断操作（POST）需要 use_ai_diagnosis 权限
        return u.is_superuser or u.has_perm("sql.use_ai_diagnosis")


class SlowQueryDiagnoseBatchStatusView(APIView):
    """批量查询已诊断状态 - 避免前端对每行逐个 GET 造成 N+1 请求

    GET /api/v1/slowquery/diagnose/batch_status/?instance_name=&db_name=&hashes=a,b,c
    返回 { "diagnosed": ["hash1", ...] }
    """

    permission_classes = [SlowQueryDiagnosePermission]

    def get(self, request):
        instance_name = request.query_params.get("instance_name")
        db_name = request.query_params.get("db_name", "")
        hashes_raw = request.query_params.get("hashes", "")
        hashes = [h for h in hashes_raw.split(",") if h][:100]

        if not instance_name or not hashes:
            return error_response("缺少 instance_name 或 hashes 参数")

        try:
            instance = _get_and_check_instance(request.user, instance_name)
        except Instance.DoesNotExist:
            return error_response("你所在组未关联该实例")

        from sql.models import AIDiagnosisTask

        cutoff = _dt.datetime.now() - _dt.timedelta(days=DIAGNOSIS_CACHE_DAYS)
        diagnosed = set(
            AIDiagnosisTask.objects
            .filter(
                instance=instance,
                db_name=db_name,
                sql_hash__in=hashes,
                status="success",
                created_at__gte=cutoff,
            )
            .values_list("sql_hash", flat=True)
            .distinct()
        )
        return success_response({"diagnosed": sorted(diagnosed)}, "success")


class SlowQueryDiagnoseView(APIView):
    """AI 慢查诊断 - 触发诊断 / 查询已有报告"""

    # GET(查报告)需 menu_slowquery，POST(触发诊断)需 use_ai_diagnosis
    permission_classes = [SlowQueryDiagnosePermission]

    def post(self, request):
        """触发 AI 诊断

        Body: { instance_name, db_name, sql_hash, force? }
        """
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name", "")
        sql_hash = request.data.get("sql_hash")
        force = request.data.get("force", False)

        if not instance_name or not sql_hash:
            return error_response("缺少 instance_name 或 sql_hash 参数")

        # 权限检查
        ok, msg = _check_diagnosis_permission(request.user)
        if not ok:
            return error_response(msg)

        # 配置检查
        ok, msg = _check_diagnosis_config()
        if not ok:
            return error_response(msg)

        try:
            instance = _get_and_check_instance(request.user, instance_name)
        except Instance.DoesNotExist:
            return error_response("你所在组未关联该实例")

        from common.utils.openai import OpenaiClient
        from sql.models import AIDiagnosisTask

        model_name = OpenaiClient().default_chat_model

        # 缓存复用检查（非 force 时）
        if not force:
            cached_task, cached_report = _get_cached_report(
                instance, db_name, sql_hash, model_name
            )
            if cached_task and cached_report:
                return success_response({
                    "task_id": cached_task.id,
                    "hit_cache": True,
                    "report": _serialize_report(cached_report),
                }, "命中 7 天内缓存报告")

        # 进行中去重（M7）：同指纹已有 pending/running 任务时复用其 task_id，
        # 前端直接轮询该任务，避免并发双击重复建任务、重复烧 token
        running_task = (
            AIDiagnosisTask.objects
            .filter(
                instance=instance, db_name=db_name, sql_hash=sql_hash,
                status__in=["pending", "running"],
            )
            .order_by("-created_at")
            .first()
        )
        if running_task:
            return success_response({
                "task_id": running_task.id,
                "hit_cache": False,
                "reused": True,
            }, "已有进行中的诊断任务，直接复用")

        # 创建诊断任务
        task = AIDiagnosisTask.objects.create(
            user=request.user,
            instance=instance,
            db_name=db_name,
            sql_hash=sql_hash,
            status="pending",
            model=model_name,
        )

        # 线程池旁路执行诊断：不依赖 django-q 队列/worker，
        # 即使后台任务队列积压（qcluster worker 卡死等），诊断照常出报告。
        executor, slots = _get_diag_executor()
        if not slots.acquire(blocking=False):
            task.status = "failed"
            task.error = (
                f"诊断并发已满（同时最多 {_DIAG_MAX_WORKERS} 个并发"
                f" + {_DIAG_MAX_PENDING} 个排队），请稍后再试"
            )
            task.finished_at = _dt.datetime.now()
            task.save(update_fields=["status", "error", "finished_at"])
            logger.error(f"诊断并发已满，任务 {task.id} 标记失败")
            return error_response("诊断并发已满，请稍后再试")

        def _run_diagnosis(_tid=task.id, _slots=slots):
            try:
                diagnose_slowquery_task(_tid)
            except Exception:
                logger.exception(f"诊断任务 {_tid} 执行异常")
            finally:
                _slots.release()
                # 线程池线程不是 Django 请求线程，任务结束主动关闭 DB 连接防泄漏
                try:
                    from django.db import connections
                    connections.close_all()
                except Exception:
                    pass

        try:
            executor.submit(_run_diagnosis)
        except Exception as e:
            slots.release()
            logger.error(f"提交诊断任务失败: {e}", exc_info=True)
            task.status = "failed"
            task.error = f"提交诊断任务失败: {e}"
            task.save(update_fields=["status", "error"])
            return error_response("提交诊断任务失败")

        # 审计日志
        try:
            from sql.models import AuditEntry
            AuditEntry.objects.create(
                user_id=request.user.id,
                user_name=request.user.username,
                user_display=request.user.display,
                action="slowquery.diagnose_start",
                extra_info=f"instance={instance_name}, db={db_name}, sql_hash={sql_hash}, task_id={task.id}",
            )
        except Exception:
            pass  # 审计失败不影响主流程

        return success_response({
            "task_id": task.id,
            "hit_cache": False,
        }, "诊断任务已提交")

    def get(self, request):
        """查询已有报告（按 instance + db + sql_hash）"""
        instance_name = request.query_params.get("instance_name")
        db_name = request.query_params.get("db_name", "")
        sql_hash = request.query_params.get("sql_hash")

        if not instance_name or not sql_hash:
            return error_response("缺少 instance_name 或 sql_hash 参数")

        try:
            instance = _get_and_check_instance(request.user, instance_name)
        except Instance.DoesNotExist:
            return error_response("你所在组未关联该实例")

        cached_task, cached_report = _get_cached_report(instance, db_name, sql_hash)
        if cached_task and cached_report:
            return success_response({
                "task_id": cached_task.id,
                "report": _serialize_report(cached_report),
            })

        # 查是否有进行中的任务
        from sql.models import AIDiagnosisTask
        running_task = (
            AIDiagnosisTask.objects
            .filter(
                instance=instance, db_name=db_name, sql_hash=sql_hash,
                status__in=["pending", "running"],
            )
            .order_by("-created_at")
            .first()
        )
        if running_task:
            # stale 任务自动判失败，前端直接展示失败原因而非继续轮询
            if _mark_stale_task_failed(running_task):
                return success_response({
                    "task_id": running_task.id,
                    "status": "failed",
                    "error": running_task.error,
                })
            return success_response({
                "task_id": running_task.id,
                "status": running_task.status,
            })

        return success_response(None, "未找到已有报告")


class SlowQueryDiagnoseTaskView(APIView):
    """轮询诊断任务状态 / 查看报告"""

    # 内部已有细粒度检查：超管 / 任务发起人 / 有慢查菜单权限
    permission_classes = [IsAuthenticated]

    def get(self, request, task_id):
        from sql.models import AIDiagnosisTask

        try:
            # select_related("report")：避免 _serialize_task 里 hasattr(task, "report")
            # 触发额外反向 OneToOne 查询（L3）
            task = AIDiagnosisTask.objects.select_related("report").get(id=task_id)
        except AIDiagnosisTask.DoesNotExist:
            return error_response("诊断任务不存在")

        # 权限：超管或任务发起人或有慢查菜单权限
        u = request.user
        if not (u.is_superuser or task.user_id == u.id or u.has_perm("sql.menu_slowquery")):
            return error_response("无权查看此诊断任务")

        # stale 任务自动判失败（worker 未运行/任务卡住时结束轮询）
        _mark_stale_task_failed(task)

        return success_response(_serialize_task(task))


class SlowQueryDiagnoseFeedbackView(APIView):
    """诊断反馈（P1）"""

    permission_classes = [SlowQueryDiagnosePermission]

    def post(self, request, report_id):
        from sql.models import AIDiagnosisReport, AIDiagnosisFeedback

        try:
            report = AIDiagnosisReport.objects.select_related("task__instance").get(id=report_id)
        except AIDiagnosisReport.DoesNotExist:
            return error_response("诊断报告不存在")

        # 防 IDOR：report 所属实例须在当前用户可访问组内
        instance = report.task.instance
        if not user_instances(request.user, db_type=[instance.db_type]).filter(id=instance.id).exists():
            return error_response("无权访问该诊断报告所属实例")

        helpful = request.data.get("helpful", True)
        reason = request.data.get("reason", "")

        feedback = AIDiagnosisFeedback.objects.create(
            report=report,
            user=request.user,
            helpful=bool(helpful),
            reason=str(reason)[:255],
        )

        # 审计
        try:
            from sql.models import AuditEntry
            AuditEntry.objects.create(
                user_id=request.user.id,
                user_name=request.user.username,
                user_display=request.user.display,
                action="slowquery.feedback",
                extra_info=f"report_id={report_id}, helpful={helpful}, reason={reason}",
            )
        except Exception:
            pass

        return success_response({"id": feedback.id}, "反馈已提交")


class SlowQueryDiagnoseWorkflowView(APIView):
    """生成工单草稿 - 从诊断建议生成 SQL 上线工单草稿

    安全红线：仅生成草稿，不直接执行任何 DDL/DML。
    提交仍走既有审核工作流 + goInception 检测。
    """

    # POST 需 use_ai_diagnosis（视图内 _check_diagnosis_permission 保留友好报错）
    permission_classes = [SlowQueryDiagnosePermission]

    def post(self, request):
        """从诊断建议生成工单草稿信息

        Body: { report_id, suggestion_index }
        返回工单草稿所需的 SQL 文本和元信息，前端用此填充工单创建表单。
        """
        from sql.models import AIDiagnosisReport

        report_id = request.data.get("report_id")
        try:
            suggestion_index = int(request.data.get("suggestion_index", 0))
        except (TypeError, ValueError):
            return error_response("suggestion_index 参数无效")

        # 权限检查
        ok, msg = _check_diagnosis_permission(request.user)
        if not ok:
            return error_response(msg)

        try:
            report = AIDiagnosisReport.objects.select_related("task__instance").get(id=report_id)
        except AIDiagnosisReport.DoesNotExist:
            return error_response("诊断报告不存在")

        # 防 IDOR：校验 report 所属实例在当前用户可访问组内，
        # 防止有 use_ai_diagnosis 权限的用户枚举 report_id 取任意报告草稿
        instance = report.task.instance
        if not user_instances(request.user, db_type=[instance.db_type]).filter(id=instance.id).exists():
            return error_response("无权访问该诊断报告所属实例")

        suggestions = report.suggestions or []
        if suggestion_index < 0 or suggestion_index >= len(suggestions):
            return error_response("建议索引无效")

        suggestion = suggestions[suggestion_index]
        suggestion_type = suggestion.get("type", "other")

        # 根据建议类型确定工单 SQL 内容
        if suggestion_type == "index_ddl":
            workflow_sql = suggestion.get("index_ddl", "")
        elif suggestion_type == "rewrite":
            workflow_sql = suggestion.get("after", "")
        else:
            workflow_sql = suggestion.get("index_ddl", "") or suggestion.get("after", "")

        if not workflow_sql:
            return error_response("该建议无可用于工单的 SQL 内容")

        # 审计
        try:
            from sql.models import AuditEntry
            AuditEntry.objects.create(
                user_id=request.user.id,
                user_name=request.user.username,
                user_display=request.user.display,
                action="slowquery.suggestion_adopt",
                extra_info=(
                    f"report_id={report_id}, suggestion_index={suggestion_index}, "
                    f"type={suggestion_type}, bottleneck_type={report.bottleneck_type}"
                ),
            )
        except Exception:
            pass

        # PRD §5.6/§10：工单带 AI 风险汇总，与手动提单一致——复用 api_workflow 的
        # _calc_ai_risk_summary 管线，输入构造自诊断报告的 severity/confidence。
        # severity 无效（unknown/空，如降级或模型异常）时传 None：管线按"无 AI 数据"
        # 返回占位，前端据此隐藏汇总卡片，而不是按 0 分误判为 low
        ai_risk_summary = {}
        try:
            from sql_api.api_workflow import WorkflowDetail

            has_ai = report.severity in ("low", "medium", "high")
            review_content = [{
                "ai_risk_level": report.severity if has_ai else None,
                "ai_risk_score": int((report.confidence or 0) * 100) if has_ai else None,
                "ai_ddl_lock_risk": (
                    "high"
                    if suggestion_type == "index_ddl" and report.severity == "high"
                    else ""
                ),
            }]
            ai_risk_summary = WorkflowDetail._calc_ai_risk_summary(review_content)
        except Exception as e:
            logger.warning(f"生成 AI 风险汇总失败: {e}")

        return success_response({
            "sql": workflow_sql,
            "suggestion_type": suggestion_type,
            "source": "ai_diagnosis",
            "report_id": report_id,
            "root_cause": report.root_cause,
            "severity": report.severity,
            "bottleneck_type": report.bottleneck_type,
            "desc": suggestion.get("desc", ""),
            "before": suggestion.get("before", ""),
            "after": suggestion.get("after", ""),
            "ai_risk_summary": ai_risk_summary,
        }, "工单草稿已生成，请前往工单提交流程确认")
