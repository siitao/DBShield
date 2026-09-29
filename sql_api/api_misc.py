"""
binlog / My2SQL / 查询 / 审计 / 回滚 / 导出 DRF APIView 集 · 收尾所有剩余旧端点。

覆盖：
  sql/binlog.py          → binlog_list, my2sql
  sql/query.py           → querylog_audit, generate_sql, check_openai
  sql/audit_log.py       → audit_log
  sql/sql_workflow.py    → list_audit, backup_sql, osc_control
  sql/query_privileges.py → applylist, userprivileges, applyforprivileges, modifyprivileges
  sql/instance.py        → schemasync
  sql/views.py           → rollback_download, sqlexport_pre_check
  sql/offlinedownload.py → offline_file_download
  common/config.py       → change_config (移至 api_config.ChangeConfigView)
  common/check.py        → go_inception (移至 api_config.CheckInceptionView)

路由：
  POST /api/v1/audit/log/                 — 通用审计日志
  POST /api/v1/audit/querylog/            — 查询日志审计
  POST /api/v1/binlog/list/               — binlog 列表
  POST /api/v1/binlog/my2sql/             — my2sql 解析（save_sql=true 走异步）
  GET  /api/v1/binlog/my2sql/task/        — 异步解析任务状态
  GET  /api/v1/binlog/my2sql/download/    — 下载异步解析结果
  POST /api/v1/query/generate_sql/        — AI 生成 SQL
  GET  /api/v1/query/check_openai/        — 探测 OpenAI
  POST /api/v1/query/applylist/           — 查询权限申请列表
  POST /api/v1/query/userprivileges/      — 用户已有权限
  POST /api/v1/query/applyforprivileges/  — 申请权限
  POST /api/v1/query/modifyprivileges/    — 变更/删除权限
  GET  /api/v1/sqlworkflow/backup_sql/    — 查看回滚 SQL
  POST /api/v1/sqlworkflow/list_audit/    — (旧) SQL 上线审计列表
  POST /api/v1/sqlworkflow/osc_control/   — OSC 进度控制
  POST /api/v1/schemasync/                — SchemaSync 对比
  GET  /api/v1/rollback/                  — 下载回滚 SQL 文件
  POST /api/v1/sqlexport/pre_check/       — 数据导出预检
  GET  /api/v1/downloadfile/              — 下载导出文件
"""
import datetime as _dt
import json as _json
import logging
import os
import re
import time
import traceback
from pathlib import Path

from django.conf import settings
from django.db.models import Q, Value as V, F
from django.db.models.functions import Concat
from django.http import JsonResponse, FileResponse, HttpResponse
from django.db import transaction as _tx
from django_q.tasks import async_task

from common.config import SysConfig
from common.utils.const import WorkflowAction, WorkflowStatus, WorkflowType
from common.utils.extend_json_encoder import encode_json as _encode
from common.utils.timer import FuncTimer
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.views import APIView
from sql.engines import get_engine
from sql.models import (
    ArchiveConfig, ArchiveLog,
    AuditEntry, Instance, QueryLog,
    QueryPrivileges, QueryPrivilegesApply,
    ResourceGroup, SqlWorkflow,
)
from sql.notify import notify_for_audit
from sql.plugins.my2sql import (
    SYNC_TIMEOUT as MY2SQL_SYNC_TIMEOUT,
    My2SQL,
    describe_failure as describe_my2sql_failure,
    is_failure as is_my2sql_failure,
)
from sql.plugins.soar import Soar
from sql.services.instance_service import resolve_instance
from sql.utils.resource_group import user_groups, user_instances
from sql.utils.sql_review import can_execute, can_view
from sql.utils.sql_utils import extract_tables, generate_sql
from sql.utils.tasks import task_info
from sql.utils.workflow_audit import Audit, AuditException, get_auditor

logger = logging.getLogger("default")


# ---------- permissions ----------


def _safe_int(value, default=0):
    """安全转整数，空/非数字入参返回默认值（避免非法入参触发 HTTP 500）"""
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


class AuditUserPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.audit_user"))


class QueryApplyListPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.menu_queryapplylist"))


class QueryApplyPrivPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.query_applypriv"))


class QueryMgtPrivPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.query_mgtpriv"))


class SqlWorkflowPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.menu_sqlworkflow"))


class SchemasyncPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.menu_schemasync"))


class My2sqlPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.menu_my2sql"))


# ========== 审计 ==========

class AuditLogView(APIView):
    permission_classes = [IsAuthenticated, AuditUserPermission]

    def post(self, request):
        limit = _safe_int(request.data.get("limit"), 0)
        offset = _safe_int(request.data.get("offset"), 0)
        limit = offset + limit
        limit = limit if limit else None
        search = request.data.get("search", "")
        action = request.data.get("action", "")
        start_date = request.data.get("start_date")
        end_date = request.data.get("end_date")

        filter_dict = {}
        if start_date and end_date:
            end_date = _dt.datetime.strptime(end_date, "%Y-%m-%d") + _dt.timedelta(days=1)
            filter_dict["action_time__range"] = (start_date, end_date)
        if action:
            filter_dict["action"] = action

        qs = AuditEntry.objects.filter(**filter_dict)
        if search:
            qs = qs.filter(
                Q(user_name__icontains=search)
                | Q(action__icontains=search)
                | Q(extra_info__icontains=search)
            )

        count = qs.count()
        rows = [row for row in qs.order_by("-action_time")[offset:limit].values(
            "user_id", "user_name", "user_display", "action", "extra_info", "action_time"
        )]
        return JsonResponse(_encode({"total": count, "rows": rows}), safe=False)


class AuditSqlWorkflowView(APIView):
    """SQL 上线工单审计列表（复用旧 sqlworkflow_list_audit 逻辑）。"""
    permission_classes = [IsAuthenticated, AuditUserPermission]

    def post(self, request):
        user = request.user
        nav_status = request.data.get("navStatus")
        instance_id = request.data.get("instance_id")
        group_id = request.data.get("group_id")
        start_date = request.data.get("start_date")
        end_date = request.data.get("end_date")
        limit = _safe_int(request.data.get("limit"), 0)
        offset = _safe_int(request.data.get("offset"), 0)
        limit = offset + limit
        limit = limit if limit else None
        search = request.data.get("search")
        syntax_type = request.data.getlist("syntax_type[]")

        filter_dict = {}
        if syntax_type:
            filter_dict["syntax_type__in"] = syntax_type
        if nav_status:
            filter_dict["status"] = nav_status
        if instance_id:
            filter_dict["instance_id"] = instance_id
        if group_id:
            filter_dict["group_id"] = group_id
        if start_date and end_date:
            end_date = _dt.datetime.strptime(end_date, "%Y-%m-%d") + _dt.timedelta(days=1)
            filter_dict["create_time__range"] = (start_date, end_date)

        if user.is_superuser or user.has_perm("sql.audit_user"):
            pass
        elif user.has_perm("sql.sql_review") or user.has_perm("sql.sql_execute_for_resource_group"):
            group_list = user_groups(user)
            group_ids = [g.group_id for g in group_list]
            filter_dict["group_id__in"] = group_ids
        else:
            filter_dict["engineer"] = user.username

        workflow = SqlWorkflow.objects.filter(**filter_dict)
        if search:
            workflow = workflow.filter(
                Q(engineer_display__icontains=search) | Q(workflow_name__icontains=search)
            )

        count = workflow.count()
        rows = [r for r in workflow.order_by("-create_time")[offset:limit].values(
            "id", "workflow_name", "engineer_display", "status", "is_backup",
            "create_time", "instance__instance_name", "db_name", "group_name",
            "syntax_type", "export_format",
        )]
        return JsonResponse(_encode({"total": count, "rows": rows}), safe=False)


class AuditQueryLogView(APIView):
    """查询日志审计列表。"""
    permission_classes = [IsAuthenticated, AuditUserPermission]

    def post(self, request):
        limit = _safe_int(request.data.get("limit"), 0)
        offset = _safe_int(request.data.get("offset"), 0)
        limit = offset + limit
        limit = limit if limit else None
        search = request.data.get("search", "")
        start_date = request.data.get("start_date")
        end_date = request.data.get("end_date")

        filter_dict = {}
        if not (request.user.is_superuser or request.user.has_perm("sql.audit_user")):
            filter_dict["username"] = request.user.username
        if start_date and end_date:
            end_date = _dt.datetime.strptime(end_date, "%Y-%m-%d") + _dt.timedelta(days=1)
            filter_dict["create_time__range"] = (start_date, end_date)

        qs = QueryLog.objects.filter(**filter_dict).filter(
            Q(sqllog__icontains=search) if search else Q()
        ).annotate(
            target_instance=F("instance_name"),
            search_db=F("db_name"),
            execute_time=F("cost_time"),
            sqllog=V("") if not search else F("sqllog"),
        ) if search else QueryLog.objects.filter(**filter_dict).annotate(
            target_instance=F("instance_name"),
            search_db=F("db_name"),
            execute_time=F("cost_time"),
        )
        count = qs.count()
        rows = [r for r in qs.order_by("-create_time")[offset:limit].values(
            "id", "username", "user_display", "target_instance", "search_db",
            "sqllog", "effect_row", "cost_time", "execute_time", "create_time",
        )]
        return JsonResponse(_encode({"total": count, "rows": rows}), safe=False)


# ========== binlog ==========

class BinlogListView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        instance_name = request.data.get("instance_name")
        try:
            instance = resolve_instance(request.user, instance_name=instance_name)
        except Exception:
            return JsonResponse({"status": 1, "msg": "实例不存在或你所在组未关联", "data": []})

        query_engine = get_engine(instance=instance)
        query_result = query_engine.query("information_schema", "show binary logs;")
        if not query_result.error:
            column_list = query_result.column_list
            rows = []
            for row in query_result.rows:
                row_info = {}
                for ri, ri_item in enumerate(row):
                    row_info[column_list[ri]] = ri_item
                rows.append(row_info)
            return JsonResponse({"status": 0, "msg": "ok", "data": rows})
        return JsonResponse({"status": 1, "msg": query_result.error})


# My2SQL 输出根目录（下载页 / 结果文件都落在这里）
MY2SQL_OUTPUT_ROOT = os.path.join(settings.BASE_DIR, "downloads", "my2sql")
# my2sql -sql 支持的取值
MY2SQL_SQL_TYPES = ("insert", "update", "delete")


def _my2sql_int(value, label):
    """空值 → None；非法值抛 ValueError（转成前端友好提示）。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("%s必须是整数" % label)


def _my2sql_datetime(value, label):
    """校验 my2sql 要求的时间格式 YYYY-MM-DD HH:MM:SS。"""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        _dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise ValueError("%s格式应为 YYYY-MM-DD HH:MM:SS" % label)
    return text


def _my2sql_names(values, label):
    """库/表过滤：列表或逗号分隔字符串 → 逗号分隔字符串（my2sql 的入参格式）。"""
    if not values:
        return None
    if isinstance(values, str):
        values = values.split(",")
    names = []
    for item in values:
        for name in str(item).split(","):
            name = name.strip()
            if not name:
                continue
            if any(c.isspace() for c in name):
                raise ValueError("%s不能包含空格" % label)
            if name not in names:
                names.append(name)
    return ",".join(names) or None


def _get_my2sql_task(user, task_id):
    """按 task_id 取 my2sql 异步任务并校验归属。

    :return: (task, None) 或 (None, 错误提示)
    """
    from django_q.models import Task

    if not task_id:
        return None, "缺少 task_id"
    try:
        task = Task.objects.get(id=task_id)
    except Exception:
        return None, "任务不存在或已被清理"
    # 只认本接口提交的任务，避免用别人的 task_id 读到其它队列任务的结果
    if not (task.name or "").startswith("my2sql-"):
        return None, "任务不存在或已被清理"
    submitter = (task.kwargs or {}).get("user")
    if not user.is_superuser and submitter != user.username:
        return None, "无权查看该任务"
    return task, None


class My2sqlView(APIView):
    """binlog 解析（my2sql）。

    - 同步（默认）：在本次请求里解析，返回前 num 条 SQL，适合几十秒内的小范围；
    - 异步（save_sql=true）：提交 django-q 任务后立即返回 task_id，解析完成由
      sql.notify.notify_for_my2sql 通知提交人，文件在 /binlog/my2sql/download/ 下载。

    百 MB 级 binlog 解析耗时以分钟计，同步请求必然超过前端超时（页面一直转圈），
    所以大范围务必走异步。
    """

    permission_classes = [My2sqlPermission]

    @staticmethod
    def _parse_options(request):
        """请求参数 → My2SQL.build_args 入参（校验失败抛 ValueError）。"""
        data = request.data
        start_file = (data.get("start_file") or "").strip() or None
        end_file = (data.get("end_file") or "").strip() or None
        start_pos = _my2sql_int(data.get("start_pos"), "起始解析位置")
        end_pos = _my2sql_int(data.get("end_pos"), "终止解析位置")
        start_time = _my2sql_datetime(data.get("start_time"), "起始解析时间")
        stop_time = _my2sql_datetime(data.get("stop_time"), "终止解析时间")
        threads = _my2sql_int(data.get("threads"), "解析线程数") or 4
        num = _my2sql_int(data.get("num"), "解析行数") or 30

        # 起始条件：给文件按 pos 解析，或只给时间由 my2sql 自行定位 binlog 文件
        if not start_file and not start_time:
            raise ValueError("请选择起始解析文件，或填写起始解析时间")
        if start_file and not end_file:
            end_file = start_file
        # my2sql 的 -stop-pos 缺省是 4，只给 -stop-file 时会被判成"起始位置不小于终止位置"
        # 直接退出，所以终止位置必须显式给出
        if end_file and end_pos is None:
            raise ValueError("请填写终止解析位置（与起始文件相同时可填该文件大小）")
        if start_file and end_file and start_pos and end_pos and (start_file, start_pos) >= (end_file, end_pos):
            raise ValueError("起始位置必须早于终止位置")
        if start_time and stop_time and start_time >= stop_time:
            raise ValueError("起始时间必须早于终止时间")
        if threads < 1 or threads > 64:
            raise ValueError("解析线程数需在 1~64 之间")
        if num < 1:
            raise ValueError("解析行数至少为 1")

        sql_types = []
        for item in data.getlist("sql_type[]"):
            for sql_type in str(item).split(","):
                sql_type = sql_type.strip().lower()
                if not sql_type:
                    continue
                if sql_type not in MY2SQL_SQL_TYPES:
                    raise ValueError("SQL 类型仅支持 insert、update、delete")
                if sql_type not in sql_types:
                    sql_types.append(sql_type)

        return {
            "work_type": "rollback" if data.get("rollback") == "true" else "2sql",
            "threads": threads,
            "num": num,
            "start_file": start_file,
            "start_pos": start_pos,
            "stop_file": end_file,
            "stop_pos": end_pos,
            "start_datetime": start_time,
            "stop_datetime": stop_time,
            "databases": _my2sql_names(data.getlist("only_schemas"), "库名"),
            "tables": _my2sql_names(data.getlist("only_tables[]"), "表名"),
            "sql_types": sql_types,
            "add_extra_info": data.get("extra_info") == "true",
            "ignore_primary_key": data.get("ignore_primary_key") == "true",
            "full_columns": data.get("full_columns") == "true",
            "no_db_prefix": data.get("no_db_prefix") == "true",
            "file_per_table": data.get("file_per_table") == "true",
            "save_sql": data.get("save_sql") == "true",
        }

    def post(self, request):
        instance_name = (request.data.get("instance_name") or "").strip()
        if not instance_name:
            return JsonResponse({"status": 1, "msg": "缺少实例名", "data": []})

        # 资源组校验（H3）：仅可解析自己所在资源组内的实例
        try:
            instance = resolve_instance(request.user, instance_name=instance_name)
        except Exception:
            return JsonResponse({"status": 1, "msg": "实例不存在或你所在组未关联", "data": []})

        try:
            options = self._parse_options(request)
        except ValueError as e:
            return JsonResponse({"status": 1, "msg": str(e), "data": []})

        username, password = instance.get_username_password()
        # 每次解析独立目录：目录名即提交时间，下载时据此定位结果
        output_dir = os.path.join(MY2SQL_OUTPUT_ROOT, str(time.time()))
        os.makedirs(output_dir, exist_ok=True)
        args = My2SQL.build_args(
            host=instance.host,
            port=instance.port,
            user=username,
            password=password,
            output_dir=output_dir,
            work_type=options["work_type"],
            threads=options["threads"],
            start_file=options["start_file"],
            start_pos=options["start_pos"],
            stop_file=options["stop_file"],
            stop_pos=options["stop_pos"],
            start_datetime=options["start_datetime"],
            stop_datetime=options["stop_datetime"],
            databases=options["databases"],
            tables=options["tables"],
            sql_types=options["sql_types"],
            add_extra_info=options["add_extra_info"],
            ignore_primary_key=options["ignore_primary_key"],
            full_columns=options["full_columns"],
            no_db_prefix=options["no_db_prefix"],
            file_per_table=options["file_per_table"],
        )

        # 异步解析：立即返回 task_id，完成后通知 + 可下载完整文件
        if options["save_sql"]:
            task_id = async_task(
                "sql_api.tasks.my2sql_execute",
                user=request.user.username,
                args=args,
                output_dir=output_dir,
                hook="sql.notify.notify_for_my2sql",
                timeout=-1,
                task_name="my2sql-%s" % os.path.basename(output_dir),
            )
            logger.info(
                "my2sql 异步解析已提交 task_id=%s 实例=%s 输出目录=%s",
                task_id,
                instance_name,
                output_dir,
            )
            return JsonResponse(
                {
                    "status": 0,
                    "msg": "已提交后台解析，完成后会通知你，可稍后在页面下载结果",
                    "data": [],
                    "task_id": task_id,
                }
            )

        my2sql = My2SQL()
        # 工具路径来自「系统配置 → 工具插件 → my2sql」，未配置时给出明确指引
        # （否则 subprocess 会抛 TypeError，用户只看到"解析失败"）
        if not my2sql.path:
            return JsonResponse(
                {
                    "status": 1,
                    "msg": "未配置 my2sql 工具路径，请在 系统配置 → 工具插件 中填写 my2sql 可执行文件路径",
                    "data": [],
                }
            )
        args_check = my2sql.check_args(args)
        if args_check["status"] == 1:
            return JsonResponse(
                {"status": 1, "msg": args_check["msg"], "data": []}
            )
        cmd_args = my2sql.generate_args2cmd(args)
        try:
            stdout, stderr, returncode, timed_out = my2sql.run(
                cmd_args, MY2SQL_SYNC_TIMEOUT
            )
        except Exception:
            logger.error("my2sql 执行失败: %s", traceback.format_exc())
            # 通用错误文案，避免泄漏引擎/连接细节（H3）
            return JsonResponse({"status": 1, "msg": "my2sql 解析失败，请检查实例连接与参数配置", "data": []})

        if timed_out:
            my2sql.remove_sql_files(output_dir)
            return JsonResponse(
                {
                    "status": 1,
                    "msg": "解析超时（超过 %d 秒），请缩小解析范围，或勾选「保存到文件（异步）」"
                    "交给后台解析。" % MY2SQL_SYNC_TIMEOUT,
                    "data": [],
                }
            )
        # my2sql 的日志在 stdout（含 [fatal]），只判 stderr 会把解析中断当成功，
        # 于是页面展示被截断的 SQL 且无人察觉
        if is_my2sql_failure(stdout, stderr, returncode):
            logger.error(
                "my2sql 解析失败 rc=%s\nstdout:\n%s\nstderr:\n%s",
                returncode,
                (stdout or "")[-4000:],
                (stderr or "")[-4000:],
            )
            my2sql.remove_sql_files(output_dir)
            return JsonResponse(
                {"status": 1, "msg": describe_my2sql_failure(stdout, stderr, returncode), "data": []}
            )

        rows = my2sql.collect_sql_rows(output_dir, limit=options["num"])
        return JsonResponse({"status": 0, "msg": "ok", "data": rows})


class My2sqlTaskView(APIView):
    """异步解析任务状态（前端轮询）。

    django-q 要等 worker 接手任务才写 Task 表，刚提交的几秒内查不到属正常，
    此时返回 state=queued / registered=false，前端继续轮询即可 —— 不能报
    "任务不存在"，否则用户刚点完提交就看到一句误导性的错误提示。
    """

    permission_classes = [My2sqlPermission]

    def get(self, request):
        from django_q.models import Task

        task_id = (request.GET.get("task_id") or "").strip()
        if not task_id:
            return JsonResponse({"status": 1, "msg": "缺少 task_id", "data": {}})
        try:
            task = Task.objects.filter(id=task_id).first()
        except Exception:
            task = None
        if task is None:
            # 还在队列里（或集群未消费）：只回状态，不含任何任务内容
            return JsonResponse(
                {
                    "status": 0,
                    "msg": "ok",
                    "data": {
                        "state": "queued",
                        "registered": False,
                        "sql_count": 0,
                        "has_file": False,
                        "error": "",
                    },
                }
            )
        # 只认本接口提交的任务，避免用别人的 task_id 读到其它队列任务的结果
        if not (task.name or "").startswith("my2sql-"):
            return JsonResponse({"status": 1, "msg": "任务不存在或已被清理", "data": {}})
        submitter = (task.kwargs or {}).get("user")
        if not request.user.is_superuser and submitter != request.user.username:
            return JsonResponse({"status": 1, "msg": "无权查看该任务", "data": {}})

        if not task.started:
            state = "queued"
        elif not task.stopped:
            state = "running"
        else:
            state = "success" if task.success else "failure"
        data = {
            "state": state,
            "registered": True,
            "sql_count": 0,
            "has_file": False,
            "error": "",
        }
        if task.stopped:
            result = task.result
            if task.success and isinstance(result, (list, tuple)) and len(result) > 1:
                data["sql_count"] = result[0]
                data["has_file"] = True
            elif not task.success:
                data["error"] = str(result).splitlines()[0][:300]
        return JsonResponse({"status": 0, "msg": "ok", "data": data})


class My2sqlDownloadView(APIView):
    """下载异步解析结果：单个 .sql 直接返回，多文件（-file-per-table）打包 zip。"""

    permission_classes = [My2sqlPermission]

    def get(self, request):
        import zipfile

        task, error = _get_my2sql_task(request.user, request.GET.get("task_id"))
        if error:
            return JsonResponse({"status": 1, "msg": error, "data": []})
        result = task.result
        output_dir = (
            result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else ""
        )
        # 目录白名单：task.result 落库可被改，下载前必须确认在 downloads/my2sql 下
        root = os.path.realpath(MY2SQL_OUTPUT_ROOT)
        real_dir = os.path.realpath(output_dir) if output_dir else ""
        if not real_dir.startswith(root + os.sep) or not os.path.isdir(real_dir):
            return JsonResponse({"status": 1, "msg": "没有可下载的解析结果", "data": []})

        sql_files = [
            os.path.join(real_dir, fn)
            for fn in sorted(os.listdir(real_dir))
            if fn.endswith(".sql") and not fn.startswith(".")
            and os.path.isfile(os.path.join(real_dir, fn))
        ]
        if not sql_files:
            return JsonResponse({"status": 1, "msg": "没有可下载的解析结果", "data": []})
        if len(sql_files) == 1:
            file_path = sql_files[0]
            return FileResponse(
                open(file_path, "rb"),
                as_attachment=True,
                filename=os.path.basename(file_path),
            )
        # 按表拆分的多文件：打包后下载（首次生成后复用）
        zip_path = os.path.join(real_dir, "my2sql-sql.zip")
        if not os.path.exists(zip_path):
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for file_path in sql_files:
                    zf.write(file_path, arcname=os.path.basename(file_path))
        return FileResponse(
            open(zip_path, "rb"),
            as_attachment=True,
            filename=os.path.basename(zip_path),
        )


# ========== 查询 / AI ==========


def _pg_rows_to_ddl(tb_name: str, rows: list) -> str:
    """pgsql information_schema 行 → DDL（实现下沉 sql/utils/sql_utils.py，此处兼容别名）。"""
    from sql.utils.sql_utils import pg_rows_to_ddl

    return pg_rows_to_ddl(tb_name, rows)


class GenerateSqlView(APIView):
    """AI 生成 SQL：调用 OpenAI，结合所选表的 DDL 作为上下文生成查询语句。

    前端传 query_desc / db_type / instance_name / db_name / tb_name / schema_name。
    若提供了 instance_name + db_name + tb_name，则先取表结构（show create table）作为
    table_schema 喂给 OpenAI，提高生成准确度。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        query_desc = (request.data.get("query_desc") or "").strip()
        db_type = (request.data.get("db_type") or "").strip()
        instance_name = (request.data.get("instance_name") or "").strip()
        db_name = (request.data.get("db_name") or "").strip()
        tb_name = (request.data.get("tb_name") or "").strip()
        schema_name = (request.data.get("schema_name") or "").strip()

        if not query_desc:
            return JsonResponse(
                {"status": 1, "msg": "请输入查询描述", "data": ""}
            )

        # 独立开关：AI 生成 SQL 可单独关闭（不影响其他 AI 能力），默认开启
        if not SysConfig().get("ai_nl2sql_enabled", True):
            return JsonResponse(
                {"status": 1, "msg": "AI 生成 SQL 功能已被管理员关闭", "data": ""}
            )

        # table_schema：尽量从库中取真实 DDL，取不到则退化为表名
        table_schema = ""
        sample_data = ""
        if instance_name and db_name and tb_name:
            # 标识符白名单：tb_name 会拼接进 describe_table / SELECT 样本查询，
            # 拒绝反引号/引号等逃逸字符（2026-08-20 审查）
            if not re.fullmatch(r"[\w$.]{1,128}", tb_name):
                return JsonResponse(
                    {"status": 1, "msg": "表名仅允许字母、数字、下划线、点号", "data": ""}
                )
            # pgsql 默认 schema 为 public（避免 WHERE schema = NULL 永远匹配不到）
            if db_type == "pgsql" and not schema_name:
                schema_name = "public"
            try:
                instance = resolve_instance(
                    request.user, instance_name=instance_name, db_type=db_type or None
                )
                engine = get_engine(instance=instance)
                rs = engine.describe_table(
                    db_name, tb_name, schema_name=schema_name or None
                )
                rows = getattr(rs, "rows", None) or []
                if rows:
                    if db_type == "pgsql":
                        # pgsql 返回 information_schema 元组，转成 LLM 能理解的 DDL 格式
                        table_schema = _pg_rows_to_ddl(tb_name, rows)
                    else:
                        # mysql 等 SHOW CREATE TABLE 直接返回 DDL
                        table_schema = "\n".join(
                            " | ".join(str(c) for c in row) for row in rows
                        )
            except Instance.DoesNotExist:
                return JsonResponse(
                    {"status": 1, "msg": "实例不存在或你所在组未关联", "data": ""}
                )
            except Exception as e:
                logger.warning("generate_sql 取表结构失败: %s", e)
                table_schema = tb_name

            # 取样本数据（LIMIT 5，不排序不聚合，大表也无压力）
            if table_schema:
                try:
                    sample_sql = f"SELECT * FROM \"{tb_name}\" LIMIT 5" if db_type == "pgsql" else f"SELECT * FROM `{tb_name}` LIMIT 5"
                    sample_rs = engine.query(
                        db_name=db_name, sql=sample_sql,
                        **({"schema_name": schema_name} if schema_name else {})
                    )
                    # 与正规查询链路同口径：开启数据脱敏时对样本结果先脱敏再喂给外部 AI
                    if sample_rs.rows and SysConfig().get("data_masking"):
                        try:
                            sample_rs = engine.query_masking(
                                db_name, sample_sql, sample_rs
                            )
                        except Exception as mask_e:
                            logger.warning("generate_sql 样本脱敏失败: %s", mask_e)
                            sample_rs.rows = []
                    sample_rows = getattr(sample_rs, "rows", None) or []
                    sample_cols = getattr(sample_rs, "column_list", None) or []
                    if sample_rows:
                        header = " | ".join(str(c) for c in sample_cols)
                        lines = [" | ".join(str(c) for c in row) for row in sample_rows]
                        sample_data = header + "\n" + "\n".join(lines)
                except Exception as e:
                    logger.info("generate_sql 取样本数据失败（不影响生成）: %s", e)
        elif tb_name:
            table_schema = tb_name

        # AI 生成 + 统一用量记账（成功/失败都记，tokens/延迟取 client 已捕获部分）
        from common.utils.ai_gateway import OpenaiClient, record_ai_usage

        usage_ctx = dict(
            capability="nl2sql",
            db_type=db_type,
            instance_name=instance_name,
            db_name=db_name,
            user_name=request.user.username,
        )
        client = None
        try:
            client = OpenaiClient(scenario="nl2sql")
            sql = client.generate_sql_by_openai(
                db_type=db_type, table_schema=table_schema, user_input=query_desc, sample_data=sample_data
            )
            record_ai_usage(client=client, **usage_ctx)
            return JsonResponse({"status": 0, "msg": "ok", "data": sql or ""})
        except ValueError as e:
            logger.warning("generate_sql 失败: %s", e)
            record_ai_usage(
                client=client, status="failed", error=str(e)[:500], **usage_ctx,
            )
            return JsonResponse({"status": 1, "msg": str(e), "data": ""})
        except Exception as e:
            logger.exception("generate_sql 异常")
            record_ai_usage(
                client=client, status="failed", error=str(e)[:500], **usage_ctx,
            )
            return JsonResponse({"status": 1, "msg": str(e), "data": ""})


class CheckOpenAIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from common.utils.ai_gateway import check_openai_config

        return JsonResponse(
            {
                "status": 0,
                "msg": "ok",
                "data": {
                    "openai": check_openai_config(),
                    # nl2sql 独立开关：关闭时前端隐藏「AI 生成 SQL」入口
                    "nl2sql_enabled": bool(SysConfig().get("ai_nl2sql_enabled", True)),
                },
            }
        )


# ========== 查询权限申请 ==========

def _db_priv(user, instance, db_name):
    return QueryPrivileges.objects.filter(
        user_name=user.username, instance=instance,
        db_name=db_name, priv_type=1, is_deleted=0,
        valid_date__gte=_dt.datetime.now(),
    ).exists()


def _tb_priv(user, instance, db_name, tb_name):
    return QueryPrivileges.objects.filter(
        user_name=user.username, instance=instance,
        db_name=db_name, table_name=tb_name, priv_type=2, is_deleted=0,
        valid_date__gte=_dt.datetime.now(),
    ).exists()


def _query_apply_audit_call_back(apply_id, workflow_status):
    # 授权落库逻辑单点收口于 services（兼容旧 import 路径）
    from sql.services.privilege_service import query_apply_audit_call_back

    return query_apply_audit_call_back(apply_id, workflow_status)


class QueryApplyListView(APIView):
    permission_classes = [IsAuthenticated, QueryApplyListPermission]

    def post(self, request):
        user = request.user
        limit = _safe_int(request.data.get("limit"), 0)
        offset = _safe_int(request.data.get("offset"), 0)
        limit = offset + limit
        search = request.data.get("search", "")

        qs = QueryPrivilegesApply.objects.all()
        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(user_display__icontains=search))
        if not user.is_superuser:
            if user.has_perm("sql.query_review"):
                group_ids = [g.group_id for g in user_groups(user)]
                qs = qs.filter(group_id__in=group_ids)
            else:
                qs = qs.filter(user_name=user.username)

        count = qs.count()
        rows = [r for r in qs.order_by("-apply_id")[offset:limit].values(
            "apply_id", "title", "instance__instance_name", "db_list",
            "priv_type", "table_list", "limit_num", "valid_date",
            "user_display", "status", "create_time", "group_name",
        )]
        return JsonResponse(_encode({"total": count, "rows": rows}), safe=False)


class UserPrivilegesView(APIView):
    permission_classes = [IsAuthenticated, QueryApplyListPermission]

    def post(self, request):
        user = request.user
        user_display = request.data.get("user_display", "all")
        limit = _safe_int(request.data.get("limit"), 0)
        offset = _safe_int(request.data.get("offset"), 0)
        limit = offset + limit
        search = request.data.get("search", "")

        qs = QueryPrivileges.objects.filter(is_deleted=0, valid_date__gte=_dt.datetime.now())
        if search:
            qs = qs.filter(
                Q(user_display__icontains=search)
                | Q(db_name__icontains=search)
                | Q(table_name__icontains=search)
            )
        if user_display != "all":
            qs = qs.filter(user_display=user_display)
        if not user.is_superuser:
            if user.has_perm("sql.query_mgtpriv"):
                group_ids = [g.group_id for g in user_groups(user)]
                qs = qs.filter(instance__queryprivilegesapply__group_id__in=group_ids)
            else:
                qs = qs.filter(user_name=user.username)

        count = qs.distinct().count()
        rows = [r for r in qs.distinct().order_by("-privilege_id")[offset:limit].values(
            "privilege_id", "user_display", "instance__instance_name",
            "db_name", "priv_type", "table_name", "limit_num", "valid_date",
        )]
        return JsonResponse(_encode({"total": count, "rows": rows}), safe=False)


class ApplyForPrivilegesView(APIView):
    permission_classes = [IsAuthenticated, QueryApplyPrivPermission]

    def post(self, request):
        user = request.user
        title = request.data.get("title")
        instance_name = request.data.get("instance_name")
        group_name = request.data.get("group_name")
        priv_type = request.data.get("priv_type")
        db_name = request.data.get("db_name")
        db_list = request.data.getlist("db_list[]")
        table_list = request.data.getlist("table_list[]")
        valid_date = request.data.get("valid_date")
        limit_num = request.data.get("limit_num")

        result = {"status": 0, "msg": "ok", "data": []}
        if int(priv_type) == 1:
            if not (title and instance_name and db_list and valid_date and limit_num):
                result["status"] = 1
                result["msg"] = "请填写完整"
                return JsonResponse(result)
        elif int(priv_type) == 2:
            if not (title and instance_name and db_name and valid_date and table_list and limit_num):
                result["status"] = 1
                result["msg"] = "请填写完整"
                return JsonResponse(result)

        try:
            user_instances(request.user, tag_codes=["can_read"]).get(instance_name=instance_name)
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！"})

        ins = Instance.objects.get(instance_name=instance_name)
        group_id = ResourceGroup.objects.get(group_name=group_name).group_id

        if int(priv_type) == 1:
            for db in db_list:
                if _db_priv(user, ins, db):
                    return JsonResponse({"status": 1, "msg": f"你已拥有{instance_name}实例{db}库权限，不能重复申请"})
        elif int(priv_type) == 2:
            if _db_priv(user, ins, db_name):
                return JsonResponse({"status": 1, "msg": f"你已拥有{instance_name}实例{db_name}库的全部权限，不能重复申请"})
            for tb in table_list:
                if _tb_priv(user, ins, db_name, tb):
                    return JsonResponse({"status": 1, "msg": f"你已拥有{instance_name}实例{db_name}.{tb}表的查询权限，不能重复申请"})

        apply_info = QueryPrivilegesApply(
            title=title, group_id=group_id, group_name=group_name,
            audit_auth_groups="", user_name=user.username, user_display=user.display,
            instance=ins, priv_type=_safe_int(priv_type, -1), valid_date=valid_date,
            status=WorkflowStatus.WAITING, limit_num=limit_num,
        )
        if int(priv_type) == 1:
            apply_info.db_list = ",".join(db_list)
            apply_info.table_list = ""
        elif int(priv_type) == 2:
            apply_info.db_list = db_name
            apply_info.table_list = ",".join(table_list)

        audit_handler = get_auditor(workflow=apply_info)
        try:
            with _tx.atomic():
                audit_handler.create_audit()
        except AuditException as e:
            logger.error(f"新建审批流失败, {str(e)}")
            return JsonResponse({"status": 1, "msg": "新建审批流失败, 请联系管理员"})

        _query_apply_audit_call_back(audit_handler.workflow.apply_id, audit_handler.audit.current_status)
        async_task(
            notify_for_audit, workflow_audit=audit_handler.audit, timeout=60,
            task_name=f"query-priv-apply-{audit_handler.workflow.apply_id}",
        )
        return JsonResponse(result)


class ModifyPrivilegesView(APIView):
    permission_classes = [IsAuthenticated, QueryMgtPrivPermission]

    def post(self, request):
        privilege_id = request.data.get("privilege_id")
        type_val = request.data.get("type")
        result = {"status": 0, "msg": "ok", "data": []}

        try:
            priv = QueryPrivileges.objects.get(privilege_id=int(privilege_id))
        except QueryPrivileges.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "待操作权限不存在"})

        if int(type_val) == 1:
            priv.is_deleted = 1
            priv.save(update_fields=["is_deleted"])
        elif int(type_val) == 2:
            valid_date = request.data.get("valid_date")
            limit_num = request.data.get("limit_num")
            priv.valid_date = valid_date
            priv.limit_num = limit_num
            priv.save(update_fields=["valid_date", "limit_num"])
        return JsonResponse(result)


# ========== SQL 上线辅助 ==========

class BackupSqlView(APIView):
    permission_classes = [IsAuthenticated, SqlWorkflowPermission]

    def get(self, request):
        workflow_id = request.GET.get("workflow_id")
        try:
            workflow = SqlWorkflow.objects.get(id=workflow_id)
        except SqlWorkflow.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "工单不存在"})

        # 回滚 SQL 含变更前数据镜像，与工单详情同口径做归属校验
        try:
            if not can_view(request.user, workflow_id):
                return JsonResponse({"status": 1, "msg": "你无权查看该工单的备份信息"})
        except SqlWorkflow.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "工单不存在"})

        query_engine = get_engine(instance=workflow.instance)
        sql_list = query_engine.get_rollback(workflow=workflow)
        rows = []
        if isinstance(sql_list, list):
            rows = []
            for item in sql_list:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    rows.append({"sql": str(item[1]), "label": str(item[0])})
                elif isinstance(item, dict):
                    rows.append(item)
                else:
                    rows.append({"sql": str(item)})
        return JsonResponse({"status": 0, "msg": "ok", "rows": rows})


class OscControlView(APIView):
    permission_classes = [IsAuthenticated, SqlWorkflowPermission]

    def post(self, request):
        workflow_id = request.data.get("workflow_id")
        sqlsha1 = request.data.get("sqlsha1")
        command = request.data.get("command")

        try:
            workflow = SqlWorkflow.objects.get(id=workflow_id)
        except SqlWorkflow.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "工单不存在"})

        # OSC pause/resume/kill 属执行控制，与执行工单同口径校验
        try:
            if not can_execute(request.user, workflow_id):
                return JsonResponse({"status": 1, "msg": "你无权控制该工单的 OSC 进度"})
        except SqlWorkflow.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "工单不存在"})

        engine = get_engine(instance=workflow.instance)
        if command == "get":
            osc_result = engine.get_osc_progress(sqlsha1=sqlsha1)
        elif command == "pause":
            osc_result = engine.pause_osc(sqlsha1=sqlsha1)
        elif command == "resume":
            osc_result = engine.resume_osc(sqlsha1=sqlsha1)
        elif command == "kill":
            osc_result = engine.kill_osc(sqlsha1=sqlsha1)
        else:
            return JsonResponse({"status": 1, "msg": f"未知 command: {command}"})

        if osc_result.error:
            return JsonResponse({"status": 1, "msg": osc_result.error})
        return JsonResponse({"status": 0, "msg": "", "rows": osc_result.to_dict(), "total": len(osc_result.to_dict())})


# ========== SchemaSync ==========

class SchemaSyncView(APIView):
    permission_classes = [IsAuthenticated, SchemasyncPermission]

    def post(self, request):
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")
        target_instance_name = request.data.get("target_instance_name")
        target_db_name = request.data.get("target_db_name")
        sync_auto_inc = request.data.get("sync_auto_inc") == "true"
        sync_comments = request.data.get("sync_comments") == "true"

        result = {"status": 0, "msg": "ok", "data": {"diff_stdout": "", "patch_stdout": "", "revert_stdout": ""}}

        if db_name == "all" or target_db_name == "all":
            db_name = "*"
            target_db_name = "*"

        # 资源组校验：源/目标实例都须在用户所在资源组内。
        # 此前直接 Instance.objects.get，持菜单权限者可对任意实例做结构对比
        # 并通过 DSN 读取其账号密码
        try:
            instance = resolve_instance(request.user, instance_name=instance_name)
            target_instance = resolve_instance(
                request.user, instance_name=target_instance_name
            )
        except Exception:
            return JsonResponse({"status": 1, "msg": "实例不存在或你所在组未关联", "data": []})

        from sql.plugins.schemasync import SchemaSync
        schema_sync = SchemaSync()
        tag = int(time.time())
        output_directory = os.path.join(settings.BASE_DIR, "downloads/schemasync/")
        os.makedirs(output_directory, exist_ok=True)

        username, password = instance.get_username_password()
        target_username, target_password = target_instance.get_username_password()

        args = {
            "sync-auto-inc": sync_auto_inc,
            "sync-comments": sync_comments,
            "charset": "utf8mb4",
            "tag": tag,
            "output-directory": output_directory,
            "source": f"mysql://{username}:{password}@{instance.host}:{instance.port}/{db_name}",
            "target": f"mysql://{target_username}:{target_password}@{target_instance.host}:{target_instance.port}/{target_db_name}",
        }

        args_check = schema_sync.check_args(args)
        if args_check["status"] == 1:
            return JsonResponse(args_check)

        cmd_args = schema_sync.generate_args2cmd(args)
        try:
            stdout, stderr = schema_sync.execute_cmd(cmd_args).communicate()
            diff_stdout = f"{stdout}{stderr}"
        except RuntimeError as e:
            logger.error(f"schemasync 执行命令失败: {e}")
            diff_stdout = "执行对比命令失败，请联系管理员"

        result["data"]["diff_stdout"] = diff_stdout

        # schemasync 的 stdout 只有摘要日志，patch/revert 脚本写在 output-directory，
        # 文件名格式为 <目标库>_<tag>.<YYYYMMDD>.(patch|revert).sql（utils.create_pnames）。
        # 目标库为 * 等字符时文件名本身含通配符，不能用 glob，按「_<tag>.」定位本次运行的文件
        patch_parts, revert_parts = [], []
        try:
            entries = sorted(os.listdir(output_directory))
        except OSError:
            entries = []
        for fn in entries:
            if f"_{tag}." not in fn:
                continue
            if fn.endswith(".patch.sql"):
                parts = patch_parts
            elif fn.endswith(".revert.sql"):
                parts = revert_parts
            else:
                continue
            try:
                with open(
                    os.path.join(output_directory, fn),
                    encoding="utf-8",
                    errors="replace",
                ) as fh:
                    content = fh.read()
            except OSError:
                logger.warning("schemasync 脚本文件读取失败: %s", fn)
                continue
            parts.append(f"-- {fn}\n{content}")

        result["data"]["patch_stdout"] = "\n".join(patch_parts)
        result["data"]["revert_stdout"] = "\n".join(revert_parts)
        return JsonResponse(result)


# ========== 回滚 / 导出（文件流 + 预检） ==========
# 替代 sql/views.py:rollback_download、sqlexport_pre_check、sql/offlinedownload.py:offline_file_download

class RollbackDownloadView(APIView):
    """下载工单回滚 SQL 文件（GET /api/v1/rollback/）。

    与旧 /rollback/ 行为一致：can_rollback 校验 → 生成 .sql → FileResponse。
    错误分支返回 JSON（旧实现 render error.html，SPA 友好化）。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from sql.utils.sql_review import can_rollback
        from sql.models import SqlWorkflow
        from django.shortcuts import get_object_or_404
        from django.core.exceptions import PermissionDenied

        workflow_id = request.GET.get("workflow_id")
        if not can_rollback(request.user, workflow_id):
            raise PermissionDenied
        if not workflow_id:
            return JsonResponse({"status": 1, "msg": "workflow_id参数为空."}, status=400)

        workflow = get_object_or_404(SqlWorkflow, id=int(workflow_id))
        try:
            query_engine = get_engine(instance=workflow.instance)
            list_backup_sql = query_engine.get_rollback(workflow=workflow)
        except Exception as msg:
            logger.error(traceback.format_exc())
            return JsonResponse({"status": 1, "msg": str(msg)}, status=500)

        path = os.path.join(settings.BASE_DIR, "downloads/rollback")
        os.makedirs(path, exist_ok=True)
        file_name = f"{path}/rollback_{workflow_id}.sql"
        with open(file_name, "w") as f:
            for sql in list_backup_sql:
                f.write(f"/*{sql[0]}*/\n{sql[1]}\n")

        response = FileResponse(open(file_name, "rb"))
        response["Content-Type"] = "application/octet-stream"
        response["Content-Disposition"] = (
            f'attachment;filename="rollback_{workflow_id}.sql"'
        )
        return response


class SqlexportPreCheckView(APIView):
    """数据导出预检（POST /api/v1/sqlexport/pre_check/）。

    替代 sql/views.py:sqlexport_pre_check，权限校验从装饰器
    @permission_required('sql.sqlexport_submit') 改为视图内 has_perm 检查。
    返回旧 {status,msg,data} 信封不变。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not (request.user.is_superuser or request.user.has_perm("sql.sqlexport_submit")):
            return JsonResponse(
                {"status": 1, "msg": "无数据导出权限"}, status=403
            )

        from sql.offlinedownload import OffLineDownLoad

        result = {"status": 0, "msg": "ok", "data": {}}
        instance_name = request.data.get("instance_name")
        db_name = request.data.get("db_name")
        sql_content = request.data.get("sql_content")

        if not instance_name or not db_name or not sql_content:
            result["status"] = 1
            result["msg"] = "页面提交参数可能为空"
            return JsonResponse(result)

        try:
            instance = user_instances(request.user, tag_codes=["can_read"]).get(
                instance_name=instance_name
            )
        except Instance.DoesNotExist:
            result["status"] = 1
            result["msg"] = "你所在组未关联该实例"
            return JsonResponse(result)

        instance.sql_content = sql_content
        instance.selected_db_name = db_name
        check_result = OffLineDownLoad().pre_count_check(workflow=instance)
        result["data"] = {
            "error_count": check_result.error_count,
            "warning_count": check_result.warning_count,
            "rows": check_result.to_dict(),
        }
        if check_result.error_count:
            result["status"] = 1
            result["msg"] = (
                check_result.rows[0].errormessage if check_result.rows else ""
            )
        return JsonResponse(result)


class DownloadFileView(APIView):
    """下载导出文件（GET /api/v1/downloadfile/）。

    替代 sql/offlinedownload.py:offline_file_download。
    local/sftp → 文件流；s3c/azure → JSON {type:'redirect',url}。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from sql.storage import DynamicStorage
        from sql.models import AuditEntry, SqlWorkflow

        try:
            from sql.offlinedownload import StorageFileResponse
        except ImportError:
            from django.http import FileResponse as StorageFileResponse

        workflow_id = request.GET.get("workflow_id", "")
        file_name = request.GET.get("file_name", " ")

        # H4：归属校验——必须关联工单且通过访问控制，防止枚举下载他人文件
        if not str(workflow_id).isdigit():
            return JsonResponse({"error": "缺少工单ID"}, status=400)
        workflow = SqlWorkflow.objects.filter(id=int(workflow_id)).first()
        if workflow is None:
            return JsonResponse({"error": "工单不存在"}, status=404)
        user = request.user
        # H4b 收口：放行条件为 超管 / 离线下载管理权限 / 工单提交人本人。
        # 不再放行 sqlexport_submit（提交导出的业务权限，与下载他人文件无关，
        # 曾导致持该权限的普通用户可下载任意用户的导出文件）
        if not (
            user.is_superuser
            or user.has_perm("sql.offline_download")
            or workflow.engineer == user.username
        ):
            return JsonResponse({"error": "无权下载该文件"}, status=403)
        # 文件名以工单记录为准，防直接传参下载任意文件；
        # 工单未记录文件名（导出未完成/历史数据）时一律拒绝，
        # 避免用户可控文件名直通 sftp/s3 等无 safe_join 兜底的后端
        if not workflow.file_name:
            return JsonResponse({"error": "该工单无可下载文件"}, status=404)
        if file_name != workflow.file_name:
            return JsonResponse({"error": "文件与工单不匹配"}, status=403)

        action = "离线下载"
        extra_info = f"工单id：{workflow_id}，文件：{file_name}"
        config = SysConfig()
        storage_type = config.get("storage_type", "local")
        storage = DynamicStorage()

        try:
            if not storage.exists(file_name):
                extra_info += "，error:文件不存在。"
                return JsonResponse({"error": "文件不存在"}, status=404)

            if storage_type in ["sftp", "local"]:
                try:
                    file = storage.open(file_name, "rb")
                    file_size = storage.size(file_name)
                    response = StorageFileResponse(file, storage=storage)
                    response["Content-Disposition"] = (
                        f'attachment; filename="{file_name}"'
                    )
                    response["Content-Length"] = str(file_size)
                    response["Content-Encoding"] = "identity"
                    return response
                except Exception as e:
                    extra_info += f"，error:{e}"
                    logger.error(extra_info)
                    return JsonResponse(
                        {"error": "文件下载失败：请联系管理员。"}, status=500
                    )

            if storage_type in ["s3c", "azure"]:
                try:
                    presigned_url = storage.url(file_name)
                    return JsonResponse({"type": "redirect", "url": presigned_url})
                except Exception as e:
                    extra_info += f"，error:{e}"
                    logger.error(extra_info)
                    return JsonResponse(
                        {"error": "文件下载失败：请联系管理员。"}, status=500
                    )

            return JsonResponse(
                {"error": f"不支持的存储类型：{storage_type}"}, status=500
            )

        except Exception as e:
            extra_info += f"，error:{e}"
            logger.error(extra_info)
            return JsonResponse({"error": "内部错误，请联系管理员。"}, status=500)

        finally:
            if request.method != "HEAD":
                AuditEntry.objects.create(
                    user_id=request.user.id,
                    user_name=request.user.username,
                    user_display=request.user.display,
                    action=action,
                    extra_info=extra_info,
                )
