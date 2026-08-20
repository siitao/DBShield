"""
会话诊断 DRF APIView 集 · 替代 sql/db_diagnostic.py 全部 6 个旧端点。

路由：
  POST /api/v1/diagnostic/process/             — 进程列表
  POST /api/v1/diagnostic/create_kill/          — 第一步：构建 kill 语句
  POST /api/v1/diagnostic/kill/                — 第二步：执行 kill
  POST /api/v1/diagnostic/tablespace/           — 表空间信息
  POST /api/v1/diagnostic/trxandlocks/          — 锁等待
  POST /api/v1/diagnostic/innodb_trx/           — 长事务
"""
import json
import logging

from django.http import JsonResponse
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.views import APIView

from common.utils.extend_json_encoder import encode_json, encode_json_bytes
from sql.engines import get_engine
from sql.services.instance_service import resolve_instance

logger = logging.getLogger("default")


# ---------- shared ----------

from common.utils.extend_json_encoder import encode_json as _encode, encode_json_bytes as _encode_bytes


def _safe_int(value, default):
    """安全转整数，空/非数字入参返回默认值（避免非法入参触发 HTTP 500）"""
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _parse_thread_ids(raw):
    """ThreadIDs 兼容 JSON 字符串/列表两种传参；非法 JSON 返回 None 由调用方拒绝"""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw

# ---------- permissions ----------

class ProcessViewPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.process_view"))


class ProcessKillPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.process_kill"))


class TablespaceViewPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.tablespace_view"))


class TrxAndLocksViewPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.trxandlocks_view"))


class TrxViewPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.trx_view"))


# ========== views ==========

class ProcessView(APIView):
    permission_classes = [IsAuthenticated, ProcessViewPermission]

    def post(self, request):
        instance_name = request.data.get("instance_name")
        command_type = request.data.get("command_type")

        try:
            instance = resolve_instance(request.user, instance_name=instance_name)
        except Exception:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        request_kwargs = {
            key: value
            for key, value in request.data.items()
            if key not in ("instance_name", "command_type")
        }

        query_engine = get_engine(instance=instance)
        query_result = query_engine.processlist(command_type=command_type, **request_kwargs)
        if query_result:
            if not query_result.error:
                return JsonResponse(
                    _encode({"status": 0, "msg": "ok", "rows": query_result.to_dict()}),
                    safe=False,
                )
            return JsonResponse({"status": 1, "msg": query_result.error})

        return JsonResponse({"status": 0, "msg": "ok", "rows": []})


class CreateKillSessionView(APIView):
    permission_classes = [IsAuthenticated, ProcessKillPermission]

    def post(self, request):
        instance_name = request.data.get("instance_name")
        thread_ids = request.data.get("ThreadIDs")

        try:
            instance = resolve_instance(request.user, instance_name=instance_name)
        except Exception:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        query_engine = get_engine(instance=instance)
        thread_ids = _parse_thread_ids(thread_ids)
        if not isinstance(thread_ids, list):
            return JsonResponse({"status": 1, "msg": "ThreadIDs 参数不合法", "data": []})
        try:
            data = query_engine.get_kill_command(thread_ids)
            return JsonResponse({"status": 0, "msg": "ok", "data": data})
        except AttributeError:
            return JsonResponse(
                {"status": 1, "msg": f"暂时不支持{instance.db_type}类型数据库通过进程id构建请求", "data": []}
            )


class KillSessionView(APIView):
    permission_classes = [IsAuthenticated, ProcessKillPermission]

    def post(self, request):
        instance_name = request.data.get("instance_name")
        thread_ids = request.data.get("ThreadIDs")

        try:
            instance = resolve_instance(request.user, instance_name=instance_name)
        except Exception:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        thread_ids = _parse_thread_ids(thread_ids)
        if not isinstance(thread_ids, list):
            return JsonResponse({"status": 1, "msg": "ThreadIDs 参数不合法", "data": []})

        engine = get_engine(instance=instance)
        r = None
        db_type = instance.db_type

        if db_type in ("mysql", "doris", "clickhouse"):
            r = engine.kill(thread_ids)
        elif db_type == "mongo":
            r = engine.kill_op(thread_ids)
        elif db_type == "oracle":
            r = engine.kill_session(thread_ids)
        elif db_type == "tdengine":
            r = engine.kill_query(thread_ids)
        elif db_type == "pgsql":
            r = engine.kill(thread_ids)
        else:
            return JsonResponse(
                {"status": 1, "msg": f"暂时不支持{db_type}类型数据库终止会话", "data": []}
            )

        if r and r.error:
            return JsonResponse({"status": 1, "msg": r.error, "data": []})
        return JsonResponse({"status": 0, "msg": "ok", "data": []})


class TableSpaceView(APIView):
    permission_classes = [IsAuthenticated, TablespaceViewPermission]

    def post(self, request):
        instance_name = request.data.get("instance_name")
        offset = _safe_int(request.data.get("offset"), 0)
        limit = _safe_int(request.data.get("limit"), 14)
        schema_search = request.data.get("schema_search", "")

        try:
            instance = resolve_instance(request.user, instance_name=instance_name)
        except Exception:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        query_engine = get_engine(instance=instance)
        query_result = query_engine.tablespace(offset, limit, schema_search=schema_search)

        if not query_result.rows and not query_result.error:
            return JsonResponse(
                {"status": 1, "msg": f"暂时不支持{instance.db_type}类型数据库的表空间信息查询", "data": []}
            )

        if query_result.error:
            return JsonResponse({"status": 1, "msg": query_result.error})

        table_space = query_result.to_dict()
        r = query_engine.tablespace_count(schema_search=schema_search)
        total = r.rows[0][0]
        return JsonResponse(
            _encode({"status": 0, "msg": "ok", "rows": table_space, "total": total}),
            safe=False,
        )


class TrxAndLocksView(APIView):
    permission_classes = [IsAuthenticated, TrxAndLocksViewPermission]

    def post(self, request):
        instance_name = request.data.get("instance_name")

        try:
            instance = resolve_instance(request.user, instance_name=instance_name)
        except Exception:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        query_engine = get_engine(instance=instance)

        if instance.db_type == "mysql":
            query_result = query_engine.trxandlocks()
        elif instance.db_type == "oracle":
            query_result = query_engine.lock_info()
        elif instance.db_type == "pgsql":
            query_result = query_engine.trxandlocks()
        else:
            return JsonResponse(
                {"status": 1, "msg": f"暂时不支持{instance.db_type}类型数据库的锁等待查询", "data": []}
            )

        if query_result.error:
            return JsonResponse({"status": 1, "msg": query_result.error})
        return JsonResponse(
            _encode({"status": 0, "msg": "ok", "rows": query_result.to_dict()}),
            safe=False,
        )


class InnodbTrxView(APIView):
    permission_classes = [IsAuthenticated, TrxViewPermission]

    def post(self, request):
        instance_name = request.data.get("instance_name")

        try:
            instance = resolve_instance(request.user, instance_name=instance_name)
        except Exception:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例", "data": []})

        query_engine = get_engine(instance=instance)
        try:
            query_result = query_engine.get_long_transaction()
        except AttributeError:
            return JsonResponse(
                {"status": 1, "msg": f"暂时不支持{instance.db_type}类型数据库的长事务查询", "data": []}
            )

        if query_result.error:
            return JsonResponse({"status": 1, "msg": query_result.error})
        return JsonResponse(
            _encode({"status": 0, "msg": "ok", "rows": query_result.to_dict()}),
            safe=False,
        )
