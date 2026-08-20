"""
数据归档 DRF APIView 集 · 替代 sql/archiver.py 全部旧端点 + 原有 ArchiveDetail/ArchiveAudit。
"""
import logging

from django.db import transaction as _tx
from django.db.models import Q, Value as V, TextField
from django.db.models.functions import Concat
from django.http import JsonResponse
from django_q.tasks import async_task
from rest_framework import views, permissions
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.utils.const import WorkflowAction, WorkflowStatus, WorkflowType
from common.utils.extend_json_encoder import encode_json as _encode
from sql.models import ArchiveConfig, ArchiveLog, Instance, ResourceGroup
from sql.notify import notify_for_audit
from sql.utils.resource_group import user_groups, user_instances
from sql.utils.workflow_audit import (
    Audit, AuditException, AuditV2, get_auditor,
)

import json as _json

logger = logging.getLogger("default")


# ---------- helpers ----------

def _safe_int(value, default):
    """安全转整数，空/非数字入参返回默认值（避免非法入参触发 HTTP 500）"""
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default

def _serialize_review_info(review_info):
    """ReviewInfo.nodes → 可 JSON 序列化的列表。"""
    nodes = []
    for n in review_info.nodes:
        nodes.append({
            "group_name": n.group.name if n.group else "自动通过",
            "is_current_node": n.is_current_node,
            "is_passed_node": n.is_passed_node,
            "is_auto_pass": n.is_auto_pass,
        })
    return nodes


def _archive_config_dict(cfg: ArchiveConfig) -> dict:
    return {
        "id": cfg.id, "title": cfg.title,
        "group_name": cfg.resource_group.group_name,
        "src_instance_name": cfg.src_instance.instance_name if cfg.src_instance else "",
        "src_db_name": cfg.src_db_name, "src_table_name": cfg.src_table_name,
        "dest_instance_name": cfg.dest_instance.instance_name if cfg.dest_instance else "",
        "dest_db_name": cfg.dest_db_name or "", "dest_table_name": cfg.dest_table_name or "",
        "mode": cfg.mode, "no_delete": cfg.no_delete, "sleep": cfg.sleep,
        "condition": cfg.condition, "status": cfg.status, "state": cfg.state,
        "user_display": cfg.user_display, "create_time": cfg.create_time,
    }


# ---------- 原有 DRF 接口（保留）----------

class ArchiveDetail(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        # IDOR 收口：归档详情含源/目标实例、条件与审批人，仅超管/组内用户可见
        archive_config = ArchiveConfig.objects.filter(pk=pk).first()
        if archive_config is None:
            return Response({"errors": "归档配置不存在"}, status=404)
        if not request.user.is_superuser:
            user_group_ids = [g.group_id for g in user_groups(request.user)]
            if archive_config.resource_group_id not in user_group_ids:
                return Response({"errors": "无权查看该归档配置"}, status=403)
        audit_handler = AuditV2(workflow=archive_config, resource_group=archive_config.resource_group)
        review_info = audit_handler.get_review_info()
        try:
            audit_handler.can_operate(WorkflowAction.PASS, request.user)
            can_review = True
        except AuditException:
            can_review = False
        logs = []
        last_operation_info = ""
        try:
            audit = Audit.detail_by_workflow_id(workflow_id=pk, workflow_type=WorkflowType.ARCHIVE)
            log_qs = Audit.logs(audit_id=audit.audit_id).values(
                "operation_type_desc", "operation_info", "operator_display", "operation_time",
            )
            logs = [row for row in log_qs]
            if logs:
                last_operation_info = logs[-1]["operation_info"] or ""
        except Exception as e:
            logger.debug(f"归档配置 {pk} 无审核日志: {e}")
        current_reviewers = []
        for node in review_info.nodes:
            if not node.is_current_node:
                continue
            for user in node.group.user_set.filter(is_active=1):
                group_names = [g.group_name for g in user_groups(user)]
                if archive_config.resource_group.group_name in group_names:
                    current_reviewers.append({
                        "username": user.username, "display": user.display or user.username,
                    })
        return Response({
            "archive": _archive_config_dict(archive_config),
            "review_info": _serialize_review_info(review_info),
            "current_reviewers": current_reviewers,
            "can_review": can_review,
            "last_operation_info": last_operation_info,
            "logs": logs,
        })


class ArchiveAudit(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        result = {"status": 0, "msg": "ok"}
        try:
            archive_id = int(request.data["archive_id"])
            audit_status = WorkflowAction(int(request.data["audit_status"]))
        except (KeyError, ValueError, TypeError) as e:
            result["status"] = 1
            result["msg"] = f"参数错误: {e}"
            return Response(result)
        audit_remark = request.data.get("audit_remark") or ""
        try:
            archive_workflow = ArchiveConfig.objects.get(id=archive_id)
        except ArchiveConfig.DoesNotExist:
            result["status"] = 1
            result["msg"] = "工单不存在"
            return Response(result)
        resource_group = archive_workflow.resource_group
        auditor = get_auditor(workflow=archive_workflow, resource_group=resource_group)
        try:
            with _tx.atomic():
                wad = auditor.operate(audit_status, request.user, audit_remark)
                auditor.workflow.status = auditor.audit.current_status
                if auditor.audit.current_status == WorkflowStatus.PASSED:
                    auditor.workflow.state = True
                auditor.workflow.save()
        except AuditException as e:
            result["status"] = 1
            result["msg"] = f"审核失败: {e}"
            return Response(result)
        async_task(
            notify_for_audit, workflow_audit=auditor.audit,
            workflow_audit_detail=wad, timeout=60,
            task_name=f"archive-audit-{archive_id}",
        )
        return Response(result)


# ---------- 新 DRF 接口（替代旧端点）----------

class ArchivePermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.menu_archive"))


class ArchiveApplyPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.archive_apply"))


class ArchiveMgtPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and (u.is_superuser or u.has_perm("sql.archive_mgt"))


# ========== views ==========

class ArchiveListView(APIView):
    permission_classes = [IsAuthenticated, ArchivePermission]

    def get(self, request):
        user = request.user
        filter_instance_id = request.GET.get("filter_instance_id")
        state = request.GET.get("state")
        limit = _safe_int(request.GET.get("limit"), 0)
        offset = _safe_int(request.GET.get("offset"), 0)
        limit = offset + limit
        search = request.GET.get("search", "")

        filter_dict = {}
        if filter_instance_id:
            filter_dict["src_instance"] = filter_instance_id
        if state == "true":
            filter_dict["state"] = True
        elif state == "false":
            filter_dict["state"] = False

        if user.is_superuser:
            pass
        elif user.has_perm("sql.archive_review"):
            group_ids = [g.group_id for g in user_groups(user)]
            filter_dict["resource_group__in"] = group_ids
        else:
            filter_dict["user_name"] = user.username

        archive_config = ArchiveConfig.objects.filter(**filter_dict)
        if search:
            archive_config = archive_config.filter(
                Q(title__icontains=search) | Q(user_display__icontains=search)
            )

        count = archive_config.count()
        lists = archive_config.order_by("-id")[offset:limit].values(
            "id", "title", "src_instance__instance_name", "src_db_name",
            "src_table_name", "dest_instance__instance_name", "dest_db_name",
            "dest_table_name", "sleep", "mode", "no_delete", "status",
            "state", "user_display", "create_time", "resource_group__group_name",
        )
        rows = [row for row in lists]
        return JsonResponse(_encode({"total": count, "rows": rows}), safe=False)


class ArchiveApplyView(APIView):
    permission_classes = [IsAuthenticated, ArchiveApplyPermission]

    def post(self, request):
        user = request.user
        title = request.data.get("title")
        group_name = request.data.get("group_name")
        src_instance_name = request.data.get("src_instance_name")
        src_db_name = request.data.get("src_db_name")
        src_table_name = request.data.get("src_table_name")
        mode = request.data.get("mode")
        dest_instance_name = request.data.get("dest_instance_name")
        dest_db_name = request.data.get("dest_db_name")
        dest_table_name = request.data.get("dest_table_name")
        condition = request.data.get("condition")
        no_delete = request.data.get("no_delete") == "true" if isinstance(request.data.get("no_delete"), str) else bool(request.data.get("no_delete"))
        sleep_val = request.data.get("sleep") or 0

        if not all([title, group_name, src_instance_name, src_db_name, src_table_name, mode, condition]):
            return JsonResponse({"status": 1, "msg": "请填写完整！", "data": {}})
        if mode == "dest" and not all([dest_instance_name, dest_db_name, dest_table_name]):
            return JsonResponse({"status": 1, "msg": "归档到实例时目标实例信息必选！", "data": {}})

        try:
            s_ins = user_instances(request.user, db_type=["mysql"]).get(instance_name=src_instance_name)
        except Instance.DoesNotExist:
            return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！", "data": {}})

        d_ins = None
        if mode == "dest":
            try:
                d_ins = user_instances(request.user, db_type=["mysql"]).get(instance_name=dest_instance_name)
            except Instance.DoesNotExist:
                return JsonResponse({"status": 1, "msg": "你所在组未关联该实例！", "data": {}})

        res_group = ResourceGroup.objects.get(group_name=group_name)
        from django.db import transaction as _tx
        with _tx.atomic():
            archive_info = ArchiveConfig(
                title=title, resource_group=res_group, audit_auth_groups="",
                src_instance=s_ins, src_db_name=src_db_name, src_table_name=src_table_name,
                dest_instance=d_ins, dest_db_name=dest_db_name or "", dest_table_name=dest_table_name or "",
                condition=condition, mode=mode, no_delete=no_delete, sleep=sleep_val,
                status=WorkflowStatus.WAITING, state=False,
                user_name=user.username, user_display=user.display,
            )
            audit_handler = get_auditor(
                workflow=archive_info, resource_group=res_group.group_name,
                resource_group_id=res_group.group_id,
            )
            try:
                audit_handler.create_audit()
            except AuditException as e:
                logger.error(f"新建审批流失败: {str(e)}")
                return JsonResponse({"status": 1, "msg": "新建审批流失败, 请联系管理员", "data": {}})
            audit_handler.workflow.status = audit_handler.audit.current_status
            if audit_handler.audit.current_status == WorkflowStatus.PASSED:
                audit_handler.workflow.state = True
            audit_handler.workflow.save()
            async_task(
                notify_for_audit, workflow_audit=audit_handler.audit, timeout=60,
                task_name=f"archive-apply-{audit_handler.workflow.id}",
            )

        return JsonResponse({
            "status": 0, "msg": "",
            "data": {
                "workflow_status": audit_handler.audit.current_status,
                "audit_id": audit_handler.audit.audit_id,
                "archive_id": audit_handler.workflow.id,
            },
        })


class ArchiveLogView(APIView):
    permission_classes = [IsAuthenticated, ArchivePermission]

    def get(self, request):
        limit = _safe_int(request.GET.get("limit"), 0)
        offset = _safe_int(request.GET.get("offset"), 0)
        limit = offset + limit
        archive_id = request.GET.get("archive_id")

        # IDOR 收口：日志含完整归档命令与错误信息，仅超管/该归档配置所属组内用户可读
        if not request.user.is_superuser:
            archive = ArchiveConfig.objects.filter(id=archive_id).first()
            if archive is None:
                return JsonResponse({"status": 1, "msg": "归档配置不存在", "data": []})
            user_group_ids = [g.group_id for g in user_groups(request.user)]
            if archive.resource_group_id not in user_group_ids:
                return JsonResponse({"status": 1, "msg": "无权查看该归档日志", "data": []})

        archive_logs = ArchiveLog.objects.filter(archive=archive_id).annotate(
            info=Concat("cmd", V("\n"), "statistics", output_field=TextField())
        )
        count = archive_logs.count()
        lists = archive_logs.order_by("-id")[offset:limit].values(
            "cmd", "info", "condition", "mode", "no_delete",
            "select_cnt", "insert_cnt", "delete_cnt",
            "success", "error_info", "start_time", "end_time",
        )
        rows = [row for row in lists]
        return JsonResponse(_encode({"total": count, "rows": rows}), safe=False)


class ArchiveSwitchView(APIView):
    permission_classes = [IsAuthenticated, ArchiveMgtPermission]

    def post(self, request):
        archive_id = request.data.get("archive_id")
        state = request.data.get("state") == "true"
        try:
            ArchiveConfig(id=archive_id, state=state).save(update_fields=["state"])
            return JsonResponse({"status": 0, "msg": "ok", "data": {}})
        except Exception as msg:
            return JsonResponse({"status": 1, "msg": str(msg), "data": {}})


class ArchiveOnceView(APIView):
    permission_classes = [IsAuthenticated, ArchiveMgtPermission]

    def get(self, request):
        archive_id = request.GET.get("archive_id")
        async_task(
            "sql.archiver.archive", archive_id, timeout=-1,
            task_name=f"archive-{archive_id}",
        )
        return JsonResponse({"status": 0, "msg": "ok", "data": {}})
