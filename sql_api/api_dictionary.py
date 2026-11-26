"""
数据字典 DRF ViewSet · 替代 sql/data_dictionary.py 全部 14 个旧接口。

Router 注册后生成（basename="dictionary"）：
  GET /api/v1/dictionary/tables/          — 表列表（按首字母分组）
  GET /api/v1/dictionary/tables/info/     — 表详情（meta/desc/index/create_sql）
  GET /api/v1/dictionary/views/           — 视图列表
  GET /api/v1/dictionary/views/info/      — 视图详情
  GET /api/v1/dictionary/triggers/        — 触发器列表
  GET /api/v1/dictionary/triggers/info/   — 触发器详情
  GET /api/v1/dictionary/procedures/      — 存储过程列表
  GET /api/v1/dictionary/procedures/info/ — 存储过程详情
  GET /api/v1/dictionary/functions/       — 函数列表
  GET /api/v1/dictionary/functions/info/  — 函数详情
  GET /api/v1/dictionary/events/          — 定时任务列表
  GET /api/v1/dictionary/events/info/     — 定时任务详情
  GET /api/v1/dictionary/export/          — 导出 HTML/文件下载
"""
import datetime
import os
from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse, JsonResponse
from django.template import loader
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated

from sql.models import Instance
from sql.services.instance_service import resolve_instance_and_engine
from sql.utils.sql_utils import quote_ident


class DataDictionaryPermission(BasePermission):
    """菜单权限：sql.menu_data_dictionary（超管放行）。"""

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return user.is_superuser or user.has_perm("sql.menu_data_dictionary")


# ---------- shared helpers ----------

def _get_engine(request):
    """从 GET 参数获取 instance + engine。"""
    instance_name = request.GET.get("instance_name", "")
    db_type = request.GET.get("db_type", "")
    return resolve_instance_and_engine(
        request.user, instance_name=instance_name, db_type=db_type
    )


def _dict_list_response(request, engine_method):
    """通用对象列表：获取 instance → engine.xxx(db_name) → 返回 JSON。"""
    instance_name = request.GET.get("instance_name", "")
    db_name = request.GET.get("db_name", "")

    if not instance_name or not db_name:
        return JsonResponse({"status": 1, "msg": "缺少 instance_name 或 db_name"})

    try:
        _instance, engine = _get_engine(request)
        # escape db_name for non-table methods (table_list uses get_group_tables_by_db
        # which is the only one where db_name doesn't need escaping)
        escaped_db = engine.escape_string(db_name) if "table" not in engine_method else db_name
        data = getattr(engine, engine_method)(db_name=escaped_db)
        return JsonResponse({"status": 0, "data": data})
    except Instance.DoesNotExist:
        return JsonResponse({"status": 1, "msg": "实例不存在或你所在组未关联"})
    except Exception as e:
        return JsonResponse({"status": 1, "msg": str(e)})


def _dict_detail_response(request, engine_method, name_param, engine_kwarg):
    """通用对象详情：获取 instance → engine.xxx(db_name, kwarg=name) → 返回 JSON。"""
    instance_name = request.GET.get("instance_name", "")
    db_name = request.GET.get("db_name", "")
    obj_name = request.GET.get(name_param, "")

    if not instance_name or not db_name or not obj_name:
        return JsonResponse(
            {"status": 1, "msg": f"缺少 instance_name、db_name 或 {name_param}"}
        )

    try:
        _instance, engine = _get_engine(request)
        db_name_safe = engine.escape_string(db_name)
        obj_name_safe = engine.escape_string(obj_name)
        data = getattr(engine, engine_method)(
            **{"db_name": db_name_safe, engine_kwarg: obj_name_safe}
        )
        return JsonResponse({"status": 0, "data": data})
    except Instance.DoesNotExist:
        return JsonResponse({"status": 1, "msg": "实例不存在或你所在组未关联"})
    except Exception as e:
        return JsonResponse({"status": 1, "msg": str(e)})


# ---------- ViewSet ----------

class DataDictionaryViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, DataDictionaryPermission]

    # ---- 表 ----

    @action(detail=False, methods=["get"], url_path="tables")
    def table_list(self, request):
        return _dict_list_response(request, "get_group_tables_by_db")

    @action(detail=False, methods=["get"], url_path="tables/info")
    def table_info(self, request):
        instance_name = request.GET.get("instance_name", "")
        db_name = request.GET.get("db_name", "")
        tb_name = request.GET.get("tb_name", "")

        if not instance_name or not db_name or not tb_name:
            return JsonResponse(
                {"status": 1, "msg": "缺少 instance_name、db_name 或 tb_name"}
            )

        try:
            instance, engine = _get_engine(request)
            db_name = engine.escape_string(db_name)
            tb_name = engine.escape_string(tb_name)
            data = {
                "meta_data": engine.get_table_meta_data(db_name=db_name, tb_name=tb_name),
                "desc": engine.get_table_desc_data(db_name=db_name, tb_name=tb_name),
                "index": engine.get_table_index_data(db_name=db_name, tb_name=tb_name),
            }
            if instance.db_type in ("mysql", "clickhouse"):
                _sql = engine.query(
                    db_name, f"show create table {quote_ident(tb_name)};"
                )
                data["create_sql"] = _sql.rows
            return JsonResponse({"status": 0, "data": data})
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "实例不存在或你所在组未关联"})
        except Exception as e:
            return JsonResponse({"status": 1, "msg": str(e)})

    # ---- 视图 ----

    @action(detail=False, methods=["get"], url_path="views")
    def view_list(self, request):
        return _dict_list_response(request, "get_views_list")

    @action(detail=False, methods=["get"], url_path="views/info")
    def view_info(self, request):
        return _dict_detail_response(request, "get_view_detail", "view_name", "view_name")

    # ---- 触发器 ----

    @action(detail=False, methods=["get"], url_path="triggers")
    def trigger_list(self, request):
        return _dict_list_response(request, "get_triggers_list")

    @action(detail=False, methods=["get"], url_path="triggers/info")
    def trigger_info(self, request):
        return _dict_detail_response(request, "get_trigger_detail", "trigger_name", "trigger_name")

    # ---- 存储过程 ----

    @action(detail=False, methods=["get"], url_path="procedures")
    def procedure_list(self, request):
        return _dict_list_response(request, "get_procedures_list")

    @action(detail=False, methods=["get"], url_path="procedures/info")
    def procedure_info(self, request):
        return _dict_detail_response(request, "get_procedure_detail", "proc_name", "proc_name")

    # ---- 函数 ----

    @action(detail=False, methods=["get"], url_path="functions")
    def function_list(self, request):
        return _dict_list_response(request, "get_functions_list")

    @action(detail=False, methods=["get"], url_path="functions/info")
    def function_info(self, request):
        return _dict_detail_response(request, "get_function_detail", "func_name", "func_name")

    # ---- 定时任务 ----

    @action(detail=False, methods=["get"], url_path="events")
    def event_list(self, request):
        return _dict_list_response(request, "get_events_list")

    @action(detail=False, methods=["get"], url_path="events/info")
    def event_info(self, request):
        return _dict_detail_response(request, "get_event_detail", "event_name", "event_name")

    # ---- 导出 ----

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        """导出数据字典 HTML。单库返回文件下载，全实例返回提示。"""
        instance_name = request.GET.get("instance_name", "")
        db_name = request.GET.get("db_name", "")

        try:
            instance, engine = _get_engine(request)
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "实例不存在或你所在组未关联"})

        if db_name:
            dbs = [engine.escape_string(db_name)]
        elif request.user.is_superuser:
            dbs = engine.get_all_databases().rows
        else:
            return JsonResponse({"status": 1, "msg": "仅管理员可以导出整个实例的字典信息！"})

        base = os.path.join(settings.BASE_DIR, "downloads", "dictionary")
        os.makedirs(base, exist_ok=True)

        for db in dbs:
            table_metas = engine.get_tables_metas_data(db_name=db)
            ctx = {
                "db_name": db_name or db,
                "tables": table_metas,
                "export_time": datetime.datetime.now(),
            }
            html = loader.render_to_string(template_name="dictionaryexport.html", context=ctx)
            dest = os.path.normpath(os.path.join(base, f"{instance_name}_{db}.html"))
            if not dest.startswith(base):
                return JsonResponse({"status": 1, "msg": "实例名或db名不合法"})
            with open(dest, "w", encoding="utf-8") as fp:
                fp.write(html)

        engine.close()

        if db_name:
            dest = os.path.normpath(os.path.join(base, f"{instance_name}_{db_name}.html"))
            if not dest.startswith(base):
                return JsonResponse({"status": 1, "msg": "实例名或db名不合法"})
            response = FileResponse(open(dest, "rb"))
            response["Content-Type"] = "application/octet-stream"
            response["Content-Disposition"] = (
                f'attachment;filename="{quote(instance_name)}_{quote(db_name)}.html"'
            )
            return response

        return JsonResponse(
            {"status": 0, "msg": f"实例{instance_name}数据字典导出成功，请到downloads目录下载！"}
        )
