from rest_framework import views, generics, status, permissions
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from .serializers import (
    UserSerializer,
    UserDetailSerializer,
    GroupSerializer,
    ResourceGroupSerializer,
    TwoFASerializer,
    UserAuthSerializer,
    TwoFAVerifySerializer,
    TwoFASaveSerializer,
    TwoFAStateSerializer,
)
from .pagination import CustomizedPagination
from .permissions import IsOwner
from .filters import UserFilter, ResourceGroupFilter
from django_redis import get_redis_connection
from django.contrib.auth.models import Group, Permission
from django.contrib.auth import authenticate, login
from django.conf import settings
from django.http import Http404
from sql.models import Users, ResourceGroup, TwoFactorAuthConfig
from common.twofa import TwoFactorAuthBase, get_authenticator
from common.config import SysConfig
from common.utils.ding_api import get_ding_user_id
import random
import json
import time


class UserList(generics.ListAPIView):
    """
    列出所有的user或者创建一个新的user
    """

    filterset_class = UserFilter
    pagination_class = CustomizedPagination
    serializer_class = UserSerializer
    queryset = Users.objects.all().order_by("id")

    @extend_schema(
        summary="用户清单",
        request=UserSerializer,
        responses={200: UserSerializer},
        description="列出所有用户（过滤，分页）",
    )
    def get(self, request):
        users = self.filter_queryset(self.queryset)
        page_user = self.paginate_queryset(queryset=users)
        serializer_obj = self.get_serializer(page_user, many=True)
        data = {"data": serializer_obj.data}
        return self.get_paginated_response(data)

    @extend_schema(
        summary="创建用户",
        request=UserSerializer,
        responses={201: UserSerializer},
        description="创建一个用户",
    )
    def post(self, request):
        serializer = UserSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserDetail(views.APIView):
    """
    用户操作
    """

    serializer_class = UserDetailSerializer

    def get_object(self, pk):
        try:
            return Users.objects.get(pk=pk)
        except Users.DoesNotExist:
            raise Http404

    @extend_schema(
        summary="更新用户",
        request=UserDetailSerializer,
        responses={200: UserDetailSerializer},
        description="更新一个用户",
    )
    def put(self, request, pk):
        user = self.get_object(pk)
        serializer = UserDetailSerializer(user, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(summary="删除用户", description="删除一个用户")
    def delete(self, request, pk):
        user = self.get_object(pk)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class GroupList(generics.ListAPIView):
    """
    列出所有的group或者创建一个新的group
    """

    pagination_class = CustomizedPagination
    serializer_class = GroupSerializer
    queryset = Group.objects.all().order_by("id")

    @extend_schema(
        summary="用户组清单",
        request=GroupSerializer,
        responses={200: GroupSerializer},
        description="列出所有用户组（过滤，分页）",
    )
    def get(self, request):
        groups = self.filter_queryset(self.queryset)
        page_groups = self.paginate_queryset(queryset=groups)
        serializer_obj = self.get_serializer(page_groups, many=True)
        data = {"data": serializer_obj.data}
        return self.get_paginated_response(data)

    @extend_schema(
        summary="创建用户组",
        request=GroupSerializer,
        responses={201: GroupSerializer},
        description="创建一个用户组",
    )
    def post(self, request):
        serializer = GroupSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class GroupDetail(views.APIView):
    """
    用户组操作
    """

    serializer_class = GroupSerializer

    def get_object(self, pk):
        try:
            return Group.objects.get(pk=pk)
        except Group.DoesNotExist:
            raise Http404

    @extend_schema(
        summary="更新用户组",
        request=GroupSerializer,
        responses={200: GroupSerializer},
        description="更新一个用户组",
    )
    def put(self, request, pk):
        group = self.get_object(pk)
        serializer = GroupSerializer(group, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(summary="删除用户组", description="删除一个用户组")
    def delete(self, request, pk):
        group = self.get_object(pk)
        group.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PermissionList(views.APIView):
    """全部权限清单（供用户表单的 user_permissions M2M 选择器使用，按模型分组）。"""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(summary="权限清单", description="返回全部权限，按 content_type 分组。")
    def get(self, request):
        perms = Permission.objects.select_related("content_type").all()
        grouped: dict = {}
        for p in perms:
            ct = p.content_type
            key = f"{ct.app_label}.{ct.model}"
            grouped.setdefault(key, []).append(
                {"id": p.id, "codename": p.codename, "name": p.name}
            )
        return Response(
            [
                {"model": k, "label": k, "permissions": v}
                for k, v in sorted(grouped.items())
            ]
        )


class ResourceGroupList(generics.ListAPIView):
    """
    列出所有的resourcegroup或者创建一个新的resourcegroup
    """

    permission_classes = [permissions.IsAuthenticated]
    pagination_class = CustomizedPagination
    serializer_class = ResourceGroupSerializer
    filterset_class = ResourceGroupFilter
    # 过滤已软删的资源组（对齐旧版 /group/group/ 的 is_deleted=0 语义）
    queryset = ResourceGroup.objects.filter(is_deleted=0).order_by("group_id")

    @extend_schema(
        summary="资源组清单",
        request=ResourceGroupSerializer,
        responses={200: ResourceGroupSerializer},
        description="列出所有资源组（过滤，分页）",
    )
    def get(self, request):
        groups = self.filter_queryset(self.queryset)
        page_groups = self.paginate_queryset(queryset=groups)
        serializer_obj = self.get_serializer(page_groups, many=True)
        data = {"data": serializer_obj.data}
        return self.get_paginated_response(data)

    @extend_schema(
        summary="创建资源组",
        request=ResourceGroupSerializer,
        responses={201: ResourceGroupSerializer},
        description="创建一个资源组",
    )
    def post(self, request):
        # 资源组属平台级配置，创建/变更仅超管（前端按钮守卫不构成安全边界）
        if not request.user.is_superuser:
            return Response({"errors": "仅超级管理员可管理资源组"}, status=status.HTTP_403_FORBIDDEN)
        serializer = ResourceGroupSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ResourceGroupDetail(views.APIView):
    """
    资源组操作
    """

    # 与 ResourceGroupList 对齐：GET 详情对普通登录用户开放；
    # PUT/DELETE 属平台级写操作，仅超管（此前仅靠前端按钮守卫，不构成安全边界）
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ResourceGroupSerializer

    def get_object(self, pk):
        try:
            return ResourceGroup.objects.get(pk=pk)
        except ResourceGroup.DoesNotExist:
            raise Http404

    @extend_schema(
        summary="资源组详情",
        responses={200: ResourceGroupSerializer},
        description="获取一个资源组详情",
    )
    def get(self, request, pk):
        group = self.get_object(pk)
        serializer = ResourceGroupSerializer(group)
        return Response(serializer.data)

    @extend_schema(
        summary="更新资源组",
        request=ResourceGroupSerializer,
        responses={200: ResourceGroupSerializer},
        description="更新一个资源组",
    )
    def put(self, request, pk):
        if not request.user.is_superuser:
            return Response({"errors": "仅超级管理员可管理资源组"}, status=status.HTTP_403_FORBIDDEN)
        group = self.get_object(pk)
        serializer = ResourceGroupSerializer(group, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(summary="删除资源组", description="软删除一个资源组（is_deleted=1）")
    def delete(self, request, pk):
        if not request.user.is_superuser:
            return Response({"errors": "仅超级管理员可管理资源组"}, status=status.HTTP_403_FORBIDDEN)
        group = self.get_object(pk)
        # 软删除，对齐旧版语义，避免误删导致关联数据丢失
        group.is_deleted = 1
        group.save(update_fields=["is_deleted"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserAuth(views.APIView):
    """
    用户认证校验
    """

    permission_classes = [IsOwner]

    @extend_schema(
        summary="用户认证校验", request=UserAuthSerializer, description="用户认证校验"
    )
    def post(self, request):
        # 参数验证
        serializer = UserAuthSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        result = {"status": 0, "msg": "认证成功"}
        engineer = request.data["engineer"]
        password = request.data["password"]

        user = authenticate(username=engineer, password=password)
        if not user:
            result = {"status": 1, "msg": "用户名或密码错误！"}

        return Response(result)


class TwoFAVerifyContext(views.APIView):
    """2FA 验证上下文：供 SPA 在密码校验通过、临时会话已建立后，
    查询 verify_mode / 可用验证方式 / 短信手机号。读 request.session（临时会话），
    不走登录态权限，故 AllowAny（与 /authenticate/ 一致）。
    """

    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="2FA 验证上下文",
        description="密码校验通过后，查询 verify_mode / auth_types / phone，供 SPA 渲染 2FA 输入。",
    )
    def post(self, request):
        username = request.session.get("user")
        if not username:
            return Response(
                {"status": 1, "msg": "无 2FA 临时会话，请重新登录"}, status=status.HTTP_400_BAD_REQUEST
            )
        verify_mode = request.session.get("verify_mode", "verify_only")
        configs = TwoFactorAuthConfig.objects.filter(username=username)
        user_auth_types = [c.auth_type for c in configs]
        auth_types = []
        display_map = dict(TwoFactorAuthConfig.auth_type_choice)
        for t in user_auth_types:
            auth_types.append({"code": t, "display": display_map.get(t, t)})
        phone = ""
        if "sms" in user_auth_types:
            sms_cfg = TwoFactorAuthConfig.objects.filter(
                username=username, auth_type="sms"
            ).first()
            phone = sms_cfg.phone if sms_cfg else ""
        return Response(
            {"status": 0, "msg": "ok", "data": {"verify_mode": verify_mode, "auth_types": auth_types, "phone": phone}}
        )


class TwoFA(views.APIView):
    """
    配置2fa
    """

    permission_classes = [permissions.AllowAny]

    @extend_schema(summary="配置2fa", request=TwoFASerializer, description="配置2fa")
    def post(self, request):
        # 参数验证
        serializer = TwoFASerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        engineer = request.data["engineer"]
        enable = request.data["enable"]
        auth_type = request.data["auth_type"]
        user = Users.objects.get(username=engineer)
        request_user = request.session.get("user")

        if request.user.is_authenticated:
            # 已登录用户只能配置自己的 2FA（H2：防越权接管/禁用他人）
            if engineer != request.user.username and not request.user.is_superuser:
                return Response({"status": 1, "msg": "无权操作其他用户的2FA配置！"})
        elif request_user:
            if request_user != engineer:
                return Response({"status": 1, "msg": "登录用户与校验用户不一致！"})
        else:
            return Response({"status": 1, "msg": "需先校验用户密码！"})

        authenticator = get_authenticator(user=user, auth_type=auth_type)
        if enable == "true":
            if auth_type == "totp":
                # 启用2fa - 先生成secret key
                result = authenticator.generate_key()
            elif auth_type == "sms":
                # 启用2fa - 先发送短信验证码
                phone = request.data["phone"]
                otp = "{:06d}".format(random.randint(0, 999999))
                result = authenticator.get_captcha(phone=phone, otp=otp)
                if result["status"] == 0:
                    r = get_redis_connection("default")
                    data = {"otp": otp, "update_time": int(time.time())}
                    r.set(f"captcha-{phone}", json.dumps(data), 300)
            else:
                # 启用2fa
                result = authenticator.enable()
        else:
            result = authenticator.disable(auth_type)

        return Response(result)


class TwoFAState(views.APIView):
    """
    查询用户2fa配置情况
    """

    permission_classes = [IsOwner]

    @extend_schema(
        summary="查询2fa配置情况",
        request=TwoFAStateSerializer,
        description="查询2fa配置情况",
    )
    def post(self, request):
        # 参数验证
        serializer = TwoFAStateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        result = {"status": 0, "msg": "ok", "data": {}}
        engineer = request.data["engineer"]
        user = Users.objects.get(username=engineer)
        configs = TwoFactorAuthConfig.objects.filter(user=user)
        result["data"]["totp"] = (
            "enabled" if configs.filter(auth_type="totp") else "disabled"
        )
        result["data"]["sms"] = (
            "enabled" if configs.filter(auth_type="sms") else "disabled"
        )

        return Response(result)


class TwoFASave(views.APIView):
    """
    保存2fa配置（TOTP)
    """

    permission_classes = [IsOwner]

    @extend_schema(
        summary="保存2fa配置", request=TwoFASaveSerializer, description="保存2fa配置"
    )
    def post(self, request):
        # 参数验证
        serializer = TwoFASaveSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        engineer = request.data["engineer"]
        auth_type = request.data["auth_type"]
        key = request.data["key"] if "key" in request.data.keys() else None
        phone = request.data["phone"] if "phone" in request.data.keys() else None
        user = Users.objects.get(username=engineer)

        authenticator = get_authenticator(user=user, auth_type=auth_type)
        if auth_type == "sms":
            result = authenticator.save(phone)
        else:
            result = authenticator.save(key)

        return Response(result)


class TwoFAVerify(views.APIView):
    """
    检验2fa密码
    """

    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="检验2fa密码", request=TwoFAVerifySerializer, description="检验2fa密码"
    )
    def post(self, request):
        # 参数验证
        serializer = TwoFAVerifySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        engineer = request.data["engineer"]
        otp = request.data["otp"]
        key = request.data["key"] if "key" in request.data.keys() else None
        phone = request.data["phone"] if "phone" in request.data.keys() else None
        user = Users.objects.get(username=engineer)
        request_user = request.session.get("user")

        if request.user.is_authenticated:
            # 已登录用户只能校验自己的 2FA（H2：防越权验证/接管他人会话）
            if engineer != request.user.username and not request.user.is_superuser:
                return Response({"status": 1, "msg": "无权校验其他用户的2FA！"})
        elif request_user:
            if request_user != engineer:
                return Response({"status": 1, "msg": "登录用户与校验用户不一致！"})
        else:
            return Response({"status": 1, "msg": "需先校验用户密码！"})

        if not request.user.is_authenticated:
            twofa_config = TwoFactorAuthConfig.objects.filter(user=user)
            if not twofa_config:
                if not key:
                    return Response({"status": 1, "msg": "用户未配置2FA！"})

        auth_type = request.data["auth_type"]
        authenticator = get_authenticator(user=user, auth_type=auth_type)
        if auth_type == "sms":
            result = authenticator.verify(otp, phone)
        else:
            result = authenticator.verify(otp, key)

        # 校验通过后自动登录，刷新expire_date
        if result["status"] == 0 and not request.user.is_authenticated:
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            request.session.set_expiry(settings.SESSION_COOKIE_AGE)

            # 更新用户ding_user_id
            if SysConfig().get("ding_to_person") is True and "admin" not in engineer:
                get_ding_user_id(engineer)

        return Response(result)


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CurrentUser(views.APIView):
    """当前登录用户信息 + 全部权限位，供前端 SPA 渲染菜单 / 路由守卫使用。

    ensure_csrf_cookie 作用于 dispatch，使得即便是 401（未登录）响应也会种下
    csrftoken cookie，供 SPA 在登录 POST 前通过 X-CSRFToken 完成校验。
    """

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="当前用户",
        description="返回当前登录用户的基本信息与全部权限（含 sql.menu_* 等），供前端菜单/路由守卫使用。",
    )
    def get(self, request):
        user = request.user
        return Response(
            {
                "id": user.id,
                "username": user.username,
                "display": user.display or user.username,
                "email": user.email or "",
                "is_superuser": user.is_superuser,
                "is_staff": user.is_staff,
                "resource_group": [rg.group_id for rg in user.resource_group.all()],
                "permissions": sorted(user.get_all_permissions()),
            }
        )
