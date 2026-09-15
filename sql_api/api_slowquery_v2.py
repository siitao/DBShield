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
from django.db import transaction
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
from sql.utils.sql_utils import (  # noqa: F401  公共脱敏/EXPLAIN 闸门自 sql/utils 下沉，此处保留再导出兼容旧引用
    mask_sql_literals,
    sanitize_explain_sql,
)

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
# 信封实现已提升为公共模块 sql_api/response.py，此处保留兼容引用

from sql_api.response import (  # noqa: F401
    error_response,
    list_response,
    success_response,
)


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

    # 挂上慢查菜单权限：前端按 menu_slowquery 隐藏入口，接口层需同样收口，
    # 否则任何登录用户可直接打 API 拿数据（此前 permission 类定义了却未挂载）
    permission_classes = [IsAuthenticated, SlowQueryV2Permission]

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

    permission_classes = [IsAuthenticated, SlowQueryV2Permission]

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

    permission_classes = [IsAuthenticated, SlowQueryV2Permission]

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

    permission_classes = [IsAuthenticated, SlowQueryV2Permission]

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
    from common.utils.ai_gateway import check_openai_config

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




def _serialize_report(report):
    """序列化诊断报告为前端可用的 dict（结构化字段即完整内容，无叙述性报告）"""
    return {
        "id": report.id,
        "task_id": report.task_id,
        "sql_hash": report.sql_hash,
        "root_cause": report.root_cause,
        "severity": report.severity,
        "bottleneck_type": report.bottleneck_type,
        "evidence": report.evidence,
        "suggestions": report.suggestions,
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



# 诊断采集/任务执行已下沉 sql/services/diagnosis.py（此处保留兼容引用）
from sql.services.diagnosis import (  # noqa: F401
    cleanup_stale_diagnosis_tasks,
    diagnose_slowquery_task,
    _mark_stale_task_failed,
)

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

        from common.utils.ai_gateway import OpenaiClient, record_ai_usage
        from sql.models import AIDiagnosisTask

        model_name = OpenaiClient().default_chat_model

        # 缓存复用检查（非 force 时）
        if not force:
            cached_task, cached_report = _get_cached_report(
                instance, db_name, sql_hash, model_name
            )
            if cached_task and cached_report:
                record_ai_usage(
                    capability="slowquery_diagnosis",
                    model=model_name,
                    db_type=instance.db_type,
                    instance_name=instance.instance_name,
                    db_name=db_name,
                    user_name=request.user.username,
                    cache_hit=True,
                )
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

        # 防 IDOR：菜单权限分支还须校验任务所属实例在当前用户可访问组内，
        # 否则仅持菜单权限即可枚举读取任意资源组的诊断报告（H3，2026-08-20 审查）
        if not u.is_superuser and task.user_id != u.id:
            instance = task.instance
            if not user_instances(u, db_type=[instance.db_type]).filter(id=instance.id).exists():
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
        from common.utils.ai_risk import VALID_JUDGED_LEVELS

        ai_risk_summary = {}
        try:
            from sql_api.api_workflow import WorkflowDetail

            has_ai = report.severity in VALID_JUDGED_LEVELS
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
