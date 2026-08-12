import datetime
import logging
import traceback

from django.contrib.auth.decorators import permission_required
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django_q.tasks import async_task
from drf_spectacular.utils import extend_schema
from rest_framework import views, generics, status, serializers, permissions
from rest_framework.response import Response

from common.config import SysConfig
from common.utils.const import WorkflowStatus, WorkflowType, WorkflowAction
from sql.engines import get_engine
from sql.models import (
    SqlWorkflow,
    SqlWorkflowContent,
    WorkflowAudit,
    Users,
    WorkflowLog,
    ArchiveConfig,
    QueryPrivilegesApply,
)
from sql.notify import notify_for_audit, notify_for_execute
from sql_api.api_misc import _query_apply_audit_call_back
from sql.utils.resource_group import user_groups
from sql.utils.sql_review import (
    can_cancel,
    can_execute,
    can_rollback,
    can_timingtask,
    can_view,
    on_correct_time_period,
)
from sql.utils.tasks import add_sql_schedule, del_schedule, task_info
from sql.utils.workflow_audit import Audit, AuditV2, get_auditor, AuditException
from .filters import WorkflowFilter, WorkflowAuditFilter
from .pagination import CustomizedPagination
from .serializers import (
    WorkflowContentSerializer,
    ExecuteCheckSerializer,
    ExecuteCheckResultSerializer,
    WorkflowAuditSerializer,
    WorkflowAuditListSerializer,
    WorkflowLogSerializer,
    WorkflowLogListSerializer,
    AuditWorkflowSerializer,
    ExecuteWorkflowSerializer,
)

logger = logging.getLogger("default")


class ExecuteCheck(views.APIView):

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="SQL检查",
        request=ExecuteCheckSerializer,
        responses={200: ExecuteCheckResultSerializer},
        description="对提供的SQL进行语法检查",
    )
    @method_decorator(permission_required("sql.sql_submit", raise_exception=True))
    def post(self, request):
        # 参数验证
        serializer = ExecuteCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.get_instance()
        # 交给engine进行检测
        try:
            db_name = request.data["db_name"]
            check_engine = get_engine(instance=instance)
            db_name = check_engine.escape_string(db_name)
            check_result = check_engine.execute_check(
                db_name=db_name, sql=request.data["full_sql"].strip()
            )
        except Exception as e:
            raise serializers.ValidationError({"errors": f"{e}"})
        check_result.rows = check_result.to_dict()
        serializer_obj = ExecuteCheckResultSerializer(check_result)
        return Response(serializer_obj.data)


class WorkflowList(generics.ListAPIView):
    """
    列出所有的workflow或者提交一条新的workflow
    """

    filterset_class = WorkflowFilter
    pagination_class = CustomizedPagination
    serializer_class = WorkflowContentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """
        1、非管理员，拥有审核权限、资源组粒度执行权限的，可以查看组内所有工单
        2、管理员，审计员，可查看所有工单
        """
        filter_dict = {}
        user = self.request.user
        # 管理员，审计员，可查看所有工单
        if user.is_superuser or user.has_perm("sql.audit_user"):
            pass
        # 非管理员，拥有审核权限、资源组粒度执行权限的，可以查看组内所有工单
        elif user.has_perm("sql.sql_review") or user.has_perm(
            "sql.sql_execute_for_resource_group"
        ):
            # group_id 属于关联的 SqlWorkflow，需跨表（SqlWorkflowContent.workflow）
            filter_dict["workflow__group_id__in"] = [
                group.group_id for group in user_groups(user)
            ]
        # 其他人只能查看自己提交的工单
        else:
            # engineer 属于关联的 SqlWorkflow，需跨表
            filter_dict["workflow__engineer"] = user.username
        return (
            SqlWorkflowContent.objects.filter(**filter_dict)
            .select_related("workflow")
            .order_by("-id")
        )

    @extend_schema(
        summary="SQL上线工单清单",
        request=WorkflowContentSerializer,
        responses={200: WorkflowContentSerializer},
        description="列出所有SQL上线工单（过滤，分页）",
    )
    def get(self, request):
        workflows = self.filter_queryset(self.get_queryset())
        page_wf = self.paginate_queryset(queryset=workflows)
        serializer_obj = self.get_serializer(page_wf, many=True)
        data = {"data": serializer_obj.data}
        return self.get_paginated_response(data)

    @extend_schema(
        summary="提交SQL上线工单",
        request=WorkflowContentSerializer,
        responses={201: WorkflowContentSerializer},
        description="提交一条SQL上线工单",
    )
    @method_decorator(permission_required("sql.sql_submit", raise_exception=True))
    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workflow_content = serializer.save()
        sys_config = SysConfig()
        is_notified = (
            "Apply" in sys_config.get("notify_phase_control").split(",")
            if sys_config.get("notify_phase_control")
            else True
        )
        if (
            workflow_content.workflow.status
            in ["workflow_manreviewing", "workflow_review_pass"]
            and is_notified
        ):
            # 获取审核信息
            workflow_audit = Audit.detail_by_workflow_id(
                workflow_id=workflow_content.workflow.id,
                workflow_type=WorkflowType.SQL_REVIEW,
            )
            async_task(
                notify_for_audit,
                workflow_audit=workflow_audit,
                timeout=60,
                task_name=f"sqlreview-submit-{workflow_content.workflow.id}",
            )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class WorkflowAuditList(generics.ListAPIView):
    """
    列出指定用户当前待自己审核的工单
    """

    permission_classes = [permissions.IsAuthenticated]
    filterset_class = WorkflowAuditFilter
    pagination_class = CustomizedPagination
    serializer_class = WorkflowAuditListSerializer
    queryset = WorkflowAudit.objects.filter(
        current_status=WorkflowStatus.WAITING
    ).order_by("-audit_id")

    @extend_schema(exclude=True)
    def get(self, request):
        return Response({"detail": "方法 “GET” 不被允许。"})

    @extend_schema(
        summary="待审核清单",
        request=WorkflowAuditSerializer,
        responses={200: WorkflowAuditListSerializer},
        description="列出指定用户待审核清单（过滤，分页）",
    )
    def post(self, request):
        # 未传 engineer 时默认取当前登录用户，避免前端可随意查询他人待办
        engineer = request.data.get("engineer") or request.user.username

        # 参数验证
        serializer = WorkflowAuditSerializer(data={"engineer": engineer})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # 先获取用户所在资源组列表
        user = Users.objects.get(username=engineer)
        group_list = user_groups(user)
        group_ids = [group.group_id for group in group_list]

        # 再获取用户所在权限组列表
        if user.is_superuser:
            auth_group_ids = [group.id for group in Group.objects.all()]
        else:
            auth_group_ids = [group.id for group in Group.objects.filter(user=user)]

        self.queryset = self.queryset.filter(
            current_status=WorkflowStatus.WAITING,
            group_id__in=group_ids,
            current_audit__in=auth_group_ids,
        )
        audit = self.filter_queryset(self.queryset)
        page_audit = self.paginate_queryset(queryset=audit)
        serializer_obj = self.get_serializer(page_audit, many=True)
        data = {"data": serializer_obj.data}
        return self.get_paginated_response(data)


class AuditWorkflow(views.APIView):
    """
    审核workflow，包括查询权限申请、SQL上线申请、数据归档申请
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="审核工单",
        request=AuditWorkflowSerializer,
        description="审核一条工单（通过或终止）",
    )
    def post(self, request):
        # 参数验证
        serializer = AuditWorkflowSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        # 此处已经通过校验, 肯定存在, 就不 try 了
        workflow_audit = WorkflowAudit.objects.get(
            workflow_id=serializer.data["workflow_id"],
            workflow_type=serializer.data["workflow_type"],
        )
        sys_config = SysConfig()
        auditor = get_auditor(audit=workflow_audit)
        # 操作主体必须以 request.user 为准，engineer 仅超管可指定（H1：防冒名审核/撤回）
        user = request.user
        engineer = serializer.data.get("engineer")
        if engineer and engineer != user.username and not user.is_superuser:
            raise serializers.ValidationError({"errors": "无权以他人身份审核工单"})
        if engineer and engineer != user.username:
            user = Users.objects.get(username=engineer)
        if serializer.data["audit_type"] == "pass":
            action = WorkflowAction.PASS
            notify_config_key = "Pass"
            success_message = "passed"
        elif serializer.data["audit_type"] == "cancel":
            notify_config_key = "Cancel"
            success_message = "canceled"
            if auditor.workflow.engineer == user.username:
                # 提交人终止自己的工单
                action = WorkflowAction.ABORT
            elif Audit.can_review(
                user,
                serializer.data["workflow_id"],
                serializer.data["workflow_type"],
            ):
                # 审核人驳回
                action = WorkflowAction.REJECT
            else:
                raise serializers.ValidationError({"errors": "用户无权操作此工单"})
        else:
            raise serializers.ValidationError(
                {"errors": "audit_type 只能是 pass 或 cancel"}
            )

        try:
            workflow_audit_detail = auditor.operate(
                action, user, serializer.data["audit_remark"]
            )
        except AuditException as e:
            raise serializers.ValidationError({"errors": f"操作失败, {str(e)}"})

        # 最后处置一下原本工单的状态
        if auditor.workflow_type == WorkflowType.QUERY:
            _query_apply_audit_call_back(
                auditor.audit.workflow_id,
                auditor.audit.current_status,
            )
        elif auditor.workflow_type == WorkflowType.SQL_REVIEW:
            if auditor.audit.current_status == WorkflowStatus.PASSED:
                auditor.workflow.status = "workflow_review_pass"
                auditor.workflow.save(update_fields=["status"])
            elif auditor.audit.current_status in [
                WorkflowStatus.ABORTED,
                WorkflowStatus.REJECTED,
            ]:
                if auditor.workflow.status == "workflow_timingtask":
                    del_schedule(f"sqlreview-timing-{auditor.workflow.id}")
                    # 将流程状态修改为人工终止流程
                auditor.workflow.status = "workflow_abort"
                auditor.workflow.save(update_fields=["status"])
        elif auditor.workflow_type == WorkflowType.ARCHIVE:
            auditor.workflow.status = auditor.audit.current_status
            if auditor.audit.current_status == WorkflowStatus.PASSED:
                auditor.workflow.state = True
            else:
                auditor.workflow.state = False
            auditor.workflow.save(update_fields=["status", "state"])

        # 发消息
        is_notified = (
            notify_config_key in sys_config.get("notify_phase_control").split(",")
            if sys_config.get("notify_phase_control")
            else True
        )
        if is_notified:
            async_task(
                notify_for_audit,
                workflow_audit=auditor.audit,
                workflow_audit_detail=workflow_audit_detail,
                timeout=60,
                task_name=f"notify-audit-{auditor.audit}-{WorkflowType(auditor.audit.workflow_type).label}",
            )
        return Response({"msg": success_message})


class ExecuteWorkflow(views.APIView):
    """
    执行workflow，包括SQL上线工单、数据归档工单
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="执行工单",
        request=ExecuteWorkflowSerializer,
        description="执行一条工单",
    )
    def post(self, request):
        # 参数验证
        serializer = ExecuteWorkflowSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        workflow_type = request.data["workflow_type"]
        workflow_id = request.data["workflow_id"]

        # 执行SQL上线工单
        if workflow_type == 2:
            mode = request.data["mode"]
            # 操作主体必须以 request.user 为准，engineer 仅超管可指定（H1：防冒名执行）
            user = request.user
            engineer = request.data.get("engineer")
            if engineer and engineer != user.username and not user.is_superuser:
                raise serializers.ValidationError({"errors": "无权以他人身份执行工单"})
            if engineer and engineer != user.username:
                user = Users.objects.get(username=engineer)

            # 校验多个权限
            if not (
                user.has_perm("sql.sql_execute")
                or user.has_perm("sql.sql_execute_for_resource_group")
            ):
                raise serializers.ValidationError({"errors": "你无权执行当前工单！"})

            if can_execute(user, workflow_id) is False:
                raise serializers.ValidationError({"errors": "你无权执行当前工单！"})

            if on_correct_time_period(workflow_id) is False:
                raise serializers.ValidationError(
                    {
                        "errors": "不在可执行时间范围内，如果需要修改执行时间请重新提交工单!"
                    }
                )

            # 获取审核信息
            audit_id = Audit.detail_by_workflow_id(
                workflow_id=workflow_id,
                workflow_type=WorkflowType.SQL_REVIEW,
            ).audit_id

            # 交由系统执行
            if mode == "auto":
                # 修改工单状态为排队中
                SqlWorkflow(id=workflow_id, status="workflow_queuing").save(
                    update_fields=["status"]
                )
                # 删除定时执行任务
                schedule_name = f"sqlreview-timing-{workflow_id}"
                del_schedule(schedule_name)
                # 加入执行队列
                async_task(
                    "sql.utils.execute_sql.execute",
                    workflow_id,
                    user,
                    hook="sql.utils.execute_sql.execute_callback",
                    timeout=-1,
                    task_name=f"sqlreview-execute-{workflow_id}",
                )
                # 增加工单日志
                Audit.add_log(
                    audit_id=audit_id,
                    operation_type=5,
                    operation_type_desc="执行工单",
                    operation_info="工单执行排队中",
                    operator=user.username,
                    operator_display=user.display,
                )

            # 线下手工执行
            elif mode == "manual":
                # 将流程状态修改为执行结束
                SqlWorkflow(
                    id=workflow_id,
                    status="workflow_finish",
                    finish_time=datetime.datetime.now(),
                ).save(update_fields=["status", "finish_time"])
                # 增加工单日志
                Audit.add_log(
                    audit_id=audit_id,
                    operation_type=6,
                    operation_type_desc="手工工单",
                    operation_info="确认手工执行结束",
                    operator=user.username,
                    operator_display=user.display,
                )
                # 开启了Execute阶段通知参数才发送消息通知
                sys_config = SysConfig()
                is_notified = (
                    "Execute" in sys_config.get("notify_phase_control").split(",")
                    if sys_config.get("notify_phase_control")
                    else True
                )
                if is_notified:
                    notify_for_execute(
                        workflow=SqlWorkflow.objects.get(id=workflow_id),
                    )
        # 执行数据归档工单
        elif workflow_type == 3:
            async_task(
                "sql.archiver.archive",
                workflow_id,
                timeout=-1,
                task_name=f"archive-{workflow_id}",
            )

        return Response({"msg": "开始执行，执行结果请到工单详情页查看"})


class WorkflowLogList(generics.ListAPIView):
    """
    获取某个工单的日志
    """

    permission_classes = [permissions.IsAuthenticated]
    pagination_class = CustomizedPagination
    serializer_class = WorkflowLogListSerializer
    queryset = WorkflowLog.objects.all()

    @extend_schema(exclude=True)
    def get(self, request):
        return Response({"detail": "方法 “GET” 不被允许。"})

    @extend_schema(
        summary="工单日志",
        request=WorkflowLogSerializer,
        responses={200: WorkflowLogListSerializer},
        description="获取某个工单的日志（分页）",
    )
    def post(self, request):
        # 参数验证
        serializer = WorkflowLogSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        audit_id = WorkflowAudit.objects.get(
            workflow_id=request.data["workflow_id"],
            workflow_type=request.data["workflow_type"],
        ).audit_id
        workflow_logs = self.queryset.filter(audit_id=audit_id).order_by("-id")
        page_log = self.paginate_queryset(queryset=workflow_logs)
        serializer_obj = self.get_serializer(page_log, many=True)
        data = {"data": serializer_obj.data}
        return self.get_paginated_response(data)


class WorkflowDetail(views.APIView):
    """SQL上线工单详情：含审批流、操作权限标志、定时任务信息"""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(summary="工单详情", description="获取工单详情、审批流、操作权限标志")
    def get(self, request, workflow_id):
        workflow_detail = get_object_or_404(SqlWorkflow, pk=workflow_id)
        if not can_view(request.user, workflow_id):
            raise PermissionDenied
        workflow_content = get_object_or_404(
            SqlWorkflowContent, workflow_id=workflow_id
        )
        data = WorkflowContentSerializer(workflow_content).data

        # 审批流（与旧版 detail 视图逻辑保持一致）
        audit_handler = AuditV2(workflow=workflow_detail)
        review_info_obj = audit_handler.get_review_info()
        review_info = [
            {
                "group_name": node.group.name if node.group else "",
                "is_current_node": node.is_current_node,
                "is_passed_node": node.is_passed_node,
                "is_auto_pass": node.is_auto_pass,
            }
            for node in review_info_obj.nodes
        ]
        # 当前审核人（当前节点权限组内、属于该工单资源组的活跃用户）
        current_reviewers = []
        for node in review_info_obj.nodes:
            if not node.is_current_node or not node.group:
                continue
            for audit_user in node.group.user_set.filter(is_active=1):
                group_names = [g.group_name for g in user_groups(audit_user)]
                if workflow_detail.group_name in group_names:
                    current_reviewers.append(
                        {
                            "username": audit_user.username,
                            "display": audit_user.display,
                        }
                    )

        # 操作权限标志 + 最近操作信息（自动审核不通过时全部不可操作）
        if workflow_detail.status != "workflow_autoreviewwrong":
            is_can_review = Audit.can_review(request.user, workflow_id, 2)
            is_can_execute = can_execute(request.user, workflow_id)
            is_can_timingtask = can_timingtask(request.user, workflow_id)
            is_can_cancel = can_cancel(request.user, workflow_id)
            is_can_rollback = can_rollback(request.user, workflow_id)
            try:
                audit_detail = Audit.detail_by_workflow_id(
                    workflow_id=workflow_id,
                    workflow_type=WorkflowType.SQL_REVIEW,
                )
                last_operation_info = (
                    Audit.logs(audit_id=audit_detail.audit_id)
                    .latest("id")
                    .operation_info
                )
            except Exception as e:
                logger.debug(f"无审核日志记录：{e}")
                last_operation_info = ""
        else:
            is_can_review = False
            is_can_execute = False
            is_can_timingtask = False
            is_can_cancel = False
            is_can_rollback = False
            last_operation_info = ""

        # 定时任务下次执行时间
        run_date = ""
        if workflow_detail.status == "workflow_timingtask":
            job = task_info(f"sqlreview-timing-{workflow_id}")
            if job:
                run_date = job.next_run

        manual = bool(SysConfig().get("manual"))
        # AI 风险评估汇总（解析 review_content，取最高风险分）
        ai_summary = self._calc_ai_risk_summary(workflow_content.review_content)
        data.update(
            {
                "review_info": review_info,
                "current_reviewers": current_reviewers,
                "last_operation_info": last_operation_info,
                "is_can_review": is_can_review,
                "is_can_execute": is_can_execute,
                "is_can_timingtask": is_can_timingtask,
                "is_can_cancel": is_can_cancel,
                "is_can_rollback": is_can_rollback,
                "manual": manual,
                "run_date": run_date,
                "ai_max_risk_level": ai_summary["ai_max_risk_level"],
                "ai_max_risk_score": ai_summary["ai_max_risk_score"],
                "ai_high_risk_count": ai_summary["ai_high_risk_count"],
                "ai_lock_high_count": ai_summary["ai_lock_high_count"],
            }
        )
        return Response(data)

    @staticmethod
    def _calc_ai_risk_summary(review_content):
        """解析工单 review_content，汇总 AI 风险评分。

        review_content 是 ReviewSet.json() 的结果（JSON 字符串或已 dict 化）。
        返回 {ai_max_risk_level, ai_max_risk_score, ai_high_risk_count}。
        解析失败或无 AI 数据时返回占位值，前端据此隐藏汇总卡片。
        """
        import json

        fallback = {
            "ai_max_risk_level": "",
            "ai_max_risk_score": 0,
            "ai_high_risk_count": 0,
            "ai_lock_high_count": 0,
        }
        if not review_content:
            return fallback
        try:
            rows = (
                review_content
                if isinstance(review_content, list)
                else json.loads(review_content)
            )
        except (ValueError, TypeError):
            return fallback

        max_score = -1
        max_level = ""
        high_count = 0
        lock_high_count = 0
        has_ai = False
        for r in rows:
            if not isinstance(r, dict):
                continue
            level = r.get("ai_risk_level")
            score = r.get("ai_risk_score")
            if level is None and score is None:
                continue
            has_ai = True
            try:
                score = int(score) if score is not None else 0
            except (TypeError, ValueError):
                score = 0
            if score > max_score:
                max_score = score
                max_level = level or ""
            if level == "high":
                high_count += 1
            if r.get("ai_ddl_lock_risk") == "high":
                lock_high_count += 1

        if not has_ai:
            return fallback
        # max_level 兜底（按 score 推断）
        if not max_level:
            max_level = (
                "high" if max_score > 70 else "medium" if max_score >= 40 else "low"
            )
        return {
            "ai_max_risk_level": max_level,
            "ai_max_risk_score": max(0, max_score),
            "ai_high_risk_count": high_count,
            "ai_lock_high_count": lock_high_count,
        }


class TimingTask(views.APIView):
    """为SQL上线工单设置定时执行"""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(summary="定时执行", description="为工单设置定时执行时间")
    def post(self, request):
        workflow_id = request.data.get("workflow_id")
        run_date = request.data.get("run_date")
        if not workflow_id or not run_date:
            raise serializers.ValidationError(
                {"errors": "workflow_id 和 run_date 不能为空"}
            )
        if run_date < datetime.datetime.now().strftime("%Y-%m-%d %H:%M"):
            raise serializers.ValidationError({"errors": "时间不能小于当前时间"})

        workflow_detail = get_object_or_404(SqlWorkflow, pk=workflow_id)
        if not (
            request.user.has_perm("sql.sql_execute")
            or request.user.has_perm("sql.sql_execute_for_resource_group")
        ):
            raise serializers.ValidationError({"errors": "你无权执行当前工单！"})
        if can_timingtask(request.user, workflow_id) is False:
            raise serializers.ValidationError({"errors": "你无权操作当前工单！"})

        run_date_dt = datetime.datetime.strptime(run_date, "%Y-%m-%d %H:%M")
        if on_correct_time_period(workflow_id, run_date_dt) is False:
            raise serializers.ValidationError(
                {"errors": "不在可执行时间范围内，如果需要修改执行时间请重新提交工单!"}
            )
        schedule_name = f"sqlreview-timing-{workflow_id}"
        try:
            with transaction.atomic():
                workflow_detail.status = "workflow_timingtask"
                workflow_detail.save()
                add_sql_schedule(schedule_name, run_date_dt, workflow_id)
                audit_id = Audit.detail_by_workflow_id(
                    workflow_id=workflow_id,
                    workflow_type=WorkflowType.SQL_REVIEW,
                ).audit_id
                Audit.add_log(
                    audit_id=audit_id,
                    operation_type=4,
                    operation_type_desc="定时执行",
                    operation_info=f"定时执行时间：{run_date_dt}",
                    operator=request.user.username,
                    operator_display=request.user.display,
                )
        except Exception as msg:
            logger.error(f"定时执行工单报错：{traceback.format_exc()}")
            raise serializers.ValidationError({"errors": str(msg)})
        return Response({"msg": "定时执行设置成功"})


class AlterRunDate(views.APIView):
    """审核人修改工单可执行时间范围"""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(summary="修改可执行时间", description="审核人修改工单可执行时间范围")
    @method_decorator(permission_required("sql.sql_review", raise_exception=True))
    def post(self, request):
        workflow_id = request.data.get("workflow_id")
        run_date_start = request.data.get("run_date_start") or None
        run_date_end = request.data.get("run_date_end") or None
        if not workflow_id:
            raise serializers.ValidationError({"errors": "workflow_id 不能为空"})
        if Audit.can_review(request.user, workflow_id, 2) is False:
            raise serializers.ValidationError({"errors": "你无权操作当前工单！"})
        try:
            SqlWorkflow(
                id=workflow_id,
                run_date_start=run_date_start,
                run_date_end=run_date_end,
            ).save(update_fields=["run_date_start", "run_date_end"])
        except Exception as msg:
            raise serializers.ValidationError({"errors": str(msg)})
        return Response({"msg": "可执行时间已更新"})
