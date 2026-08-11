# -*- coding: UTF-8 -*-
"""账号密码登录 / 退出登录 DRF APIView（替代旧 /authenticate/ 与 /logout/）。

- POST /api/v1/authenticate/  账号密码校验（AllowAny）
- POST /api/v1/logout/        退出登录（IsAuthenticated）

复用 common.auth.DBShieldAuth 的校验/锁定/2FA 临时会话逻辑，仅把传输层从
HttpResponse(json) 换成 JsonResponse，并保持旧 {status,msg,data} 信封不变，
SPA Login.vue 的状态机（status=0 且 data=null → 已登录；data=sessionKey → 需 2FA；
1 失败；3 锁定）无需改动。

SSO（OIDC/DingTalk/CAS）登录入口仍是整页跳转的 /oidc/authenticate/ 等，
不在此处。
"""

from django.contrib.auth import logout as django_logout
from django.http import JsonResponse, HttpResponseRedirect
from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import views, permissions

from common.auth import DBShieldAuth
from common.config import SysConfig
from sql.models import TwoFactorAuthConfig
from common.utils.ding_api import get_ding_user_id


class AuthenticateView(views.APIView):
    """账号密码登录（AllowAny）。

    body（form-encoded 或 JSON 均可）：{username, password}
    返回旧信封：
      status=0, data=null   → 已直接登录
      status=0, data=<key>  → 需 2FA，key 为临时会话 session_key
      status=1              → 用户名或密码错误
      status=3              → 账号锁定
    """

    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="账号密码登录",
        description="校验用户名密码；若启用 2FA 返回临时会话 key，否则直接建立登录态。"
        "支持 form-encoded 与 JSON 两种 body。",
    )
    def post(self, request):
        # DBShieldAuth 内部用 request.POST 取字段。DRF 对 form-encoded 会正确填充 request.POST；
        # 但 JSON body 时 request.POST 为空。这里把 JSON 字段合并进一份可写 QueryDict，
        # 让 DBShieldAuth 能统一通过 request.POST 读到（不污染原始 request）。
        if not request.POST and request.data:
            from django.http import QueryDict

            qd = QueryDict(mutable=True)
            for k, v in request.data.items():
                qd[k] = "" if v is None else str(v)
            request.POST = qd

        result = DBShieldAuth(request).authenticate()
        if result["status"] == 0:
            authenticated_user = result["data"]
            twofa_enabled = TwoFactorAuthConfig.objects.filter(user=authenticated_user)
            enforce_2fa = SysConfig().get("enforce_2fa")

            # enforce_2fa 开启但用户未配置 2FA：拒绝登录（首次绑定流程已废弃）
            if enforce_2fa and not twofa_enabled:
                return JsonResponse(
                    {
                        "status": 1,
                        "msg": "管理员已开启强制两步验证，但你尚未配置 2FA，请联系管理员先在用户页绑定。",
                    }
                )

            # 已配置 2FA：建临时会话，前端进入 2FA 验证（verify_only）
            if twofa_enabled:
                from django.contrib.sessions.backends.db import SessionStore

                s = SessionStore()
                s["user"] = authenticated_user.username
                s["verify_mode"] = "verify_only"
                s.set_expiry(300)
                s.create()
                result = {"status": 0, "msg": "ok", "data": s.session_key}
            else:  # 未启用 2FA，直接登录
                from django.contrib.auth import login as django_login

                django_login(request, authenticated_user)
                # 拉取钉钉 user_id（仅用于单独发消息，失败不影响登录）
                username = request.POST.get("username")
                if (
                    SysConfig().get("ding_to_person") is True
                    and username
                    and "admin" not in username
                ):
                    try:
                        get_ding_user_id(username)
                    except Exception:
                        pass
                result = {"status": 0, "msg": "ok", "data": None}

        return JsonResponse(result)


class LogoutView(views.APIView):
    """退出登录。

    - 若启用钉钉认证且用户有 ding_user_id，302 重定向到钉钉登出页（与旧 sign_out 一致）；
    - 否则返回 JSON {status:0}，前端自行跳到 /login。
    """

    permission_classes = [permissions.AllowAny]  # 退出登录本身允许匿名调用

    @extend_schema(
        summary="退出登录",
        description="销毁当前会话；启用钉钉认证时重定向到钉钉登出页。",
    )
    def post(self, request):
        user = request.user
        django_logout(request)
        if getattr(user, "ding_user_id", None) and getattr(
            settings, "ENABLE_DINGDING", False
        ):
            return HttpResponseRedirect("https://login.dingtalk.com/oauth2/logout")
        return JsonResponse({"status": 0, "msg": "ok"})
