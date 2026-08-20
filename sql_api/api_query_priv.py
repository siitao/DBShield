"""查询权限申请的 SPA 接口：申请详情（JSON）+ 审核（JSON）。

旧的 ``sql/query_privileges.py`` 里 5 个 /query/* 接口（列表/申请/用户权限/变更/审核）
在迁移期继续供 SPA 调用；唯独审核接口 ``query_priv_audit`` 成功后 302 跳详情页、失败
render error.html，对 SPA（XHR）不友好。这里补两个 JSON 接口供 SPA 使用，不动旧的。
"""

import logging

from django.db import transaction
from django_q.tasks import async_task
from drf_spectacular.utils import extend_schema
from rest_framework import views, permissions
from rest_framework.response import Response

from common.utils.const import WorkflowAction, WorkflowStatus, WorkflowType
from sql.models import QueryPrivilegesApply
from sql.notify import notify_for_audit
from sql_api.api_misc import _query_apply_audit_call_back
from sql.utils.resource_group import user_groups
from sql.utils.workflow_audit import Audit, AuditException, AuditV2, get_auditor

logger = logging.getLogger("default")


def _serialize_review_info(review_info):
    """ReviewInfo.nodes → 可 JSON 序列化的列表（对齐旧版 workflow_display 展示）。"""
    nodes = []
    for n in review_info.nodes:
        nodes.append(
            {
                "group_name": n.group.name if n.group else "自动通过",
                "is_current_node": n.is_current_node,
                "is_passed_node": n.is_passed_node,
                "is_auto_pass": n.is_auto_pass,
            }
        )
    return nodes


class QueryPrivApplyDetail(views.APIView):
    """查询权限申请详情（含审批流 / 是否可审核 / 当前审核人 / 日志）。"""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="查询权限申请详情",
        description="返回申请信息、审批流节点、当前审核人、是否可审核、审核日志等，供 SPA 详情页渲染。",
    )
    def get(self, request, apply_id):
        # IDOR 收口：申请详情含库表/行数限制与审批信息，仅超管/提交人/同资源组用户可见
        workflow_detail = QueryPrivilegesApply.objects.filter(apply_id=apply_id).first()
        if workflow_detail is None:
            return Response({"errors": "申请不存在"}, status=404)
        if not request.user.is_superuser:
            user_group_ids = [g.group_id for g in user_groups(request.user)]
            if (
                workflow_detail.user_name != request.user.username
                and workflow_detail.group_id not in user_group_ids
            ):
                return Response({"errors": "无权查看该申请"}, status=403)
        audit_handler = AuditV2(workflow=workflow_detail)
        review_info = audit_handler.get_review_info()

        is_can_review = Audit.can_review(request.user, apply_id, WorkflowType.QUERY)

        # 审核日志
        logs = []
        last_operation_info = ""
        try:
            audit = Audit.detail_by_workflow_id(
                workflow_id=apply_id, workflow_type=WorkflowType.QUERY
            )
            log_qs = Audit.logs(audit_id=audit.audit_id).values(
                "operation_type_desc",
                "operation_info",
                "operator_display",
                "operation_time",
            )
            logs = [row for row in log_qs]
            if logs:
                last_operation_info = logs[-1]["operation_info"] or ""
        except Exception as e:  # noqa: BLE001
            logger.debug(f"查询权限申请 {apply_id} 无审核日志: {e}")

        # 当前审核人（当前节点所在 group 中、与申请同资源组的在职用户）
        current_reviewers = []
        for node in review_info.nodes:
            if not node.is_current_node:
                continue
            for user in node.group.user_set.filter(is_active=1):
                group_names = [g.group_name for g in user_groups(user)]
                if workflow_detail.group_name in group_names:
                    current_reviewers.append(
                        {"username": user.username, "display": user.display or user.username}
                    )

        apply = {
            "apply_id": workflow_detail.apply_id,
            "title": workflow_detail.title,
            "group_name": workflow_detail.group_name,
            "user_display": workflow_detail.user_display,
            "user_name": workflow_detail.user_name,
            "instance_name": workflow_detail.instance.instance_name if workflow_detail.instance else "",
            "priv_type": workflow_detail.priv_type,
            "db_list": workflow_detail.db_list,
            "table_list": workflow_detail.table_list,
            "limit_num": workflow_detail.limit_num,
            "valid_date": workflow_detail.valid_date,
            "status": workflow_detail.status,
            "create_time": workflow_detail.create_time,
        }
        return Response(
            {
                "apply": apply,
                "review_info": _serialize_review_info(review_info),
                "current_reviewers": current_reviewers,
                "is_can_review": is_can_review,
                "last_operation_info": last_operation_info,
                "logs": logs,
            }
        )


class QueryPrivAudit(views.APIView):
    """查询权限审核（JSON 版，替代旧的 302 ``query_priv_audit``）。

    audit_status: 1 通过 / 2 驳回（WorkflowAction.PASS / REJECT）。
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="查询权限审核",
        description="通过 / 驳回查询权限申请，返回 JSON {status,msg}（不再 302）。需 sql.query_review 权限。",
    )
    def post(self, request):
        result = {"status": 0, "msg": "ok"}
        try:
            apply_id = int(request.data["apply_id"])
            audit_status = WorkflowAction(int(request.data["audit_status"]))
        except (KeyError, ValueError, TypeError) as e:
            result["status"] = 1
            result["msg"] = f"参数错误: {e}"
            return Response(result)

        audit_remark = request.data.get("audit_remark") or ""

        try:
            apply = QueryPrivilegesApply.objects.get(apply_id=apply_id)
        except QueryPrivilegesApply.DoesNotExist:
            result["status"] = 1
            result["msg"] = "工单不存在"
            return Response(result)

        if not Audit.can_review(request.user, apply_id, WorkflowType.QUERY):
            result["status"] = 1
            result["msg"] = "你无权审核该工单"
            return Response(result)

        auditor = get_auditor(workflow=apply)
        try:
            with transaction.atomic():
                workflow_audit_detail = auditor.operate(
                    audit_status, request.user, audit_remark
                )
                # 统一 callback：内部做授权和更新数据库
                _query_apply_audit_call_back(
                    auditor.audit.workflow_id, auditor.audit.current_status
                )
        except AuditException as e:
            result["status"] = 1
            result["msg"] = f"审核失败: {e}"
            return Response(result)

        # 消息通知
        async_task(
            notify_for_audit,
            workflow_audit=auditor.audit,
            workflow_audit_detail=workflow_audit_detail,
            timeout=60,
            task_name=f"query-priv-audit-{apply_id}",
        )
        return Response(result)
