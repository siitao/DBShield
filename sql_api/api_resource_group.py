"""
资源组管理 DRF APIView 集 · 替代 sql/resource_group.py 全部 8 个旧端点。

路由：
  GET  /api/v1/group/list/          — 资源组列表
  POST /api/v1/group/relations/      — 已关联对象
  POST /api/v1/group/unassociated/   — 未关联对象
  POST /api/v1/group/instances/      — 关联实例列表
  GET  /api/v1/group/user_instances/ — 用户可访问实例
  POST /api/v1/group/addrelation/    — 添加关联
  POST /api/v1/group/removerelation/ — 移除关联
  POST /api/v1/group/auditors/       — 审批流程查询
  POST /api/v1/group/changeauditors/ — 审批流程设置
"""
import logging
import traceback
from itertools import chain

import json as _json

from django.contrib.auth.models import Group
from django.db.models import F, Value, IntegerField
from django.http import JsonResponse
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.views import APIView

from common.utils.convert import Convert
from common.utils.extend_json_encoder import encode_json as _encode
from sql.models import Instance, ResourceGroup, Users
from sql.services.resource_service import list_user_accessible_instances
from sql.utils.resource_group import user_groups
from sql.utils.workflow_audit import Audit

logger = logging.getLogger("default")


# ---------- permissions ----------

class SuperuserPermission(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return u and u.is_authenticated and u.is_superuser


# ========== views ==========

class GroupListView(APIView):
    permission_classes = [IsAuthenticated, SuperuserPermission]

    def post(self, request):
        limit = int(request.data.get("limit", 0))
        offset = int(request.data.get("offset", 0))
        limit = offset + limit
        search = request.data.get("search", "")

        group_obj = ResourceGroup.objects.filter(
            group_name__icontains=search, is_deleted=0
        )
        group_count = group_obj.count()
        group_list = group_obj[offset:limit].values(
            "group_id", "group_name", "ding_webhook"
        )
        rows = [row for row in group_list]
        return JsonResponse(
            _encode({"total": group_count, "rows": rows}), safe=False
        )


class RelationsView(APIView):
    permission_classes = [IsAuthenticated, SuperuserPermission]

    def post(self, request):
        group_id = int(request.data.get("group_id"))
        object_type = str(request.data.get("type", ""))
        limit = int(request.data.get("limit", 0))
        offset = int(request.data.get("offset", 0))
        limit = offset + limit
        search = request.data.get("search")

        resource_group = ResourceGroup.objects.get(group_id=group_id)
        rows_users = resource_group.users_set.all()
        rows_instances = resource_group.instance_set.all()
        if search:
            rows_users = rows_users.filter(display__contains=search)
            rows_instances = rows_instances.filter(instance_name__contains=search)
        rows_users = rows_users.annotate(
            object_id=F("id"),
            object_type=Value(0, output_field=IntegerField()),
            object_name=F("display"),
            group_id=F("resource_group__group_id"),
            group_name=F("resource_group__group_name"),
        ).values("object_type", "object_id", "object_name", "group_id", "group_name")
        rows_instances = rows_instances.annotate(
            object_id=F("id"),
            object_type=Value(1, output_field=IntegerField()),
            object_name=F("instance_name"),
            group_id=F("resource_group__group_id"),
            group_name=F("resource_group__group_name"),
        ).values("object_type", "object_id", "object_name", "group_id", "group_name")

        if object_type == "0":
            count = rows_users.count()
            rows = [row for row in rows_users][offset:limit]
        elif object_type == "1":
            count = rows_instances.count()
            rows = [row for row in rows_instances][offset:limit]
        else:
            rows = list(chain(rows_users, rows_instances))
            count = len(rows)
            rows = rows[offset:limit]

        return JsonResponse(
            _encode({"status": 0, "msg": "ok", "total": count, "rows": rows}),
            safe=False,
        )


class UnassociatedView(APIView):
    permission_classes = [IsAuthenticated, SuperuserPermission]

    def post(self, request):
        group_id = int(request.data.get("group_id"))
        object_type = int(request.data.get("object_type", -1))
        resource_group = ResourceGroup.objects.get(group_id=group_id)

        if object_type == 0:
            associated_ids = [u.id for u in resource_group.users_set.all()]
            rows = (
                Users.objects.exclude(pk__in=associated_ids)
                .annotate(object_id=F("pk"), object_name=F("display"))
                .values("object_id", "object_name")
            )
        elif object_type == 1:
            associated_ids = [i.id for i in resource_group.instance_set.all()]
            rows = (
                Instance.objects.exclude(pk__in=associated_ids)
                .annotate(object_id=F("pk"), object_name=F("instance_name"))
                .values("object_id", "object_name")
            )
        else:
            return JsonResponse({"status": 1, "msg": "关联对象类型不正确"})

        rows = [row for row in rows]
        return JsonResponse(
            {"status": 0, "msg": "ok", "rows": rows, "total": len(rows)}
        )


def _check_group_access(user, group_name):
    """按组名校验访问权限：超管放行，普通用户仅可访问自己所在资源组。

    返回 (group, error JsonResponse)；group 为 None 时 error 非 None。
    工单提交等常规流程只查本人所在组，收口后不影响正常使用。
    """
    group = ResourceGroup.objects.filter(group_name=group_name, is_deleted=0).first()
    if group is None:
        return None, JsonResponse({"status": 1, "msg": "资源组不存在"})
    if not user.is_superuser:
        user_group_ids = [g.group_id for g in user_groups(user)]
        if group.group_id not in user_group_ids:
            return None, JsonResponse({"status": 1, "msg": "无权访问该资源组"})
    return group, None


class InstancesView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        group_name = request.data.get("group_name")
        tag_code = request.data.get("tag_code")
        db_type = request.data.get("db_type")

        # 收口：此前任意登录用户可按 group_name 枚举任意资源组的实例清单
        group, err = _check_group_access(request.user, group_name)
        if err:
            return err

        ins = group.instance_set.all()
        filter_dict = {}
        if db_type:
            filter_dict["db_type"] = db_type
        if tag_code:
            filter_dict["instance_tag__tag_code"] = tag_code
            filter_dict["instance_tag__active"] = True
        ins = (
            ins.filter(**filter_dict)
            .order_by(Convert("instance_name", "gbk").asc())
            .values("id", "type", "db_type", "instance_name")
        )
        rows = [row for row in ins]
        return JsonResponse({"status": 0, "msg": "ok", "data": rows})


class UserInstancesView(APIView):
    """GET — 用户可访问实例列表（通过资源组间接关联）。"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        result = list_user_accessible_instances(
            user=request.user,
            type=request.GET.get("type"),
            db_type=request.GET.getlist("db_type[]"),
            tag_codes=request.GET.getlist("tag_codes[]"),
        )
        return JsonResponse(result)


class AddRelationView(APIView):
    permission_classes = [IsAuthenticated, SuperuserPermission]

    def post(self, request):
        try:
            group_id = int(request.data.get("group_id"))
        except (TypeError, ValueError):
            return JsonResponse({"status": 1, "msg": "资源组ID不合法"})
        object_type = str(request.data.get("object_type", ""))
        object_info = request.data.get("object_info")
        if isinstance(object_info, str):
            object_list = _json.loads(object_info)
        else:
            object_list = object_info
        try:
            resource_group = ResourceGroup.objects.get(group_id=group_id)
            obj_ids = [int(obj.split(",")[0]) for obj in object_list]
            if object_type == "0":
                resource_group.users_set.add(*Users.objects.filter(pk__in=obj_ids))
            elif object_type == "1":
                resource_group.instance_set.add(*Instance.objects.filter(pk__in=obj_ids))
            result = {"status": 0, "msg": "ok"}
        except Exception as e:
            logger.error(traceback.format_exc())
            result = {"status": 1, "msg": str(e)}
        return JsonResponse(result)


class RemoveRelationView(APIView):
    permission_classes = [IsAuthenticated, SuperuserPermission]

    def post(self, request):
        try:
            group_id = int(request.data.get("group_id"))
        except (TypeError, ValueError):
            return JsonResponse({"status": 1, "msg": "资源组ID不合法"})
        object_type = str(request.data.get("object_type", ""))
        object_info = request.data.get("object_info")
        if isinstance(object_info, str):
            object_list = _json.loads(object_info)
        else:
            object_list = object_info
        try:
            resource_group = ResourceGroup.objects.get(group_id=group_id)
            obj_ids = [int(obj.split(",")[0]) for obj in object_list]
            if object_type == "0":
                resource_group.users_set.remove(*Users.objects.filter(pk__in=obj_ids))
            elif object_type == "1":
                resource_group.instance_set.remove(*Instance.objects.filter(pk__in=obj_ids))
            result = {"status": 0, "msg": "ok"}
        except Exception as e:
            logger.error(traceback.format_exc())
            result = {"status": 1, "msg": str(e)}
        return JsonResponse(result)


class AuditorsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        group_name = request.data.get("group_name")
        workflow_type = request.data.get("workflow_type")
        result = {
            "status": 0,
            "msg": "ok",
            "data": {"auditors": "", "auditors_display": ""},
        }
        if group_name:
            # 收口：审批组结构仅本人所在资源组（或超管）可查
            group, err = _check_group_access(request.user, group_name)
            if err:
                return err
            audit_auth_groups = Audit.settings(
                group_id=group.group_id, workflow_type=workflow_type
            )
        else:
            return JsonResponse({"status": 1, "msg": "参数错误"})

        if audit_auth_groups:
            for auth_group_id in audit_auth_groups.split(","):
                try:
                    Group.objects.get(id=auth_group_id)
                except Exception:
                    return JsonResponse(
                        {"status": 1, "msg": "审批流程权限组不存在，请重新配置！"}
                    )
            names = "->".join(
                Group.objects.get(id=gid).name
                for gid in audit_auth_groups.split(",")
            )
            result["data"]["auditors"] = audit_auth_groups
            result["data"]["auditors_display"] = names

        return JsonResponse(result)


class ChangeAuditorsView(APIView):
    permission_classes = [IsAuthenticated, SuperuserPermission]

    def post(self, request):
        auth_groups = request.data.get("audit_auth_groups")
        group_name = request.data.get("group_name")
        workflow_type = request.data.get("workflow_type")
        result = {"status": 0, "msg": "ok", "data": []}

        group_id = ResourceGroup.objects.get(group_name=group_name).group_id
        audit_auth_groups = [
            str(Group.objects.get(name=g).id)
            for g in auth_groups.split(",")
        ]
        try:
            Audit.change_settings(group_id, workflow_type, ",".join(audit_auth_groups))
        except Exception as msg:
            logger.error(traceback.format_exc())
            result["msg"] = str(msg)
            result["status"] = 1

        return JsonResponse(result)
